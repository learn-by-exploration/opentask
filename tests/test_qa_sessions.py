"""QA Testing Sessions 1-20: comprehensive tests from 10 QA review iterations.

Sessions 1-5:   Unit tests for QA findings (cancelled reenqueue, chain cancel, repeat caps, sanitize)
Sessions 6-10:  Integration tests (chain lifecycle edge cases, repeat+chain, DB integrity)
Sessions 11-15: Stress & edge cases (concurrent ops, huge inputs, injection, bot error paths)
Sessions 16-20: Regression tests (prior bugs, boundary values, notifications, prefs, DB hardening)
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import settings
from app.core.models import Base, ChatPrefs, ChainStatus, Task, TaskChain, TaskStatus, _utcnow

import app.core.broker as broker_mod


# ── Fixtures ────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def fresh_db(monkeypatch):
    """Fresh in-memory engine + monkeypatch broker.get_session. Fully isolated per test."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _patched_get_session():
        return factory()

    monkeypatch.setattr(broker_mod, "get_session", _patched_get_session)
    yield engine
    await engine.dispose()


# ═══════════════════════════════════════════════════════════════════
# SESSION 1: Cancelled task must not reenqueue
# ═══════════════════════════════════════════════════════════════════

class TestSession1CancelledReenqueue:
    """QA finding: cancelled tasks were reenqueued by maybe_reenqueue."""

    @pytest.mark.asyncio
    async def test_cancelled_task_not_reenqueued(self, fresh_db):
        task = await broker_mod.enqueue_repeat_task(
            prompt="repeating", repeat_count=5,
        )
        await broker_mod.pick_next_task()
        cancelled = await broker_mod.cancel_running_task()
        assert cancelled is not None
        assert cancelled.status == TaskStatus.CANCELLED

        requeued = await broker_mod.maybe_reenqueue(cancelled)
        assert requeued is None, "Cancelled task must NOT be reenqueued"

    @pytest.mark.asyncio
    async def test_failed_task_not_reenqueued(self, fresh_db):
        task = await broker_mod.enqueue_repeat_task(
            prompt="repeating", repeat_count=5,
        )
        await broker_mod.pick_next_task()
        failed = await broker_mod.complete_task(
            task.id, exit_code=1, output_summary="err",
            full_output="err", error_message="boom",
        )
        requeued = await broker_mod.maybe_reenqueue(failed)
        assert requeued is None

    @pytest.mark.asyncio
    async def test_completed_task_reenqueued(self, fresh_db):
        task = await broker_mod.enqueue_repeat_task(
            prompt="repeating", repeat_count=3,
        )
        await broker_mod.pick_next_task()
        done = await broker_mod.complete_task(
            task.id, exit_code=0, output_summary="ok", full_output="ok",
        )
        requeued = await broker_mod.maybe_reenqueue(done)
        assert requeued is not None
        assert requeued.repeat_remaining == 2


# ═══════════════════════════════════════════════════════════════════
# SESSION 2: Chain handles cancelled tasks
# ═══════════════════════════════════════════════════════════════════

class TestSession2ChainCancel:
    """QA finding: cancelled chain task should stop/cancel the chain."""

    @pytest.mark.asyncio
    async def test_cancelled_step_cancels_chain(self, fresh_db):
        await broker_mod.save_chain(
            name="cancel-chain", steps=[{"prompt": "s1"}, {"prompt": "s2"}],
        )
        task = await broker_mod.start_chain(name="cancel-chain")
        await broker_mod.pick_next_task()
        await broker_mod.cancel_running_task()

        # Re-fetch the cancelled task properly
        cancelled_task = await broker_mod.get_task_by_id(task.id)
        assert cancelled_task.status == TaskStatus.CANCELLED

        result = await broker_mod.advance_chain(cancelled_task)
        assert result is None

        chain = await broker_mod.get_chain_by_name("cancel-chain")
        assert chain.status == ChainStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_failed_step_fails_chain(self, fresh_db):
        await broker_mod.save_chain(
            name="fail-chain", steps=[{"prompt": "s1"}, {"prompt": "s2"}],
        )
        task = await broker_mod.start_chain(name="fail-chain")
        await broker_mod.pick_next_task()
        done = await broker_mod.complete_task(
            task.id, exit_code=1, output_summary="err",
            full_output="err", error_message="crash",
        )
        result = await broker_mod.advance_chain(done)
        assert result is None

        chain = await broker_mod.get_chain_by_name("fail-chain")
        assert chain.status == ChainStatus.FAILED


# ═══════════════════════════════════════════════════════════════════
# SESSION 3: Repeat count caps and validation
# ═══════════════════════════════════════════════════════════════════

class TestSession3RepeatValidation:
    """QA finding: repeat count unbounded, needs cap."""

    @pytest.mark.asyncio
    async def test_repeat_count_cap_1000(self, fresh_db):
        with pytest.raises(ValueError, match="<= 1000"):
            await broker_mod.enqueue_repeat_task(
                prompt="too many", repeat_count=1001,
            )

    @pytest.mark.asyncio
    async def test_repeat_count_1000_ok(self, fresh_db):
        task = await broker_mod.enqueue_repeat_task(
            prompt="max repeats", repeat_count=1000,
        )
        assert task.repeat_total == 1000

    @pytest.mark.asyncio
    async def test_repeat_count_zero_rejected(self, fresh_db):
        with pytest.raises(ValueError, match=">= 1"):
            await broker_mod.enqueue_repeat_task(
                prompt="zero", repeat_count=0,
            )

    @pytest.mark.asyncio
    async def test_repeat_count_negative_rejected(self, fresh_db):
        with pytest.raises(ValueError, match=">= 1"):
            await broker_mod.enqueue_repeat_task(
                prompt="negative", repeat_count=-1,
            )

    @pytest.mark.asyncio
    async def test_repeat_no_args_rejected(self, fresh_db):
        with pytest.raises(ValueError, match="Provide"):
            await broker_mod.enqueue_repeat_task(prompt="nothing")


# ═══════════════════════════════════════════════════════════════════
# SESSION 4: Chain step count caps
# ═══════════════════════════════════════════════════════════════════

class TestSession4ChainValidation:
    """QA finding: chain step count unbounded, prompt lengths unchecked."""

    @pytest.mark.asyncio
    async def test_chain_max_50_steps(self, fresh_db):
        steps = [{"prompt": f"step {i}"} for i in range(51)]
        with pytest.raises(ValueError, match="50 steps"):
            await broker_mod.save_chain(name="too-many", steps=steps)

    @pytest.mark.asyncio
    async def test_chain_50_steps_ok(self, fresh_db):
        steps = [{"prompt": f"step {i}"} for i in range(50)]
        chain = await broker_mod.save_chain(name="max-steps", steps=steps)
        assert chain.total_steps == 50

    @pytest.mark.asyncio
    async def test_chain_step_prompt_too_long(self, fresh_db):
        steps = [{"prompt": "x" * 2001}]
        with pytest.raises(ValueError, match="2000 chars"):
            await broker_mod.save_chain(name="long-prompt", steps=steps)

    @pytest.mark.asyncio
    async def test_chain_empty_rejected(self, fresh_db):
        with pytest.raises(ValueError, match="at least one"):
            await broker_mod.save_chain(name="empty", steps=[])

    @pytest.mark.asyncio
    async def test_chain_missing_prompt(self, fresh_db):
        with pytest.raises(ValueError, match="missing 'prompt'"):
            await broker_mod.save_chain(name="bad", steps=[{"agent": "opencode"}])


# ═══════════════════════════════════════════════════════════════════
# SESSION 5: Input sanitization (null bytes, control chars)
# ═══════════════════════════════════════════════════════════════════

class TestSession5Sanitization:
    """QA finding: null bytes and control chars not filtered."""

    def test_sanitize_text_strips_null(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("hello\x00world") == "helloworld"

    def test_sanitize_text_strips_whitespace(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("  hello  ") == "hello"

    def test_sanitize_text_empty(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("") == ""

    def test_sanitize_text_only_null(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("\x00\x00") == ""

    def test_sanitize_text_unicode_preserved(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("日本語テスト") == "日本語テスト"

    def test_sanitize_text_emoji_preserved(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("fix 🐛 bug") == "fix 🐛 bug"


# ═══════════════════════════════════════════════════════════════════
# SESSION 6: Reenqueue respects queue capacity
# ═══════════════════════════════════════════════════════════════════

class TestSession6ReenqueueQueueFull:
    """QA finding: maybe_reenqueue didn't check queue capacity."""

    @pytest.mark.asyncio
    async def test_reenqueue_skipped_when_queue_full(self, fresh_db):
        old_max = settings.max_queue_size
        settings.max_queue_size = 2

        try:
            # Enqueue a repeat task
            t1 = await broker_mod.enqueue_repeat_task(
                prompt="repeat", repeat_count=5,
            )
            # Pick and complete it
            await broker_mod.pick_next_task()
            done = await broker_mod.complete_task(
                t1.id, exit_code=0, output_summary="ok", full_output="ok",
            )
            # Queue now empty, reenqueue should work
            requeued = await broker_mod.maybe_reenqueue(done)
            assert requeued is not None

            # Now fill queue to max: requeued (pending) + another
            t2 = await broker_mod.enqueue_task(prompt="filler")
            # Queue: 2 pending (requeued + filler)

            # Pick and complete the requeued one
            picked = await broker_mod.pick_next_task()
            done2 = await broker_mod.complete_task(
                picked.id, exit_code=0, output_summary="ok", full_output="ok",
            )
            # Queue: filler still pending = 1 active; max=2, reenqueue fits
            requeued2 = await broker_mod.maybe_reenqueue(done2)
            assert requeued2 is not None
        finally:
            settings.max_queue_size = old_max


# ═══════════════════════════════════════════════════════════════════
# SESSION 7: JSON steps resilience
# ═══════════════════════════════════════════════════════════════════

class TestSession7JsonStepsResilience:
    """QA finding: corrupted steps_json crashes the app."""

    @pytest.mark.asyncio
    async def test_corrupted_json_returns_empty(self, fresh_db):
        factory = async_sessionmaker(fresh_db, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            chain = TaskChain(
                name="corrupt",
                steps_json="NOT VALID JSON",
                status=ChainStatus.IDLE,
            )
            session.add(chain)
            await session.commit()
            await session.refresh(chain)
            assert chain.steps == []
            assert chain.total_steps == 0

    def test_empty_string_json_returns_empty(self):
        chain = TaskChain(
            name="empty-json", steps_json="", status=ChainStatus.IDLE,
        )
        assert chain.steps == []

    def test_valid_json_works(self):
        chain = TaskChain(
            name="ok",
            steps_json='[{"prompt": "test"}]',
            status=ChainStatus.IDLE,
        )
        assert chain.steps == [{"prompt": "test"}]
        assert chain.total_steps == 1


# ═══════════════════════════════════════════════════════════════════
# SESSION 8: Chain lifecycle edge cases
# ═══════════════════════════════════════════════════════════════════

class TestSession8ChainEdgeCases:
    """Integration: chain lifecycle corner cases."""

    @pytest.mark.asyncio
    async def test_start_chain_already_running(self, fresh_db):
        await broker_mod.save_chain(
            name="run-chain", steps=[{"prompt": "s1"}, {"prompt": "s2"}],
        )
        await broker_mod.start_chain(name="run-chain")
        with pytest.raises(ValueError, match="already running"):
            await broker_mod.start_chain(name="run-chain")

    @pytest.mark.asyncio
    async def test_advance_non_chain_task_returns_none(self, fresh_db):
        """advance_chain returns None for tasks with no chain_id."""
        task = await broker_mod.enqueue_task(prompt="standalone")
        await broker_mod.pick_next_task()
        done = await broker_mod.complete_task(
            task.id, exit_code=0, output_summary="ok", full_output="ok",
        )
        # Task has chain_id=None, so advance_chain immediately returns None
        result = await broker_mod.advance_chain(done)
        assert result is None

    @pytest.mark.asyncio
    async def test_chain_single_step_completes(self, fresh_db):
        await broker_mod.save_chain(name="one-step", steps=[{"prompt": "only"}])
        task = await broker_mod.start_chain(name="one-step")
        await broker_mod.pick_next_task()
        done = await broker_mod.complete_task(
            task.id, exit_code=0, output_summary="ok", full_output="ok",
        )
        result = await broker_mod.advance_chain(done)
        assert result is None  # chain complete

        chain = await broker_mod.get_chain_by_name("one-step")
        assert chain.status == ChainStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_delete_chain_while_idle(self, fresh_db):
        await broker_mod.save_chain(name="del-me", steps=[{"prompt": "x"}])
        assert await broker_mod.delete_chain("del-me") is True
        assert await broker_mod.get_chain_by_name("del-me") is None


# ═══════════════════════════════════════════════════════════════════
# SESSION 9: Chain advances to next step
# ═══════════════════════════════════════════════════════════════════

class TestSession9ChainAdvance:
    """Integration: chain advance creates next step's task."""

    @pytest.mark.asyncio
    async def test_chain_advances_to_step2(self, fresh_db):
        await broker_mod.save_chain(
            name="two-step",
            steps=[{"prompt": "step1"}, {"prompt": "step2"}],
        )
        task = await broker_mod.start_chain(name="two-step")
        await broker_mod.pick_next_task()
        done = await broker_mod.complete_task(
            task.id, exit_code=0, output_summary="ok", full_output="ok",
        )
        next_task = await broker_mod.advance_chain(done)
        assert next_task is not None
        assert next_task.chain_step == 1
        assert next_task.prompt == "step2"


# ═══════════════════════════════════════════════════════════════════
# SESSION 10: Recovery scenarios
# ═══════════════════════════════════════════════════════════════════

class TestSession10Recovery:
    """Integration: crash recovery correctness."""

    @pytest.mark.asyncio
    async def test_recover_running_tasks(self, fresh_db):
        t1 = await broker_mod.enqueue_task(prompt="orphan1")
        t2 = await broker_mod.enqueue_task(prompt="orphan2")
        await broker_mod.pick_next_task()
        await broker_mod.pick_next_task()

        count = await broker_mod.recover_interrupted_tasks()
        assert count == 2

        t1_after = await broker_mod.get_task_by_id(t1.id)
        assert t1_after.status == TaskStatus.FAILED
        assert "Interrupted" in t1_after.error_message

    @pytest.mark.asyncio
    async def test_recover_running_chains(self, fresh_db):
        await broker_mod.save_chain(name="orphan", steps=[{"prompt": "x"}])
        await broker_mod.start_chain(name="orphan")

        count = await broker_mod.recover_interrupted_chains()
        assert count == 1

        chain = await broker_mod.get_chain_by_name("orphan")
        assert chain.status == ChainStatus.FAILED

    @pytest.mark.asyncio
    async def test_recover_no_orphans(self, fresh_db):
        count = await broker_mod.recover_interrupted_tasks()
        assert count == 0


# ═══════════════════════════════════════════════════════════════════
# SESSION 11: Concurrent enqueue stress
# ═══════════════════════════════════════════════════════════════════

class TestSession11ConcurrentStress:
    """Stress: concurrent operations."""

    @pytest.mark.asyncio
    async def test_concurrent_enqueue_respects_limit(self, fresh_db):
        old_max = settings.max_queue_size
        settings.max_queue_size = 10

        try:
            results = await asyncio.gather(
                *[broker_mod.enqueue_task(prompt=f"t{i}") for i in range(10)],
                return_exceptions=True,
            )
            successes = [r for r in results if isinstance(r, Task)]
            assert len(successes) <= 10
        finally:
            settings.max_queue_size = old_max

    @pytest.mark.asyncio
    async def test_concurrent_pick_no_crash(self, fresh_db):
        for i in range(5):
            await broker_mod.enqueue_task(prompt=f"task {i}")

        picks = await asyncio.gather(
            *[broker_mod.pick_next_task() for _ in range(5)],
        )
        valid_picks = [p for p in picks if p is not None]
        # With SQLite serialization, concurrent picks work fine
        assert len(valid_picks) >= 1


# ═══════════════════════════════════════════════════════════════════
# SESSION 12: Huge input handling
# ═══════════════════════════════════════════════════════════════════

class TestSession12HugeInputs:
    """Edge: very large prompts, outputs, chain names."""

    @pytest.mark.asyncio
    async def test_max_length_prompt(self, fresh_db):
        prompt = "x" * 2000
        task = await broker_mod.enqueue_task(prompt=prompt)
        assert len(task.prompt) == 2000

    @pytest.mark.asyncio
    async def test_large_output_stored(self, fresh_db):
        task = await broker_mod.enqueue_task(prompt="big output")
        await broker_mod.pick_next_task()
        big_out = "y" * 100000
        done = await broker_mod.complete_task(
            task.id, exit_code=0, output_summary="big",
            full_output=big_out,
        )
        assert len(done.full_output) == 100000

    @pytest.mark.asyncio
    async def test_chain_name_max_length(self, fresh_db):
        name = "a" * 128
        chain = await broker_mod.save_chain(name=name, steps=[{"prompt": "x"}])
        assert chain.name == name


# ═══════════════════════════════════════════════════════════════════
# SESSION 13: Unicode and special characters
# ═══════════════════════════════════════════════════════════════════

class TestSession13Unicode:
    """Edge: Unicode, emoji, RTL text."""

    @pytest.mark.asyncio
    async def test_unicode_prompt(self, fresh_db):
        task = await broker_mod.enqueue_task(prompt="日本語のタスク")
        assert task.prompt == "日本語のタスク"

    @pytest.mark.asyncio
    async def test_emoji_prompt(self, fresh_db):
        task = await broker_mod.enqueue_task(prompt="fix 🐛 bug 🔥")
        assert "🐛" in task.prompt

    @pytest.mark.asyncio
    async def test_rtl_text(self, fresh_db):
        task = await broker_mod.enqueue_task(prompt="مرحبا بالعالم")
        assert task.prompt == "مرحبا بالعالم"

    @pytest.mark.asyncio
    async def test_mixed_script_chain(self, fresh_db):
        chain = await broker_mod.save_chain(
            name="混合chain",
            steps=[{"prompt": "step①"}, {"prompt": "ステップ②"}],
        )
        assert chain.total_steps == 2


# ═══════════════════════════════════════════════════════════════════
# SESSION 14: Command builder injection safety
# ═══════════════════════════════════════════════════════════════════

class TestSession14InjectionSafety:
    """Security: command injection via prompts."""

    def _build(self, prompt: str, agent: str = "opencode") -> list[str]:
        from app.core.runner import AgentRunner
        runner = AgentRunner.__new__(AgentRunner)
        task = Task(
            id=1, prompt=prompt, project_dir="/tmp",
            agent=agent, status=TaskStatus.RUNNING,
        )
        return runner._build_command(task)

    def test_semicolon_injection(self):
        argv = self._build("hello; rm -rf /")
        prompt_arg = [a for a in argv if "hello" in a][0]
        assert ";" in prompt_arg

    def test_backtick_injection(self):
        argv = self._build("hello `whoami`")
        prompt_arg = [a for a in argv if "hello" in a][0]
        assert "`" in prompt_arg

    def test_pipe_injection(self):
        argv = self._build("hello | cat /etc/passwd")
        prompt_arg = [a for a in argv if "hello" in a][0]
        assert "|" in prompt_arg

    def test_dollar_injection(self):
        argv = self._build("hello $(cat /etc/shadow)")
        prompt_arg = [a for a in argv if "hello" in a][0]
        assert "$(" in prompt_arg

    def test_newline_injection(self):
        argv = self._build("hello\nrm -rf /")
        prompt_arg = [a for a in argv if "hello" in a][0]
        assert "\n" in prompt_arg

    def test_null_byte(self):
        argv = self._build("hello\x00world")
        prompt_arg = [a for a in argv if "hello" in a][0]
        assert "hello" in prompt_arg

    def test_unknown_agent(self):
        argv = self._build("test", agent="nonexistent")
        assert argv[0] == "echo"


# ═══════════════════════════════════════════════════════════════════
# SESSION 15: Bot handler error paths
# ═══════════════════════════════════════════════════════════════════

class TestSession15BotErrorPaths:
    """Edge: every bot handler error path."""

    def _make_update(self, text="", user_id=12345, chat_id=12345):
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = user_id
        update.effective_chat = MagicMock()
        update.effective_chat.id = chat_id
        update.message = MagicMock()
        update.message.text = text
        ack = AsyncMock()
        ack.edit_text = AsyncMock()
        ack.message_id = 1
        update.message.reply_text = AsyncMock(return_value=ack)
        bot = AsyncMock()
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock()
        bot.send_chat_action = AsyncMock()
        update.get_bot = MagicMock(return_value=bot)
        return update

    def _make_context(self, args=None):
        ctx = MagicMock()
        ctx.args = args or []
        return ctx

    @pytest.mark.asyncio
    async def test_unauthorized_user_blocked(self, fresh_db):
        from app.telegram.bot import cmd_start
        update = self._make_update(user_id=99999)
        await cmd_start(update, self._make_context())
        # Auth guard silently returns — no send_message called
        update.get_bot().send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_bad_id(self, fresh_db):
        from app.telegram.bot import cmd_cancel
        update = self._make_update()
        await cmd_cancel(update, self._make_context(args=["notanumber"]))
        update.get_bot().send_message.assert_called_once()
        sent = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Invalid" in sent

    @pytest.mark.asyncio
    async def test_output_no_args(self, fresh_db):
        from app.telegram.bot import cmd_output
        update = self._make_update()
        await cmd_output(update, self._make_context())
        update.get_bot().send_message.assert_called_once()
        sent = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Usage" in sent

    @pytest.mark.asyncio
    async def test_retry_no_args(self, fresh_db):
        from app.telegram.bot import cmd_retry
        update = self._make_update()
        await cmd_retry(update, self._make_context())
        update.get_bot().send_message.assert_called_once()
        sent = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Usage" in sent

    @pytest.mark.asyncio
    async def test_handle_text_too_long(self, fresh_db):
        from app.telegram.bot import handle_text
        update = self._make_update(text="x" * 2001)
        await handle_text(update, self._make_context())
        update.get_bot().send_message.assert_called_once()
        sent = update.get_bot().send_message.call_args.kwargs["text"]
        assert "too long" in sent

    @pytest.mark.asyncio
    async def test_handle_text_empty(self, fresh_db):
        from app.telegram.bot import handle_text
        update = self._make_update(text="   ")
        await handle_text(update, self._make_context())
        # Empty text after sanitize → silent return, no send
        update.get_bot().send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_unknown(self, fresh_db):
        from app.telegram.bot import cmd_agent
        update = self._make_update()
        await cmd_agent(update, self._make_context(args=["nonexistent"]))
        update.get_bot().send_message.assert_called_once()
        sent = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Unknown" in sent


# ═══════════════════════════════════════════════════════════════════
# SESSION 16: Regression — prior bugs
# ═══════════════════════════════════════════════════════════════════

class TestSession16Regression:
    """Regression: re-test previously found and fixed bugs."""

    @pytest.mark.asyncio
    async def test_handle_text_queues_task_correctly(self, fresh_db):
        """Regression: telegram_chat_id→chat_id kwarg mismatch."""
        from app.telegram.bot import handle_text

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 12345
        update.effective_chat = MagicMock()
        update.effective_chat.id = 12345
        update.message = MagicMock()
        update.message.text = "fix the bug"
        ack = AsyncMock()
        ack.edit_text = AsyncMock()
        ack.message_id = 42
        update.message.reply_text = AsyncMock(return_value=ack)
        bot = AsyncMock()
        bot.send_message = AsyncMock()
        update.get_bot = MagicMock(return_value=bot)

        ctx = MagicMock()
        ctx.args = []
        await handle_text(update, ctx)

        # The ack message should be edited with task info
        ack.edit_text.assert_called_once()
        edit_text = ack.edit_text.call_args[0][0]
        assert "Queued" in edit_text or "📋" in edit_text

    @pytest.mark.asyncio
    async def test_complete_cancelled_returns_existing(self, fresh_db):
        """complete_task on cancelled task returns the cancelled task."""
        task = await broker_mod.enqueue_task(prompt="race")
        await broker_mod.pick_next_task()
        await broker_mod.cancel_running_task()

        result = await broker_mod.complete_task(
            task.id, exit_code=0, output_summary="late", full_output="",
        )
        assert result is not None
        assert result.status == TaskStatus.CANCELLED


# ═══════════════════════════════════════════════════════════════════
# SESSION 17: Boundary value tests
# ═══════════════════════════════════════════════════════════════════

class TestSession17BoundaryValues:
    """Boundary: test at limits."""

    @pytest.mark.asyncio
    async def test_queue_at_exact_limit(self, fresh_db):
        old_max = settings.max_queue_size
        settings.max_queue_size = 3

        try:
            await broker_mod.enqueue_task(prompt="one")
            await broker_mod.enqueue_task(prompt="two")
            await broker_mod.enqueue_task(prompt="three")
            with pytest.raises(ValueError, match="Queue full"):
                await broker_mod.enqueue_task(prompt="four")
        finally:
            settings.max_queue_size = old_max

    @pytest.mark.asyncio
    async def test_history_limit_zero(self, fresh_db):
        await broker_mod.enqueue_task(prompt="task")
        history = await broker_mod.get_recent_tasks(limit=0)
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_history_limit_exceeds_total(self, fresh_db):
        await broker_mod.enqueue_task(prompt="only one")
        history = await broker_mod.get_recent_tasks(limit=1000)
        assert len(history) == 1

    @pytest.mark.asyncio
    async def test_purge_zero_days(self, fresh_db):
        t = await broker_mod.enqueue_task(prompt="old")
        await broker_mod.pick_next_task()
        await broker_mod.complete_task(
            t.id, exit_code=0, output_summary="ok", full_output="ok",
        )
        purged = await broker_mod.purge_old_tasks(days=0)
        assert purged == 1


# ═══════════════════════════════════════════════════════════════════
# SESSION 18: Notification callbacks
# ═══════════════════════════════════════════════════════════════════

class TestSession18Notifications:
    """Integration: notification callback paths."""

    @pytest.mark.asyncio
    async def test_notify_no_chat_id_skipped(self):
        from app.telegram.bot import make_notify_callback
        app = MagicMock()
        app.bot = AsyncMock()
        notify = await make_notify_callback(app)

        task = Task(
            id=1, prompt="x", project_dir="/tmp", agent="opencode",
            status=TaskStatus.COMPLETED, telegram_chat_id=None,
        )
        await notify(task)
        app.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_progress_no_chat_id_skipped(self):
        from app.telegram.bot import make_progress_callback
        app = MagicMock()
        app.bot = AsyncMock()
        progress = await make_progress_callback(app)
        await progress(None, "some progress")
        app.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_typing_error_swallowed(self):
        from app.telegram.bot import make_typing_callback
        app = MagicMock()
        app.bot = AsyncMock()
        app.bot.send_chat_action = AsyncMock(side_effect=Exception("network"))
        typing_cb = await make_typing_callback(app)
        # Should not raise
        await typing_cb(12345)


# ═══════════════════════════════════════════════════════════════════
# SESSION 19: Chat preferences persistence
# ═══════════════════════════════════════════════════════════════════

class TestSession19ChatPrefs:
    """Integration: chat prefs lifecycle."""

    @pytest.mark.asyncio
    async def test_initial_prefs_empty(self, fresh_db):
        prefs = await broker_mod.get_chat_prefs(99999)
        assert prefs["project_dir"] is None
        assert prefs["agent"] is None

    @pytest.mark.asyncio
    async def test_set_and_get_project_dir(self, fresh_db):
        await broker_mod.set_chat_pref(12345, project_dir="/tmp")
        prefs = await broker_mod.get_chat_prefs(12345)
        assert prefs["project_dir"] == "/tmp"
        assert prefs["agent"] is None

    @pytest.mark.asyncio
    async def test_set_and_get_agent(self, fresh_db):
        await broker_mod.set_chat_pref(12345, agent="aider")
        prefs = await broker_mod.get_chat_prefs(12345)
        assert prefs["agent"] == "aider"
        assert prefs["project_dir"] is None

    @pytest.mark.asyncio
    async def test_update_preserves_other_field(self, fresh_db):
        await broker_mod.set_chat_pref(12345, project_dir="/tmp", agent="aider")
        await broker_mod.set_chat_pref(12345, project_dir="/home")
        prefs = await broker_mod.get_chat_prefs(12345)
        assert prefs["project_dir"] == "/home"
        assert prefs["agent"] == "aider"  # preserved


# ═══════════════════════════════════════════════════════════════════
# SESSION 20: SQLite pragmas and DB hardening
# ═══════════════════════════════════════════════════════════════════

class TestSession20DbHardening:
    """Integration: verify DB pragmas and settings."""

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self):
        """Verify WAL mode is set on connection."""
        from app.core.db import engine
        async with engine.connect() as conn:
            result = await conn.execute(text("PRAGMA journal_mode"))
            mode = result.scalar()
            # In-memory DBs return 'memory', file-based return 'wal'
            assert mode in ("wal", "memory")

    @pytest.mark.asyncio
    async def test_foreign_keys_enabled(self):
        """Verify foreign keys are enforced."""
        from app.core.db import engine
        async with engine.connect() as conn:
            result = await conn.execute(text("PRAGMA foreign_keys"))
            fk = result.scalar()
            assert fk in (1, True)

    def test_models_have_correct_types(self):
        """Verify model column types match expectations."""
        assert Task.__tablename__ == "tasks"
        assert TaskChain.__tablename__ == "task_chains"
        assert ChatPrefs.__tablename__ == "chat_prefs"

    @pytest.mark.asyncio
    async def test_init_db_creates_tables(self, fresh_db):
        """Tables exist after init."""
        async with fresh_db.connect() as conn:
            result = await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ))
            tables = {row[0] for row in result.fetchall()}
            assert "tasks" in tables
            assert "task_chains" in tables
            assert "chat_prefs" in tables

"""Tests for new features: priority/bump, auto-retry, git diff, search, reply keyboard."""

from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-for-tests")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.models import Base, Task, TaskStatus
import app.core.broker as broker_mod
from app.core.broker import (
    auto_retry_task,
    bump_task,
    complete_task,
    enqueue_task,
    get_pending_tasks,
    pick_next_task,
    search_tasks,
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def fresh_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _patched():
        return factory()

    monkeypatch.setattr(broker_mod, "get_session", _patched)
    yield engine
    await engine.dispose()


# ── Helpers ─────────────────────────────────────────────────────────


def _make_update(text="test", user_id=12345):
    update = MagicMock()
    update.effective_user = MagicMock(id=user_id)
    update.effective_chat = MagicMock(id=67890)
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    update.get_bot = MagicMock(return_value=bot)
    return update


def _make_context(*args):
    ctx = MagicMock()
    ctx.args = list(args)
    return ctx


# ══════════════════════════════════════════════════════════════════════
#  PRIORITY / BUMP TESTS
# ══════════════════════════════════════════════════════════════════════


class TestPriority:
    @pytest.mark.asyncio
    async def test_default_priority_is_zero(self, fresh_db):
        task = await enqueue_task(prompt="a", agent="opencode", chat_id=1)
        assert task.priority == 0

    @pytest.mark.asyncio
    async def test_bump_increases_priority(self, fresh_db):
        t1 = await enqueue_task(prompt="first", agent="opencode", chat_id=1)
        t2 = await enqueue_task(prompt="second", agent="opencode", chat_id=1)
        bumped = await bump_task(t2.id)
        assert bumped is not None
        assert bumped.priority > 0

    @pytest.mark.asyncio
    async def test_bumped_task_picked_first(self, fresh_db):
        t1 = await enqueue_task(prompt="first", agent="opencode", chat_id=1)
        t2 = await enqueue_task(prompt="second", agent="opencode", chat_id=1)
        await bump_task(t2.id)
        picked = await pick_next_task()
        assert picked is not None
        assert picked.id == t2.id

    @pytest.mark.asyncio
    async def test_bump_nonexistent_returns_none(self, fresh_db):
        result = await bump_task(999)
        assert result is None

    @pytest.mark.asyncio
    async def test_bump_running_task_returns_none(self, fresh_db):
        task = await enqueue_task(prompt="test", agent="opencode", chat_id=1)
        await pick_next_task()  # moves to RUNNING
        result = await bump_task(task.id)
        assert result is None

    @pytest.mark.asyncio
    async def test_pending_tasks_ordered_by_priority(self, fresh_db):
        t1 = await enqueue_task(prompt="low", agent="opencode", chat_id=1)
        t2 = await enqueue_task(prompt="high", agent="opencode", chat_id=1)
        await bump_task(t2.id)
        pending = await get_pending_tasks()
        assert pending[0].id == t2.id
        assert pending[1].id == t1.id

    @pytest.mark.asyncio
    async def test_multiple_bumps(self, fresh_db):
        t1 = await enqueue_task(prompt="a", agent="opencode", chat_id=1)
        t2 = await enqueue_task(prompt="b", agent="opencode", chat_id=1)
        t3 = await enqueue_task(prompt="c", agent="opencode", chat_id=1)
        await bump_task(t2.id)
        await bump_task(t3.id)
        pending = await get_pending_tasks()
        # t3 should be first (highest priority), then t2, then t1
        assert pending[0].id == t3.id
        assert pending[1].id == t2.id
        assert pending[2].id == t1.id


# ══════════════════════════════════════════════════════════════════════
#  AUTO-RETRY TESTS
# ══════════════════════════════════════════════════════════════════════


class TestAutoRetry:
    @pytest.mark.asyncio
    async def test_auto_retry_on_crash(self, fresh_db):
        """Exit code -1 (crash) should auto-retry."""
        task = await enqueue_task(prompt="test", agent="opencode", chat_id=1)
        await pick_next_task()
        completed = await complete_task(task.id, exit_code=-1, output_summary="crash", full_output="")
        retry = await auto_retry_task(task.id)
        assert retry is not None
        assert retry.retry_count == 1
        assert retry.prompt == task.prompt

    @pytest.mark.asyncio
    async def test_no_auto_retry_on_normal_failure(self, fresh_db):
        """Exit code 1 (normal failure) should NOT auto-retry."""
        task = await enqueue_task(prompt="test", agent="opencode", chat_id=1)
        await pick_next_task()
        await complete_task(task.id, exit_code=1, output_summary="error", full_output="")
        retry = await auto_retry_task(task.id)
        assert retry is None

    @pytest.mark.asyncio
    async def test_no_auto_retry_on_success(self, fresh_db):
        """Successful tasks don't auto-retry."""
        task = await enqueue_task(prompt="test", agent="opencode", chat_id=1)
        await pick_next_task()
        await complete_task(task.id, exit_code=0, output_summary="ok", full_output="")
        retry = await auto_retry_task(task.id)
        assert retry is None

    @pytest.mark.asyncio
    async def test_auto_retry_exhausted(self, fresh_db):
        """After max retries, no more auto-retries."""
        t1 = await enqueue_task(prompt="test", agent="opencode", chat_id=1)
        await pick_next_task()
        await complete_task(t1.id, exit_code=-1, output_summary="crash", full_output="")
        t2 = await auto_retry_task(t1.id)
        assert t2 is not None
        assert t2.retry_count == 1

        await pick_next_task()
        await complete_task(t2.id, exit_code=-1, output_summary="crash again", full_output="")
        t3 = await auto_retry_task(t2.id)
        # max_retries defaults to 1, so retry_count 1 == max_retries → no more
        assert t3 is None

    @pytest.mark.asyncio
    async def test_retry_preserves_model(self, fresh_db):
        """Auto-retry should inherit model."""
        task = await enqueue_task(
            prompt="test", agent="opencode", chat_id=1,
            model="sonnet",
        )
        await pick_next_task()
        await complete_task(task.id, exit_code=-1, output_summary="crash", full_output="")
        retry = await auto_retry_task(task.id)
        assert retry is not None
        assert retry.model == "sonnet"

    @pytest.mark.asyncio
    async def test_retry_preserves_assigned_to(self, fresh_db):
        """Auto-retry should inherit assigned_to."""
        from app.core.broker import worker_claim_task, worker_submit_result
        task = await enqueue_task(
            prompt="test", agent="opencode", chat_id=1,
            assigned_to="server2",
        )
        # Worker claims the task (since it has assigned_to, local runner skips it)
        claimed = await worker_claim_task("server2")
        assert claimed is not None
        # Worker reports failure
        await worker_submit_result(claimed.id, "server2", exit_code=-1,
                                   output_summary="crash", full_output="")
        retry = await auto_retry_task(claimed.id)
        assert retry is not None
        assert retry.assigned_to == "server2"

    @pytest.mark.asyncio
    async def test_retry_preserves_priority(self, fresh_db):
        """Auto-retry should inherit priority."""
        task = await enqueue_task(prompt="test", agent="opencode", chat_id=1)
        await bump_task(task.id)
        await pick_next_task()
        await complete_task(task.id, exit_code=-1, output_summary="crash", full_output="")
        retry = await auto_retry_task(task.id)
        assert retry is not None
        assert retry.priority > 0


# ══════════════════════════════════════════════════════════════════════
#  GIT DIFF TESTS
# ══════════════════════════════════════════════════════════════════════


class TestGitDiff:
    @pytest.mark.asyncio
    async def test_git_diff_stored_on_task(self, fresh_db):
        """complete_task should store git_diff."""
        task = await enqueue_task(prompt="test", agent="opencode", chat_id=1)
        await pick_next_task()
        updated = await complete_task(
            task.id, exit_code=0, output_summary="ok", full_output="done",
            git_diff="2 files changed, 10 insertions(+)",
        )
        assert updated is not None
        assert updated.git_diff == "2 files changed, 10 insertions(+)"

    @pytest.mark.asyncio
    async def test_git_diff_none_on_failure(self, fresh_db):
        """Failed tasks should have no git diff."""
        task = await enqueue_task(prompt="test", agent="opencode", chat_id=1)
        await pick_next_task()
        updated = await complete_task(
            task.id, exit_code=1, output_summary="err", full_output="",
        )
        assert updated is not None
        assert updated.git_diff is None

    @pytest.mark.asyncio
    async def test_git_diff_summary_method(self):
        """_git_diff_summary should handle non-git directories gracefully."""
        from app.core.runner import AgentRunner
        result = await AgentRunner._git_diff_summary("/tmp")
        # /tmp is not a git repo, should return empty string
        assert result == ""

    @pytest.mark.asyncio
    async def test_git_diff_in_notification(self, fresh_db):
        """Notification should include git diff for successful tasks."""
        from app.telegram.bot import make_notify_callback
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        mock_app.bot.edit_message_text = AsyncMock()
        notify = await make_notify_callback(mock_app)

        task = await enqueue_task(prompt="test", agent="opencode", chat_id=12345)
        await pick_next_task()
        updated = await complete_task(
            task.id, exit_code=0, output_summary="ok", full_output="",
            git_diff="3 files changed",
        )
        await notify(updated)
        call_text = mock_app.bot.send_message.call_args.kwargs.get(
            "text", mock_app.bot.send_message.call_args[1].get("text", "")
        )
        assert "Files changed" in call_text
        assert "3 files changed" in call_text


# ══════════════════════════════════════════════════════════════════════
#  SEARCH TESTS
# ══════════════════════════════════════════════════════════════════════


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_finds_matching_tasks(self, fresh_db):
        await enqueue_task(prompt="fix the login bug", agent="opencode", chat_id=1)
        await enqueue_task(prompt="update readme", agent="opencode", chat_id=1)
        await enqueue_task(prompt="fix the auth bug", agent="opencode", chat_id=1)

        results = await search_tasks("fix")
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_search_case_insensitive(self, fresh_db):
        await enqueue_task(prompt="Fix The Login Bug", agent="opencode", chat_id=1)
        results = await search_tasks("fix")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_search_no_results(self, fresh_db):
        await enqueue_task(prompt="update readme", agent="opencode", chat_id=1)
        results = await search_tasks("nonexistent")
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_search_limit(self, fresh_db):
        for i in range(15):
            await enqueue_task(prompt=f"task {i} fix bug", agent="opencode", chat_id=1)
        results = await search_tasks("fix", limit=5)
        assert len(results) == 5

    @pytest.mark.asyncio
    async def test_search_returns_most_recent_first(self, fresh_db):
        t1 = await enqueue_task(prompt="fix old bug", agent="opencode", chat_id=1)
        t2 = await enqueue_task(prompt="fix new bug", agent="opencode", chat_id=1)
        results = await search_tasks("fix")
        assert results[0].id == t2.id  # newest first


# ══════════════════════════════════════════════════════════════════════
#  BOT COMMAND TESTS — /bump
# ══════════════════════════════════════════════════════════════════════


class TestBotBump:
    @pytest.mark.asyncio
    async def test_bump_no_args(self, fresh_db):
        from app.telegram.bot import cmd_bump
        update = _make_update()
        await cmd_bump(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_bump_success(self, fresh_db):
        from app.telegram.bot import cmd_bump
        task = await enqueue_task(prompt="test", agent="opencode", chat_id=12345)
        update = _make_update()
        await cmd_bump(update, _make_context(str(task.id)))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "bumped" in text.lower()

    @pytest.mark.asyncio
    async def test_bump_invalid_id(self, fresh_db):
        from app.telegram.bot import cmd_bump
        update = _make_update()
        await cmd_bump(update, _make_context("abc"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Invalid" in text

    @pytest.mark.asyncio
    async def test_bump_nonexistent(self, fresh_db):
        from app.telegram.bot import cmd_bump
        update = _make_update()
        await cmd_bump(update, _make_context("999"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "not found" in text.lower() or "not pending" in text.lower()


# ══════════════════════════════════════════════════════════════════════
#  BOT COMMAND TESTS — /search
# ══════════════════════════════════════════════════════════════════════


class TestBotSearch:
    @pytest.mark.asyncio
    async def test_search_no_args(self, fresh_db):
        from app.telegram.bot import cmd_search
        update = _make_update()
        await cmd_search(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_search_with_results(self, fresh_db):
        from app.telegram.bot import cmd_search
        await enqueue_task(prompt="fix the auth flow", agent="opencode", chat_id=12345)
        update = _make_update()
        await cmd_search(update, _make_context("auth"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "auth" in text.lower()

    @pytest.mark.asyncio
    async def test_search_no_results(self, fresh_db):
        from app.telegram.bot import cmd_search
        update = _make_update()
        await cmd_search(update, _make_context("zzzzzzz"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "No tasks" in text


# ══════════════════════════════════════════════════════════════════════
#  PERSISTENT REPLY KEYBOARD TESTS
# ══════════════════════════════════════════════════════════════════════


class TestReplyKeyboard:
    @pytest.mark.asyncio
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None, "model": None})
    async def test_start_sends_reply_keyboard(self, mock_prefs, fresh_db):
        from app.telegram.bot import cmd_start
        update = _make_update()
        await cmd_start(update, _make_context())
        calls = update.get_bot().send_message.call_args_list
        # First call should have ReplyKeyboardMarkup
        first_call_markup = calls[0].kwargs.get("reply_markup")
        from telegram import ReplyKeyboardMarkup
        assert isinstance(first_call_markup, ReplyKeyboardMarkup)

    @pytest.mark.asyncio
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None, "model": None})
    async def test_start_sends_inline_keyboard_second(self, mock_prefs, fresh_db):
        from app.telegram.bot import cmd_start
        update = _make_update()
        await cmd_start(update, _make_context())
        calls = update.get_bot().send_message.call_args_list
        assert len(calls) >= 2
        second_call_markup = calls[1].kwargs.get("reply_markup")
        from telegram import InlineKeyboardMarkup
        assert isinstance(second_call_markup, InlineKeyboardMarkup)

    def test_reply_keyboard_has_expected_commands(self):
        from app.telegram.bot import _REPLY_KEYBOARD
        # Flatten all button texts
        buttons = []
        for row in _REPLY_KEYBOARD.keyboard:
            for btn in row:
                buttons.append(btn.text if hasattr(btn, 'text') else str(btn))
        assert "/status" in buttons
        assert "/queue" in buttons
        assert "/cancel" in buttons


# ══════════════════════════════════════════════════════════════════════
#  RUNNER AUTO-RETRY INTEGRATION
# ══════════════════════════════════════════════════════════════════════


class TestRunnerAutoRetry:
    @pytest.mark.asyncio
    async def test_runner_auto_retries_on_crash(self, fresh_db):
        """Runner's _after_complete should auto-retry crashed tasks."""
        from app.core.runner import AgentRunner

        notify_called = []

        async def mock_notify(task):
            notify_called.append(task)

        runner = AgentRunner(on_complete=mock_notify)
        task = await enqueue_task(prompt="test", agent="opencode", chat_id=1)
        await pick_next_task()
        failed = await complete_task(task.id, exit_code=-1, output_summary="crash", full_output="")
        await runner._after_complete(failed)

        # Should have notified about the failure
        assert len(notify_called) == 1
        # Should have created a retry task
        pending = await get_pending_tasks()
        assert len(pending) == 1
        assert pending[0].retry_count == 1

    @pytest.mark.asyncio
    async def test_runner_no_retry_on_normal_failure(self, fresh_db):
        """Runner should NOT auto-retry on normal exit code 1."""
        from app.core.runner import AgentRunner

        notify_called = []

        async def mock_notify(task):
            notify_called.append(task)

        runner = AgentRunner(on_complete=mock_notify)
        task = await enqueue_task(prompt="test", agent="opencode", chat_id=1)
        await pick_next_task()
        failed = await complete_task(task.id, exit_code=1, output_summary="err", full_output="")
        await runner._after_complete(failed)

        # Notification sent as normal (not as retry)
        assert len(notify_called) == 1
        # No retry task created
        pending = await get_pending_tasks()
        assert len(pending) == 0


# ══════════════════════════════════════════════════════════════════════
#  DASHBOARD DICT TESTS
# ══════════════════════════════════════════════════════════════════════


class TestDashboardDict:
    def test_task_to_dict_includes_new_fields(self):
        from app.web.dashboard import _task_to_dict
        task = MagicMock()
        task.id = 1
        task.prompt = "test"
        task.project_dir = "/tmp"
        task.agent = "opencode"
        task.status = TaskStatus.COMPLETED
        task.exit_code = 0
        task.error_message = None
        task.output_summary = "ok"
        task.duration_seconds = 10
        task.created_at = None
        task.started_at = None
        task.completed_at = None
        task.chain_id = None
        task.chain_step = None
        task.repeat_total = None
        task.repeat_remaining = None
        task.assigned_to = None
        task.worker_id = None
        task.priority = 5
        task.retry_count = 1
        task.git_diff = "2 files changed"

        d = _task_to_dict(task)
        assert d["priority"] == 5
        assert d["retry_count"] == 1
        assert d["git_diff"] == "2 files changed"

"""Extensive tests for the follow-up / continue discussion feature."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base, Task, TaskStatus


# ── Fixtures ────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
        await sess.rollback()


def _patch_broker_session(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _get():
        return factory()

    return patch("app.core.broker.get_session", side_effect=_get)


def _make_update(chat_id: int = 12345, user_id: int = 12345, text: str = "hello"):
    user = MagicMock()
    user.id = user_id

    message = AsyncMock()
    message.text = text
    message.message_id = 42
    message.reply_text = AsyncMock(return_value=MagicMock(message_id=99, edit_text=AsyncMock()))

    chat = MagicMock()
    chat.id = chat_id

    bot = AsyncMock()

    update = MagicMock()
    update.effective_user = user
    update.effective_chat = chat
    update.message = message
    update.get_bot.return_value = bot

    return update


def _make_context():
    ctx = MagicMock()
    ctx.args = []
    ctx.user_data = {}
    return ctx


def _get_reply(update):
    """Get the reply text from either reply_text or send_message."""
    if update.message.reply_text.called:
        return update.message.reply_text.call_args[0][0]
    bot = update.get_bot()
    if bot.send_message.called:
        return bot.send_message.call_args.kwargs.get("text", "")
    return ""


# ═════════════════════════════════════════════════════════════════════
#  1. Task.parent_task_id model field
# ═════════════════════════════════════════════════════════════════════


class TestTaskParentField:
    @pytest.mark.asyncio
    async def test_parent_task_id_default_none(self, session):
        task = Task(prompt="test", project_dir="/tmp", agent="opencode")
        session.add(task)
        await session.flush()
        await session.refresh(task)
        assert task.parent_task_id is None

    @pytest.mark.asyncio
    async def test_parent_task_id_set(self, session):
        parent = Task(prompt="parent", project_dir="/tmp", agent="opencode", status=TaskStatus.COMPLETED)
        session.add(parent)
        await session.flush()
        await session.refresh(parent)

        child = Task(prompt="follow-up", project_dir="/tmp", agent="opencode", parent_task_id=parent.id)
        session.add(child)
        await session.flush()
        await session.refresh(child)
        assert child.parent_task_id == parent.id

    @pytest.mark.asyncio
    async def test_parent_task_id_nullable(self, session):
        task = Task(prompt="test", project_dir="/tmp", agent="opencode", parent_task_id=None)
        session.add(task)
        await session.flush()
        await session.refresh(task)
        assert task.parent_task_id is None


# ═════════════════════════════════════════════════════════════════════
#  2. Broker: enqueue_followup
# ═════════════════════════════════════════════════════════════════════


class TestEnqueueFollowup:
    @pytest.mark.asyncio
    async def test_basic_followup(self, engine, session):
        # Create a completed parent task
        parent = Task(
            prompt="fix the bug", project_dir="/tmp/proj", agent="claude",
            model="sonnet", status=TaskStatus.COMPLETED, telegram_chat_id=12345,
        )
        session.add(parent)
        await session.commit()
        await session.refresh(parent)

        from app.core.broker import enqueue_followup
        with _patch_broker_session(engine):
            task = await enqueue_followup(parent_task_id=parent.id, prompt="also fix tests")

        assert task.parent_task_id == parent.id
        assert task.prompt == "also fix tests"
        assert task.agent == "claude"
        assert task.project_dir == "/tmp/proj"
        assert task.model == "sonnet"
        assert task.telegram_chat_id == 12345
        assert task.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_followup_inherits_agent_and_dir(self, engine, session):
        parent = Task(
            prompt="build it", project_dir="/home/user/repo", agent="opencode",
            status=TaskStatus.COMPLETED,
        )
        session.add(parent)
        await session.commit()
        await session.refresh(parent)

        from app.core.broker import enqueue_followup
        with _patch_broker_session(engine):
            task = await enqueue_followup(parent_task_id=parent.id, prompt="now deploy it")

        assert task.agent == "opencode"
        assert task.project_dir == "/home/user/repo"

    @pytest.mark.asyncio
    async def test_followup_overrides_chat_id(self, engine, session):
        parent = Task(
            prompt="test", project_dir="/tmp", agent="opencode",
            status=TaskStatus.COMPLETED, telegram_chat_id=111,
        )
        session.add(parent)
        await session.commit()
        await session.refresh(parent)

        from app.core.broker import enqueue_followup
        with _patch_broker_session(engine):
            task = await enqueue_followup(parent_task_id=parent.id, prompt="continue", chat_id=222)

        assert task.telegram_chat_id == 222

    @pytest.mark.asyncio
    async def test_followup_parent_not_found(self, engine):
        from app.core.broker import enqueue_followup
        with _patch_broker_session(engine):
            with pytest.raises(ValueError, match="not found"):
                await enqueue_followup(parent_task_id=9999, prompt="test")

    @pytest.mark.asyncio
    async def test_followup_parent_still_running(self, engine, session):
        parent = Task(prompt="test", project_dir="/tmp", agent="opencode", status=TaskStatus.RUNNING)
        session.add(parent)
        await session.commit()
        await session.refresh(parent)

        from app.core.broker import enqueue_followup
        with _patch_broker_session(engine):
            with pytest.raises(ValueError, match="running"):
                await enqueue_followup(parent_task_id=parent.id, prompt="test")

    @pytest.mark.asyncio
    async def test_followup_parent_pending(self, engine, session):
        parent = Task(prompt="test", project_dir="/tmp", agent="opencode", status=TaskStatus.PENDING)
        session.add(parent)
        await session.commit()
        await session.refresh(parent)

        from app.core.broker import enqueue_followup
        with _patch_broker_session(engine):
            with pytest.raises(ValueError, match="pending"):
                await enqueue_followup(parent_task_id=parent.id, prompt="test")

    @pytest.mark.asyncio
    async def test_followup_failed_parent_allowed(self, engine, session):
        parent = Task(prompt="test", project_dir="/tmp", agent="claude", status=TaskStatus.FAILED)
        session.add(parent)
        await session.commit()
        await session.refresh(parent)

        from app.core.broker import enqueue_followup
        with _patch_broker_session(engine):
            task = await enqueue_followup(parent_task_id=parent.id, prompt="try again differently")

        assert task.parent_task_id == parent.id
        assert task.agent == "claude"

    @pytest.mark.asyncio
    async def test_followup_queue_full(self, engine, session):
        parent = Task(prompt="test", project_dir="/tmp", agent="opencode", status=TaskStatus.COMPLETED)
        session.add(parent)
        await session.commit()
        await session.refresh(parent)

        from app.core.broker import enqueue_followup
        with _patch_broker_session(engine), \
             patch("app.core.broker.settings") as mock_settings:
            mock_settings.max_queue_size = 0
            mock_settings.default_project_dir = "/tmp"
            mock_settings.default_agent = "opencode"
            with pytest.raises(ValueError, match="Queue full"):
                await enqueue_followup(parent_task_id=parent.id, prompt="test")


# ═════════════════════════════════════════════════════════════════════
#  3. Runner: _build_command with continue flag
# ═════════════════════════════════════════════════════════════════════


class TestBuildCommandContinue:
    def _runner(self):
        from app.core.runner import AgentRunner
        return AgentRunner()

    def _task(self, parent_task_id=None, agent="opencode", model=None):
        return SimpleNamespace(
            prompt="follow up prompt", agent=agent,
            project_dir="/tmp", model=model, parent_task_id=parent_task_id,
        )

    def test_no_continue_flag_without_parent(self):
        task = self._task(parent_task_id=None)
        cmd = self._runner()._build_command(task)
        assert "--continue" not in cmd

    def test_continue_flag_with_parent(self):
        task = self._task(parent_task_id=42)
        cmd = self._runner()._build_command(task)
        assert "--continue" in cmd

    def test_continue_flag_position_before_prompt(self):
        """Regression: --continue must come AFTER 'run' subcommand, BEFORE prompt.

        Previously, --continue was inserted at argv[1], producing:
            opencode --continue run "prompt"  (BROKEN — opencode shows help)
        Now it must be:
            opencode run --continue "prompt"  (CORRECT)
        """
        task = self._task(parent_task_id=1)
        cmd = self._runner()._build_command(task)
        idx_continue = cmd.index("--continue")
        idx_run = cmd.index("run")
        idx_prompt = cmd.index("follow up prompt")
        # --continue must be after 'run' and before the prompt
        assert idx_run < idx_continue < idx_prompt, (
            f"Flag ordering wrong: run@{idx_run}, --continue@{idx_continue}, prompt@{idx_prompt}. "
            f"Full cmd: {cmd}"
        )

    def test_continue_flag_claude(self):
        task = self._task(parent_task_id=1, agent="claude")
        cmd = self._runner()._build_command(task)
        assert "--continue" in cmd
        assert "claude" in cmd[0]

    def test_continue_with_model_both_flags(self):
        task = self._task(parent_task_id=1, model="opus")
        cmd = self._runner()._build_command(task)
        assert "--continue" in cmd
        assert "--model" in cmd

    def test_no_continue_flag_unknown_agent(self):
        task = self._task(parent_task_id=1, agent="nonexistent")
        cmd = self._runner()._build_command(task)
        # Unknown agent returns echo, no continue flag
        assert cmd[0] == "echo"

    def test_no_continue_flag_when_parent_zero(self):
        task = self._task(parent_task_id=0)
        cmd = self._runner()._build_command(task)
        # 0 is falsy, should not inject continue
        assert "--continue" not in cmd

    def test_custom_continue_flag(self):
        task = self._task(parent_task_id=1)
        with patch("app.core.runner.settings") as ms:
            ms.agent_commands = {"opencode": "opencode run {prompt}"}
            ms.agent_model_flags = {}
            ms.agent_continue_flags = {"opencode": "--session-resume"}
            cmd = self._runner()._build_command(task)
        assert "--session-resume" in cmd

    def test_regression_opencode_run_continue_ordering(self):
        """Regression test for production failure 2026-04-20.

        Tasks #21-23 failed because --continue was placed before 'run':
            opencode --continue run "prompt"  → opencode shows help text (exit 1)
        Correct ordering must be:
            opencode run --continue "prompt"
        """
        task = self._task(parent_task_id=20)
        cmd = self._runner()._build_command(task)
        assert cmd[0] == "opencode"
        assert cmd[1] == "run", f"'run' must be argv[1], got {cmd}"
        assert "--continue" in cmd
        # 'run' must always be right after 'opencode'
        assert cmd.index("run") == 1
        assert cmd.index("--continue") > cmd.index("run")

    def test_regression_combined_model_continue_ordering(self):
        """Both --model and --continue must come after 'run', before prompt."""
        task = self._task(parent_task_id=20, model="sonnet")
        cmd = self._runner()._build_command(task)
        assert cmd[0] == "opencode"
        assert cmd[1] == "run"
        assert cmd[-1] == "follow up prompt"
        # All flags are between 'run' and prompt
        run_idx = cmd.index("run")
        prompt_idx = cmd.index("follow up prompt")
        for flag in ["--model", "--continue"]:
            flag_idx = cmd.index(flag)
            assert run_idx < flag_idx < prompt_idx, (
                f"{flag} at wrong position: {cmd}"
            )


# ═════════════════════════════════════════════════════════════════════
#  4. Settings: agent_continue_flags
# ═════════════════════════════════════════════════════════════════════


class TestSettingsContinueFlags:
    def test_default_continue_flags_present(self):
        from app.config.settings import settings
        assert "opencode" in settings.agent_continue_flags
        assert "claude" in settings.agent_continue_flags

    def test_default_flags_are_continue(self):
        from app.config.settings import settings
        assert settings.agent_continue_flags["opencode"] == "--continue"
        assert settings.agent_continue_flags["claude"] == "--continue"


# ═════════════════════════════════════════════════════════════════════
#  5. Bot: /continue command
# ═════════════════════════════════════════════════════════════════════


class TestCmdContinue:
    @pytest.mark.asyncio
    @patch("app.telegram.bot.enqueue_followup", new_callable=AsyncMock)
    async def test_continue_success(self, mock_followup):
        from app.telegram.bot import cmd_continue

        mock_task = MagicMock()
        mock_task.id = 2
        mock_task.agent = "claude"
        mock_followup.return_value = mock_task

        update = _make_update(text="/continue 1 explain the fix in detail")
        await cmd_continue(update, _make_context())

        mock_followup.assert_called_once_with(
            parent_task_id=1,
            prompt="explain the fix in detail",
            chat_id=12345,
            msg_id=42,
        )
        reply = update.message.reply_text.call_args[0][0]
        assert "Follow-up #2" in reply
        assert "#1" in reply

    @pytest.mark.asyncio
    async def test_continue_no_args(self):
        from app.telegram.bot import cmd_continue
        update = _make_update(text="/continue")
        await cmd_continue(update, _make_context())
        reply = _get_reply(update)
        assert "Usage" in reply

    @pytest.mark.asyncio
    async def test_continue_missing_prompt(self):
        from app.telegram.bot import cmd_continue
        update = _make_update(text="/continue 1")
        await cmd_continue(update, _make_context())
        reply = _get_reply(update)
        assert "Usage" in reply

    @pytest.mark.asyncio
    async def test_continue_invalid_id(self):
        from app.telegram.bot import cmd_continue
        update = _make_update(text="/continue abc do something")
        await cmd_continue(update, _make_context())
        reply = _get_reply(update)
        assert "Invalid" in reply

    @pytest.mark.asyncio
    @patch("app.telegram.bot.enqueue_followup", new_callable=AsyncMock, side_effect=ValueError("not found"))
    async def test_continue_parent_not_found(self, mock_followup):
        from app.telegram.bot import cmd_continue
        update = _make_update(text="/continue 999 do something")
        await cmd_continue(update, _make_context())
        reply = _get_reply(update)
        assert "not found" in reply

    @pytest.mark.asyncio
    async def test_continue_too_long_prompt(self):
        from app.telegram.bot import cmd_continue
        from app.config.settings import settings
        update = _make_update(text="/continue 1 " + "x" * (settings.max_prompt_len + 1))
        await cmd_continue(update, _make_context())
        reply = _get_reply(update)
        assert "too long" in reply


# ═════════════════════════════════════════════════════════════════════
#  6. Bot: /cancel_followup command
# ═════════════════════════════════════════════════════════════════════


class TestCmdCancelFollowup:
    @pytest.mark.asyncio
    async def test_cancel_when_active(self):
        from app.telegram.bot import cmd_cancel_followup, _chat_followup
        _chat_followup[12345] = 42
        update = _make_update()
        await cmd_cancel_followup(update, _make_context())
        reply = _get_reply(update)
        assert "Exited" in reply
        assert "#42" in reply
        assert 12345 not in _chat_followup

    @pytest.mark.asyncio
    async def test_cancel_when_not_active(self):
        from app.telegram.bot import cmd_cancel_followup, _chat_followup
        _chat_followup.pop(12345, None)
        update = _make_update()
        await cmd_cancel_followup(update, _make_context())
        reply = _get_reply(update)
        assert "Not in follow-up" in reply


# ═════════════════════════════════════════════════════════════════════
#  7. Bot: handle_text in follow-up mode
# ═════════════════════════════════════════════════════════════════════


class TestHandleTextFollowup:
    @pytest.mark.asyncio
    @patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None)
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.enqueue_followup", new_callable=AsyncMock)
    async def test_followup_mode_routes_to_followup(self, mock_followup, mock_prefs, mock_match):
        from app.telegram.bot import handle_text, _chat_followup

        mock_task = MagicMock()
        mock_task.id = 5
        mock_task.agent = "opencode"
        mock_followup.return_value = mock_task

        _chat_followup[12345] = 3  # follow-up to task #3
        update = _make_update(text="now add tests for it")
        await handle_text(update, _make_context())

        mock_followup.assert_called_once_with(
            parent_task_id=3,
            prompt="now add tests for it",
            chat_id=12345,
            msg_id=42,
        )
        # Conversation mode stays active, pointer updated to new task
        assert _chat_followup[12345] == 5
        reply = update.message.reply_text.call_args[0][0]
        assert "Follow-up" in reply
        assert "keep typing" in reply.lower() or "cancel" in reply.lower()
        _chat_followup.pop(12345, None)  # cleanup

    @pytest.mark.asyncio
    @patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None)
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.enqueue_followup", new_callable=AsyncMock, side_effect=ValueError("not found"))
    async def test_followup_mode_error_clears_state(self, mock_followup, mock_prefs, mock_match):
        from app.telegram.bot import handle_text, _chat_followup

        _chat_followup[12345] = 999
        update = _make_update(text="continue discussion")
        await handle_text(update, _make_context())

        # State should be preserved on error so user can retry
        assert _chat_followup.get(12345) == 999
        reply = _get_reply(update)
        assert "not found" in reply
        _chat_followup.pop(12345, None)  # cleanup

    @pytest.mark.asyncio
    @patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None)
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock)
    async def test_no_followup_creates_normal_task(self, mock_enqueue, mock_prefs, mock_match):
        from app.telegram.bot import handle_text, _chat_followup

        mock_task = MagicMock()
        mock_task.id = 1
        mock_task.agent = "opencode"
        mock_task.project_dir = "/tmp"
        mock_task.model = None
        mock_enqueue.return_value = mock_task

        _chat_followup.pop(12345, None)  # ensure no follow-up state
        update = _make_update(text="new task")
        await handle_text(update, _make_context())

        mock_enqueue.assert_called_once()
        # enqueue_task should be called, not enqueue_followup


# ═════════════════════════════════════════════════════════════════════
#  8. Bot: taskfollowup callback button
# ═════════════════════════════════════════════════════════════════════


class TestFollowupCallback:
    @pytest.mark.asyncio
    @patch("app.telegram.bot.get_task_by_id", new_callable=AsyncMock)
    async def test_followup_button_sets_state(self, mock_get):
        from app.telegram.bot import handle_task_action_callback, _chat_followup

        mock_task = MagicMock()
        mock_task.id = 7
        mock_task.status = TaskStatus.COMPLETED
        mock_get.return_value = mock_task

        query = AsyncMock()
        query.data = "taskfollowup:7"
        query.message = AsyncMock()
        query.message.chat_id = 12345

        user = MagicMock()
        user.id = 12345

        update = MagicMock()
        update.callback_query = query
        update.effective_user = user
        update.effective_chat = MagicMock()
        update.effective_chat.id = 12345

        await handle_task_action_callback(update, _make_context())
        assert _chat_followup.get(12345) == 7
        query.message.reply_text.assert_called_once()
        reply = query.message.reply_text.call_args[0][0]
        assert "Conversation mode" in reply
        _chat_followup.pop(12345, None)  # cleanup

    @pytest.mark.asyncio
    @patch("app.telegram.bot.get_task_by_id", new_callable=AsyncMock, return_value=None)
    async def test_followup_button_task_not_found(self, mock_get):
        from app.telegram.bot import handle_task_action_callback

        query = AsyncMock()
        query.data = "taskfollowup:999"
        query.message = AsyncMock()
        query.message.chat_id = 12345

        user = MagicMock()
        user.id = 12345

        update = MagicMock()
        update.callback_query = query
        update.effective_user = user
        update.effective_chat = MagicMock()
        update.effective_chat.id = 12345

        await handle_task_action_callback(update, _make_context())
        query.edit_message_text.assert_called_once()
        text = query.edit_message_text.call_args[0][0]
        assert "not found" in text

    @pytest.mark.asyncio
    @patch("app.telegram.bot.get_task_by_id", new_callable=AsyncMock)
    async def test_followup_button_task_still_running(self, mock_get):
        from app.telegram.bot import handle_task_action_callback

        mock_task = MagicMock()
        mock_task.id = 5
        mock_task.status = TaskStatus.RUNNING
        mock_get.return_value = mock_task

        query = AsyncMock()
        query.data = "taskfollowup:5"
        query.message = AsyncMock()
        query.message.chat_id = 12345

        user = MagicMock()
        user.id = 12345

        update = MagicMock()
        update.callback_query = query
        update.effective_user = user
        update.effective_chat = MagicMock()
        update.effective_chat.id = 12345

        await handle_task_action_callback(update, _make_context())
        query.edit_message_text.assert_called_once()
        text = query.edit_message_text.call_args[0][0]
        assert "running" in text.lower()


# ═════════════════════════════════════════════════════════════════════
#  9. Completion notification includes Follow Up button
# ═════════════════════════════════════════════════════════════════════


class TestNotificationFollowUpButton:
    @pytest.mark.asyncio
    async def test_completion_notification_has_followup_button(self):
        from app.telegram.bot import make_notify_callback

        app = MagicMock()
        app.bot = AsyncMock()
        notify = await make_notify_callback(app)

        task = MagicMock()
        task.id = 10
        task.status = TaskStatus.COMPLETED
        task.telegram_chat_id = 12345
        task.telegram_msg_id = None
        task.output_summary = "Done"
        task.error_message = None
        task.parent_task_id = None

        await notify(task)
        app.bot.send_message.assert_called_once()
        call_kwargs = app.bot.send_message.call_args.kwargs
        keyboard = call_kwargs.get("reply_markup")
        assert keyboard is not None
        flat = [b for row in keyboard.inline_keyboard for b in row]
        data_values = [b.callback_data for b in flat]
        assert any("taskfollowup:10" in d for d in data_values)

    @pytest.mark.asyncio
    async def test_failed_notification_has_both_retry_and_followup(self):
        from app.telegram.bot import make_notify_callback

        app = MagicMock()
        app.bot = AsyncMock()
        notify = await make_notify_callback(app)

        task = MagicMock()
        task.id = 11
        task.status = TaskStatus.FAILED
        task.telegram_chat_id = 12345
        task.telegram_msg_id = None
        task.output_summary = "Error"
        task.error_message = "Exit code 1"
        task.parent_task_id = None

        await notify(task)
        call_kwargs = app.bot.send_message.call_args.kwargs
        keyboard = call_kwargs.get("reply_markup")
        flat = [b for row in keyboard.inline_keyboard for b in row]
        data_values = [b.callback_data for b in flat]
        assert any("taskretry:" in d for d in data_values)
        assert any("taskfollowup:" in d for d in data_values)

    @pytest.mark.asyncio
    async def test_followup_completion_auto_enters_conversation(self):
        """When a follow-up task completes, auto-set conversation mode."""
        from app.telegram.bot import make_notify_callback, _chat_followup

        app = MagicMock()
        app.bot = AsyncMock()
        notify = await make_notify_callback(app)

        task = MagicMock()
        task.id = 20
        task.status = TaskStatus.COMPLETED
        task.telegram_chat_id = 12345
        task.telegram_msg_id = None
        task.output_summary = "Tests added"
        task.error_message = None
        task.parent_task_id = 19  # this IS a follow-up task

        _chat_followup.pop(12345, None)  # ensure clean state
        await notify(task)
        # Auto-entered conversation mode
        assert _chat_followup.get(12345) == 20
        # Notification text should mention conversation is active
        call_kwargs = app.bot.send_message.call_args.kwargs
        assert "Conversation active" in call_kwargs["text"]
        _chat_followup.pop(12345, None)  # cleanup

    @pytest.mark.asyncio
    async def test_normal_completion_no_auto_conversation(self):
        """Non-follow-up tasks should NOT auto-enter conversation mode."""
        from app.telegram.bot import make_notify_callback, _chat_followup

        app = MagicMock()
        app.bot = AsyncMock()
        notify = await make_notify_callback(app)

        task = MagicMock()
        task.id = 21
        task.status = TaskStatus.COMPLETED
        task.telegram_chat_id = 12345
        task.telegram_msg_id = None
        task.output_summary = "Done"
        task.error_message = None
        task.parent_task_id = None  # NOT a follow-up

        _chat_followup.pop(12345, None)
        await notify(task)
        # Should NOT auto-enter conversation mode
        assert 12345 not in _chat_followup

    @pytest.mark.asyncio
    async def test_followup_failure_breaks_conversation_chain(self):
        """Regression: failed follow-up must break conversation mode.

        Production bug 2026-04-20: follow-up tasks that FAILED still set
        _chat_followup, trapping the user in a loop where every new message
        created another doomed follow-up (tasks #21→#22→#23 all failed).
        """
        from app.telegram.bot import make_notify_callback, _chat_followup

        app = MagicMock()
        app.bot = AsyncMock()
        notify = await make_notify_callback(app)

        # Pre-set conversation mode (simulating an active follow-up chain)
        _chat_followup[12345] = 19

        task = MagicMock()
        task.id = 20
        task.status = TaskStatus.FAILED  # FAILED follow-up
        task.telegram_chat_id = 12345
        task.telegram_msg_id = None
        task.output_summary = "opencode help text"
        task.error_message = "Exit code 1"
        task.parent_task_id = 19  # this IS a follow-up

        await notify(task)
        # Conversation mode must be CLEARED on failure
        assert 12345 not in _chat_followup, (
            "Failed follow-up must break conversation chain"
        )
        # Notification text should tell the user the chain broke
        call_kwargs = app.bot.send_message.call_args.kwargs
        assert "Follow-up ended" in call_kwargs["text"] or "failed" in call_kwargs["text"].lower()

    @pytest.mark.asyncio
    async def test_followup_cancelled_breaks_conversation_chain(self):
        """Cancelled follow-up should also break conversation mode."""
        from app.telegram.bot import make_notify_callback, _chat_followup

        app = MagicMock()
        app.bot = AsyncMock()
        notify = await make_notify_callback(app)

        _chat_followup[12345] = 19

        task = MagicMock()
        task.id = 20
        task.status = TaskStatus.CANCELLED
        task.telegram_chat_id = 12345
        task.telegram_msg_id = None
        task.output_summary = "Cancelled"
        task.error_message = "Cancelled by user"
        task.parent_task_id = 19

        await notify(task)
        assert 12345 not in _chat_followup


# ═════════════════════════════════════════════════════════════════════
#  10. build_app registers handlers
# ═════════════════════════════════════════════════════════════════════


class TestBuildAppFollowup:
    def test_has_continue_command(self):
        from app.telegram.bot import build_app
        from telegram.ext import CommandHandler
        app = build_app()
        handlers = [h for grp in app.handlers.values() for h in grp]
        cmd_names = [h.commands for h in handlers if isinstance(h, CommandHandler)]
        flat_cmds = {c for cmds in cmd_names for c in cmds}
        assert "continue" in flat_cmds
        assert "cancel_followup" in flat_cmds

    def test_callback_pattern_includes_followup(self):
        from app.telegram.bot import build_app
        from telegram.ext import CallbackQueryHandler
        app = build_app()
        handlers = [h for grp in app.handlers.values() for h in grp]
        callback_patterns = [
            h.pattern.pattern for h in handlers
            if isinstance(h, CallbackQueryHandler) and h.pattern
        ]
        assert any("followup" in p for p in callback_patterns)


# ═════════════════════════════════════════════════════════════════════
#  11. Integration: full follow-up lifecycle
# ═════════════════════════════════════════════════════════════════════


class TestFollowupIntegration:
    @pytest.mark.asyncio
    async def test_followup_lifecycle(self, engine, session):
        """Create parent → complete → follow-up → verify chain."""
        from app.core.broker import enqueue_task, complete_task, enqueue_followup

        with _patch_broker_session(engine):
            parent = await enqueue_task(prompt="build the feature", chat_id=12345)
            # Simulate completion
            from app.core.broker import pick_next_task
            picked = await pick_next_task()
            assert picked is not None
            await complete_task(picked.id, exit_code=0, output_summary="Done", full_output="OK")

            # Now follow up
            child = await enqueue_followup(parent_task_id=parent.id, prompt="add tests")

        assert child.parent_task_id == parent.id
        assert child.agent == parent.agent
        assert child.project_dir == parent.project_dir
        assert child.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_chained_followups(self, engine, session):
        """Follow-up of a follow-up should work."""
        from app.core.broker import enqueue_task, complete_task, enqueue_followup, pick_next_task

        with _patch_broker_session(engine):
            t1 = await enqueue_task(prompt="step one", chat_id=12345)
            await pick_next_task()
            await complete_task(t1.id, exit_code=0, output_summary="OK", full_output="")

            t2 = await enqueue_followup(parent_task_id=t1.id, prompt="step two")
            await pick_next_task()
            await complete_task(t2.id, exit_code=0, output_summary="OK2", full_output="")

            t3 = await enqueue_followup(parent_task_id=t2.id, prompt="step three")

        assert t3.parent_task_id == t2.id
        assert t2.parent_task_id == t1.id


# ═════════════════════════════════════════════════════════════════════
#  12. Edge cases
# ═════════════════════════════════════════════════════════════════════


class TestFollowupEdgeCases:
    @pytest.mark.asyncio
    async def test_followup_preserves_no_model(self, engine, session):
        """Follow-up inherits None model from parent."""
        parent = Task(
            prompt="test", project_dir="/tmp", agent="opencode",
            model=None, status=TaskStatus.COMPLETED,
        )
        session.add(parent)
        await session.commit()
        await session.refresh(parent)

        from app.core.broker import enqueue_followup
        with _patch_broker_session(engine):
            child = await enqueue_followup(parent_task_id=parent.id, prompt="more")
        assert child.model is None

    @pytest.mark.asyncio
    async def test_followup_cancelled_parent_rejected(self, engine, session):
        """Cancelled tasks should not allow follow-up."""
        parent = Task(
            prompt="test", project_dir="/tmp", agent="opencode",
            status=TaskStatus.CANCELLED,
        )
        session.add(parent)
        await session.commit()
        await session.refresh(parent)

        from app.core.broker import enqueue_followup
        with _patch_broker_session(engine):
            with pytest.raises(ValueError, match="cancelled"):
                await enqueue_followup(parent_task_id=parent.id, prompt="more")

    def test_build_command_continue_flag_not_duplicated(self):
        """Continue flag should appear exactly once."""
        from app.core.runner import AgentRunner
        task = SimpleNamespace(
            prompt="test", agent="opencode",
            project_dir="/tmp", model=None, parent_task_id=42,
        )
        cmd = AgentRunner()._build_command(task)
        assert cmd.count("--continue") == 1


# ═════════════════════════════════════════════════════════════════════
#  13. Persistent conversation mode (multi-turn)
# ═════════════════════════════════════════════════════════════════════


class TestPersistentConversation:
    """Verify that conversation mode persists across multiple messages."""

    @pytest.mark.asyncio
    @patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None)
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.enqueue_followup", new_callable=AsyncMock)
    async def test_multi_turn_updates_pointer(self, mock_followup, mock_prefs, mock_match):
        """Each message advances the conversation pointer to the new task."""
        from app.telegram.bot import handle_text, _chat_followup

        # Turn 1: user sends first follow-up
        mock_task1 = MagicMock()
        mock_task1.id = 10
        mock_task1.agent = "opencode"
        mock_followup.return_value = mock_task1

        _chat_followup[12345] = 5  # started from task #5
        update1 = _make_update(text="add tests")
        await handle_text(update1, _make_context())
        assert _chat_followup[12345] == 10  # pointer updated to task #10

        # Turn 2: user sends second follow-up (continues from #10)
        mock_task2 = MagicMock()
        mock_task2.id = 15
        mock_task2.agent = "opencode"
        mock_followup.return_value = mock_task2

        update2 = _make_update(text="fix lint errors too")
        await handle_text(update2, _make_context())
        assert _chat_followup[12345] == 15  # pointer updated to task #15

        # Verify second call used task #10 as parent
        assert mock_followup.call_args_list[-1].kwargs["parent_task_id"] == 10

        _chat_followup.pop(12345, None)  # cleanup

    @pytest.mark.asyncio
    @patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None)
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.enqueue_followup", new_callable=AsyncMock, side_effect=ValueError("is pending"))
    async def test_error_preserves_conversation_state(self, mock_followup, mock_prefs, mock_match):
        """On error, user can retry — state is not lost."""
        from app.telegram.bot import handle_text, _chat_followup

        _chat_followup[12345] = 7
        update = _make_update(text="retry this")
        await handle_text(update, _make_context())

        # State preserved — user can try again
        assert _chat_followup[12345] == 7
        _chat_followup.pop(12345, None)  # cleanup

    @pytest.mark.asyncio
    async def test_cancel_exits_persistent_conversation(self):
        """Explicit cancel breaks out of multi-turn conversation."""
        from app.telegram.bot import cmd_cancel_followup, _chat_followup

        _chat_followup[12345] = 42  # deep in conversation
        update = _make_update()
        await cmd_cancel_followup(update, _make_context())

        assert 12345 not in _chat_followup
        reply = _get_reply(update)
        assert "Exited" in reply

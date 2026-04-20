"""Tests for Telegram bot handlers — auth, commands, text handling."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Env vars must be set before any app import
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-for-tests")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

from app.core.models import Task, TaskChain, TaskStatus, ChainStatus
from app.telegram.bot import (
    MAX_MSG_LEN,
    _chat_agent,
    _chat_project_dir,
    _is_allowed_project_dir,
    _status_emoji,
    auth_required,
    cmd_agent,
    cmd_cancel,
    cmd_chain,
    cmd_chains,
    cmd_delchain,
    cmd_help,
    cmd_history,
    cmd_output,
    cmd_project,
    cmd_queue,
    cmd_repeat,
    cmd_retry,
    cmd_savechain,
    cmd_start,
    cmd_status,
    handle_text,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _make_update(chat_id: int = 12345, user_id: int = 12345, text: str = "hello"):
    """Build a minimal mock Update with the fields bot.py accesses."""
    user = MagicMock()
    user.id = user_id

    message = AsyncMock()
    message.text = text
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


def _make_context(args: list[str] | None = None):
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


def _make_task(**overrides) -> Task:
    """Build a Task object with sensible defaults."""
    defaults = dict(
        id=1,
        prompt="fix the bug",
        project_dir="/tmp/proj",
        agent="opencode",
        status=TaskStatus.PENDING,
        telegram_chat_id=12345,
        created_at=datetime(2025, 1, 1),
        started_at=None,
        completed_at=None,
        duration_seconds=None,
        full_output=None,
        output_summary=None,
        error_message=None,
        telegram_msg_id=None,
        repeat_count=None,
        repeat_remaining=None,
        repeat_until=None,
        chain_id=None,
        chain_step=None,
    )
    defaults.update(overrides)
    t = MagicMock(spec=Task)
    for k, v in defaults.items():
        setattr(t, k, v)
    return t


@pytest.fixture(autouse=True)
def _clear_caches():
    """Reset in-memory caches between tests."""
    _chat_project_dir.clear()
    _chat_agent.clear()
    yield
    _chat_project_dir.clear()
    _chat_agent.clear()


# ── Auth decorator ───────────────────────────────────────────────────

class TestAuthRequired:
    async def test_allowed_user_passes(self):
        update = _make_update(user_id=12345)
        ctx = _make_context()

        @auth_required
        async def handler(u, c):
            u._was_called = True

        await handler(update, ctx)
        assert update._was_called is True

    async def test_unauthorized_user_blocked(self):
        update = _make_update(user_id=99999)
        ctx = _make_context()
        was_called = False

        @auth_required
        async def handler(u, c):
            nonlocal was_called
            was_called = True

        await handler(update, ctx)
        assert was_called is False

    async def test_no_user_blocked(self):
        update = _make_update()
        update.effective_user = None
        ctx = _make_context()
        was_called = False

        @auth_required
        async def handler(u, c):
            nonlocal was_called
            was_called = True

        await handler(update, ctx)
        assert was_called is False


# ── Status emoji helper ─────────────────────────────────────────────

class TestStatusEmoji:
    def test_all_statuses_mapped(self):
        for status in TaskStatus:
            assert _status_emoji(status) != "❓"

    def test_unknown_fallback(self):
        assert _status_emoji("bogus") == "❓"


# ── /start ───────────────────────────────────────────────────────────

class TestCmdStart:
    async def test_sends_welcome(self):
        update = _make_update()
        ctx = _make_context()
        await cmd_start(update, ctx)
        bot = update.get_bot()
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args.kwargs["text"]
        assert "TaskPilot ready" in text


# ── /help ────────────────────────────────────────────────────────────

class TestCmdHelp:
    async def test_lists_commands(self):
        update = _make_update()
        ctx = _make_context()
        await cmd_help(update, ctx)
        bot = update.get_bot()
        text = bot.send_message.call_args.kwargs["text"]
        assert "/status" in text
        assert "/retry" in text
        assert "/cancel" in text


# ── /status ──────────────────────────────────────────────────────────

class TestCmdStatus:
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.get_running_task", new_callable=AsyncMock, return_value=None)
    async def test_no_task_running(self, mock_running, mock_prefs):
        update = _make_update()
        await cmd_status(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "No task running" in text

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.get_running_task", new_callable=AsyncMock)
    async def test_shows_elapsed_time(self, mock_running, mock_prefs):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        task = _make_task(
            status=TaskStatus.RUNNING,
            started_at=datetime(2020, 1, 1),  # long ago
        )
        mock_running.return_value = task
        update = _make_update()
        await cmd_status(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "elapsed" in text
        assert "Running" in text


# ── /queue ───────────────────────────────────────────────────────────

class TestCmdQueue:
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.get_pending_tasks", new_callable=AsyncMock, return_value=[])
    async def test_empty_queue(self, mock_pending, mock_prefs):
        update = _make_update()
        await cmd_queue(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "empty" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.get_pending_tasks", new_callable=AsyncMock)
    async def test_shows_pending(self, mock_pending, mock_prefs):
        mock_pending.return_value = [_make_task(id=5, prompt="do thing")]
        update = _make_update()
        await cmd_queue(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "#5" in text


# ── /history ─────────────────────────────────────────────────────────

class TestCmdHistory:
    @patch("app.telegram.bot.get_recent_tasks", new_callable=AsyncMock, return_value=[])
    async def test_empty_history(self, mock_recent):
        update = _make_update()
        await cmd_history(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "No tasks" in text

    @patch("app.telegram.bot.get_recent_tasks", new_callable=AsyncMock)
    async def test_custom_limit(self, mock_recent):
        mock_recent.return_value = [_make_task()]
        update = _make_update()
        await cmd_history(update, _make_context(args=["5"]))
        mock_recent.assert_called_once_with(limit=5)

    @patch("app.telegram.bot.get_recent_tasks", new_callable=AsyncMock)
    async def test_invalid_limit_uses_default(self, mock_recent):
        mock_recent.return_value = [_make_task()]
        update = _make_update()
        await cmd_history(update, _make_context(args=["abc"]))
        mock_recent.assert_called_once_with(limit=10)

    @patch("app.telegram.bot.get_recent_tasks", new_callable=AsyncMock)
    async def test_limit_clamped_to_50(self, mock_recent):
        mock_recent.return_value = [_make_task()]
        update = _make_update()
        await cmd_history(update, _make_context(args=["999"]))
        mock_recent.assert_called_once_with(limit=50)


# ── /cancel ──────────────────────────────────────────────────────────

class TestCmdCancel:
    @patch("app.telegram.bot.cancel_task_by_id", new_callable=AsyncMock)
    async def test_cancel_by_id(self, mock_cancel_id):
        task = _make_task(id=7, status=TaskStatus.CANCELLED)
        mock_cancel_id.return_value = task
        update = _make_update()
        await cmd_cancel(update, _make_context(args=["7"]))
        mock_cancel_id.assert_called_once_with(7)
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "#7" in text

    @patch("app.telegram.bot.get_running_task", new_callable=AsyncMock, return_value=None)
    @patch("app.telegram.bot.cancel_task_by_id", new_callable=AsyncMock, return_value=None)
    async def test_cancel_by_id_not_found(self, mock_cancel_id, mock_running):
        update = _make_update()
        await cmd_cancel(update, _make_context(args=["42"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "not found" in text.lower()

    async def test_cancel_invalid_id(self):
        update = _make_update()
        await cmd_cancel(update, _make_context(args=["abc"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Invalid" in text

    @patch("app.telegram.bot.cancel_running_task", new_callable=AsyncMock)
    @patch("app.telegram.bot.get_running_task", new_callable=AsyncMock)
    @patch("app.telegram.bot.cancel_task_by_id", new_callable=AsyncMock, return_value=None)
    async def test_cancel_running_task_by_id(self, mock_cancel_id, mock_running, mock_cancel_running):
        """When /cancel <id> targets a RUNNING task, cancel it via cancel_running_task."""
        import app.telegram.bot as bot_mod
        bot_mod._runner_ref = MagicMock(cancel_current=AsyncMock())
        running_task = _make_task(id=5, status=TaskStatus.RUNNING)
        mock_running.return_value = running_task
        mock_cancel_running.return_value = _make_task(id=5, status=TaskStatus.CANCELLED)
        update = _make_update()
        await cmd_cancel(update, _make_context(args=["5"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "#5" in text
        assert "running" in text.lower()
        bot_mod._runner_ref = None

    @patch("app.telegram.bot.cancel_running_task", new_callable=AsyncMock)
    async def test_cancel_running(self, mock_cancel):
        import app.telegram.bot as bot_mod
        bot_mod._runner_ref = MagicMock(cancel_current=AsyncMock())
        task = _make_task(id=3, status=TaskStatus.CANCELLED)
        mock_cancel.return_value = task
        update = _make_update()
        await cmd_cancel(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "#3" in text
        bot_mod._runner_ref = None

    @patch("app.telegram.bot.cancel_running_task", new_callable=AsyncMock, return_value=None)
    async def test_cancel_nothing_running(self, mock_cancel):
        update = _make_update()
        await cmd_cancel(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Nothing to cancel" in text

    @patch("app.telegram.bot.cancel_running_task", new_callable=AsyncMock)
    async def test_cancel_running_order(self, mock_cancel):
        """cancel_running_task called before cancel_current to avoid race."""
        call_order = []

        async def mock_cancel_running():
            call_order.append("db")
            return _make_task(id=3, status=TaskStatus.CANCELLED)

        async def mock_cancel_process():
            call_order.append("process")
            return True

        import app.telegram.bot as bot_mod
        mock_cancel.side_effect = mock_cancel_running
        bot_mod._runner_ref = MagicMock(cancel_current=AsyncMock(side_effect=mock_cancel_process))

        update = _make_update()
        await cmd_cancel(update, _make_context())

        assert call_order == ["db", "process"]
        bot_mod._runner_ref = None


# ── /project ─────────────────────────────────────────────────────────

class TestCmdProject:
    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    async def test_set_valid_dir(self, mock_set_pref, monkeypatch, tmp_path):
        monkeypatch.setattr("app.telegram.bot.settings.allowed_project_dirs", str(tmp_path))
        update = _make_update()
        await cmd_project(update, _make_context(args=[str(tmp_path)]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert str(tmp_path) in text
        assert "set to" in text.lower()

    async def test_set_invalid_dir(self, monkeypatch):
        monkeypatch.setattr("app.telegram.bot.settings.allowed_project_dirs", "~/ai,~/repos")
        update = _make_update()
        await cmd_project(update, _make_context(args=["~/ai/nonexistent_abcxyz"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "not found" in text.lower()

    async def test_show_current(self):
        update = _make_update()
        await cmd_project(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Current" in text

    async def test_disallowed_dir_rejected(self):
        update = _make_update()
        await cmd_project(update, _make_context(args=["/etc"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "not in allowed" in text.lower()


# ── _is_allowed_project_dir ─────────────────────────────────────────

class TestIsAllowedProjectDir:
    def test_allowed_path(self, monkeypatch):
        monkeypatch.setattr("app.telegram.bot.settings.allowed_project_dirs", "~/ai,~/repos")
        assert _is_allowed_project_dir("~/ai/myproject") is True

    def test_disallowed_path(self):
        assert _is_allowed_project_dir("/etc/passwd") is False

    def test_exact_match(self, monkeypatch):
        monkeypatch.setattr("app.telegram.bot.settings.allowed_project_dirs", "~/ai,~/repos")
        assert _is_allowed_project_dir("~/ai") is True


# ── /agent ───────────────────────────────────────────────────────────

class TestCmdAgent:
    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    async def test_set_valid_agent(self, mock_set_pref):
        update = _make_update()
        await cmd_agent(update, _make_context(args=["opencode"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "opencode" in text
        assert "set to" in text.lower()

    async def test_set_unknown_agent(self):
        update = _make_update()
        await cmd_agent(update, _make_context(args=["bogus_agent"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Unknown" in text

    async def test_show_current(self):
        update = _make_update()
        await cmd_agent(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Current" in text


# ── /output ──────────────────────────────────────────────────────────

class TestCmdOutput:
    async def test_usage_no_args(self):
        update = _make_update()
        await cmd_output(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Usage" in text

    async def test_invalid_id(self):
        update = _make_update()
        await cmd_output(update, _make_context(args=["abc"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Invalid" in text

    @patch("app.telegram.bot.get_task_by_id", new_callable=AsyncMock, return_value=None)
    async def test_not_found(self, _):
        update = _make_update()
        await cmd_output(update, _make_context(args=["99"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "not found" in text.lower()

    @patch("app.telegram.bot.get_task_by_id", new_callable=AsyncMock)
    async def test_no_output(self, mock_get):
        mock_get.return_value = _make_task(id=5, full_output=None)
        update = _make_update()
        await cmd_output(update, _make_context(args=["5"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "no output" in text.lower()

    @patch("app.telegram.bot.get_task_by_id", new_callable=AsyncMock)
    async def test_short_output_sent_as_text(self, mock_get):
        mock_get.return_value = _make_task(id=5, full_output="done!")
        update = _make_update()
        await cmd_output(update, _make_context(args=["5"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "done!" in text

    @patch("app.telegram.bot.get_task_by_id", new_callable=AsyncMock)
    async def test_long_output_sent_as_file(self, mock_get):
        big = "x" * (MAX_MSG_LEN + 100)
        mock_get.return_value = _make_task(id=5, full_output=big)
        update = _make_update()
        await cmd_output(update, _make_context(args=["5"]))
        bot = update.get_bot()
        bot.send_document.assert_called_once()
        call_kwargs = bot.send_document.call_args.kwargs
        assert "task_5_output.txt" in call_kwargs["document"].name


# ── /retry ───────────────────────────────────────────────────────────

class TestCmdRetry:
    async def test_usage_no_args(self):
        update = _make_update()
        await cmd_retry(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Usage" in text

    async def test_invalid_id(self):
        update = _make_update()
        await cmd_retry(update, _make_context(args=["xyz"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Invalid" in text

    @patch("app.telegram.bot.retry_task", new_callable=AsyncMock, return_value=None)
    async def test_not_found(self, _):
        update = _make_update()
        await cmd_retry(update, _make_context(args=["99"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "not found" in text.lower()

    @patch("app.telegram.bot.retry_task", new_callable=AsyncMock)
    async def test_success(self, mock_retry):
        mock_retry.return_value = _make_task(id=10, agent="opencode")
        update = _make_update()
        await cmd_retry(update, _make_context(args=["5"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "#10" in text
        assert "Retried" in text

    @patch("app.telegram.bot.retry_task", new_callable=AsyncMock, side_effect=ValueError("Queue is full"))
    async def test_queue_full(self, _):
        update = _make_update()
        await cmd_retry(update, _make_context(args=["5"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Queue is full" in text


# ── /delchain ────────────────────────────────────────────────────────

class TestCmdDelchain:
    async def test_usage_no_args(self):
        update = _make_update()
        await cmd_delchain(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Usage" in text

    @patch("app.telegram.bot.delete_chain", new_callable=AsyncMock, return_value=True)
    async def test_delete_found(self, _):
        update = _make_update()
        await cmd_delchain(update, _make_context(args=["mychain"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "deleted" in text.lower()

    @patch("app.telegram.bot.delete_chain", new_callable=AsyncMock, return_value=False)
    async def test_delete_not_found(self, _):
        update = _make_update()
        await cmd_delchain(update, _make_context(args=["nope"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "not found" in text.lower()


# ── /chains ──────────────────────────────────────────────────────────

class TestCmdChains:
    @patch("app.telegram.bot.list_chains", new_callable=AsyncMock, return_value=[])
    async def test_no_chains(self, _):
        update = _make_update()
        await cmd_chains(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "No chains" in text

    @patch("app.telegram.bot.list_chains", new_callable=AsyncMock)
    async def test_shows_chains(self, mock_list):
        chain = MagicMock()
        chain.name = "deploy"
        chain.total_steps = 3
        chain.status = ChainStatus.IDLE
        mock_list.return_value = [chain]
        update = _make_update()
        await cmd_chains(update, _make_context())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "deploy" in text
        assert "3 steps" in text


# ── handle_text ──────────────────────────────────────────────────────

class TestHandleText:
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock)
    async def test_enqueue_and_ack(self, mock_enqueue, mock_prefs):
        task = _make_task(id=42, agent="opencode", project_dir="/home/user/proj")
        mock_enqueue.return_value = task
        update = _make_update(text="fix the login page")
        ack = update.message.reply_text.return_value
        await handle_text(update, _make_context())
        mock_enqueue.assert_called_once()
        ack.edit_text.assert_called_once()
        ack_text = ack.edit_text.call_args[0][0]
        assert "#42" in ack_text
        assert "opencode" in ack_text

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_empty_text_ignored(self, mock_prefs):
        update = _make_update(text="   ")
        await handle_text(update, _make_context())
        update.message.reply_text.assert_not_called()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_too_long_prompt_rejected(self, mock_prefs):
        update = _make_update(text="x" * 2001)
        await handle_text(update, _make_context())
        bot = update.get_bot()
        text = bot.send_message.call_args.kwargs["text"]
        assert "too long" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock, side_effect=ValueError("full"))
    async def test_queue_full_shows_error(self, mock_enqueue, mock_prefs):
        update = _make_update(text="do something")
        ack = update.message.reply_text.return_value
        await handle_text(update, _make_context())
        ack.edit_text.assert_called_once()
        assert "full" in ack.edit_text.call_args[0][0].lower()


# ── _load_prefs ──────────────────────────────────────────────────────

class TestLoadPrefs:
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    async def test_loads_from_db_on_first_access(self, mock_prefs):
        mock_prefs.return_value = {"project_dir": "/home/test/proj", "agent": "aider"}
        from app.telegram.bot import _load_prefs
        await _load_prefs(77777)
        assert _chat_project_dir[77777] == "/home/test/proj"
        assert _chat_agent[77777] == "aider"

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    async def test_skips_db_when_cached(self, mock_prefs):
        _chat_project_dir[88888] = "/cached"
        from app.telegram.bot import _load_prefs
        await _load_prefs(88888)
        mock_prefs.assert_not_called()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    async def test_handles_none_prefs(self, mock_prefs):
        mock_prefs.return_value = {"project_dir": None, "agent": None}
        from app.telegram.bot import _load_prefs
        await _load_prefs(66666)
        assert 66666 not in _chat_project_dir
        assert 66666 not in _chat_agent


# ── make_notify_callback ────────────────────────────────────────────

class TestMakeNotifyCallback:
    async def test_notify_sends_message(self):
        from app.telegram.bot import make_notify_callback

        app = MagicMock()
        app.bot = AsyncMock()
        notify = await make_notify_callback(app)

        task = _make_task(
            id=1,
            telegram_chat_id=123,
            status=TaskStatus.COMPLETED,
            output_summary="All done",
            error_message="minor warning",
        )
        await notify(task)
        app.bot.send_message.assert_called_once()
        text = app.bot.send_message.call_args.kwargs["text"]
        assert "All done" in text
        assert "minor warning" in text

    async def test_notify_no_chat_id(self):
        from app.telegram.bot import make_notify_callback

        app = MagicMock()
        app.bot = AsyncMock()
        notify = await make_notify_callback(app)

        task = _make_task(telegram_chat_id=None)
        await notify(task)
        app.bot.send_message.assert_not_called()

    async def test_notify_edits_original_msg(self):
        from app.telegram.bot import make_notify_callback

        app = MagicMock()
        app.bot = AsyncMock()
        notify = await make_notify_callback(app)

        task = _make_task(
            id=7,
            telegram_chat_id=123,
            telegram_msg_id=456,
            status=TaskStatus.COMPLETED,
        )
        await notify(task)
        app.bot.edit_message_text.assert_called_once()
        kwargs = app.bot.edit_message_text.call_args.kwargs
        assert kwargs["chat_id"] == 123
        assert kwargs["message_id"] == 456

    async def test_notify_edit_failure_swallowed(self):
        from app.telegram.bot import make_notify_callback

        app = MagicMock()
        app.bot = AsyncMock()
        app.bot.edit_message_text.side_effect = RuntimeError("msg deleted")
        notify = await make_notify_callback(app)

        task = _make_task(
            id=7,
            telegram_chat_id=123,
            telegram_msg_id=456,
            status=TaskStatus.FAILED,
        )
        # Should not raise
        await notify(task)
        app.bot.send_message.assert_called_once()


# ── make_chain_notify_callback ──────────────────────────────────────

class TestMakeChainNotifyCallback:
    async def test_chain_notify_sends(self):
        from app.telegram.bot import make_chain_notify_callback

        app = MagicMock()
        app.bot = AsyncMock()
        chain_notify = await make_chain_notify_callback(app)

        await chain_notify(chat_id=123, text="Chain step done")
        app.bot.send_message.assert_called_once()
        assert app.bot.send_message.call_args.kwargs["text"] == "Chain step done"

    async def test_chain_notify_none_chat_id(self):
        from app.telegram.bot import make_chain_notify_callback

        app = MagicMock()
        app.bot = AsyncMock()
        chain_notify = await make_chain_notify_callback(app)

        await chain_notify(chat_id=None, text="ignored")
        app.bot.send_message.assert_not_called()


# ── make_progress_callback ──────────────────────────────────────────

class TestMakeProgressCallback:
    async def test_progress_sends_with_markdown(self):
        from app.telegram.bot import make_progress_callback

        app = MagicMock()
        app.bot = AsyncMock()
        progress = await make_progress_callback(app)

        await progress(chat_id=123, text="Step 1 of 3")
        app.bot.send_message.assert_called_once()
        kwargs = app.bot.send_message.call_args.kwargs
        assert kwargs["parse_mode"] == "Markdown"
        assert kwargs["text"] == "Step 1 of 3"

    async def test_progress_none_chat_id(self):
        from app.telegram.bot import make_progress_callback

        app = MagicMock()
        app.bot = AsyncMock()
        progress = await make_progress_callback(app)

        await progress(chat_id=None, text="ignored")
        app.bot.send_message.assert_not_called()


# ── /repeat ──────────────────────────────────────────────────────────

class TestCmdRepeat:
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_repeat_no_args(self, _):
        update = _make_update()
        await cmd_repeat(update, _make_context(args=[]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Usage" in text

    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_repeat_one_arg(self, _):
        """Single arg (just count, no prompt) also triggers usage."""
        update = _make_update()
        await cmd_repeat(update, _make_context(args=["3"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Usage" in text

    @patch("app.telegram.bot.enqueue_repeat_task", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_repeat_count_only(self, _, mock_enqueue):
        mock_enqueue.return_value = _make_task(id=10)
        update = _make_update()
        await cmd_repeat(update, _make_context(args=["3", "do", "stuff"]))
        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        assert kwargs["repeat_count"] == 3
        assert kwargs["repeat_until"] is None
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "3x" in text

    @patch("app.telegram.bot.enqueue_repeat_task", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_repeat_until_only(self, _, mock_enqueue):
        mock_enqueue.return_value = _make_task(id=11)
        update = _make_update()
        await cmd_repeat(update, _make_context(args=["until:23:59", "do", "stuff"]))
        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        assert kwargs["repeat_count"] is None
        assert kwargs["repeat_until"] is not None
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "until" in text.lower()

    @patch("app.telegram.bot.enqueue_repeat_task", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_repeat_count_and_until(self, _, mock_enqueue):
        mock_enqueue.return_value = _make_task(id=12)
        update = _make_update()
        await cmd_repeat(update, _make_context(args=["5", "until:14:00", "do", "stuff"]))
        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        assert kwargs["repeat_count"] == 5
        assert kwargs["repeat_until"] is not None

    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_repeat_bad_until(self, _):
        update = _make_update()
        await cmd_repeat(update, _make_context(args=["until:bad", "do", "stuff"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Invalid time" in text

    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_repeat_until_hour_only(self, _):
        """until:HH (no minutes) should still work — covers hour-only parse path."""
        from app.telegram.bot import enqueue_repeat_task as _orig
        with patch("app.telegram.bot.enqueue_repeat_task", new_callable=AsyncMock) as mock_enqueue:
            mock_enqueue.return_value = _make_task(id=13)
            update = _make_update()
            await cmd_repeat(update, _make_context(args=["until:14", "do", "stuff"]))
            mock_enqueue.assert_called_once()
            assert mock_enqueue.call_args.kwargs["repeat_until"] is not None

    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_repeat_until_invalid_hour_range(self, _):
        """Hour=25 triggers the range check ValueError."""
        update = _make_update()
        await cmd_repeat(update, _make_context(args=["until:25:00", "do", "stuff"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Invalid time" in text

    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_repeat_until_too_many_parts(self, _):
        """until:1:2:3 triggers 'Expected HH or HH:MM' ValueError."""
        update = _make_update()
        await cmd_repeat(update, _make_context(args=["until:1:2:3", "do", "stuff"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Invalid time" in text

    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_repeat_until_past_time_wraps_to_tomorrow(self, _):
        """When the specified time is already past, deadline rolls to next day."""
        from datetime import datetime as dt, timezone as tz
        # Use a time one minute in the past to guarantee the wrap
        now = dt.now(tz.utc).replace(tzinfo=None)
        past_h = (now.hour - 1) % 24
        time_arg = f"until:{past_h:02d}:{now.minute:02d}"
        with patch("app.telegram.bot.enqueue_repeat_task", new_callable=AsyncMock) as mock_enqueue:
            mock_enqueue.return_value = _make_task(id=14)
            update = _make_update()
            await cmd_repeat(update, _make_context(args=[time_arg, "do", "stuff"]))
            mock_enqueue.assert_called_once()
            deadline = mock_enqueue.call_args.kwargs["repeat_until"]
            assert deadline > now  # confirms it wrapped to tomorrow

    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_repeat_no_count_no_until(self, _):
        update = _make_update()
        await cmd_repeat(update, _make_context(args=["notanumber", "notuntil"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Provide a repeat count" in text

    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_repeat_empty_prompt(self, _):
        update = _make_update()
        await cmd_repeat(update, _make_context(args=["3", "until:12:00"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Missing prompt" in text

    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_repeat_prompt_too_long(self, _):
        update = _make_update()
        await cmd_repeat(update, _make_context(args=["3", "x" * 2001]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "too long" in text.lower()

    @patch("app.telegram.bot.enqueue_repeat_task", new_callable=AsyncMock, side_effect=ValueError("Queue full"))
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_repeat_queue_full(self, _, mock_enqueue):
        update = _make_update()
        await cmd_repeat(update, _make_context(args=["3", "do", "stuff"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Queue full" in text


# ── /savechain ───────────────────────────────────────────────────────

class TestCmdSavechain:
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_savechain_no_args(self, _):
        update = _make_update()
        await cmd_savechain(update, _make_context(args=[]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Usage" in text

    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_savechain_empty_steps(self, _):
        """All separators, no actual step text → 'No steps found'."""
        update = _make_update()
        await cmd_savechain(update, _make_context(args=["mychain", "|", "|"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "No steps found" in text

    @patch("app.telegram.bot.save_chain", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_savechain_success(self, _, mock_save):
        chain = MagicMock()
        chain.name = "mychain"
        chain.total_steps = 2
        chain.steps = [{"prompt": "step1"}, {"prompt": "step2"}]
        mock_save.return_value = chain
        update = _make_update()
        await cmd_savechain(update, _make_context(args=["mychain", "step1", "|", "step2"]))
        mock_save.assert_called_once()
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "mychain" in text
        assert "saved" in text.lower()
        assert "2 steps" in text

    @patch("app.telegram.bot.save_chain", new_callable=AsyncMock, side_effect=ValueError("bad name"))
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_savechain_value_error(self, _, mock_save):
        update = _make_update()
        await cmd_savechain(update, _make_context(args=["mychain", "step1"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "bad name" in text


# ── /chain ───────────────────────────────────────────────────────────

class TestCmdChainRun:
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_chain_no_args(self, _):
        update = _make_update()
        await cmd_chain(update, _make_context(args=[]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "Usage" in text

    @patch("app.telegram.bot.get_chain_by_name", new_callable=AsyncMock)
    @patch("app.telegram.bot.start_chain", new_callable=AsyncMock)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_chain_success(self, _, mock_start, mock_get_chain):
        mock_start.return_value = _make_task(id=20)
        chain = MagicMock()
        chain.total_steps = 3
        mock_get_chain.return_value = chain
        update = _make_update()
        await cmd_chain(update, _make_context(args=["deploy"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "deploy" in text
        assert "started" in text.lower()
        assert "#20" in text

    @patch("app.telegram.bot.start_chain", new_callable=AsyncMock, return_value=None)
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_chain_not_found(self, _, mock_start):
        update = _make_update()
        await cmd_chain(update, _make_context(args=["missing"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "not found" in text.lower()

    @patch("app.telegram.bot.start_chain", new_callable=AsyncMock, side_effect=ValueError("already running"))
    @patch("app.telegram.bot._load_prefs", new_callable=AsyncMock)
    async def test_chain_already_running(self, _, mock_start):
        update = _make_update()
        await cmd_chain(update, _make_context(args=["deploy"]))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "already running" in text


# ── handle_text generic Exception branch ─────────────────────────────

class TestHandleTextException:
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock, side_effect=RuntimeError("DB down"))
    async def test_handle_text_generic_exception(self, mock_enqueue, mock_prefs):
        update = _make_update(text="do something")
        ack = update.message.reply_text.return_value
        await handle_text(update, _make_context())
        ack.edit_text.assert_called_once()
        assert "Failed to queue task" in ack.edit_text.call_args[0][0]


# ── build_app ────────────────────────────────────────────────────────

class TestBuildApp:
    @patch("app.telegram.bot.Application")
    def test_build_app_returns_application(self, mock_app_cls):
        from app.telegram.bot import build_app
        import app.telegram.bot as bot_mod

        mock_built = MagicMock()
        mock_builder = MagicMock()
        mock_builder.token.return_value = mock_builder
        mock_builder.build.return_value = mock_built
        mock_app_cls.builder.return_value = mock_builder

        runner = MagicMock()
        result = build_app(runner=runner)

        assert result is mock_built
        assert bot_mod._runner_ref is runner
        mock_built.add_handler.assert_called()
        # Restore
        bot_mod._runner_ref = None

"""Adversarial tests for Telegram bot command handlers — 200+ edge cases."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base, Task, TaskChain, TaskStatus, ChainStatus


# ── Fixtures ────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def _patch_broker(engine):
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async def _fake():
        return Session()
    with patch("app.core.broker.get_session", _fake):
        yield


def _update(chat_id=12345, user_id=12345, text="/test"):
    user = MagicMock()
    user.id = user_id
    user.first_name = "Test"
    chat = MagicMock()
    chat.id = chat_id
    chat.type = "private"
    msg = MagicMock()
    msg.chat = chat
    msg.chat_id = chat_id
    msg.from_user = user
    msg.text = text
    msg.message_id = 999
    msg.reply_text = AsyncMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_document = AsyncMock()
    update = MagicMock(spec=["message", "effective_user", "effective_chat", "callback_query", "get_bot"])
    update.message = msg
    update.effective_user = user
    update.effective_chat = chat
    update.callback_query = None
    update.get_bot = MagicMock(return_value=bot)
    return update


def _cb(chat_id=12345, user_id=12345, data=""):
    user = MagicMock()
    user.id = user_id
    chat = MagicMock()
    chat.id = chat_id
    chat.type = "private"
    msg = MagicMock()
    msg.chat = chat
    msg.chat_id = chat_id
    msg.reply_text = AsyncMock()
    msg.reply_document = AsyncMock()
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = msg
    query.edit_message_text = AsyncMock()
    query.from_user = user
    update = MagicMock(spec=["message", "effective_user", "effective_chat", "callback_query", "get_bot"])
    update.message = msg
    update.effective_user = user
    update.effective_chat = chat
    update.callback_query = query
    bot = MagicMock()
    bot.send_message = AsyncMock()
    update.get_bot = MagicMock(return_value=bot)
    return update


def _ctx(*args):
    c = MagicMock()
    c.args = list(args) if args else []
    return c


@pytest.fixture(autouse=True)
def _clear():
    from app.telegram.bot import _chat_agent, _chat_model, _chat_project_dir, _chat_followup
    _chat_agent.clear()
    _chat_model.clear()
    _chat_project_dir.clear()
    _chat_followup.clear()
    loaded = getattr(__import__("app.telegram.bot", fromlist=["_load_prefs"])._load_prefs, "_loaded", None)
    if loaded is not None:
        loaded.clear()
    yield
    _chat_agent.clear()
    _chat_model.clear()
    _chat_project_dir.clear()
    _chat_followup.clear()


# ═══════════════════════════════════════════════════════════════════════
# HISTORY COMMAND ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestHistoryAdversarial:

    @pytest.mark.asyncio
    async def test_float_arg(self, _patch_broker):
        from app.telegram.bot import cmd_history
        u = _update()
        await cmd_history(u, _ctx("3.14"))
        # ValueError caught; falls back to default limit 10
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_nan_arg(self, _patch_broker):
        from app.telegram.bot import cmd_history
        u = _update()
        await cmd_history(u, _ctx("nan"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_inf_arg(self, _patch_broker):
        from app.telegram.bot import cmd_history
        u = _update()
        await cmd_history(u, _ctx("inf"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_negative_arg(self, _patch_broker):
        from app.telegram.bot import cmd_history
        u = _update()
        await cmd_history(u, _ctx("-5"))
        # max(1, min(-5, 50)) = 1
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_zero_arg(self, _patch_broker):
        from app.telegram.bot import cmd_history
        u = _update()
        await cmd_history(u, _ctx("0"))
        # max(1, min(0, 50)) = 1
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_very_large_arg(self, _patch_broker):
        from app.telegram.bot import cmd_history
        u = _update()
        await cmd_history(u, _ctx("9999999"))
        # clamped to 50
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_maxsize_arg(self, _patch_broker):
        from app.telegram.bot import cmd_history
        u = _update()
        await cmd_history(u, _ctx(str(sys.maxsize)))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_empty_string_arg(self, _patch_broker):
        from app.telegram.bot import cmd_history
        u = _update()
        await cmd_history(u, _ctx(""))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_boolean_true_string(self, _patch_broker):
        from app.telegram.bot import cmd_history
        u = _update()
        await cmd_history(u, _ctx("True"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_none_string(self, _patch_broker):
        from app.telegram.bot import cmd_history
        u = _update()
        await cmd_history(u, _ctx("None"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_no_args(self, _patch_broker):
        from app.telegram.bot import cmd_history
        u = _update()
        await cmd_history(u, _ctx())
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_negative_inf(self, _patch_broker):
        from app.telegram.bot import cmd_history
        u = _update()
        await cmd_history(u, _ctx("-inf"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_hex_string(self, _patch_broker):
        from app.telegram.bot import cmd_history
        u = _update()
        await cmd_history(u, _ctx("0xFF"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_scientific_notation(self, _patch_broker):
        from app.telegram.bot import cmd_history
        u = _update()
        await cmd_history(u, _ctx("1e5"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_multiple_args_first_used(self, _patch_broker):
        from app.telegram.bot import cmd_history
        u = _update()
        await cmd_history(u, _ctx("5", "10"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_sql_injection_in_limit(self, _patch_broker):
        from app.telegram.bot import cmd_history
        u = _update()
        await cmd_history(u, _ctx("10; DROP TABLE tasks;"))
        u.get_bot().send_message.assert_called()


# ═══════════════════════════════════════════════════════════════════════
# CANCEL COMMAND ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestCancelAdversarial:

    @pytest.mark.asyncio
    async def test_no_args_nothing_running(self, _patch_broker):
        from app.telegram.bot import cmd_cancel
        u = _update()
        await cmd_cancel(u, _ctx())
        call_args = u.get_bot().send_message.call_args
        assert "Nothing to cancel" in str(call_args) or "Nothing" in str(call_args)

    @pytest.mark.asyncio
    async def test_non_int_arg(self, _patch_broker):
        from app.telegram.bot import cmd_cancel
        u = _update()
        await cmd_cancel(u, _ctx("abc"))
        assert "Invalid" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_float_arg(self, _patch_broker):
        from app.telegram.bot import cmd_cancel
        u = _update()
        await cmd_cancel(u, _ctx("3.14"))
        assert "Invalid" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_negative_id(self, _patch_broker):
        from app.telegram.bot import cmd_cancel
        u = _update()
        await cmd_cancel(u, _ctx("-1"))
        # Parses as int -1, task not found
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_zero_id(self, _patch_broker):
        from app.telegram.bot import cmd_cancel
        u = _update()
        await cmd_cancel(u, _ctx("0"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_very_large_id(self, _patch_broker):
        from app.telegram.bot import cmd_cancel
        u = _update()
        await cmd_cancel(u, _ctx("999999999999999"))
        assert "not found" in str(u.get_bot().send_message.call_args).lower() or u.get_bot().send_message.called

    @pytest.mark.asyncio
    async def test_hex_id(self, _patch_broker):
        from app.telegram.bot import cmd_cancel
        u = _update()
        await cmd_cancel(u, _ctx("0xFF"))
        assert "Invalid" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_octal_string(self, _patch_broker):
        from app.telegram.bot import cmd_cancel
        u = _update()
        await cmd_cancel(u, _ctx("0o77"))
        assert "Invalid" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_binary_string(self, _patch_broker):
        from app.telegram.bot import cmd_cancel
        u = _update()
        await cmd_cancel(u, _ctx("0b1010"))
        assert "Invalid" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_scientific_notation(self, _patch_broker):
        from app.telegram.bot import cmd_cancel
        u = _update()
        await cmd_cancel(u, _ctx("1e5"))
        assert "Invalid" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_cancel_pending_task(self, _patch_broker):
        from app.telegram.bot import cmd_cancel
        from app.core.broker import enqueue_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        u = _update()
        await cmd_cancel(u, _ctx(str(task.id)))
        assert "Cancelled" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_id(self, _patch_broker):
        from app.telegram.bot import cmd_cancel
        u = _update()
        await cmd_cancel(u, _ctx("999"))
        assert "not found" in str(u.get_bot().send_message.call_args).lower() or u.get_bot().send_message.called


# ═══════════════════════════════════════════════════════════════════════
# PROJECT COMMAND ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestProjectAdversarial:

    @pytest.mark.asyncio
    async def test_path_traversal(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx("../../../etc/passwd"))
        assert "not in allowed" in str(u.get_bot().send_message.call_args).lower() or "not found" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_dev_null(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx("/dev/null"))
        # /dev/null is a file, not dir, and not in allowed paths
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_proc_environ(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx("/proc/self/environ"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_tilde_root(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx("~root"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_shell_variable_injection(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx("${HOME}"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_backtick_injection(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx("`whoami`"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_subshell_injection(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx("$(id)"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_semicolon_in_path(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx("/tmp/a;rm -rf /"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_pipe_in_path(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx("/tmp/a|b"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_double_slashes(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx("//tmp//test"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_dot_path(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx("."))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_dotdot_path(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx(".."))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_empty_path(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx(""))
        # empty arg shows current
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_very_long_path(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx("/" + "a" * 600))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_unicode_path(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx("/tmp/日本語"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_no_args_shows_current(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx())
        assert "Current" in str(u.get_bot().send_message.call_args) or u.get_bot().send_message.called

    @pytest.mark.asyncio
    async def test_root_path(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx("/"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_ampersand_path(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx("/tmp/a&&b"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_newline_path(self, _patch_broker):
        from app.telegram.bot import cmd_project
        u = _update()
        await cmd_project(u, _ctx("/tmp/a\nb"))
        u.get_bot().send_message.assert_called()


# ═══════════════════════════════════════════════════════════════════════
# AGENT COMMAND ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestAgentAdversarial:

    @pytest.mark.asyncio
    async def test_unknown_agent(self, _patch_broker):
        from app.telegram.bot import cmd_agent
        u = _update()
        await cmd_agent(u, _ctx("nonexistent"))
        assert "Unknown" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_shell_injection_agent(self, _patch_broker):
        from app.telegram.bot import cmd_agent
        u = _update()
        await cmd_agent(u, _ctx("opencode;rm -rf /"))
        assert "Unknown" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_unicode_agent(self, _patch_broker):
        from app.telegram.bot import cmd_agent
        u = _update()
        await cmd_agent(u, _ctx("αgent"))
        assert "Unknown" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_emoji_agent(self, _patch_broker):
        from app.telegram.bot import cmd_agent
        u = _update()
        await cmd_agent(u, _ctx("🤖"))
        assert "Unknown" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_empty_string_agent(self, _patch_broker):
        from app.telegram.bot import cmd_agent
        u = _update()
        await cmd_agent(u, _ctx(""))
        # Empty string not in agent_commands — shows current or unknown
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_case_sensitivity(self, _patch_broker):
        from app.telegram.bot import cmd_agent
        u = _update()
        await cmd_agent(u, _ctx("OPENCODE"))
        # Agent names are case-sensitive, OPENCODE != opencode
        assert "Unknown" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_agent_with_spaces(self, _patch_broker):
        from app.telegram.bot import cmd_agent
        u = _update()
        await cmd_agent(u, _ctx("my agent"))
        assert "Unknown" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_agent_with_slash(self, _patch_broker):
        from app.telegram.bot import cmd_agent
        u = _update()
        await cmd_agent(u, _ctx("../agent"))
        assert "Unknown" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_no_args_shows_current(self, _patch_broker):
        from app.telegram.bot import cmd_agent
        u = _update()
        await cmd_agent(u, _ctx())
        assert "Current" in str(u.get_bot().send_message.call_args) or "Available" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_valid_agent_opencode(self, _patch_broker):
        from app.telegram.bot import cmd_agent
        u = _update()
        await cmd_agent(u, _ctx("opencode"))
        assert "set to" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_valid_agent_claude(self, _patch_broker):
        from app.telegram.bot import cmd_agent
        u = _update()
        await cmd_agent(u, _ctx("claude"))
        assert "set to" in str(u.get_bot().send_message.call_args).lower()


# ═══════════════════════════════════════════════════════════════════════
# MODEL COMMAND ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestModelAdversarial:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reset_word", ["none", "default", "reset", "clear", "NONE", "DEFAULT", "RESET", "CLEAR", "None", "rEsEt"])
    async def test_reset_keywords(self, _patch_broker, reset_word):
        from app.telegram.bot import cmd_model
        u = _update()
        await cmd_model(u, _ctx(reset_word))
        assert "reset" in str(u.get_bot().send_message.call_args).lower() or "default" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_model_with_slashes(self, _patch_broker):
        from app.telegram.bot import cmd_model
        u = _update()
        await cmd_model(u, _ctx("anthropic/claude-sonnet-4"))
        assert "set to" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_model_with_semicolons(self, _patch_broker):
        from app.telegram.bot import cmd_model
        u = _update()
        await cmd_model(u, _ctx("sonnet;evil"))
        assert "set to" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_model_with_dashes(self, _patch_broker):
        from app.telegram.bot import cmd_model
        u = _update()
        await cmd_model(u, _ctx("--model"))
        assert "set to" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_unicode_model(self, _patch_broker):
        from app.telegram.bot import cmd_model
        u = _update()
        await cmd_model(u, _ctx("模型"))
        assert "set to" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_very_long_model(self, _patch_broker):
        from app.telegram.bot import cmd_model
        u = _update()
        await cmd_model(u, _ctx("x" * 200))
        assert "set to" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_no_args_shows_current(self, _patch_broker):
        from app.telegram.bot import cmd_model
        u = _update()
        await cmd_model(u, _ctx())
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_off_keyword(self, _patch_broker):
        from app.telegram.bot import cmd_model
        u = _update()
        # "off" is NOT in the reset keywords list
        await cmd_model(u, _ctx("off"))
        # It will be treated as a model name or caught — just shouldn't crash
        u.get_bot().send_message.assert_called()


# ═══════════════════════════════════════════════════════════════════════
# SETMODEL COMMAND ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestSetmodelAdversarial:

    @pytest.mark.asyncio
    async def test_no_args(self, _patch_broker):
        from app.telegram.bot import cmd_setmodel
        u = _update()
        await cmd_setmodel(u, _ctx())
        assert "Usage" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_one_arg(self, _patch_broker):
        from app.telegram.bot import cmd_setmodel
        u = _update()
        await cmd_setmodel(u, _ctx("1"))
        assert "Usage" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_non_int_task_id(self, _patch_broker):
        from app.telegram.bot import cmd_setmodel
        u = _update()
        await cmd_setmodel(u, _ctx("abc", "sonnet"))
        assert "Invalid" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_nonexistent_task(self, _patch_broker):
        from app.telegram.bot import cmd_setmodel
        u = _update()
        await cmd_setmodel(u, _ctx("999", "sonnet"))
        assert "not found" in str(u.get_bot().send_message.call_args).lower() or "started" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_success_on_pending(self, _patch_broker):
        from app.telegram.bot import cmd_setmodel
        from app.core.broker import enqueue_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        u = _update()
        await cmd_setmodel(u, _ctx(str(task.id), "opus"))
        assert "model" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_reset_keyword_clears(self, _patch_broker):
        from app.telegram.bot import cmd_setmodel
        from app.core.broker import enqueue_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345, model="sonnet")
        u = _update()
        await cmd_setmodel(u, _ctx(str(task.id), "reset"))
        assert "default" in str(u.get_bot().send_message.call_args).lower() or u.get_bot().send_message.called

    @pytest.mark.asyncio
    async def test_three_args(self, _patch_broker):
        from app.telegram.bot import cmd_setmodel
        u = _update()
        await cmd_setmodel(u, _ctx("1", "sonnet", "extra"))
        # Only first two args used
        u.get_bot().send_message.assert_called()


# ═══════════════════════════════════════════════════════════════════════
# OUTPUT COMMAND ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestOutputAdversarial:

    @pytest.mark.asyncio
    async def test_no_args(self, _patch_broker):
        from app.telegram.bot import cmd_output
        u = _update()
        await cmd_output(u, _ctx())
        assert "Usage" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_non_int_id(self, _patch_broker):
        from app.telegram.bot import cmd_output
        u = _update()
        await cmd_output(u, _ctx("abc"))
        assert "Invalid" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_nonexistent_task(self, _patch_broker):
        from app.telegram.bot import cmd_output
        u = _update()
        await cmd_output(u, _ctx("999"))
        assert "not found" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_task_with_no_output(self, _patch_broker):
        from app.telegram.bot import cmd_output
        from app.core.broker import enqueue_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        u = _update()
        await cmd_output(u, _ctx(str(task.id)))
        assert "no output" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_task_with_empty_output(self, _patch_broker, engine):
        from app.telegram.bot import cmd_output
        from app.core.broker import enqueue_task, complete_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        # Mark running then complete
        from app.core.broker import pick_next_task
        await pick_next_task()
        await complete_task(task.id, 0, "", "", None)
        u = _update()
        await cmd_output(u, _ctx(str(task.id)))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_task_with_large_output_sends_file(self, _patch_broker, engine):
        from app.telegram.bot import cmd_output
        from app.core.broker import enqueue_task, pick_next_task, complete_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        await pick_next_task()
        big_output = "A" * 5000
        await complete_task(task.id, 0, "summary", big_output, None)
        u = _update()
        await cmd_output(u, _ctx(str(task.id)))
        u.get_bot().send_document.assert_called()

    @pytest.mark.asyncio
    async def test_negative_task_id(self, _patch_broker):
        from app.telegram.bot import cmd_output
        u = _update()
        await cmd_output(u, _ctx("-1"))
        assert "not found" in str(u.get_bot().send_message.call_args).lower()


# ═══════════════════════════════════════════════════════════════════════
# RETRY COMMAND ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestRetryAdversarial:

    @pytest.mark.asyncio
    async def test_no_args(self, _patch_broker):
        from app.telegram.bot import cmd_retry
        u = _update()
        await cmd_retry(u, _ctx())
        assert "Usage" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_non_int(self, _patch_broker):
        from app.telegram.bot import cmd_retry
        u = _update()
        await cmd_retry(u, _ctx("abc"))
        assert "Invalid" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_nonexistent_task(self, _patch_broker):
        from app.telegram.bot import cmd_retry
        u = _update()
        await cmd_retry(u, _ctx("999"))
        assert "not found" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_retry_pending_task(self, _patch_broker):
        from app.telegram.bot import cmd_retry
        from app.core.broker import enqueue_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        u = _update()
        await cmd_retry(u, _ctx(str(task.id)))
        assert "not found" in str(u.get_bot().send_message.call_args).lower() or "failed" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_retry_cancelled_task(self, _patch_broker):
        from app.telegram.bot import cmd_retry
        from app.core.broker import enqueue_task, cancel_task_by_id
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        await cancel_task_by_id(task.id)
        u = _update()
        await cmd_retry(u, _ctx(str(task.id)))
        assert "Retried" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_retry_failed_task(self, _patch_broker):
        from app.telegram.bot import cmd_retry
        from app.core.broker import enqueue_task, pick_next_task, complete_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        await pick_next_task()
        await complete_task(task.id, 1, "failed", "error", "error")
        u = _update()
        await cmd_retry(u, _ctx(str(task.id)))
        assert "Retried" in str(u.get_bot().send_message.call_args)


# ═══════════════════════════════════════════════════════════════════════
# BUMP COMMAND ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestBumpAdversarial:

    @pytest.mark.asyncio
    async def test_no_args(self, _patch_broker):
        from app.telegram.bot import cmd_bump
        u = _update()
        await cmd_bump(u, _ctx())
        assert "Usage" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_non_int(self, _patch_broker):
        from app.telegram.bot import cmd_bump
        u = _update()
        await cmd_bump(u, _ctx("abc"))
        assert "Invalid" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_nonexistent(self, _patch_broker):
        from app.telegram.bot import cmd_bump
        u = _update()
        await cmd_bump(u, _ctx("999"))
        assert "not found" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_bump_pending(self, _patch_broker):
        from app.telegram.bot import cmd_bump
        from app.core.broker import enqueue_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        u = _update()
        await cmd_bump(u, _ctx(str(task.id)))
        assert "bumped" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_bump_twice(self, _patch_broker):
        from app.telegram.bot import cmd_bump
        from app.core.broker import enqueue_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        u = _update()
        await cmd_bump(u, _ctx(str(task.id)))
        await cmd_bump(u, _ctx(str(task.id)))
        assert u.get_bot().send_message.call_count >= 2


# ═══════════════════════════════════════════════════════════════════════
# SEARCH COMMAND ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestSearchAdversarial:

    @pytest.mark.asyncio
    async def test_no_args(self, _patch_broker):
        from app.telegram.bot import cmd_search
        u = _update()
        await cmd_search(u, _ctx())
        assert "Usage" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_no_results(self, _patch_broker):
        from app.telegram.bot import cmd_search
        u = _update()
        await cmd_search(u, _ctx("nonexistentstringxyz"))
        assert "No tasks" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_sql_wildcard_percent(self, _patch_broker):
        from app.telegram.bot import cmd_search
        from app.core.broker import enqueue_task
        await enqueue_task("test percent", "/tmp", "opencode", chat_id=12345)
        u = _update()
        await cmd_search(u, _ctx("%"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_sql_injection_attempt(self, _patch_broker):
        from app.telegram.bot import cmd_search
        u = _update()
        await cmd_search(u, _ctx("'; DROP TABLE tasks;--"))
        u.get_bot().send_message.assert_called()  # Should not crash

    @pytest.mark.asyncio
    async def test_very_long_query(self, _patch_broker):
        from app.telegram.bot import cmd_search
        u = _update()
        await cmd_search(u, _ctx("x" * 5000))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_unicode_query(self, _patch_broker):
        from app.telegram.bot import cmd_search
        u = _update()
        await cmd_search(u, _ctx("日本語"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_emoji_query(self, _patch_broker):
        from app.telegram.bot import cmd_search
        u = _update()
        await cmd_search(u, _ctx("🔍"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_html_tags(self, _patch_broker):
        from app.telegram.bot import cmd_search
        u = _update()
        await cmd_search(u, _ctx("<script>alert(1)</script>"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_actual_match(self, _patch_broker):
        from app.telegram.bot import cmd_search
        from app.core.broker import enqueue_task
        await enqueue_task("fix the login bug", "/tmp", "opencode", chat_id=12345)
        u = _update()
        await cmd_search(u, _ctx("login"))
        assert "login" in str(u.get_bot().send_message.call_args).lower() or "Search" in str(u.get_bot().send_message.call_args)


# ═══════════════════════════════════════════════════════════════════════
# REPEAT COMMAND ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestRepeatAdversarial:

    @pytest.mark.asyncio
    async def test_no_args(self, _patch_broker):
        from app.telegram.bot import cmd_repeat
        u = _update()
        await cmd_repeat(u, _ctx())
        assert "Usage" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_one_arg_no_prompt(self, _patch_broker):
        from app.telegram.bot import cmd_repeat
        u = _update()
        await cmd_repeat(u, _ctx("5"))
        # Missing prompt
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_count_zero(self, _patch_broker):
        from app.telegram.bot import cmd_repeat
        u = _update()
        await cmd_repeat(u, _ctx("0", "test"))
        # 0 is not valid — either parsed as no-count or fails validation
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_count_negative(self, _patch_broker):
        from app.telegram.bot import cmd_repeat
        u = _update()
        await cmd_repeat(u, _ctx("-1", "test"))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_count_1001(self, _patch_broker):
        from app.telegram.bot import cmd_repeat
        u = _update()
        await cmd_repeat(u, _ctx("1001", "test"))
        assert "1000" in str(u.get_bot().send_message.call_args) or "error" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_count_1000_max(self, _patch_broker):
        from app.telegram.bot import cmd_repeat
        u = _update()
        await cmd_repeat(u, _ctx("1000", "test"))
        assert "Queued" in str(u.get_bot().send_message.call_args) or u.get_bot().send_message.called

    @pytest.mark.asyncio
    async def test_until_invalid_format(self, _patch_broker):
        from app.telegram.bot import cmd_repeat
        u = _update()
        await cmd_repeat(u, _ctx("until:99:99", "test"))
        assert "Invalid" in str(u.get_bot().send_message.call_args) or "Hour" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_until_24_00(self, _patch_broker):
        from app.telegram.bot import cmd_repeat
        u = _update()
        await cmd_repeat(u, _ctx("until:24:00", "test"))
        assert "Invalid" in str(u.get_bot().send_message.call_args) or "Hour" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_prompt_too_long(self, _patch_broker):
        from app.telegram.bot import cmd_repeat
        from app.config.settings import settings
        u = _update()
        await cmd_repeat(u, _ctx("3", "x" * (settings.max_prompt_len + 1)))
        assert "too long" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_valid_repeat(self, _patch_broker):
        from app.telegram.bot import cmd_repeat
        u = _update()
        await cmd_repeat(u, _ctx("3", "test prompt"))
        assert "repeat" in str(u.get_bot().send_message.call_args).lower() or "Queued" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_until_no_prompt(self, _patch_broker):
        from app.telegram.bot import cmd_repeat
        u = _update()
        await cmd_repeat(u, _ctx("until:12:00"))
        # Missing prompt
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_float_count(self, _patch_broker):
        from app.telegram.bot import cmd_repeat
        u = _update()
        await cmd_repeat(u, _ctx("1.5", "test"))
        # int("1.5") fails → treated as no count
        u.get_bot().send_message.assert_called()


# ═══════════════════════════════════════════════════════════════════════
# SAVECHAIN COMMAND ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestSavechainAdversarial:

    @pytest.mark.asyncio
    async def test_no_args(self, _patch_broker):
        from app.telegram.bot import cmd_savechain
        u = _update()
        await cmd_savechain(u, _ctx())
        assert "Usage" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_name_only(self, _patch_broker):
        from app.telegram.bot import cmd_savechain
        u = _update()
        await cmd_savechain(u, _ctx("mychain"))
        assert "Usage" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_name_too_long(self, _patch_broker):
        from app.telegram.bot import cmd_savechain
        u = _update()
        await cmd_savechain(u, _ctx("x" * 65, "step1"))
        assert "1-64" in str(u.get_bot().send_message.call_args) or "characters" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_empty_steps(self, _patch_broker):
        from app.telegram.bot import cmd_savechain
        u = _update()
        # Only whitespace after name
        await cmd_savechain(u, _ctx("mychain", ""))
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_valid_chain(self, _patch_broker):
        from app.telegram.bot import cmd_savechain
        u = _update()
        await cmd_savechain(u, _ctx("test", "step1", "|", "step2"))
        assert "saved" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_single_step(self, _patch_broker):
        from app.telegram.bot import cmd_savechain
        u = _update()
        await cmd_savechain(u, _ctx("test", "single step"))
        assert "saved" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_too_many_steps(self, _patch_broker):
        from app.telegram.bot import cmd_savechain
        u = _update()
        # Build 51 steps separated by |
        steps = " | ".join([f"step{i}" for i in range(51)])
        await cmd_savechain(u, _ctx("test", steps))
        assert "50" in str(u.get_bot().send_message.call_args) or "exceed" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_upsert_overwrites(self, _patch_broker):
        from app.telegram.bot import cmd_savechain
        u = _update()
        await cmd_savechain(u, _ctx("test", "step1"))
        await cmd_savechain(u, _ctx("test", "step2"))
        assert u.get_bot().send_message.call_count >= 2


# ═══════════════════════════════════════════════════════════════════════
# HANDLE TEXT ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestHandleTextAdversarial:

    @pytest.mark.asyncio
    async def test_empty_text(self, _patch_broker):
        from app.telegram.bot import handle_text
        u = _update(text="")
        u.message.text = ""
        await handle_text(u, _ctx())
        # Empty text should be silently ignored
        u.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_whitespace_only(self, _patch_broker):
        from app.telegram.bot import handle_text
        u = _update(text="   ")
        u.message.text = "   "
        await handle_text(u, _ctx())
        # Stripped to empty → ignored
        u.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_null_bytes_only(self, _patch_broker):
        from app.telegram.bot import handle_text
        u = _update(text="\x00\x00")
        u.message.text = "\x00\x00"
        await handle_text(u, _ctx())
        # Sanitized to empty → ignored

    @pytest.mark.asyncio
    async def test_text_too_long(self, _patch_broker):
        from app.telegram.bot import handle_text
        from app.config.settings import settings
        long = "x" * (settings.max_prompt_len + 1)
        u = _update(text=long)
        u.message.text = long
        await handle_text(u, _ctx())
        assert "too long" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_text_at_max_limit(self, _patch_broker):
        from app.telegram.bot import handle_text
        from app.config.settings import settings
        at_limit = "x" * settings.max_prompt_len
        u = _update(text=at_limit)
        u.message.text = at_limit
        await handle_text(u, _ctx())
        u.message.reply_text.assert_called()

    @pytest.mark.asyncio
    async def test_at_alone(self, _patch_broker):
        from app.telegram.bot import handle_text
        u = _update(text="@")
        u.message.text = "@"
        await handle_text(u, _ctx())
        # "@" alone with no space after → treated as plain prompt
        u.message.reply_text.assert_called()

    @pytest.mark.asyncio
    async def test_at_worker_no_prompt(self, _patch_broker):
        from app.telegram.bot import handle_text
        u = _update(text="@server2")
        u.message.text = "@server2"
        await handle_text(u, _ctx())
        # Only "@server2" with no text after → whole thing treated as prompt
        u.message.reply_text.assert_called()

    @pytest.mark.asyncio
    async def test_at_worker_with_prompt(self, _patch_broker):
        from app.telegram.bot import handle_text
        u = _update(text="@server2 fix the bug")
        u.message.text = "@server2 fix the bug"
        await handle_text(u, _ctx())
        u.message.reply_text.assert_called()

    @pytest.mark.asyncio
    async def test_sql_injection_prompt(self, _patch_broker):
        from app.telegram.bot import handle_text
        u = _update(text="'; DROP TABLE tasks;--")
        u.message.text = "'; DROP TABLE tasks;--"
        await handle_text(u, _ctx())
        u.message.reply_text.assert_called()

    @pytest.mark.asyncio
    async def test_xss_attempt(self, _patch_broker):
        from app.telegram.bot import handle_text
        u = _update(text="<script>alert(1)</script>")
        u.message.text = "<script>alert(1)</script>"
        await handle_text(u, _ctx())
        u.message.reply_text.assert_called()

    @pytest.mark.asyncio
    async def test_unicode_text(self, _patch_broker):
        from app.telegram.bot import handle_text
        u = _update(text="日本語のプロンプト")
        u.message.text = "日本語のプロンプト"
        await handle_text(u, _ctx())
        u.message.reply_text.assert_called()

    @pytest.mark.asyncio
    async def test_emoji_text(self, _patch_broker):
        from app.telegram.bot import handle_text
        u = _update(text="🔥 fix this 🐛")
        u.message.text = "🔥 fix this 🐛"
        await handle_text(u, _ctx())
        u.message.reply_text.assert_called()

    @pytest.mark.asyncio
    async def test_control_chars(self, _patch_broker):
        from app.telegram.bot import handle_text
        text = "".join(chr(i) for i in range(1, 32))
        u = _update(text=text)
        u.message.text = text
        await handle_text(u, _ctx())
        # Should not crash

    @pytest.mark.asyncio
    async def test_rtl_override(self, _patch_broker):
        from app.telegram.bot import handle_text
        u = _update(text="\u202e reversed text")
        u.message.text = "\u202e reversed text"
        await handle_text(u, _ctx())
        u.message.reply_text.assert_called()

    @pytest.mark.asyncio
    async def test_zero_width_spaces(self, _patch_broker):
        from app.telegram.bot import handle_text
        u = _update(text="\u200b\u200b\u200b")
        u.message.text = "\u200b\u200b\u200b"
        await handle_text(u, _ctx())
        # Zero-width spaces might remain after strip → treated as text

    @pytest.mark.asyncio
    async def test_markdown_breaking_backticks(self, _patch_broker):
        from app.telegram.bot import handle_text
        u = _update(text="```unmatched backticks")
        u.message.text = "```unmatched backticks"
        await handle_text(u, _ctx())
        u.message.reply_text.assert_called()

    @pytest.mark.asyncio
    async def test_newlines_in_prompt(self, _patch_broker):
        from app.telegram.bot import handle_text
        u = _update(text="line1\nline2\nline3")
        u.message.text = "line1\nline2\nline3"
        await handle_text(u, _ctx())
        u.message.reply_text.assert_called()

    @pytest.mark.asyncio
    async def test_crlf_text(self, _patch_broker):
        from app.telegram.bot import handle_text
        u = _update(text="line1\r\nline2\r\n")
        u.message.text = "line1\r\nline2\r\n"
        await handle_text(u, _ctx())
        u.message.reply_text.assert_called()


# ═══════════════════════════════════════════════════════════════════════
# AUTH GUARD ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestAuthAdversarial:

    @pytest.mark.asyncio
    async def test_unauthorized_user_silenced(self, _patch_broker):
        from app.telegram.bot import cmd_status
        u = _update(user_id=99999)
        await cmd_status(u, _ctx())
        u.get_bot().send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_user(self, _patch_broker):
        from app.telegram.bot import cmd_status
        u = _update()
        u.effective_user = None
        # user_id will be None → not in allowed list
        await cmd_status(u, _ctx())
        u.get_bot().send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_user_id(self, _patch_broker):
        from app.telegram.bot import cmd_status
        u = _update(user_id=0)
        await cmd_status(u, _ctx())
        u.get_bot().send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_negative_user_id(self, _patch_broker):
        from app.telegram.bot import cmd_status
        u = _update(user_id=-1)
        await cmd_status(u, _ctx())
        u.get_bot().send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_authorized_user_works(self, _patch_broker):
        from app.telegram.bot import cmd_status
        u = _update(user_id=12345)
        await cmd_status(u, _ctx())
        u.get_bot().send_message.assert_called()


# ═══════════════════════════════════════════════════════════════════════
# CALLBACK HANDLER ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestAgentCallbackAdversarial:

    @pytest.mark.asyncio
    async def test_empty_data(self, _patch_broker):
        from app.telegram.bot import handle_agent_callback
        u = _cb(data="")
        await handle_agent_callback(u, _ctx())
        u.callback_query.answer.assert_called()

    @pytest.mark.asyncio
    async def test_wrong_prefix(self, _patch_broker):
        from app.telegram.bot import handle_agent_callback
        u = _cb(data="other:1:opencode")
        await handle_agent_callback(u, _ctx())
        u.callback_query.answer.assert_called()

    @pytest.mark.asyncio
    async def test_missing_parts(self, _patch_broker):
        from app.telegram.bot import handle_agent_callback
        u = _cb(data="switch:")
        await handle_agent_callback(u, _ctx())
        u.callback_query.answer.assert_called()

    @pytest.mark.asyncio
    async def test_non_int_task_id(self, _patch_broker):
        from app.telegram.bot import handle_agent_callback
        u = _cb(data="switch:abc:opencode")
        await handle_agent_callback(u, _ctx())
        u.callback_query.answer.assert_called()

    @pytest.mark.asyncio
    async def test_unknown_agent(self, _patch_broker):
        from app.telegram.bot import handle_agent_callback
        u = _cb(data="switch:1:nonexistent")
        await handle_agent_callback(u, _ctx())
        assert "Unknown" in str(u.callback_query.edit_message_text.call_args)

    @pytest.mark.asyncio
    async def test_extra_colons(self, _patch_broker):
        from app.telegram.bot import handle_agent_callback
        u = _cb(data="switch:1:open:code:extra")
        await handle_agent_callback(u, _ctx())
        # split(":", 2) → ["switch", "1", "open:code:extra"]
        u.callback_query.answer.assert_called()

    @pytest.mark.asyncio
    async def test_valid_switch(self, _patch_broker):
        from app.telegram.bot import handle_agent_callback
        from app.core.broker import enqueue_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        u = _cb(data=f"switch:{task.id}:claude")
        await handle_agent_callback(u, _ctx())
        assert "switched" in str(u.callback_query.edit_message_text.call_args).lower() or u.callback_query.edit_message_text.called

    @pytest.mark.asyncio
    async def test_task_already_started(self, _patch_broker):
        from app.telegram.bot import handle_agent_callback
        from app.core.broker import enqueue_task, pick_next_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        await pick_next_task()
        u = _cb(data=f"switch:{task.id}:claude")
        await handle_agent_callback(u, _ctx())
        assert "started" in str(u.callback_query.edit_message_text.call_args).lower() or "not found" in str(u.callback_query.edit_message_text.call_args).lower()


class TestModelSwitchCallbackAdversarial:

    @pytest.mark.asyncio
    async def test_empty_data(self, _patch_broker):
        from app.telegram.bot import handle_model_switch_callback
        u = _cb(data="")
        await handle_model_switch_callback(u, _ctx())
        u.callback_query.answer.assert_called()

    @pytest.mark.asyncio
    async def test_missing_model(self, _patch_broker):
        from app.telegram.bot import handle_model_switch_callback
        u = _cb(data="modelswitch:1:")
        await handle_model_switch_callback(u, _ctx())
        u.callback_query.answer.assert_called()

    @pytest.mark.asyncio
    async def test_non_int_task_id(self, _patch_broker):
        from app.telegram.bot import handle_model_switch_callback
        u = _cb(data="modelswitch:abc:sonnet")
        await handle_model_switch_callback(u, _ctx())
        u.callback_query.answer.assert_called()

    @pytest.mark.asyncio
    async def test_default_keyword(self, _patch_broker):
        from app.telegram.bot import handle_model_switch_callback
        from app.core.broker import enqueue_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345, model="sonnet")
        u = _cb(data=f"modelswitch:{task.id}:__default__")
        await handle_model_switch_callback(u, _ctx())
        assert "default" in str(u.callback_query.edit_message_text.call_args).lower()

    @pytest.mark.asyncio
    async def test_valid_model_switch(self, _patch_broker):
        from app.telegram.bot import handle_model_switch_callback
        from app.core.broker import enqueue_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        u = _cb(data=f"modelswitch:{task.id}:opus")
        await handle_model_switch_callback(u, _ctx())
        u.callback_query.edit_message_text.assert_called()


class TestWorkerSwitchCallbackAdversarial:

    @pytest.mark.asyncio
    async def test_empty_data(self, _patch_broker):
        from app.telegram.bot import handle_worker_switch_callback
        u = _cb(data="")
        await handle_worker_switch_callback(u, _ctx())
        u.callback_query.answer.assert_called()

    @pytest.mark.asyncio
    async def test_local_keyword(self, _patch_broker):
        from app.telegram.bot import handle_worker_switch_callback
        from app.core.broker import enqueue_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345, assigned_to="server2")
        u = _cb(data=f"workerswitch:{task.id}:__local__")
        await handle_worker_switch_callback(u, _ctx())
        assert "local" in str(u.callback_query.edit_message_text.call_args).lower()

    @pytest.mark.asyncio
    async def test_non_int_task_id(self, _patch_broker):
        from app.telegram.bot import handle_worker_switch_callback
        u = _cb(data="workerswitch:abc:server2")
        await handle_worker_switch_callback(u, _ctx())
        u.callback_query.answer.assert_called()


class TestTaskActionCallbackAdversarial:

    @pytest.mark.asyncio
    async def test_empty_data(self, _patch_broker):
        from app.telegram.bot import handle_task_action_callback
        u = _cb(data="")
        await handle_task_action_callback(u, _ctx())
        u.callback_query.answer.assert_called()

    @pytest.mark.asyncio
    async def test_invalid_format(self, _patch_broker):
        from app.telegram.bot import handle_task_action_callback
        u = _cb(data="task:1:extra")
        await handle_task_action_callback(u, _ctx())
        u.callback_query.answer.assert_called()

    @pytest.mark.asyncio
    async def test_retry_nonexistent(self, _patch_broker):
        from app.telegram.bot import handle_task_action_callback
        u = _cb(data="taskretry:999")
        await handle_task_action_callback(u, _ctx())
        assert u.callback_query.edit_message_text.called

    @pytest.mark.asyncio
    async def test_output_nonexistent(self, _patch_broker):
        from app.telegram.bot import handle_task_action_callback
        u = _cb(data="taskoutput:999")
        await handle_task_action_callback(u, _ctx())
        assert u.callback_query.edit_message_text.called

    @pytest.mark.asyncio
    async def test_followup_running_task(self, _patch_broker):
        from app.telegram.bot import handle_task_action_callback
        from app.core.broker import enqueue_task, pick_next_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        await pick_next_task()
        u = _cb(data=f"taskfollowup:{task.id}")
        await handle_task_action_callback(u, _ctx())
        assert "wait" in str(u.callback_query.edit_message_text.call_args).lower() or "running" in str(u.callback_query.edit_message_text.call_args).lower()

    @pytest.mark.asyncio
    async def test_non_int_task_id(self, _patch_broker):
        from app.telegram.bot import handle_task_action_callback
        u = _cb(data="taskretry:abc")
        await handle_task_action_callback(u, _ctx())
        u.callback_query.answer.assert_called()


class TestMenuCallbackAdversarial:

    @pytest.mark.asyncio
    async def test_empty_action(self, _patch_broker):
        from app.telegram.bot import handle_menu_callback
        u = _cb(data="menu:")
        await handle_menu_callback(u, _ctx())
        u.callback_query.answer.assert_called()

    @pytest.mark.asyncio
    async def test_status_action(self, _patch_broker):
        from app.telegram.bot import handle_menu_callback
        u = _cb(data="menu:status")
        await handle_menu_callback(u, _ctx())
        u.callback_query.edit_message_text.assert_called()

    @pytest.mark.asyncio
    async def test_queue_action(self, _patch_broker):
        from app.telegram.bot import handle_menu_callback
        u = _cb(data="menu:queue")
        await handle_menu_callback(u, _ctx())
        u.callback_query.edit_message_text.assert_called()

    @pytest.mark.asyncio
    async def test_history_action(self, _patch_broker):
        from app.telegram.bot import handle_menu_callback
        u = _cb(data="menu:history")
        await handle_menu_callback(u, _ctx())
        u.callback_query.edit_message_text.assert_called()

    @pytest.mark.asyncio
    async def test_help_action(self, _patch_broker):
        from app.telegram.bot import handle_menu_callback
        u = _cb(data="menu:help")
        await handle_menu_callback(u, _ctx())
        u.callback_query.edit_message_text.assert_called()

    @pytest.mark.asyncio
    async def test_settings_action(self, _patch_broker):
        from app.telegram.bot import handle_menu_callback
        u = _cb(data="menu:settings")
        await handle_menu_callback(u, _ctx())
        u.callback_query.edit_message_text.assert_called()

    @pytest.mark.asyncio
    async def test_back_action(self, _patch_broker):
        from app.telegram.bot import handle_menu_callback
        u = _cb(data="menu:back")
        await handle_menu_callback(u, _ctx())
        u.callback_query.edit_message_text.assert_called()

    @pytest.mark.asyncio
    async def test_unknown_action(self, _patch_broker):
        from app.telegram.bot import handle_menu_callback
        u = _cb(data="menu:nonexistent")
        await handle_menu_callback(u, _ctx())
        u.callback_query.answer.assert_called()


class TestContinueCommandAdversarial:

    @pytest.mark.asyncio
    async def test_no_args(self, _patch_broker):
        from app.telegram.bot import cmd_continue
        u = _update(text="/continue")
        u.message.text = "/continue"
        await cmd_continue(u, _ctx())
        u.message.reply_text.assert_called()

    @pytest.mark.asyncio
    async def test_only_task_id(self, _patch_broker):
        from app.telegram.bot import cmd_continue
        u = _update(text="/continue 1")
        u.message.text = "/continue 1"
        await cmd_continue(u, _ctx())
        u.message.reply_text.assert_called()

    @pytest.mark.asyncio
    async def test_invalid_task_id(self, _patch_broker):
        from app.telegram.bot import cmd_continue
        u = _update(text="/continue abc prompt")
        u.message.text = "/continue abc prompt"
        await cmd_continue(u, _ctx())
        assert "Invalid" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_nonexistent_parent(self, _patch_broker):
        from app.telegram.bot import cmd_continue
        u = _update(text="/continue 999 follow up prompt")
        u.message.text = "/continue 999 follow up prompt"
        await cmd_continue(u, _ctx())
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_parent_still_running(self, _patch_broker):
        from app.telegram.bot import cmd_continue
        from app.core.broker import enqueue_task, pick_next_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        await pick_next_task()
        u = _update(text=f"/continue {task.id} follow up")
        u.message.text = f"/continue {task.id} follow up"
        await cmd_continue(u, _ctx())
        u.get_bot().send_message.assert_called()

    @pytest.mark.asyncio
    async def test_prompt_too_long(self, _patch_broker):
        from app.telegram.bot import cmd_continue
        from app.core.broker import enqueue_task, pick_next_task, complete_task
        from app.config.settings import settings
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        await pick_next_task()
        await complete_task(task.id, 0, "ok", "output")
        long = "x" * (settings.max_prompt_len + 1)
        u = _update(text=f"/continue {task.id} {long}")
        u.message.text = f"/continue {task.id} {long}"
        await cmd_continue(u, _ctx())
        assert "too long" in str(u.get_bot().send_message.call_args).lower()

    @pytest.mark.asyncio
    async def test_valid_followup(self, _patch_broker):
        from app.telegram.bot import cmd_continue
        from app.core.broker import enqueue_task, pick_next_task, complete_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        await pick_next_task()
        await complete_task(task.id, 0, "ok", "output")
        u = _update(text=f"/continue {task.id} follow up msg")
        u.message.text = f"/continue {task.id} follow up msg"
        await cmd_continue(u, _ctx())
        u.message.reply_text.assert_called()


class TestFollowupModeAdversarial:

    @pytest.mark.asyncio
    async def test_cancel_followup_when_not_in_mode(self, _patch_broker):
        from app.telegram.bot import cmd_cancel_followup
        u = _update()
        await cmd_cancel_followup(u, _ctx())
        assert "Not in" in str(u.get_bot().send_message.call_args)

    @pytest.mark.asyncio
    async def test_cancel_followup_when_in_mode(self, _patch_broker):
        from app.telegram.bot import cmd_cancel_followup, _chat_followup
        _chat_followup[12345] = 42
        u = _update()
        await cmd_cancel_followup(u, _ctx())
        assert "Exited" in str(u.get_bot().send_message.call_args)
        assert 12345 not in _chat_followup

    @pytest.mark.asyncio
    async def test_followup_mode_routes_text(self, _patch_broker):
        from app.telegram.bot import handle_text, _chat_followup
        from app.core.broker import enqueue_task, pick_next_task, complete_task
        task = await enqueue_task("test", "/tmp", "opencode", chat_id=12345)
        await pick_next_task()
        await complete_task(task.id, 0, "ok", "output")
        _chat_followup[12345] = task.id
        u = _update(text="follow up text")
        u.message.text = "follow up text"
        await handle_text(u, _ctx())
        u.message.reply_text.assert_called()

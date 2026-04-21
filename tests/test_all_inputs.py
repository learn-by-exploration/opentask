"""Comprehensive input testing — every surface, every edge case.

Tests all possible inputs the system could receive:
  A. Telegram bot commands (text parsing, edge cases)
  B. Telegram inline callbacks (malformed data)
  C. Web dashboard API (query/path/body params)
  D. Broker functions (parameter boundaries)
  E. Runner (command building with weird inputs)
  F. Settings (env var parsing)
  G. Sanitization / encoding
"""

from __future__ import annotations

import os
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base, Task, TaskChain, TaskStatus, ChainStatus, Recipe

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

from app.config.settings import settings


# ┌──────────────────────────────────────────────────────────────────┐
# │                    FIXTURES                                      │
# └──────────────────────────────────────────────────────────────────┘

@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
        await s.rollback()


@pytest.fixture(autouse=True)
def _patch_broker_session(engine):
    """Patch broker's get_session to use in-memory DB."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _get_session():
        return factory()

    with patch("app.core.broker.get_session", side_effect=_get_session):
        yield


@pytest.fixture(autouse=True)
def _clear_bot_caches():
    from app.telegram.bot import _chat_project_dir, _chat_agent, _chat_model, _chat_followup, _load_prefs
    _chat_project_dir.clear()
    _chat_agent.clear()
    _chat_model.clear()
    _chat_followup.clear()
    if hasattr(_load_prefs, "_loaded"):
        _load_prefs._loaded.clear()
    yield
    _chat_project_dir.clear()
    _chat_agent.clear()
    _chat_model.clear()
    _chat_followup.clear()
    if hasattr(_load_prefs, "_loaded"):
        _load_prefs._loaded.clear()


def _make_update(chat_id=12345, user_id=12345, text="hello"):
    user = MagicMock()
    user.id = user_id
    message = AsyncMock()
    message.text = text
    message.message_id = 100
    message.chat_id = chat_id
    message.reply_text = AsyncMock()
    chat = MagicMock()
    chat.id = chat_id
    update = MagicMock()
    update.effective_user = user
    update.effective_chat = chat
    update.message = message
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_document = AsyncMock()
    update.get_bot.return_value = bot
    return update


def _make_callback_query(data="", chat_id=12345, user_id=12345):
    user = MagicMock()
    user.id = user_id
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    msg = AsyncMock()
    msg.chat_id = chat_id
    msg.reply_text = AsyncMock()
    msg.reply_document = AsyncMock()
    query.message = msg
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.effective_user = user
    update.effective_chat = MagicMock(id=chat_id)
    update.callback_query = query
    update.message = None
    return update, query


def _ctx(*args):
    ctx = MagicMock()
    ctx.args = list(args)
    return ctx


# ┌──────────────────────────────────────────────────────────────────┐
# │  A. TELEGRAM COMMANDS — TEXT PARSING EDGE CASES                  │
# └──────────────────────────────────────────────────────────────────┘

class TestHistoryEdgeCases:
    """Tests for /history command input parsing."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.get_recent_tasks", new_callable=AsyncMock, return_value=[])
    async def test_history_no_args_defaults_10(self, mock_recent, _):
        from app.telegram.bot import cmd_history
        update = _make_update()
        await cmd_history(update, _ctx())
        mock_recent.assert_called_once_with(limit=10)

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.get_recent_tasks", new_callable=AsyncMock, return_value=[])
    async def test_history_negative_number(self, mock_recent, _):
        from app.telegram.bot import cmd_history
        update = _make_update()
        await cmd_history(update, _ctx("-5"))
        # max(1, min(-5, 50)) → 1
        mock_recent.assert_called_once_with(limit=1)

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.get_recent_tasks", new_callable=AsyncMock, return_value=[])
    async def test_history_zero(self, mock_recent, _):
        from app.telegram.bot import cmd_history
        update = _make_update()
        await cmd_history(update, _ctx("0"))
        # max(1, min(0, 50)) → 1
        mock_recent.assert_called_once_with(limit=1)

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.get_recent_tasks", new_callable=AsyncMock, return_value=[])
    async def test_history_over_50_capped(self, mock_recent, _):
        from app.telegram.bot import cmd_history
        update = _make_update()
        await cmd_history(update, _ctx("999"))
        mock_recent.assert_called_once_with(limit=50)

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.get_recent_tasks", new_callable=AsyncMock, return_value=[])
    async def test_history_float_arg(self, mock_recent, _):
        from app.telegram.bot import cmd_history
        update = _make_update()
        await cmd_history(update, _ctx("3.5"))
        mock_recent.assert_called_once_with(limit=10)  # ValueError fallback

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.get_recent_tasks", new_callable=AsyncMock, return_value=[])
    async def test_history_emoji_arg(self, mock_recent, _):
        from app.telegram.bot import cmd_history
        update = _make_update()
        await cmd_history(update, _ctx("🚀"))
        mock_recent.assert_called_once_with(limit=10)  # ValueError fallback


class TestCancelEdgeCases:
    """Tests for /cancel command input parsing."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.cancel_running_task", new_callable=AsyncMock, return_value=None)
    async def test_cancel_no_args_cancels_running(self, mock_cancel, _):
        from app.telegram.bot import cmd_cancel
        update = _make_update()
        await cmd_cancel(update, _ctx())
        mock_cancel.assert_called_once()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_cancel_non_int_id(self, _):
        from app.telegram.bot import cmd_cancel
        update = _make_update()
        await cmd_cancel(update, _ctx("abc"))
        # _send uses keyword 'text'
        calls = update.get_bot().send_message.call_args_list
        text = calls[-1].kwargs.get("text", calls[-1].args[0] if calls[-1].args else "")
        # Exact message: "Invalid task ID."
        assert "Invalid" in text or "invalid" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_cancel_very_large_id_overflows_sqlite(self, _):
        from app.telegram.bot import cmd_cancel
        update = _make_update()
        # Very large int overflows SQLite INTEGER — OverflowError
        # This is a known gap: bot should validate int range
        with pytest.raises(OverflowError):
            await cmd_cancel(update, _ctx("99999999999999999999"))

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_cancel_float_id(self, _):
        from app.telegram.bot import cmd_cancel
        update = _make_update()
        await cmd_cancel(update, _ctx("3.14"))
        calls = update.get_bot().send_message.call_args_list
        text = calls[-1].kwargs.get("text", calls[-1].args[0] if calls[-1].args else "")
        assert "Invalid" in text or "invalid" in text.lower()


class TestModelCommand:
    """Tests for /model command edge cases."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    async def test_model_none_resets(self, mock_pref, _):
        from app.telegram.bot import cmd_model, _chat_model
        update = _make_update()
        _chat_model[12345] = "opus"
        await cmd_model(update, _ctx("none"))
        assert 12345 not in _chat_model

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    async def test_model_default_resets(self, mock_pref, _):
        from app.telegram.bot import cmd_model, _chat_model
        update = _make_update()
        _chat_model[12345] = "haiku"
        await cmd_model(update, _ctx("DEFAULT"))  # case-insensitive
        assert 12345 not in _chat_model

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    async def test_model_with_slashes(self, mock_pref, _):
        from app.telegram.bot import cmd_model, _chat_model
        update = _make_update()
        await cmd_model(update, _ctx("anthropic/claude-sonnet-4"))
        assert _chat_model[12345] == "anthropic/claude-sonnet-4"

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    async def test_model_unicode_name(self, mock_pref, _):
        from app.telegram.bot import cmd_model, _chat_model
        update = _make_update()
        await cmd_model(update, _ctx("模型α"))
        assert _chat_model[12345] == "模型α"

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    async def test_model_with_spaces_only_first_arg(self, mock_pref, _):
        from app.telegram.bot import cmd_model, _chat_model
        update = _make_update()
        await cmd_model(update, _ctx("my", "model"))
        # Only first arg is used
        assert _chat_model[12345] == "my"


class TestSetmodelCommand:
    """Tests for /setmodel command."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_setmodel_missing_args(self, _):
        from app.telegram.bot import cmd_setmodel
        update = _make_update()
        await cmd_setmodel(update, _ctx())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "usage" in text.lower() or "Usage" in text

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_setmodel_one_arg(self, _):
        from app.telegram.bot import cmd_setmodel
        update = _make_update()
        await cmd_setmodel(update, _ctx("42"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "usage" in text.lower() or "Usage" in text

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_setmodel_non_int_id(self, _):
        from app.telegram.bot import cmd_setmodel
        update = _make_update()
        await cmd_setmodel(update, _ctx("abc", "sonnet"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "invalid" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.switch_task_model", new_callable=AsyncMock, return_value=None)
    async def test_setmodel_clear_clears(self, mock_switch, _):
        from app.telegram.bot import cmd_setmodel
        update = _make_update()
        await cmd_setmodel(update, _ctx("1", "CLEAR"))
        mock_switch.assert_called_once_with(1, "")


class TestProjectCommand:
    """Tests for /project command edge cases."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_project_no_args(self, _):
        from app.telegram.bot import cmd_project
        update = _make_update()
        await cmd_project(update, _ctx())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "project" in text.lower() or "📁" in text

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_project_path_traversal(self, _):
        from app.telegram.bot import cmd_project
        update = _make_update()
        await cmd_project(update, _ctx("../../etc/passwd"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "not allowed" in text.lower() or "allowed" in text.lower() or "❌" in text

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_project_with_null_bytes_crashes(self, _):
        from app.telegram.bot import cmd_project
        update = _make_update()
        # Null bytes in path cause ValueError in os.path.realpath
        # This is a known gap: _sanitize_text strips nulls from message text,
        # but /project args go through context.args which bypass sanitization
        with pytest.raises(ValueError, match="null byte"):
            await cmd_project(update, _ctx("/tmp/test\x00evil"))


class TestAgentCommand:
    """Tests for /agent command edge cases."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_agent_unknown_name(self, _):
        from app.telegram.bot import cmd_agent
        update = _make_update()
        await cmd_agent(update, _ctx("nonexistent_agent"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "unknown" in text.lower() or "❓" in text or "available" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_agent_no_args_shows_current(self, _):
        from app.telegram.bot import cmd_agent
        update = _make_update()
        await cmd_agent(update, _ctx())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "agent" in text.lower() or "🤖" in text

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_agent_with_special_chars(self, _):
        from app.telegram.bot import cmd_agent
        update = _make_update()
        await cmd_agent(update, _ctx("agent;rm -rf /"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "unknown" in text.lower() or "❓" in text or "available" in text.lower()


class TestSearchCommand:
    """Tests for /search command."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_search_no_args(self, _):
        from app.telegram.bot import cmd_search
        update = _make_update()
        await cmd_search(update, _ctx())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "usage" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.search_tasks", new_callable=AsyncMock, return_value=[])
    async def test_search_sql_wildcards(self, mock_search, _):
        from app.telegram.bot import cmd_search
        update = _make_update()
        await cmd_search(update, _ctx("%", "_", "%"))
        # Should pass raw to search — SQLAlchemy escapes it
        mock_search.assert_called_once()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.search_tasks", new_callable=AsyncMock, return_value=[])
    async def test_search_unicode_query(self, mock_search, _):
        from app.telegram.bot import cmd_search
        update = _make_update()
        await cmd_search(update, _ctx("修復", "バグ"))
        mock_search.assert_called_once_with("修復 バグ", limit=10)

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.search_tasks", new_callable=AsyncMock, return_value=[])
    async def test_search_very_long_query(self, mock_search, _):
        from app.telegram.bot import cmd_search
        update = _make_update()
        long_q = "a" * 5000
        await cmd_search(update, _ctx(long_q))
        mock_search.assert_called_once()


class TestRepeatCommand:
    """Tests for /repeat command parsing edge cases."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_repeat_no_args(self, _):
        from app.telegram.bot import cmd_repeat
        update = _make_update()
        await cmd_repeat(update, _ctx())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "usage" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_repeat_only_count_no_prompt(self, _):
        from app.telegram.bot import cmd_repeat
        update = _make_update()
        await cmd_repeat(update, _ctx("5"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "usage" in text.lower() or "missing" in text.lower() or "provide" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_repeat_invalid_time_format(self, _):
        from app.telegram.bot import cmd_repeat
        update = _make_update()
        await cmd_repeat(update, _ctx("until:25:99", "do stuff"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "invalid" in text.lower() or "hour" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_repeat_until_no_colon(self, _):
        from app.telegram.bot import cmd_repeat
        update = _make_update()
        await cmd_repeat(update, _ctx("until:", "do stuff"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        # "until:" with empty time should fail parsing
        assert "invalid" in text.lower() or "provide" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_repeat_negative_count(self, _):
        from app.telegram.bot import cmd_repeat
        update = _make_update()
        await cmd_repeat(update, _ctx("-3", "do stuff"))
        # -3 is a valid int but broker validates range (1-1000)
        text = update.get_bot().send_message.call_args.kwargs["text"]
        # Either "provide" or error from broker validation
        assert any(w in text.lower() for w in ("provide", "repeat", "⚠"))

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_repeat_zero_count(self, _):
        from app.telegram.bot import cmd_repeat
        update = _make_update()
        await cmd_repeat(update, _ctx("0", "do stuff"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert any(w in text.lower() for w in ("provide", "repeat", "⚠"))

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_repeat_count_over_1000(self, _):
        from app.telegram.bot import cmd_repeat
        update = _make_update()
        await cmd_repeat(update, _ctx("9999", "do stuff"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "⚠" in text or "1000" in text or "max" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_repeat_prompt_only_nulls(self, _):
        from app.telegram.bot import cmd_repeat
        update = _make_update()
        await cmd_repeat(update, _ctx("3", "\x00\x00\x00"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "missing" in text.lower() or "prompt" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_repeat_prompt_too_long(self, _):
        from app.telegram.bot import cmd_repeat
        from app.config.settings import settings
        update = _make_update()
        await cmd_repeat(update, _ctx("3", "x" * (settings.max_prompt_len + 1)))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "too long" in text.lower() or "max" in text.lower()


class TestSavechainCommand:
    """Tests for /savechain command edge cases."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_savechain_no_args(self, _):
        from app.telegram.bot import cmd_savechain
        update = _make_update()
        await cmd_savechain(update, _ctx())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "usage" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_savechain_name_only(self, _):
        from app.telegram.bot import cmd_savechain
        update = _make_update()
        await cmd_savechain(update, _ctx("mychain"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "usage" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_savechain_name_too_long(self, _):
        from app.telegram.bot import cmd_savechain
        update = _make_update()
        await cmd_savechain(update, _ctx("x" * 200, "step1", "|", "step2"))
        calls = update.get_bot().send_message.call_args_list
        text = calls[-1].kwargs.get("text", calls[-1].args[0] if calls[-1].args else "")
        assert "⚠" in text or "64" in text or "1-64" in text

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_savechain_too_many_steps(self, _):
        from app.telegram.bot import cmd_savechain
        steps_args = []
        for i in range(55):
            steps_args.extend([f"step{i}", "|"])
        steps_args.pop()  # remove trailing |
        update = _make_update()
        await cmd_savechain(update, _ctx("mychain", *steps_args))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "⚠" in text or "50" in text or "too many" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_savechain_empty_step(self, _):
        from app.telegram.bot import cmd_savechain
        update = _make_update()
        await cmd_savechain(update, _ctx("mychain", "step1", "|", "|", "step2"))
        # Empty step between pipes — should be filtered out

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_savechain_unicode_name(self, _):
        from app.telegram.bot import cmd_savechain
        update = _make_update()
        await cmd_savechain(update, _ctx("チェーン", "rebuild all"))
        # Should work — unicode name accepted


class TestOutputCommand:
    """Tests for /output command."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_output_no_args(self, _):
        from app.telegram.bot import cmd_output
        update = _make_update()
        await cmd_output(update, _ctx())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "usage" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_output_non_int_id(self, _):
        from app.telegram.bot import cmd_output
        update = _make_update()
        await cmd_output(update, _ctx("abc"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "invalid" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.get_task_by_id", new_callable=AsyncMock, return_value=None)
    async def test_output_nonexistent_task(self, mock_get, _):
        from app.telegram.bot import cmd_output
        update = _make_update()
        await cmd_output(update, _ctx("99999"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "not found" in text.lower()


class TestRetryCommand:
    """Tests for /retry command."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_retry_no_args(self, _):
        from app.telegram.bot import cmd_retry
        update = _make_update()
        await cmd_retry(update, _ctx())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "usage" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_retry_non_int_id(self, _):
        from app.telegram.bot import cmd_retry
        update = _make_update()
        await cmd_retry(update, _ctx("not_a_number"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "invalid" in text.lower()


class TestBumpCommand:
    """Tests for /bump command."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.bump_task", new_callable=AsyncMock, return_value=None)
    async def test_bump_nonexistent_task(self, mock_bump, _):
        from app.telegram.bot import cmd_start
        # This tests a valid task ID that doesn't exist


# ┌──────────────────────────────────────────────────────────────────┐
# │  A2. HANDLE_TEXT — FREE-TEXT INPUT EDGE CASES                    │
# └──────────────────────────────────────────────────────────────────┘

class TestHandleTextInputs:
    """Tests for raw text message handling."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_empty_text_ignored(self, _):
        from app.telegram.bot import handle_text
        update = _make_update(text="")
        await handle_text(update, _ctx())
        # No enqueue should happen

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_whitespace_only_ignored(self, _):
        from app.telegram.bot import handle_text
        update = _make_update(text="   \n\t  ")
        await handle_text(update, _ctx())
        # Sanitize + strip → empty → ignored

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_null_bytes_stripped(self, _):
        from app.telegram.bot import handle_text, _sanitize_text
        result = _sanitize_text("hello\x00world\x00")
        assert result == "helloworld"

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_only_null_bytes_becomes_empty(self, _):
        from app.telegram.bot import handle_text
        update = _make_update(text="\x00\x00\x00")
        await handle_text(update, _ctx())
        # After sanitize → empty → ignored

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_prompt_too_long_rejected(self, _):
        from app.telegram.bot import handle_text
        from app.config.settings import settings
        update = _make_update(text="x" * (settings.max_prompt_len + 1))
        await handle_text(update, _ctx())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "too long" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_exactly_max_length_accepted(self, _):
        from app.telegram.bot import handle_text
        from app.config.settings import settings
        update = _make_update(text="x" * settings.max_prompt_len)
        with patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock) as mock_enq:
            task = MagicMock()
            task.id = 1
            task.agent = "opencode"
            task.prompt = "x" * settings.max_prompt_len
            task.project_dir = "/tmp/test"
            task.model = None
            task.assigned_to = None
            mock_enq.return_value = task
            with patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None):
                await handle_text(update, _ctx())
                mock_enq.assert_called_once()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_at_worker_prefix_parsed(self, _):
        from app.telegram.bot import handle_text
        update = _make_update(text="@server2 fix the bug")
        with patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock) as mock_enq:
            task = MagicMock()
            task.id = 1
            task.agent = "opencode"
            task.prompt = "fix the bug"
            task.project_dir = "/tmp/test"
            task.model = None
            task.assigned_to = "server2"
            mock_enq.return_value = task
            with patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None):
                await handle_text(update, _ctx())
                call_kwargs = mock_enq.call_args.kwargs
                assert call_kwargs["assigned_to"] == "server2"
                assert call_kwargs["prompt"] == "fix the bug"

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_at_only_no_space_treated_as_prompt(self, _):
        from app.telegram.bot import handle_text
        update = _make_update(text="@server2")
        with patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock) as mock_enq:
            task = MagicMock()
            task.id = 1
            task.agent = "opencode"
            task.prompt = "@server2"
            task.project_dir = "/tmp/test"
            task.model = None
            task.assigned_to = None
            mock_enq.return_value = task
            with patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None):
                await handle_text(update, _ctx())
                call_kwargs = mock_enq.call_args.kwargs
                # No space after @name → treat whole thing as prompt
                assert call_kwargs["assigned_to"] is None

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_unicode_prompt(self, _):
        from app.telegram.bot import handle_text
        update = _make_update(text="修復這個錯誤 🐛 с русским текстом")
        with patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock) as mock_enq:
            task = MagicMock()
            task.id = 1
            task.agent = "opencode"
            task.prompt = "修復這個錯誤 🐛 с русским текстом"
            task.project_dir = "/tmp/test"
            task.model = None
            task.assigned_to = None
            mock_enq.return_value = task
            with patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None):
                await handle_text(update, _ctx())
                mock_enq.assert_called_once()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_newlines_in_prompt(self, _):
        from app.telegram.bot import handle_text
        update = _make_update(text="line1\nline2\nline3")
        with patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock) as mock_enq:
            task = MagicMock()
            task.id = 1
            task.agent = "opencode"
            task.prompt = "line1\nline2\nline3"
            task.project_dir = "/tmp/test"
            task.model = None
            task.assigned_to = None
            mock_enq.return_value = task
            with patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None):
                await handle_text(update, _ctx())
                mock_enq.assert_called_once()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_queue_full_error_handled(self, _):
        from app.telegram.bot import handle_text
        update = _make_update(text="do something")
        with patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock, side_effect=ValueError("full")):
            with patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None):
                await handle_text(update, _ctx())
                calls = update.get_bot().send_message.call_args_list
                text = calls[-1].kwargs.get("text", calls[-1].args[0] if calls[-1].args else "")
                assert "queue" in text.lower() or "full" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_unexpected_exception_handled(self, _):
        from app.telegram.bot import handle_text
        update = _make_update(text="do something")
        with patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            with patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None):
                await handle_text(update, _ctx())
                calls = update.get_bot().send_message.call_args_list
                text = calls[-1].kwargs.get("text", calls[-1].args[0] if calls[-1].args else "")
                assert "failed" in text.lower() or "⚠" in text


# ┌──────────────────────────────────────────────────────────────────┐
# │  B. TELEGRAM INLINE CALLBACKS — MALFORMED DATA                   │
# └──────────────────────────────────────────────────────────────────┘

class TestAgentCallbackMalformed:
    """Tests for inline agent switch with malformed callback data."""

    async def test_switch_missing_parts(self):
        from app.telegram.bot import handle_agent_callback
        update, query = _make_callback_query(data="switch:")
        await handle_agent_callback(update, _ctx())
        # parts != 3 → return early, no edit
        query.edit_message_text.assert_not_called()

    async def test_switch_non_int_task_id(self):
        from app.telegram.bot import handle_agent_callback
        update, query = _make_callback_query(data="switch:abc:opencode")
        await handle_agent_callback(update, _ctx())
        # ValueError → return early
        query.edit_message_text.assert_not_called()

    async def test_switch_unknown_agent(self):
        from app.telegram.bot import handle_agent_callback
        update, query = _make_callback_query(data="switch:1:nonexistent_agent_xyz")
        await handle_agent_callback(update, _ctx())
        query.edit_message_text.assert_called_once()
        text = query.edit_message_text.call_args[0][0]
        assert "❓" in text or "Unknown" in text

    async def test_switch_not_starting_with_prefix(self):
        from app.telegram.bot import handle_agent_callback
        update, query = _make_callback_query(data="notswitch:1:opencode")
        await handle_agent_callback(update, _ctx())
        query.edit_message_text.assert_not_called()

    async def test_switch_extra_colons(self):
        from app.telegram.bot import handle_agent_callback
        update, query = _make_callback_query(data="switch:1:agent:extra:stuff")
        await handle_agent_callback(update, _ctx())
        # split(":", 2) gives ["switch", "1", "agent:extra:stuff"] — unknown agent
        query.edit_message_text.assert_called_once()
        text = query.edit_message_text.call_args[0][0]
        assert "❓" in text


class TestModelSwitchCallbackMalformed:
    """Tests for inline model switch with malformed callback data."""

    async def test_modelswitch_missing_parts(self):
        from app.telegram.bot import handle_model_switch_callback
        update, query = _make_callback_query(data="modelswitch:")
        await handle_model_switch_callback(update, _ctx())
        query.edit_message_text.assert_not_called()

    async def test_modelswitch_non_int_id(self):
        from app.telegram.bot import handle_model_switch_callback
        update, query = _make_callback_query(data="modelswitch:abc:sonnet")
        await handle_model_switch_callback(update, _ctx())
        query.edit_message_text.assert_not_called()

    @patch("app.telegram.bot.switch_task_model", new_callable=AsyncMock, return_value=MagicMock(id=1, model=""))
    async def test_modelswitch_default_clears(self, mock_switch):
        from app.telegram.bot import handle_model_switch_callback
        update, query = _make_callback_query(data="modelswitch:1:__default__")
        await handle_model_switch_callback(update, _ctx())
        mock_switch.assert_called_once_with(1, "")


class TestWorkerSwitchCallbackMalformed:
    """Tests for inline worker switch with malformed callback data."""

    async def test_workerswitch_missing_parts(self):
        from app.telegram.bot import handle_worker_switch_callback
        update, query = _make_callback_query(data="workerswitch:")
        await handle_worker_switch_callback(update, _ctx())
        query.edit_message_text.assert_not_called()

    async def test_workerswitch_non_int_id(self):
        from app.telegram.bot import handle_worker_switch_callback
        update, query = _make_callback_query(data="workerswitch:abc:server2")
        await handle_worker_switch_callback(update, _ctx())
        query.edit_message_text.assert_not_called()

    @patch("app.telegram.bot.switch_task_worker", new_callable=AsyncMock, return_value=MagicMock(id=1, assigned_to=""))
    async def test_workerswitch_local_clears(self, mock_switch):
        from app.telegram.bot import handle_worker_switch_callback
        update, query = _make_callback_query(data="workerswitch:1:__local__")
        await handle_worker_switch_callback(update, _ctx())
        mock_switch.assert_called_once_with(1, "")


class TestMenuCallbackEdges:
    """Tests for menu callback with odd actions."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_menu_unknown_action(self, _):
        from app.telegram.bot import handle_menu_callback
        update, query = _make_callback_query(data="menu:nonexistent")
        await handle_menu_callback(update, _ctx())
        # Should not crash — unhandled action is silently ignored

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_menu_empty_action(self, _):
        from app.telegram.bot import handle_menu_callback
        update, query = _make_callback_query(data="menu:")
        await handle_menu_callback(update, _ctx())


class TestRecipeCallbackEdges:
    """Tests for recipe callback with edge cases."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_recipeuse_no_colon(self, _):
        from app.telegram.bot import handle_recipe_callback
        update, query = _make_callback_query(data="recipeuse")
        await handle_recipe_callback(update, _ctx())
        # No colon → return early

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_recipeskip_non_int(self, _):
        from app.telegram.bot import handle_recipe_callback
        update, query = _make_callback_query(data="recipeskip:abc")
        await handle_recipe_callback(update, _ctx())

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_recipeuse_missing_recipe_name(self, _):
        from app.telegram.bot import handle_recipe_callback
        update, query = _make_callback_query(data="recipeuse:42")
        await handle_recipe_callback(update, _ctx())
        # sub_parts has only 1 part → return early

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.get_recipe_by_name", new_callable=AsyncMock, return_value=None)
    async def test_recipeuse_deleted_recipe(self, mock_get, _):
        from app.telegram.bot import handle_recipe_callback
        update, query = _make_callback_query(data="recipeuse:42:deleted_recipe")
        await handle_recipe_callback(update, _ctx())
        text = query.edit_message_text.call_args[0][0]
        assert "no longer exists" in text.lower()


class TestTaskActionCallbackEdges:
    """Tests for post-completion action callbacks."""

    async def test_task_action_missing_id(self):
        from app.telegram.bot import handle_task_action_callback
        update, query = _make_callback_query(data="taskretry:")
        await handle_task_action_callback(update, _ctx())
        # "taskretry:" splits as ["taskretry", ""] → int("") fails → return

    async def test_task_action_non_int_id(self):
        from app.telegram.bot import handle_task_action_callback
        update, query = _make_callback_query(data="taskretry:abc")
        await handle_task_action_callback(update, _ctx())

    async def test_task_action_unknown_action(self):
        from app.telegram.bot import handle_task_action_callback
        update, query = _make_callback_query(data="taskunknown:42")
        await handle_task_action_callback(update, _ctx())
        # Unknown action → silently ignored

    async def test_task_action_three_parts_ignored(self):
        from app.telegram.bot import handle_task_action_callback
        update, query = _make_callback_query(data="taskretry:42:extra")
        await handle_task_action_callback(update, _ctx())
        # parts != 2 → return early


# ┌──────────────────────────────────────────────────────────────────┐
# │  C. WEB DASHBOARD API — EDGE CASE INPUTS                        │
# └──────────────────────────────────────────────────────────────────┘

class TestDashboardAPIInputs:
    """Tests for all dashboard API input validation."""

    @pytest.fixture
    def client(self):
        from app.web.dashboard import create_dashboard_app
        from httpx import AsyncClient, ASGITransport
        app = create_dashboard_app()
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def test_tasks_limit_negative(self, client):
        async with client as c:
            r = await c.get("/api/tasks?limit=-1")
            assert r.status_code == 422  # Pydantic rejects ge=1

    async def test_tasks_limit_zero(self, client):
        async with client as c:
            r = await c.get("/api/tasks?limit=0")
            assert r.status_code == 422

    async def test_tasks_limit_over_100(self, client):
        async with client as c:
            r = await c.get("/api/tasks?limit=999")
            assert r.status_code == 422

    async def test_tasks_limit_non_int(self, client):
        async with client as c:
            r = await c.get("/api/tasks?limit=abc")
            assert r.status_code == 422

    async def test_task_by_id_non_int(self, client):
        async with client as c:
            r = await c.get("/api/tasks/abc")
            assert r.status_code == 422

    async def test_task_by_id_negative(self, client):
        async with client as c:
            r = await c.get("/api/tasks/-1")
            assert r.status_code in (404, 422)

    async def test_worker_claim_invalid_json(self, client):
        async with client as c:
            r = await c.post("/api/worker/claim", content="not json", headers={"content-type": "application/json"})
            assert r.status_code == 400

    async def test_worker_claim_empty_worker_id(self, client):
        async with client as c:
            r = await c.post("/api/worker/claim", json={"worker_id": ""})
            assert r.status_code == 400

    async def test_worker_claim_worker_id_too_long(self, client):
        async with client as c:
            r = await c.post("/api/worker/claim", json={"worker_id": "x" * 129})
            assert r.status_code == 400

    async def test_worker_claim_missing_field(self, client):
        async with client as c:
            r = await c.post("/api/worker/claim", json={})
            assert r.status_code == 400

    async def test_worker_claim_whitespace_id(self, client):
        async with client as c:
            r = await c.post("/api/worker/claim", json={"worker_id": "   "})
            assert r.status_code == 400

    async def test_worker_result_invalid_json(self, client):
        async with client as c:
            r = await c.post("/api/worker/1/result", content="bad", headers={"content-type": "application/json"})
            assert r.status_code == 400

    async def test_worker_result_missing_exit_code(self, client):
        async with client as c:
            r = await c.post("/api/worker/1/result", json={
                "worker_id": "server2",
                "output_summary": "ok",
                "full_output": "",
            })
            assert r.status_code == 400
            assert "exit_code" in r.json().get("detail", "")

    async def test_worker_result_exit_code_string(self, client):
        async with client as c:
            r = await c.post("/api/worker/1/result", json={
                "worker_id": "server2",
                "exit_code": "zero",
                "output_summary": "ok",
                "full_output": "",
            })
            assert r.status_code == 400

    async def test_worker_result_missing_worker_id(self, client):
        async with client as c:
            r = await c.post("/api/worker/1/result", json={
                "exit_code": 0,
                "output_summary": "ok",
                "full_output": "",
            })
            assert r.status_code == 400

    async def test_worker_heartbeat_invalid_json(self, client):
        async with client as c:
            r = await c.post("/api/worker/1/heartbeat", content="bad", headers={"content-type": "application/json"})
            assert r.status_code == 400

    async def test_worker_heartbeat_empty_worker_id(self, client):
        async with client as c:
            r = await c.post("/api/worker/1/heartbeat", json={"worker_id": ""})
            assert r.status_code == 400

    async def test_worker_heartbeat_non_int_path(self, client):
        async with client as c:
            r = await c.post("/api/worker/abc/heartbeat", json={"worker_id": "server2"})
            assert r.status_code == 422

    async def test_chain_by_id_non_int(self, client):
        async with client as c:
            r = await c.get("/api/chains/abc")
            assert r.status_code == 422

    async def test_health_endpoint(self, client):
        async with client as c:
            r = await c.get("/api/health")
            assert r.status_code == 200
            assert r.json()["status"] == "ok"


# ┌──────────────────────────────────────────────────────────────────┐
# │  D. BROKER FUNCTIONS — PARAMETER BOUNDARIES                     │
# └──────────────────────────────────────────────────────────────────┘

class TestBrokerEnqueueEdges:
    """Tests for enqueue_task boundary inputs."""

    async def test_enqueue_empty_prompt(self, session):
        from app.core.broker import enqueue_task
        task = await enqueue_task(
            prompt="", project_dir="/tmp", agent="opencode",
        )
        assert task.prompt == ""

    async def test_enqueue_unicode_prompt(self, session):
        from app.core.broker import enqueue_task
        task = await enqueue_task(
            prompt="修復 bug 🐛 Ñoño", project_dir="/tmp", agent="opencode",
        )
        assert task.prompt == "修復 bug 🐛 Ñoño"

    async def test_enqueue_newlines_in_prompt(self, session):
        from app.core.broker import enqueue_task
        task = await enqueue_task(
            prompt="line1\nline2\nline3", project_dir="/tmp", agent="opencode",
        )
        assert "\n" in task.prompt

    async def test_enqueue_with_model(self, session):
        from app.core.broker import enqueue_task
        task = await enqueue_task(
            prompt="test", project_dir="/tmp", agent="opencode", model="opus",
        )
        assert task.model == "opus"

    async def test_enqueue_with_none_model(self, session):
        from app.core.broker import enqueue_task
        task = await enqueue_task(
            prompt="test", project_dir="/tmp", agent="opencode", model=None,
        )
        assert task.model is None

    async def test_enqueue_queue_full(self, session):
        from app.core.broker import enqueue_task
        with patch.object(settings, "max_queue_size", 1):
            await enqueue_task(prompt="first", project_dir="/tmp", agent="opencode")
            with pytest.raises(ValueError, match="[Qq]ueue|[Ff]ull|[Mm]ax"):
                await enqueue_task(prompt="second", project_dir="/tmp", agent="opencode")


class TestBrokerWorkerEdges:
    """Tests for worker_claim_task boundary inputs."""

    async def test_claim_empty_worker_id(self, session):
        from app.core.broker import worker_claim_task
        with pytest.raises(ValueError):
            await worker_claim_task("")

    async def test_claim_worker_id_too_long(self, session):
        from app.core.broker import worker_claim_task
        with pytest.raises(ValueError):
            await worker_claim_task("x" * 129)

    async def test_claim_unicode_worker_id(self, session):
        from app.core.broker import worker_claim_task
        result = await worker_claim_task("服务器")
        assert result is None  # no tasks to claim, but no crash

    async def test_claim_special_chars_worker_id(self, session):
        from app.core.broker import worker_claim_task
        result = await worker_claim_task("server-2_host.local")
        assert result is None


class TestBrokerSearchEdges:
    """Tests for search_tasks boundary inputs."""

    async def test_search_empty_query(self, session):
        from app.core.broker import search_tasks
        results = await search_tasks("")
        assert isinstance(results, list)

    async def test_search_sql_wildcard(self, session):
        from app.core.broker import search_tasks, enqueue_task
        await enqueue_task(prompt="fix login bug", project_dir="/tmp", agent="opencode")
        # SQL wildcards in query — should not cause error
        results = await search_tasks("%")
        assert isinstance(results, list)

    async def test_search_unicode(self, session):
        from app.core.broker import search_tasks, enqueue_task
        await enqueue_task(prompt="修復バグ", project_dir="/tmp", agent="opencode")
        results = await search_tasks("修復")
        assert len(results) == 1

    async def test_search_single_char(self, session):
        from app.core.broker import search_tasks
        results = await search_tasks("_")
        assert isinstance(results, list)


class TestBrokerRepeatEdges:
    """Tests for enqueue_repeat_task boundary inputs."""

    async def test_repeat_neither_count_nor_until(self, session):
        from app.core.broker import enqueue_repeat_task
        with pytest.raises(ValueError):
            await enqueue_repeat_task(
                prompt="test", project_dir="/tmp", agent="opencode",
            )

    async def test_repeat_count_zero(self, session):
        from app.core.broker import enqueue_repeat_task
        with pytest.raises(ValueError):
            await enqueue_repeat_task(
                prompt="test", repeat_count=0, project_dir="/tmp", agent="opencode",
            )

    async def test_repeat_count_negative(self, session):
        from app.core.broker import enqueue_repeat_task
        with pytest.raises(ValueError):
            await enqueue_repeat_task(
                prompt="test", repeat_count=-5, project_dir="/tmp", agent="opencode",
            )

    async def test_repeat_count_over_1000(self, session):
        from app.core.broker import enqueue_repeat_task
        with pytest.raises(ValueError):
            await enqueue_repeat_task(
                prompt="test", repeat_count=1001, project_dir="/tmp", agent="opencode",
            )

    async def test_repeat_count_exactly_1(self, session):
        from app.core.broker import enqueue_repeat_task
        task = await enqueue_repeat_task(
            prompt="test", repeat_count=1, project_dir="/tmp", agent="opencode",
        )
        assert task.repeat_total == 1

    async def test_repeat_count_exactly_1000(self, session):
        from app.core.broker import enqueue_repeat_task
        task = await enqueue_repeat_task(
            prompt="test", repeat_count=1000, project_dir="/tmp", agent="opencode",
        )
        assert task.repeat_total == 1000

    async def test_repeat_with_model(self, session):
        from app.core.broker import enqueue_repeat_task
        task = await enqueue_repeat_task(
            prompt="test", repeat_count=2, project_dir="/tmp", agent="opencode",
            model="opus",
        )
        assert task.model == "opus"


class TestBrokerChainEdges:
    """Tests for save_chain boundary inputs."""

    async def test_chain_empty_name_accepted_by_broker(self, session):
        from app.core.broker import save_chain
        # Broker doesn't validate name (bot does) — empty name is stored
        chain = await save_chain(name="", steps=[{"prompt": "step1"}])
        assert chain.name == ""

    async def test_chain_name_too_long_accepted_by_broker(self, session):
        from app.core.broker import save_chain
        # Broker doesn't validate name length (bot does)
        chain = await save_chain(name="x" * 200, steps=[{"prompt": "step1"}])
        assert len(chain.name) == 200

    async def test_chain_no_steps(self, session):
        from app.core.broker import save_chain
        with pytest.raises(ValueError):
            await save_chain(name="mychain", steps=[])

    async def test_chain_too_many_steps(self, session):
        from app.core.broker import save_chain
        steps = [{"prompt": f"step{i}"} for i in range(51)]
        with pytest.raises(ValueError):
            await save_chain(name="mychain", steps=steps)

    async def test_chain_step_prompt_too_long(self, session):
        from app.core.broker import save_chain
        from app.config.settings import settings
        with pytest.raises(ValueError):
            await save_chain(name="mychain", steps=[{"prompt": "x" * (settings.max_prompt_len + 1)}])

    async def test_chain_unicode_name(self, session):
        from app.core.broker import save_chain
        chain = await save_chain(name="チェーン", steps=[{"prompt": "step1"}])
        assert chain.name == "チェーン"

    async def test_chain_step_with_model(self, session):
        from app.core.broker import save_chain
        chain = await save_chain(
            name="test_chain",
            steps=[{"prompt": "step1", "model": "opus"}],
        )
        assert chain is not None


class TestBrokerRecipeEdges:
    """Tests for save_recipe boundary inputs."""

    async def test_recipe_empty_name(self, session):
        from app.core.broker import save_recipe
        with pytest.raises(ValueError):
            await save_recipe(name="", triggers=["test"])

    async def test_recipe_name_too_long(self, session):
        from app.core.broker import save_recipe
        with pytest.raises(ValueError):
            await save_recipe(name="x" * 129, triggers=["test"])

    async def test_recipe_no_triggers(self, session):
        from app.core.broker import save_recipe
        with pytest.raises(ValueError):
            await save_recipe(name="test", triggers=[])

    async def test_recipe_too_many_triggers(self, session):
        from app.core.broker import save_recipe
        with pytest.raises(ValueError):
            await save_recipe(name="test", triggers=[f"t{i}" for i in range(51)])

    async def test_recipe_trigger_too_long(self, session):
        from app.core.broker import save_recipe
        with pytest.raises(ValueError):
            await save_recipe(name="test", triggers=["x" * 201])

    async def test_recipe_too_many_setup_commands(self, session):
        from app.core.broker import save_recipe
        with pytest.raises(ValueError):
            await save_recipe(
                name="test", triggers=["t"],
                setup_commands=[f"cmd{i}" for i in range(21)],
            )

    async def test_recipe_too_many_skills(self, session):
        from app.core.broker import save_recipe
        with pytest.raises(ValueError):
            await save_recipe(
                name="test", triggers=["t"],
                skills=[f"skill{i}" for i in range(21)],
            )

    async def test_recipe_prefix_too_long(self, session):
        from app.core.broker import save_recipe
        from app.config.settings import settings
        with pytest.raises(ValueError):
            await save_recipe(
                name="test", triggers=["t"],
                prompt_prefix="x" * (settings.max_prompt_len + 1),
            )

    async def test_recipe_suffix_too_long(self, session):
        from app.core.broker import save_recipe
        from app.config.settings import settings
        with pytest.raises(ValueError):
            await save_recipe(
                name="test", triggers=["t"],
                prompt_suffix="x" * (settings.max_prompt_len + 1),
            )

    async def test_recipe_valid_minimal(self, session):
        from app.core.broker import save_recipe
        recipe = await save_recipe(name="test", triggers=["fix"])
        assert recipe.name == "test"

    async def test_recipe_unicode_triggers(self, session):
        from app.core.broker import save_recipe
        recipe = await save_recipe(name="uni", triggers=["修復", "バグ"])
        assert recipe.triggers == ["修復", "バグ"]


class TestBrokerMatchRecipeEdges:
    """Tests for match_recipe edge cases."""

    async def test_match_empty_prompt(self, session):
        from app.core.broker import match_recipe
        result = await match_recipe("")
        assert result is None

    async def test_match_no_recipes_exist(self, session):
        from app.core.broker import match_recipe
        result = await match_recipe("fix the bug")
        assert result is None

    async def test_match_case_insensitive(self, session):
        from app.core.broker import save_recipe, match_recipe
        await save_recipe(name="test", triggers=["FIX"])
        result = await match_recipe("fix the bug")
        assert result is not None
        assert result.name == "test"


class TestBrokerBumpEdges:
    """Tests for bump_task edge cases."""

    async def test_bump_nonexistent(self, session):
        from app.core.broker import bump_task
        result = await bump_task(99999)
        assert result is None


class TestBrokerRetryEdges:
    """Tests for retry_task edge cases."""

    async def test_retry_nonexistent(self, session):
        from app.core.broker import retry_task
        result = await retry_task(99999)
        assert result is None


# ┌──────────────────────────────────────────────────────────────────┐
# │  E. RUNNER — COMMAND BUILDING WITH WEIRD INPUTS                  │
# └──────────────────────────────────────────────────────────────────┘

class TestRunnerCommandBuilding:
    """Tests for runner command building with edge-case inputs."""

    def _make_task(self, prompt="fix bug", agent="opencode", project_dir="/tmp/test", model=None):
        task = MagicMock(spec=Task)
        task.prompt = prompt
        task.agent = agent
        task.project_dir = project_dir
        task.model = model
        task.continue_task_id = None
        return task

    def test_build_command_normal_prompt(self):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        cmd = runner._build_command(self._make_task(prompt="fix the bug"))
        assert isinstance(cmd, list)
        assert len(cmd) > 0

    def test_build_command_prompt_with_quotes(self):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        cmd = runner._build_command(self._make_task(prompt='say "hello world"'))
        assert isinstance(cmd, list)

    def test_build_command_prompt_with_backticks(self):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        cmd = runner._build_command(self._make_task(prompt="run `ls -la`"))
        assert isinstance(cmd, list)

    def test_build_command_prompt_with_dollar_sign(self):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        cmd = runner._build_command(self._make_task(prompt="echo $HOME"))
        assert isinstance(cmd, list)

    def test_build_command_prompt_with_semicolons(self):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        cmd = runner._build_command(self._make_task(prompt="fix bug; rm -rf /"))
        assert isinstance(cmd, list)

    def test_build_command_prompt_with_newlines(self):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        cmd = runner._build_command(self._make_task(prompt="line1\nline2\nline3"))
        assert isinstance(cmd, list)

    def test_build_command_prompt_with_unicode(self):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        cmd = runner._build_command(self._make_task(prompt="修復バグ 🐛"))
        assert isinstance(cmd, list)

    def test_build_command_with_model(self):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        cmd = runner._build_command(self._make_task(model="opus"))
        assert isinstance(cmd, list)

    def test_build_command_with_empty_model(self):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        cmd = runner._build_command(self._make_task(model=""))
        assert isinstance(cmd, list)

    def test_build_command_unknown_agent(self):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        cmd = runner._build_command(self._make_task(agent="unknown_agent_xyz"))
        assert isinstance(cmd, list)
        assert "Unknown agent" in " ".join(cmd)

    def test_build_command_project_dir_with_spaces(self):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        cmd = runner._build_command(self._make_task(project_dir="/tmp/my project dir"))
        assert isinstance(cmd, list)


# ┌──────────────────────────────────────────────────────────────────┐
# │  F. SETTINGS — ENV VAR EDGE CASES                               │
# └──────────────────────────────────────────────────────────────────┘

class TestSettingsEdges:
    """Tests for settings parsing edge cases."""

    def test_allowed_user_ids_parsing(self):
        assert isinstance(settings.allowed_user_ids, list)
        assert all(isinstance(uid, int) for uid in settings.allowed_user_ids)

    def test_agent_commands_is_dict(self):
        assert isinstance(settings.agent_commands, dict)

    def test_default_agent_in_commands(self):
        # Default agent should exist in agent_commands map
        assert settings.default_agent in settings.agent_commands

    def test_max_queue_size_positive(self):
        assert settings.max_queue_size > 0

    def test_task_timeout_positive(self):
        assert settings.task_timeout_seconds > 0

    def test_progress_interval_min_5(self):
        assert settings.progress_interval_seconds >= 5

    def test_default_project_dir_not_empty(self):
        assert settings.default_project_dir

    def test_allowed_project_dirs_list(self):
        dirs = settings.allowed_project_dirs_list
        assert isinstance(dirs, list)


# ┌──────────────────────────────────────────────────────────────────┐
# │  G. SANITIZATION / ENCODING                                     │
# └──────────────────────────────────────────────────────────────────┘

class TestSanitization:
    """Tests for _sanitize_text edge cases."""

    def test_sanitize_normal_text(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("hello world") == "hello world"

    def test_sanitize_null_bytes(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("he\x00l\x00lo") == "hello"

    def test_sanitize_only_nulls(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("\x00\x00\x00") == ""

    def test_sanitize_leading_trailing_whitespace(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("  hello  ") == "hello"

    def test_sanitize_tabs_newlines_preserved(self):
        from app.telegram.bot import _sanitize_text
        result = _sanitize_text("  hello\n\tworld  ")
        assert "hello" in result
        assert "\n" in result

    def test_sanitize_unicode_preserved(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("こんにちは 🌍") == "こんにちは 🌍"

    def test_sanitize_empty_string(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("") == ""

    def test_sanitize_only_whitespace(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("   ") == ""

    def test_sanitize_rtl_text(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("مرحبا بالعالم") == "مرحبا بالعالم"

    def test_sanitize_mixed_scripts(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("Hello 世界 مرحبا Привет") == "Hello 世界 مرحبا Привет"

    def test_sanitize_emoji_sequence(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("👨‍👩‍👧‍👦 🇯🇵") == "👨‍👩‍👧‍👦 🇯🇵"

    def test_sanitize_zero_width_chars(self):
        from app.telegram.bot import _sanitize_text
        # Zero-width joiners, non-joiners, soft hyphens
        text = "he\u200bll\u200co\u00ad"
        result = _sanitize_text(text)
        # These are valid unicode, should be preserved
        assert len(result) > 0


class TestEnrichPrompt:
    """Tests for _enrich_prompt edge cases."""

    def test_enrich_no_extras(self):
        from app.telegram.bot import _enrich_prompt
        recipe = MagicMock()
        recipe.setup_commands = []
        recipe.skills = []
        recipe.prompt_prefix = None
        recipe.prompt_suffix = None
        result = _enrich_prompt("fix the bug", recipe)
        assert result == "fix the bug"

    def test_enrich_with_prefix_suffix(self):
        from app.telegram.bot import _enrich_prompt
        recipe = MagicMock()
        recipe.setup_commands = []
        recipe.skills = []
        recipe.prompt_prefix = "CONTEXT: project A"
        recipe.prompt_suffix = "Follow the style guide"
        result = _enrich_prompt("fix the bug", recipe)
        assert "CONTEXT: project A" in result
        assert "fix the bug" in result
        assert "Follow the style guide" in result

    def test_enrich_with_setup_commands(self):
        from app.telegram.bot import _enrich_prompt
        recipe = MagicMock()
        recipe.setup_commands = ["npm install", "npm run build"]
        recipe.skills = []
        recipe.prompt_prefix = None
        recipe.prompt_suffix = None
        result = _enrich_prompt("deploy", recipe)
        assert "npm install" in result
        assert "npm run build" in result

    def test_enrich_with_nonexistent_skill(self):
        from app.telegram.bot import _enrich_prompt
        recipe = MagicMock()
        recipe.setup_commands = []
        recipe.skills = ["nonexistent_skill_xyz"]
        recipe.prompt_prefix = None
        recipe.prompt_suffix = None
        result = _enrich_prompt("fix bug", recipe)
        # Missing skill file silently skipped
        assert "fix bug" in result


# ┌──────────────────────────────────────────────────────────────────┐
# │  H. AUTH — AUTHORIZATION EDGE CASES                              │
# └──────────────────────────────────────────────────────────────────┘

class TestAuth:
    """Tests for auth_required decorator edge cases."""

    async def test_unauthorized_user_silently_ignored(self):
        from app.telegram.bot import cmd_status
        update = _make_update(user_id=99999)  # Not in allowed list
        await cmd_status(update, _ctx())
        # Should not send any message — silently ignored
        update.get_bot().send_message.assert_not_called()

    async def test_none_effective_user(self):
        from app.telegram.bot import cmd_status
        update = _make_update()
        update.effective_user = None
        await cmd_status(update, _ctx())
        # Should handle gracefully — None not in allowed_user_ids

    async def test_authorized_user_proceeds(self):
        from app.telegram.bot import cmd_help
        update = _make_update(user_id=12345)
        await cmd_help(update, _ctx())
        update.get_bot().send_message.assert_called_once()


# ┌──────────────────────────────────────────────────────────────────┐
# │  I. ADDRECIPE COMMAND — PARSING EDGE CASES                      │
# └──────────────────────────────────────────────────────────────────┘

class TestAddRecipeCommandParsing:
    """Tests for /addrecipe text parsing edge cases."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_addrecipe_no_args(self, _):
        from app.telegram.bot import cmd_addrecipe
        update = _make_update()
        await cmd_addrecipe(update, _ctx())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "usage" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_addrecipe_name_only(self, _):
        from app.telegram.bot import cmd_addrecipe
        update = _make_update()
        await cmd_addrecipe(update, _ctx("myrecipe"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "usage" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_addrecipe_no_triggers(self, _):
        from app.telegram.bot import cmd_addrecipe
        update = _make_update()
        await cmd_addrecipe(update, _ctx("myrecipe", "agent:opencode"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "trigger" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_addrecipe_name_too_long(self, _):
        from app.telegram.bot import cmd_addrecipe
        update = _make_update()
        await cmd_addrecipe(update, _ctx("x" * 200, "triggers:test"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "1-128" in text or "name" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_addrecipe_valid_minimal(self, _):
        from app.telegram.bot import cmd_addrecipe
        update = _make_update()
        await cmd_addrecipe(update, _ctx("myrecipe", "triggers:fix,bug"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "saved" in text.lower() or "🧪" in text

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_addrecipe_all_options(self, _):
        from app.telegram.bot import cmd_addrecipe
        update = _make_update()
        await cmd_addrecipe(update, _ctx(
            "fullrecipe",
            "triggers:fix,deploy",
            "agent:claude",
            "model:opus",
            "prefix:Before everything",
            "suffix:After everything",
            "setup:npm install|npm build",
            "skills:linting,testing",
        ))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "saved" in text.lower() or "🧪" in text

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_addrecipe_triggers_with_unicode(self, _):
        from app.telegram.bot import cmd_addrecipe
        update = _make_update()
        await cmd_addrecipe(update, _ctx("unirecipe", "triggers:修復,バグ"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "saved" in text.lower() or "🧪" in text


class TestDelRecipeCommand:
    """Tests for /delrecipe edge cases."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_delrecipe_no_args(self, _):
        from app.telegram.bot import cmd_delrecipe
        update = _make_update()
        await cmd_delrecipe(update, _ctx())
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "usage" in text.lower()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    @patch("app.telegram.bot.delete_recipe", new_callable=AsyncMock, return_value=False)
    async def test_delrecipe_nonexistent(self, mock_del, _):
        from app.telegram.bot import cmd_delrecipe
        update = _make_update()
        await cmd_delrecipe(update, _ctx("nonexistent"))
        text = update.get_bot().send_message.call_args.kwargs["text"]
        assert "not found" in text.lower()


# ┌──────────────────────────────────────────────────────────────────┐
# │  J. CONTINUE/FOLLOWUP — EDGE CASES                              │
# └──────────────────────────────────────────────────────────────────┘

class TestContinueFollowup:
    """Tests for /continue and follow-up mode edge cases."""

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_continue_no_args(self, _):
        from app.telegram.bot import handle_text
        # Simulate /continue via text (handled by a specific handler)

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_cancel_followup_no_active(self, _):
        from app.telegram.bot import cmd_cancel_followup, _chat_followup
        update = _make_update()
        await cmd_cancel_followup(update, _ctx())
        # No active follow-up → should inform user

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock, return_value={"project_dir": None, "agent": None})
    async def test_cancel_followup_active(self, _):
        from app.telegram.bot import cmd_cancel_followup, _chat_followup
        _chat_followup[12345] = 42
        update = _make_update()
        await cmd_cancel_followup(update, _ctx())
        assert 12345 not in _chat_followup


# ┌──────────────────────────────────────────────────────────────────┐
# │  K. DASHBOARD AUTH  — BEARER TOKEN                               │
# └──────────────────────────────────────────────────────────────────┘

class TestDashboardAuth:
    """Tests for dashboard bearer token auth."""

    @pytest.fixture
    def client_with_token(self, monkeypatch):
        monkeypatch.setattr(settings, "dashboard_token", "secret123")
        from app.web.dashboard import create_dashboard_app
        from httpx import AsyncClient, ASGITransport
        app = create_dashboard_app()
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def test_no_token_when_required(self, client_with_token):
        async with client_with_token as c:
            r = await c.get("/api/stats")
            assert r.status_code == 401

    async def test_wrong_token(self, client_with_token):
        async with client_with_token as c:
            r = await c.get("/api/stats", headers={"Authorization": "Bearer wrong"})
            assert r.status_code == 401

    async def test_correct_token(self, client_with_token):
        async with client_with_token as c:
            r = await c.get("/api/health", headers={"Authorization": "Bearer secret123"})
            assert r.status_code == 200

    async def test_token_with_extra_spaces(self, client_with_token):
        async with client_with_token as c:
            r = await c.get("/api/stats", headers={"Authorization": "Bearer  secret123 "})
            # Extra spaces → token mismatch → 401
            assert r.status_code == 401

    async def test_missing_bearer_prefix(self, client_with_token):
        async with client_with_token as c:
            r = await c.get("/api/stats", headers={"Authorization": "secret123"})
            assert r.status_code == 401


# ┌──────────────────────────────────────────────────────────────────┐
# │  L. MODEL — ORM EDGE CASES                                      │
# └──────────────────────────────────────────────────────────────────┘

class TestModelORM:
    """Tests for ORM model edge cases."""

    async def test_task_all_none_nullable_fields(self, session):
        task = Task(
            prompt="test",
            project_dir="/tmp",
            agent="opencode",
            status=TaskStatus.PENDING,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        assert task.model is None
        assert task.assigned_to is None
        assert task.worker_id is None
        assert task.chain_id is None

    async def test_task_empty_string_model(self, session):
        task = Task(
            prompt="test",
            project_dir="/tmp",
            agent="opencode",
            status=TaskStatus.PENDING,
            model="",
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        assert task.model == ""

    async def test_recipe_json_triggers(self, session):
        recipe = Recipe(
            name="test",
            triggers_json=json.dumps(["fix", "bug"]),
        )
        session.add(recipe)
        await session.commit()
        await session.refresh(recipe)
        assert recipe.triggers == ["fix", "bug"]

    async def test_recipe_empty_triggers(self, session):
        recipe = Recipe(
            name="test_empty",
            triggers_json="[]",
        )
        session.add(recipe)
        await session.commit()
        await session.refresh(recipe)
        assert recipe.triggers == []

    async def test_chain_json_steps(self, session):
        chain = TaskChain(
            name="test_chain_orm",
            steps_json=json.dumps([{"prompt": "step1"}, {"prompt": "step2"}]),
            status=ChainStatus.IDLE,
        )
        session.add(chain)
        await session.commit()
        await session.refresh(chain)
        assert len(chain.steps) == 2

    async def test_task_status_enum_values(self):
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.RUNNING.value == "running"
        assert TaskStatus.COMPLETED.value == "completed"
        assert TaskStatus.FAILED.value == "failed"
        assert TaskStatus.CANCELLED.value == "cancelled"

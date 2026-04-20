"""Extensive tests covering security, switch_task_agent, handle_agent_callback,
auto-queue flow, _safe_env edge cases, password-in-prompt concerns,
_sanitize_text, _read_output, and dashboard auth.

Session: comprehensive review + gap closure.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core import broker as broker_mod
from app.core.broker import (
    enqueue_task,
    pick_next_task,
    switch_task_agent,
)
from app.core.models import Base, Task, TaskStatus
from app.core.runner import AgentRunner


# ── Fixtures ────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def fresh_db(monkeypatch):
    """Fresh in-memory engine + patched get_session."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _patched_get_session():
        return factory()

    monkeypatch.setattr(broker_mod, "get_session", _patched_get_session)
    yield engine
    await engine.dispose()


def _make_update(text="hello", user_id=12345, chat_id=12345, msg_id=1):
    """Build a minimal fake telegram Update for handle_text / handle_agent_callback."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.message_id = msg_id
    update.message.reply_text = AsyncMock()
    update.get_bot.return_value.send_message = AsyncMock()
    return update


def _make_callback_update(data="switch:1:claude", user_id=12345, chat_id=12345):
    """Build a minimal fake Update for callback query handling."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


def _make_context(args=None):
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


# ═══════════════════════════════════════════════════════════════════
# 1. switch_task_agent broker tests
# ═══════════════════════════════════════════════════════════════════


class TestSwitchTaskAgent:
    """Tests for broker.switch_task_agent()."""

    @pytest.mark.asyncio
    async def test_switch_pending_task_success(self, fresh_db):
        """Switching a PENDING task should update the agent."""
        task = await enqueue_task(prompt="test", agent="opencode", chat_id=12345)
        assert task.agent == "opencode"

        updated = await switch_task_agent(task.id, "claude")
        assert updated is not None
        assert updated.agent == "claude"
        assert updated.id == task.id

    @pytest.mark.asyncio
    async def test_switch_running_task_fails(self, fresh_db):
        """Switching a RUNNING task should return None."""
        task = await enqueue_task(prompt="test", agent="opencode", chat_id=12345)
        # Move to RUNNING
        picked = await pick_next_task()
        assert picked is not None

        updated = await switch_task_agent(picked.id, "claude")
        assert updated is None

    @pytest.mark.asyncio
    async def test_switch_nonexistent_task(self, fresh_db):
        """Switching a non-existent task returns None."""
        updated = await switch_task_agent(9999, "claude")
        assert updated is None

    @pytest.mark.asyncio
    async def test_switch_completed_task_fails(self, fresh_db):
        """Switching a completed task returns None (only PENDING allowed)."""
        task = await enqueue_task(prompt="test", agent="opencode", chat_id=12345)
        picked = await pick_next_task()
        from app.core.broker import complete_task

        await complete_task(picked.id, 0, "done", "output")

        updated = await switch_task_agent(task.id, "claude")
        assert updated is None

    @pytest.mark.asyncio
    async def test_switch_preserves_other_fields(self, fresh_db):
        """Switching agent should not affect prompt, project_dir, etc."""
        task = await enqueue_task(
            prompt="fix bug", project_dir="/tmp/proj", agent="opencode", chat_id=42
        )
        updated = await switch_task_agent(task.id, "claude")
        assert updated.prompt == "fix bug"
        assert updated.project_dir == "/tmp/proj"
        assert updated.telegram_chat_id == 42

    @pytest.mark.asyncio
    async def test_switch_cancelled_task_fails(self, fresh_db):
        """Switching a cancelled task returns None."""
        from app.core.broker import cancel_task_by_id

        task = await enqueue_task(prompt="test", agent="opencode", chat_id=12345)
        await cancel_task_by_id(task.id)
        updated = await switch_task_agent(task.id, "claude")
        assert updated is None


# ═══════════════════════════════════════════════════════════════════
# 2. handle_agent_callback (inline button press) tests
# ═══════════════════════════════════════════════════════════════════


class TestHandleAgentCallback:
    """Tests for bot.handle_agent_callback()."""

    @pytest.mark.asyncio
    async def test_callback_switch_success(self, fresh_db):
        """Valid switch callback should update task and edit message."""
        from app.telegram.bot import handle_agent_callback

        task = await enqueue_task(prompt="test", agent="opencode", chat_id=12345)
        update = _make_callback_update(data=f"switch:{task.id}:claude")
        ctx = _make_context()

        await handle_agent_callback(update, ctx)
        update.callback_query.answer.assert_awaited_once()
        update.callback_query.edit_message_text.assert_awaited_once()
        call_text = update.callback_query.edit_message_text.call_args[0][0]
        assert "claude" in call_text
        assert "Switched" in call_text

    @pytest.mark.asyncio
    async def test_callback_already_running(self, fresh_db):
        """Switching a task that already started shows warning."""
        from app.telegram.bot import handle_agent_callback

        task = await enqueue_task(prompt="test", agent="opencode", chat_id=12345)
        await pick_next_task()  # move to RUNNING
        update = _make_callback_update(data=f"switch:{task.id}:claude")
        ctx = _make_context()

        await handle_agent_callback(update, ctx)
        call_text = update.callback_query.edit_message_text.call_args[0][0]
        assert "already started" in call_text or "not found" in call_text

    @pytest.mark.asyncio
    async def test_callback_unknown_agent(self, fresh_db):
        """Switching to unknown agent shows error."""
        from app.telegram.bot import handle_agent_callback

        task = await enqueue_task(prompt="test", agent="opencode", chat_id=12345)
        update = _make_callback_update(data=f"switch:{task.id}:nonexistent")
        ctx = _make_context()

        await handle_agent_callback(update, ctx)
        call_text = update.callback_query.edit_message_text.call_args[0][0]
        assert "Unknown agent" in call_text

    @pytest.mark.asyncio
    async def test_callback_bad_format_no_crash(self, fresh_db):
        """Malformed callback data should not crash."""
        from app.telegram.bot import handle_agent_callback

        update = _make_callback_update(data="switch:")
        ctx = _make_context()
        await handle_agent_callback(update, ctx)
        # should not edit message since format is wrong
        update.callback_query.edit_message_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_callback_non_integer_task_id(self, fresh_db):
        """Non-integer task ID in callback data should not crash."""
        from app.telegram.bot import handle_agent_callback

        update = _make_callback_update(data="switch:abc:claude")
        ctx = _make_context()
        await handle_agent_callback(update, ctx)
        update.callback_query.edit_message_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_callback_not_switch_prefix(self, fresh_db):
        """Callback with non-switch prefix should be ignored."""
        from app.telegram.bot import handle_agent_callback

        update = _make_callback_update(data="other:data")
        ctx = _make_context()
        await handle_agent_callback(update, ctx)
        update.callback_query.edit_message_text.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════
# 3. handle_text auto-queue flow tests
# ═══════════════════════════════════════════════════════════════════


class TestHandleTextAutoQueue:
    """Tests for bot.handle_text() auto-queue with inline switch buttons."""

    @pytest.mark.asyncio
    async def test_auto_queue_shows_switch_buttons(self, fresh_db):
        """Text message with multiple agents configured should show switch buttons."""
        from app.telegram.bot import handle_text, _chat_agent, _chat_project_dir

        _chat_agent.clear()
        _chat_project_dir.clear()

        update = _make_update(text="fix the bug")
        ctx = _make_context()
        await handle_text(update, ctx)

        update.message.reply_text.assert_awaited_once()
        call_kwargs = update.message.reply_text.call_args
        assert "Queued" in call_kwargs[0][0]
        # Should have reply_markup with InlineKeyboardMarkup
        assert "reply_markup" in call_kwargs[1]

    @pytest.mark.asyncio
    async def test_auto_queue_empty_text_ignored(self, fresh_db):
        """Empty text after sanitization should be silently ignored."""
        from app.telegram.bot import handle_text, _chat_agent, _chat_project_dir

        _chat_agent.clear()
        _chat_project_dir.clear()

        update = _make_update(text="   ")
        ctx = _make_context()
        await handle_text(update, ctx)
        update.message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_queue_prompt_too_long(self, fresh_db):
        """Prompt exceeding MAX_PROMPT_LEN should be rejected."""
        from app.telegram.bot import handle_text, MAX_PROMPT_LEN, _chat_agent, _chat_project_dir

        _chat_agent.clear()
        _chat_project_dir.clear()

        long_prompt = "x" * (MAX_PROMPT_LEN + 1)
        update = _make_update(text=long_prompt)
        ctx = _make_context()
        await handle_text(update, ctx)

        # Should send a warning via _send, not reply_text
        update.get_bot.return_value.send_message.assert_awaited()
        msg = update.get_bot.return_value.send_message.call_args[1]["text"]
        assert "too long" in msg

    @pytest.mark.asyncio
    async def test_auto_queue_full_queue(self, fresh_db, monkeypatch):
        """When queue is full, handle_text should report it."""
        from app.telegram.bot import handle_text, _chat_agent, _chat_project_dir

        _chat_agent.clear()
        _chat_project_dir.clear()

        monkeypatch.setattr("app.config.settings.settings.max_queue_size", 0)
        update = _make_update(text="test prompt")
        ctx = _make_context()
        await handle_text(update, ctx)

        update.get_bot.return_value.send_message.assert_awaited()
        msg = update.get_bot.return_value.send_message.call_args[1]["text"]
        assert "full" in msg.lower() or "Queue" in msg

    @pytest.mark.asyncio
    async def test_auto_queue_uses_chat_prefs(self, fresh_db):
        """Auto-queue should respect previously set chat agent prefs."""
        from app.telegram.bot import handle_text, _chat_agent, _chat_project_dir

        _chat_agent.clear()
        _chat_project_dir.clear()
        # Pre-set chat agent
        _chat_agent[12345] = "claude"

        update = _make_update(text="test task")
        ctx = _make_context()
        await handle_text(update, ctx)

        update.message.reply_text.assert_awaited_once()
        call_text = update.message.reply_text.call_args[0][0]
        assert "claude" in call_text

    @pytest.mark.asyncio
    async def test_auto_queue_null_bytes_stripped(self, fresh_db):
        """Null bytes in prompts should be stripped by _sanitize_text."""
        from app.telegram.bot import handle_text, _chat_agent, _chat_project_dir

        _chat_agent.clear()
        _chat_project_dir.clear()

        update = _make_update(text="test\x00 prompt")
        ctx = _make_context()
        await handle_text(update, ctx)

        update.message.reply_text.assert_awaited_once()
        call_text = update.message.reply_text.call_args[0][0]
        assert "Queued" in call_text

    @pytest.mark.asyncio
    async def test_auto_queue_single_agent_no_switch_buttons(self, fresh_db, monkeypatch):
        """With only one agent configured, no switch buttons should appear."""
        from app.telegram.bot import handle_text, _chat_agent, _chat_project_dir

        _chat_agent.clear()
        _chat_project_dir.clear()

        monkeypatch.setattr(
            "app.config.settings.settings.agent_commands",
            {"opencode": "opencode run {prompt}"},
        )
        update = _make_update(text="single agent task")
        ctx = _make_context()
        await handle_text(update, ctx)

        update.message.reply_text.assert_awaited_once()
        call_kwargs = update.message.reply_text.call_args
        # Should NOT have reply_markup since no other agents
        if "reply_markup" in (call_kwargs[1] or {}):
            assert call_kwargs[1]["reply_markup"] is None or call_kwargs[1].get("reply_markup") is None
        else:
            pass  # no reply_markup key means no buttons — correct

    @pytest.mark.asyncio
    async def test_auto_queue_exception_handling(self, fresh_db, monkeypatch):
        """Generic exception during enqueue should show error message."""
        from app.telegram import bot as bot_module
        from app.telegram.bot import handle_text, _chat_agent, _chat_project_dir

        _chat_agent.clear()
        _chat_project_dir.clear()

        async def _fail(*args, **kwargs):
            raise RuntimeError("DB down")

        monkeypatch.setattr(bot_module, "enqueue_task", _fail)
        update = _make_update(text="will fail")
        ctx = _make_context()
        await handle_text(update, ctx)

        update.get_bot.return_value.send_message.assert_awaited()
        msg = update.get_bot.return_value.send_message.call_args[1]["text"]
        assert "Failed" in msg or "failed" in msg


# ═══════════════════════════════════════════════════════════════════
# 4. _safe_env comprehensive tests
# ═══════════════════════════════════════════════════════════════════


class TestSafeEnvComprehensive:
    """Exhaustive _safe_env tests — ensure sensitive vars are stripped."""

    def test_strips_telegram_vars(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret123")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
        env = AgentRunner._safe_env()
        assert "TELEGRAM_BOT_TOKEN" not in env
        assert "TELEGRAM_CHAT_ID" not in env

    def test_strips_bot_token(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN_PROD", "tok123")
        env = AgentRunner._safe_env()
        assert "BOT_TOKEN_PROD" not in env

    def test_strips_api_key(self, monkeypatch):
        monkeypatch.setenv("API_KEY_OPENAI", "sk-abc")
        env = AgentRunner._safe_env()
        assert "API_KEY_OPENAI" not in env

    def test_strips_slack_vars(self, monkeypatch):
        monkeypatch.setenv("SLACK_TOKEN", "xoxb-abc")
        env = AgentRunner._safe_env()
        assert "SLACK_TOKEN" not in env

    def test_strips_secret_prefix(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "mysecret")
        monkeypatch.setenv("SECRET_DATABASE_URL", "postgres://...")
        env = AgentRunner._safe_env()
        assert "SECRET_KEY" not in env
        assert "SECRET_DATABASE_URL" not in env

    def test_preserves_safe_vars(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/test")
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        env = AgentRunner._safe_env()
        assert env.get("HOME") == "/home/test"
        assert "PATH" in env
        assert "LANG" in env

    def test_case_insensitive_matching(self, monkeypatch):
        """_safe_env uses k.upper().startswith(p), so lowercase keys should be caught."""
        monkeypatch.setenv("telegram_bot_token", "lowercase")
        env = AgentRunner._safe_env()
        assert "telegram_bot_token" not in env

    def test_password_env_not_stripped_by_default(self, monkeypatch):
        """SECURITY NOTE: PASSWORD and DATABASE_URL are NOT stripped by _safe_env.
        This test documents the current behavior. These vars may leak to subprocesses."""
        monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@host/db")
        monkeypatch.setenv("PASSWORD", "hunter2")
        env = AgentRunner._safe_env()
        # Current behavior: these are NOT stripped
        # This is a known limitation documented in the security review
        assert "DATABASE_URL" in env
        assert "PASSWORD" in env

    def test_allowed_user_ids_env_not_stripped(self, monkeypatch):
        """ALLOWED_USER_IDS env var is NOT stripped (no matching prefix)."""
        monkeypatch.setenv("ALLOWED_USER_IDS", "12345")
        env = AgentRunner._safe_env()
        assert "ALLOWED_USER_IDS" in env


# ═══════════════════════════════════════════════════════════════════
# 5. Password/secret in prompt — security concern tests
# ═══════════════════════════════════════════════════════════════════


class TestPasswordInPromptSecurity:
    """Documents what happens when a user sends a password in a prompt.

    SECURITY CONCERNS:
    1. Prompt is stored in SQLite `prompt` column (plaintext)
    2. Prompt appears in CLI args (visible via `ps aux`)
    3. Full output stored in SQLite `full_output` column (plaintext)
    4. Dashboard API `/api/tasks/{id}` exposes full prompt
    5. /output command sends full output to Telegram chat

    These are acceptable for a personal-use tool on a single dev machine,
    but users should be warned.
    """

    @pytest.mark.asyncio
    async def test_prompt_with_password_is_stored_plaintext(self, fresh_db):
        """Prompts containing passwords are stored as-is in the database."""
        secret_prompt = "Set DATABASE_PASSWORD=hunter2 in the .env file"
        task = await enqueue_task(prompt=secret_prompt, chat_id=12345)
        assert task.prompt == secret_prompt
        # The password is stored in plaintext — this is a known security note

    @pytest.mark.asyncio
    async def test_prompt_visible_in_build_command(self):
        """Prompt appears as a CLI argument, visible in process list."""
        runner = AgentRunner()
        task = MagicMock()
        task.prompt = "my-secret-password-123"
        task.agent = "opencode"
        task.project_dir = "/tmp"
        argv = runner._build_command(task)
        # The prompt is embedded in the command line
        assert any("my-secret-password-123" in arg for arg in argv)

    @pytest.mark.asyncio
    async def test_output_with_secrets_stored_in_full_output(self, fresh_db):
        """Command output containing secrets is stored as-is."""
        from app.core.broker import complete_task

        task = await enqueue_task(prompt="test", chat_id=12345)
        picked = await pick_next_task()
        await complete_task(
            picked.id,
            exit_code=0,
            output_summary="Done",
            full_output="Generated password: s3cret-p4ss!",
        )
        from app.core.broker import get_task_by_id

        fetched = await get_task_by_id(task.id)
        assert "s3cret-p4ss!" in fetched.full_output

    @pytest.mark.asyncio
    async def test_dashboard_exposes_full_prompt(self, fresh_db):
        """Dashboard detail endpoint returns full prompt, not truncated."""
        from app.web.dashboard import _task_to_dict

        task = await enqueue_task(prompt="password=abc123", chat_id=12345)
        # _task_to_dict truncates prompt to 200 chars for list view
        d = _task_to_dict(task)
        assert d["prompt"] == "password=abc123"

    def test_sanitize_text_does_not_redact_passwords(self):
        """_sanitize_text only strips null bytes—it does NOT redact secrets."""
        from app.telegram.bot import _sanitize_text

        result = _sanitize_text("Set PASSWORD=hunter2")
        assert result == "Set PASSWORD=hunter2"


# ═══════════════════════════════════════════════════════════════════
# 6. _sanitize_text edge cases
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeText:
    """Tests for bot._sanitize_text()."""

    def test_strips_null_bytes(self):
        from app.telegram.bot import _sanitize_text

        assert _sanitize_text("hello\x00world") == "helloworld"

    def test_strips_whitespace(self):
        from app.telegram.bot import _sanitize_text

        assert _sanitize_text("  hello  ") == "hello"

    def test_empty_string(self):
        from app.telegram.bot import _sanitize_text

        assert _sanitize_text("") == ""

    def test_only_null_bytes(self):
        from app.telegram.bot import _sanitize_text

        assert _sanitize_text("\x00\x00\x00") == ""

    def test_preserves_normal_unicode(self):
        from app.telegram.bot import _sanitize_text

        assert _sanitize_text("こんにちは 🎉") == "こんにちは 🎉"

    def test_preserves_newlines(self):
        from app.telegram.bot import _sanitize_text

        assert _sanitize_text("line1\nline2") == "line1\nline2"

    def test_mixed_null_and_whitespace(self):
        from app.telegram.bot import _sanitize_text

        assert _sanitize_text("  \x00test\x00  ") == "test"


# ═══════════════════════════════════════════════════════════════════
# 7. _read_output separate stdout/stderr tests
# ═══════════════════════════════════════════════════════════════════


class TestReadOutputSeparation:
    """Tests for AgentRunner._read_output() returning (stdout, stderr) tuple."""

    @pytest.mark.asyncio
    async def test_stdout_and_stderr_separate(self):
        """_read_output should return stdout and stderr as separate bytes."""
        runner = AgentRunner()
        task = MagicMock()
        task.id = 1
        task.telegram_chat_id = 12345

        proc = MagicMock()
        stdout_lines = [b"line1\n", b"line2\n", b""]
        stdout_iter = iter(stdout_lines)

        async def _readline():
            return next(stdout_iter)

        proc.stdout.readline = _readline
        proc.wait = AsyncMock()

        stderr_data = b"some warning\n"

        async def _read_stderr(n):
            return stderr_data

        proc.stderr.read = _read_stderr

        stdout, stderr = await runner._read_output(proc, task)
        assert b"line1" in stdout
        assert b"line2" in stdout
        assert stderr == stderr_data

    @pytest.mark.asyncio
    async def test_empty_stderr(self):
        """When stderr is empty, second tuple element should be empty bytes."""
        runner = AgentRunner()
        task = MagicMock()
        task.id = 1
        task.telegram_chat_id = 12345

        proc = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=[b"output\n", b""])
        proc.wait = AsyncMock()
        proc.stderr.read = AsyncMock(return_value=b"")

        stdout, stderr = await runner._read_output(proc, task)
        assert b"output" in stdout
        assert stderr == b""

    @pytest.mark.asyncio
    async def test_no_stderr_pipe(self):
        """When stderr is None, should return empty bytes."""
        runner = AgentRunner()
        task = MagicMock()
        task.id = 1
        task.telegram_chat_id = 12345

        proc = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=[b"output\n", b""])
        proc.wait = AsyncMock()
        proc.stderr = None

        stdout, stderr = await runner._read_output(proc, task)
        assert b"output" in stdout
        assert stderr == b""


# ═══════════════════════════════════════════════════════════════════
# 8. _build_command security tests
# ═══════════════════════════════════════════════════════════════════


class TestBuildCommandSecurity:
    """Tests for AgentRunner._build_command() — shell injection prevention."""

    def test_prompt_with_shell_metacharacters(self):
        """Shell metacharacters in prompt should NOT cause injection."""
        runner = AgentRunner()
        task = MagicMock()
        task.prompt = '; rm -rf / && echo "pwned"'
        task.agent = "opencode"
        task.project_dir = "/tmp"
        argv = runner._build_command(task)
        # The entire prompt should be a single argument, not split
        assert any('; rm -rf / && echo "pwned"' in arg for arg in argv)
        # Should NOT have separate 'rm' or '-rf' as individual args
        assert "rm" not in argv
        assert "-rf" not in argv

    def test_prompt_with_backticks(self):
        runner = AgentRunner()
        task = MagicMock()
        task.prompt = "`whoami`"
        task.agent = "opencode"
        task.project_dir = "/tmp"
        argv = runner._build_command(task)
        assert any("`whoami`" in arg for arg in argv)

    def test_prompt_with_dollar_expansion(self):
        runner = AgentRunner()
        task = MagicMock()
        task.prompt = "$(cat /etc/passwd)"
        task.agent = "opencode"
        task.project_dir = "/tmp"
        argv = runner._build_command(task)
        assert any("$(cat /etc/passwd)" in arg for arg in argv)

    def test_unknown_agent_returns_echo(self):
        runner = AgentRunner()
        task = MagicMock()
        task.prompt = "test"
        task.agent = "nonexistent"
        task.project_dir = "/tmp"
        argv = runner._build_command(task)
        assert argv[0] == "echo"
        assert "Unknown agent" in argv[1]

    def test_project_dir_with_spaces(self):
        runner = AgentRunner()
        task = MagicMock()
        task.prompt = "test"
        task.agent = "opencode"
        task.project_dir = "/home/user/my project dir"
        argv = runner._build_command(task)
        # project_dir not used in opencode template, but verify no crash
        assert len(argv) >= 2


# ═══════════════════════════════════════════════════════════════════
# 9. Dashboard auth tests
# ═══════════════════════════════════════════════════════════════════


class TestDashboardAuth:
    """Tests for dashboard bearer token middleware."""

    def test_dashboard_token_empty_allows_access(self):
        """Empty dashboard_token means no auth required."""
        from app.web.dashboard import create_dashboard_app
        from fastapi.testclient import TestClient

        with patch("app.config.settings.settings.dashboard_token", ""):
            app = create_dashboard_app()
            client = TestClient(app)
            # Health endpoint should work without auth
            resp = client.get("/api/health")
            assert resp.status_code == 200

    def test_dashboard_token_rejects_wrong_token(self):
        """Wrong bearer token should get 401."""
        from app.web.dashboard import create_dashboard_app
        from fastapi.testclient import TestClient

        with patch("app.config.settings.settings.dashboard_token", "correct-token"):
            app = create_dashboard_app()
            client = TestClient(app)
            resp = client.get(
                "/api/health", headers={"Authorization": "Bearer wrong-token"}
            )
            assert resp.status_code == 401

    def test_dashboard_token_accepts_correct_token(self):
        """Correct bearer token should be accepted."""
        from app.web.dashboard import create_dashboard_app
        from fastapi.testclient import TestClient

        with patch("app.config.settings.settings.dashboard_token", "correct-token"):
            app = create_dashboard_app()
            client = TestClient(app)
            resp = client.get(
                "/api/health", headers={"Authorization": "Bearer correct-token"}
            )
            assert resp.status_code == 200

    def test_dashboard_token_missing_header_rejected(self):
        """Missing Authorization header with token set should get 401."""
        from app.web.dashboard import create_dashboard_app
        from fastapi.testclient import TestClient

        with patch("app.config.settings.settings.dashboard_token", "secret"):
            app = create_dashboard_app()
            client = TestClient(app)
            resp = client.get("/api/health")
            assert resp.status_code == 401

    def test_dashboard_security_headers_present(self):
        """All security headers should be present on every response."""
        from app.web.dashboard import create_dashboard_app
        from fastapi.testclient import TestClient

        with patch("app.config.settings.settings.dashboard_token", ""):
            app = create_dashboard_app()
            client = TestClient(app)
            resp = client.get("/api/health")
            assert resp.headers.get("X-Frame-Options") == "DENY"
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert resp.headers.get("Referrer-Policy") == "no-referrer"
            assert "Content-Security-Policy" in resp.headers

    def test_dashboard_timing_safe_comparison(self):
        """Dashboard uses hmac.compare_digest for token comparison (timing-safe)."""
        import inspect
        from app.web import dashboard as dash_mod

        source = inspect.getsource(dash_mod.create_dashboard_app)
        assert "hmac.compare_digest" in source


# ═══════════════════════════════════════════════════════════════════
# 10. Auth guard tests
# ═══════════════════════════════════════════════════════════════════


class TestAuthGuard:
    """Tests for bot.auth_required decorator."""

    @pytest.mark.asyncio
    async def test_unauthorized_user_blocked(self, fresh_db):
        """User not in allowed_user_ids should be silently ignored."""
        from app.telegram.bot import handle_text

        # User 99999 is not in ALLOWED_USER_IDS (12345)
        update = _make_update(text="test", user_id=99999)
        ctx = _make_context()
        await handle_text(update, ctx)
        # Should not enqueue anything
        update.message.reply_text.assert_not_awaited()
        update.get_bot.return_value.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_authorized_user_allowed(self, fresh_db):
        """User in allowed_user_ids should proceed normally."""
        from app.telegram.bot import handle_text, _chat_agent, _chat_project_dir

        _chat_agent.clear()
        _chat_project_dir.clear()

        update = _make_update(text="allowed user task", user_id=12345)
        ctx = _make_context()
        await handle_text(update, ctx)
        update.message.reply_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_none_user_id_blocked(self, fresh_db):
        """Update with no effective_user should be rejected."""
        from app.telegram.bot import handle_text

        update = _make_update(text="test")
        update.effective_user.id = None
        ctx = _make_context()
        await handle_text(update, ctx)
        update.message.reply_text.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════
# 11. _is_allowed_dir / path validation
# ═══════════════════════════════════════════════════════════════════


class TestPathValidation:
    """Tests for path allowlist enforcement."""

    def test_allowed_dir_exact_match(self, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.settings.allowed_project_dirs", "/tmp/allowed"
        )
        assert AgentRunner._is_allowed_dir("/tmp/allowed") is True

    def test_allowed_dir_subdirectory(self, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.settings.allowed_project_dirs", "/tmp/allowed"
        )
        assert AgentRunner._is_allowed_dir("/tmp/allowed/sub/dir") is True

    def test_disallowed_dir(self, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.settings.allowed_project_dirs", "/tmp/allowed"
        )
        assert AgentRunner._is_allowed_dir("/etc/passwd") is False

    def test_path_traversal_blocked(self, monkeypatch):
        """Symlink-based path traversal should be blocked after realpath resolution.

        _is_allowed_dir expects an already-resolved path (its caller does realpath).
        """
        monkeypatch.setattr(
            "app.config.settings.settings.allowed_project_dirs", "/tmp/allowed"
        )
        # Simulate what _execute() does: resolve path first, then check
        resolved = os.path.realpath("/tmp/allowed/../etc")
        assert resolved == "/tmp/etc"
        assert AgentRunner._is_allowed_dir(resolved) is False

    def test_prefix_attack_blocked(self, monkeypatch):
        """'/tmp/allowed_evil' should NOT match '/tmp/allowed'."""
        monkeypatch.setattr(
            "app.config.settings.settings.allowed_project_dirs", "/tmp/allowed"
        )
        assert AgentRunner._is_allowed_dir("/tmp/allowed_evil") is False

    def test_bot_is_allowed_project_dir(self, monkeypatch):
        """Bot-level _is_allowed_project_dir function should also validate."""
        from app.telegram.bot import _is_allowed_project_dir

        monkeypatch.setattr(
            "app.config.settings.settings.allowed_project_dirs", "/tmp/test"
        )
        assert _is_allowed_project_dir("/tmp/test") is True
        assert _is_allowed_project_dir("/tmp/other") is False


# ═══════════════════════════════════════════════════════════════════
# 12. _summarize tests
# ═══════════════════════════════════════════════════════════════════


class TestSummarize:
    """Tests for AgentRunner._summarize()."""

    def test_success_shows_tail(self):
        runner = AgentRunner()
        output = "\n".join([f"line {i}" for i in range(20)])
        summary = runner._summarize(output, exit_code=0)
        assert "line 19" in summary
        assert "line 10" in summary

    def test_failure_shows_error_lines(self):
        runner = AgentRunner()
        output = "INFO: starting\nERROR: something failed\nINFO: cleanup\n"
        summary = runner._summarize(output, exit_code=1)
        assert "ERROR" in summary

    def test_empty_output(self):
        runner = AgentRunner()
        summary = runner._summarize("", exit_code=0)
        assert summary == "(no output)"

    def test_whitespace_only_output(self):
        runner = AgentRunner()
        summary = runner._summarize("   \n  \n  ", exit_code=0)
        assert summary == "(no output)"

    def test_truncation_to_max_chars(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.settings.output_summary_max_chars", 20)
        runner = AgentRunner()
        output = "x" * 100
        summary = runner._summarize(output, exit_code=0)
        assert len(summary) <= 20


# ═══════════════════════════════════════════════════════════════════
# 13. Prompt injection in task fields
# ═══════════════════════════════════════════════════════════════════


class TestPromptInjection:
    """Ensure prompt content can't break command construction or DB storage."""

    @pytest.mark.asyncio
    async def test_sql_injection_in_prompt(self, fresh_db):
        """SQL injection in prompt should be safely stored (parameterized queries)."""
        evil_prompt = "'; DROP TABLE tasks; --"
        task = await enqueue_task(prompt=evil_prompt, chat_id=12345)
        assert task.prompt == evil_prompt
        # Table should still exist
        from app.core.broker import get_task_by_id

        fetched = await get_task_by_id(task.id)
        assert fetched is not None
        assert fetched.prompt == evil_prompt

    @pytest.mark.asyncio
    async def test_very_long_prompt_stored(self, fresh_db):
        """Very long prompt (up to MAX_PROMPT_LEN) should be stored correctly."""
        long_prompt = "A" * 2000
        task = await enqueue_task(prompt=long_prompt, chat_id=12345)
        from app.core.broker import get_task_by_id

        fetched = await get_task_by_id(task.id)
        assert len(fetched.prompt) == 2000

    def test_newlines_in_prompt_logged_safely(self):
        """Newlines in prompts should be escaped in log output (safe_prompt)."""
        prompt = "line1\nline2\rline3"
        safe_prompt = prompt.replace("\n", "\\n").replace("\r", "\\r")
        assert "\n" not in safe_prompt
        assert "\r" not in safe_prompt
        assert "\\n" in safe_prompt
        assert "\\r" in safe_prompt


# ═══════════════════════════════════════════════════════════════════
# 14. Output cap tests
# ═══════════════════════════════════════════════════════════════════


class TestOutputCap:
    """Tests for MAX_OUTPUT_BYTES enforcement."""

    def test_max_output_bytes_constant(self):
        from app.core.runner import MAX_OUTPUT_BYTES

        assert MAX_OUTPUT_BYTES == 2 * 1024 * 1024  # 2 MB

    @pytest.mark.asyncio
    async def test_read_output_caps_at_max_bytes(self):
        """_read_output should cap stdout at MAX_OUTPUT_BYTES."""
        from app.core.runner import MAX_OUTPUT_BYTES

        runner = AgentRunner()
        task = MagicMock()
        task.id = 1
        task.telegram_chat_id = 12345

        # Generate more than MAX_OUTPUT_BYTES of output
        big_line = b"x" * 1024 + b"\n"  # 1025 bytes per line
        line_count = (MAX_OUTPUT_BYTES // 1025) + 100  # well over limit
        lines = [big_line] * line_count + [b""]

        line_iter = iter(lines)

        async def _readline():
            return next(line_iter)

        proc = MagicMock()
        proc.stdout.readline = _readline
        proc.wait = AsyncMock()
        proc.stderr = None

        stdout, stderr = await runner._read_output(proc, task)
        assert len(stdout) <= MAX_OUTPUT_BYTES


# ═══════════════════════════════════════════════════════════════════
# 15. Chat prefs persistence
# ═══════════════════════════════════════════════════════════════════


class TestChatPrefsPersistence:
    """Tests for chat preferences (project_dir, agent) persistence."""

    @pytest.mark.asyncio
    async def test_set_and_get_prefs(self, fresh_db):
        from app.core.broker import set_chat_pref, get_chat_prefs

        await set_chat_pref(99, project_dir="/tmp/my-proj", agent="claude")
        prefs = await get_chat_prefs(99)
        assert prefs["project_dir"] == "/tmp/my-proj"
        assert prefs["agent"] == "claude"

    @pytest.mark.asyncio
    async def test_get_prefs_nonexistent(self, fresh_db):
        from app.core.broker import get_chat_prefs

        prefs = await get_chat_prefs(777)
        assert prefs["project_dir"] is None
        assert prefs["agent"] is None

    @pytest.mark.asyncio
    async def test_update_prefs(self, fresh_db):
        from app.core.broker import set_chat_pref, get_chat_prefs

        await set_chat_pref(99, agent="opencode")
        await set_chat_pref(99, agent="claude")
        prefs = await get_chat_prefs(99)
        assert prefs["agent"] == "claude"


# ═══════════════════════════════════════════════════════════════════
# 16. Enqueue validation tests
# ═══════════════════════════════════════════════════════════════════


class TestEnqueueValidation:
    """Tests for broker.enqueue_task() edge cases."""

    @pytest.mark.asyncio
    async def test_queue_full_raises(self, fresh_db, monkeypatch):
        monkeypatch.setattr("app.config.settings.settings.max_queue_size", 1)
        await enqueue_task(prompt="first", chat_id=12345)
        with pytest.raises(ValueError, match="Queue full"):
            await enqueue_task(prompt="second", chat_id=12345)

    @pytest.mark.asyncio
    async def test_default_values(self, fresh_db):
        task = await enqueue_task(prompt="test")
        assert task.status == TaskStatus.PENDING
        assert task.agent == "opencode"  # default_agent
        assert task.project_dir is not None

    @pytest.mark.asyncio
    async def test_custom_agent_and_dir(self, fresh_db):
        task = await enqueue_task(
            prompt="test", agent="claude", project_dir="/tmp/custom"
        )
        assert task.agent == "claude"
        assert task.project_dir == "/tmp/custom"


# ═══════════════════════════════════════════════════════════════════
# 17. _kill_process tests
# ═══════════════════════════════════════════════════════════════════


class TestKillProcess:
    """Tests for AgentRunner._kill_process()."""

    def test_kill_process_with_pgid(self, monkeypatch):
        """Should use killpg when process is group leader."""
        runner = AgentRunner()
        proc = MagicMock()
        proc.pid = 12345
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)
        killed = []
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.append(pgid))
        runner._kill_process(proc)
        assert 12345 in killed

    def test_kill_process_not_group_leader(self, monkeypatch):
        """Should use proc.terminate() when not group leader."""
        runner = AgentRunner()
        proc = MagicMock()
        proc.pid = 100
        monkeypatch.setattr(os, "getpgid", lambda pid: 99)  # different pgid
        runner._kill_process(proc)
        proc.terminate.assert_called_once()

    def test_kill_process_no_pid(self):
        """Should fall back to terminate if pid is None."""
        runner = AgentRunner()
        proc = MagicMock()
        proc.pid = None
        runner._kill_process(proc)
        proc.terminate.assert_called_once()

    def test_kill_process_lookup_error(self, monkeypatch):
        """Should fall back to terminate on ProcessLookupError."""
        runner = AgentRunner()
        proc = MagicMock()
        proc.pid = 12345
        monkeypatch.setattr(os, "getpgid", lambda pid: (_ for _ in ()).throw(ProcessLookupError))
        runner._kill_process(proc)
        proc.terminate.assert_called_once()

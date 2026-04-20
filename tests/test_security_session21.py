"""Security-focused tests covering fixes from QA sessions 11-20."""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.core.models import Task, TaskStatus, ChainStatus


def _make_task(**overrides) -> Task:
    defaults = {
        "id": 1,
        "prompt": "test prompt",
        "project_dir": "/home/user/project",
        "agent": "opencode",
        "status": TaskStatus.RUNNING,
        "model": None,
        "priority": 0,
        "retry_count": 0,
        "max_retries": 1,
        "git_diff": None,
    }
    defaults.update(overrides)
    return Task(**defaults)


# ── SEC-002: Sensitive env vars stripped from subprocess ────────────


class TestSafeEnv:
    """Verify _safe_env strips credentials from subprocess environment."""

    def test_strips_telegram_prefixed_vars(self, monkeypatch):
        from app.core.runner import AgentRunner

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret123")
        monkeypatch.setenv("TELEGRAM_API_KEY", "key456")
        monkeypatch.setenv("HOME", "/home/user")

        env = AgentRunner._safe_env()
        assert "TELEGRAM_BOT_TOKEN" not in env
        assert "TELEGRAM_API_KEY" not in env
        assert env.get("HOME") == "/home/user"

    def test_strips_bot_token_prefix(self, monkeypatch):
        from app.core.runner import AgentRunner

        monkeypatch.setenv("BOT_TOKEN_MAIN", "tok")

        env = AgentRunner._safe_env()
        assert "BOT_TOKEN_MAIN" not in env

    def test_strips_slack_prefix(self, monkeypatch):
        from app.core.runner import AgentRunner

        monkeypatch.setenv("SLACK_WEBHOOK", "https://hooks.slack.com/...")

        env = AgentRunner._safe_env()
        assert "SLACK_WEBHOOK" not in env

    def test_strips_api_key_prefix(self, monkeypatch):
        from app.core.runner import AgentRunner

        monkeypatch.setenv("API_KEY_OPENAI", "sk-...")

        env = AgentRunner._safe_env()
        assert "API_KEY_OPENAI" not in env

    def test_strips_secret_prefix(self, monkeypatch):
        from app.core.runner import AgentRunner

        monkeypatch.setenv("SECRET_DB_PASSWORD", "hunter2")

        env = AgentRunner._safe_env()
        assert "SECRET_DB_PASSWORD" not in env

    def test_case_insensitive_stripping(self, monkeypatch):
        from app.core.runner import AgentRunner

        monkeypatch.setenv("telegram_bot_token", "low")

        env = AgentRunner._safe_env()
        assert "telegram_bot_token" not in env

    def test_preserves_safe_vars(self, monkeypatch):
        from app.core.runner import AgentRunner

        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("LANG", "en_US.UTF-8")

        env = AgentRunner._safe_env()
        assert "PATH" in env
        assert "LANG" in env


# ── SEC-010 / PATH-001: Runtime project_dir validation ─────────────


class TestIsAllowedDir:
    """Verify _is_allowed_dir enforces path allowlist."""

    def test_allowed_exact_match(self, monkeypatch):
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        monkeypatch.setattr(settings, "allowed_project_dirs", ["~/ai"])
        real = os.path.realpath(os.path.expanduser("~/ai"))
        assert AgentRunner._is_allowed_dir(real) is True

    def test_allowed_subdirectory(self, monkeypatch):
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        monkeypatch.setattr(settings, "allowed_project_dirs", ["~/ai"])
        real = os.path.realpath(os.path.expanduser("~/ai/myproject"))
        assert AgentRunner._is_allowed_dir(real) is True

    def test_disallowed_path(self, monkeypatch):
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        monkeypatch.setattr(settings, "allowed_project_dirs", ["~/ai"])
        assert AgentRunner._is_allowed_dir("/etc/secret") is False

    def test_partial_match_rejected(self, monkeypatch):
        """~/ai-other must not match ~/ai."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        monkeypatch.setattr(settings, "allowed_project_dirs", ["~/ai"])
        real = os.path.realpath(os.path.expanduser("~/ai-other"))
        assert AgentRunner._is_allowed_dir(real) is False

    @pytest.mark.asyncio
    async def test_execute_rejects_disallowed_dir(self, monkeypatch, tmp_path):
        """_execute should fail the task when project_dir is not in allowed list."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        monkeypatch.setattr(settings, "agent_commands", {"test": "echo hello"})
        monkeypatch.setattr(settings, "allowed_project_dirs", ["/only/this/dir"])

        captured = {}

        async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None, git_diff=None):
            captured["exit_code"] = exit_code
            captured["error_message"] = error_message
            task = _make_task(id=task_id, status=TaskStatus.FAILED)
            return task

        import app.core.runner as runner_mod
        monkeypatch.setattr(runner_mod, "complete_task", fake_complete)

        runner = AgentRunner()
        task = _make_task(agent="test", project_dir=str(tmp_path))
        await runner._execute(task)

        assert captured["exit_code"] == -1
        assert "validation failed" in captured["error_message"]


# ── HARD-002: Process group isolation ──────────────────────────────


class TestProcessGroupIsolation:
    """Verify subprocess is created with start_new_session=True."""

    @pytest.mark.asyncio
    async def test_subprocess_is_session_leader(self, monkeypatch, tmp_path):
        """Child process should be in its own process group."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        # Command prints its own pgid and pid
        monkeypatch.setattr(
            settings,
            "agent_commands",
            {"test": "python3 -c 'import os; print(f\"pid={os.getpid()} pgid={os.getpgid(0)}\")'"},
        )
        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])

        captured = {}

        async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None, git_diff=None):
            captured["full_output"] = full_output
            captured["exit_code"] = exit_code
            task = _make_task(id=task_id, status=TaskStatus.COMPLETED)
            task.exit_code = exit_code
            return task

        import app.core.runner as runner_mod
        monkeypatch.setattr(runner_mod, "complete_task", fake_complete)

        runner = AgentRunner()
        task = _make_task(agent="test", project_dir=str(tmp_path))
        await runner._execute(task)

        # Parse output: pid=123 pgid=123 means process is its own group leader
        output = captured.get("full_output", "")
        parts = {}
        for token in output.strip().split():
            k, _, v = token.partition("=")
            parts[k] = int(v)

        assert parts["pid"] == parts["pgid"], "Process should be its own group leader"

    def test_kill_process_only_kills_group_leader(self):
        """_kill_process should only use killpg for session-leader processes."""
        from app.core.runner import AgentRunner

        runner = AgentRunner()

        class FakeProc:
            def __init__(self, pid):
                self.pid = pid
                self.terminated = False

            def terminate(self):
                self.terminated = True

        proc = FakeProc(pid=99999)

        # Since pid 99999 doesn't exist, should fall through to terminate
        runner._kill_process(proc)
        assert proc.terminated

    def test_kill_process_no_pid(self):
        """_kill_process handles proc with no pid."""
        from app.core.runner import AgentRunner

        runner = AgentRunner()

        class FakeProc:
            pid = None
            terminated = False

            def terminate(self):
                self.terminated = True

        proc = FakeProc()
        runner._kill_process(proc)
        assert proc.terminated


# ── SEC-003: advance_chain checks queue capacity ───────────────────


class TestAdvanceChainQueueCapacity:
    """Verify advance_chain respects queue limits."""

    @pytest.mark.asyncio
    async def test_advance_chain_skips_when_queue_full(self, monkeypatch, tmp_path):
        from app.core.broker import advance_chain
        from app.config.settings import settings
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from app.core.models import Base
        import app.core.db as db_mod

        # Create a fresh engine
        db_file = str(tmp_path / "test.db")
        fresh_engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)
        fresh_session_factory = async_sessionmaker(fresh_engine, class_=AsyncSession, expire_on_commit=False)

        async with fresh_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        monkeypatch.setattr(db_mod, "engine", fresh_engine)
        monkeypatch.setattr(db_mod, "async_session", fresh_session_factory)
        monkeypatch.setattr(settings, "max_queue_size", 1)

        from app.core.broker import enqueue_task

        # Enqueue one task to fill the queue (max_queue_size=1)
        await enqueue_task(
            prompt="existing",
            project_dir="~/ai",
            agent="test",
            chat_id=1,
        )

        # Create a completed task that belongs to a chain
        task = _make_task(status=TaskStatus.COMPLETED)
        task.chain_id = 1
        task.chain_step = 0
        task.repeat_remaining = None
        task.repeat_until = None

        # advance_chain should return None because queue is full
        result = await advance_chain(task)
        assert result is None


# ── RACE-010: delete_chain rejects RUNNING chains ──────────────────


class TestDeleteChainRunningGuard:
    """Verify delete_chain raises ValueError for running chains."""

    @pytest.mark.asyncio
    async def test_delete_running_chain_raises(self, monkeypatch, tmp_path):
        from app.core.broker import delete_chain
        from app.config.settings import settings
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from app.core.models import Base
        import app.core.db as db_mod

        db_file = str(tmp_path / "test.db")
        fresh_engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)
        fresh_session_factory = async_sessionmaker(fresh_engine, class_=AsyncSession, expire_on_commit=False)

        async with fresh_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        monkeypatch.setattr(db_mod, "engine", fresh_engine)
        monkeypatch.setattr(db_mod, "async_session", fresh_session_factory)

        from app.core.models import TaskChain
        from app.core.db import get_session
        import json

        # Create a chain directly in the DB
        session = await get_session()
        async with session, session.begin():
            chain = TaskChain(
                name="test-chain",
                steps_json=json.dumps([{"prompt": "step1", "project_dir": "~/ai", "agent": "test"}]),
                status=ChainStatus.RUNNING,
            )
            session.add(chain)

        with pytest.raises(ValueError, match="Cannot delete chain.*while running"):
            await delete_chain("test-chain")


# ── DOS-003: repeat_until max 24h deadline ─────────────────────────


class TestRepeatUntilMaxDeadline:
    """Verify repeat_until cannot exceed 24 hours from now."""

    @pytest.mark.asyncio
    async def test_repeat_until_beyond_24h_rejected(self, monkeypatch, tmp_path):
        from app.core.broker import enqueue_repeat_task
        from app.config.settings import settings
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from app.core.models import Base
        import app.core.db as db_mod

        db_file = str(tmp_path / "test.db")
        fresh_engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)
        fresh_session_factory = async_sessionmaker(fresh_engine, class_=AsyncSession, expire_on_commit=False)

        async with fresh_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        monkeypatch.setattr(db_mod, "engine", fresh_engine)
        monkeypatch.setattr(db_mod, "async_session", fresh_session_factory)

        far_future = datetime.utcnow() + timedelta(hours=48)

        with pytest.raises(ValueError, match="cannot be more than 24 hours"):
            await enqueue_repeat_task(
                prompt="test",
                project_dir="~/ai",
                agent="test",
                chat_id=1,
                repeat_until=far_future,
            )

    @pytest.mark.asyncio
    async def test_repeat_until_within_24h_accepted(self, monkeypatch, tmp_path):
        from app.core.broker import enqueue_repeat_task
        from app.config.settings import settings
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from app.core.models import Base
        import app.core.db as db_mod

        db_file = str(tmp_path / "test.db")
        fresh_engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)
        fresh_session_factory = async_sessionmaker(fresh_engine, class_=AsyncSession, expire_on_commit=False)

        async with fresh_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        monkeypatch.setattr(db_mod, "engine", fresh_engine)
        monkeypatch.setattr(db_mod, "async_session", fresh_session_factory)

        near_future = datetime.utcnow() + timedelta(hours=1)

        task = await enqueue_repeat_task(
            prompt="test",
            project_dir="~/ai",
            agent="test",
            chat_id=1,
            repeat_until=near_future,
        )
        assert task is not None
        assert task.repeat_until == near_future


# ── HARD-003: progress_interval_seconds >= 5 ──────────────────────


class TestProgressIntervalValidation:
    """Verify progress_interval_seconds cannot be < 5."""

    def test_low_interval_rejected(self):
        from pydantic import ValidationError
        from app.config.settings import Settings

        with pytest.raises(ValidationError, match="progress_interval_seconds"):
            Settings(
                telegram_bot_token="test",
                progress_interval_seconds=0,
            )

    def test_valid_interval_accepted(self):
        from app.config.settings import Settings

        s = Settings(
            telegram_bot_token="test",
            progress_interval_seconds=5,
        )
        assert s.progress_interval_seconds == 5


# ── HARD-008: Log injection protection ─────────────────────────────


class TestLogInjection:
    """Verify newlines in prompts are escaped before logging."""

    def test_build_command_preserves_newlines_in_prompt(self):
        """Newlines aren't stripped from the actual command, only from logs."""
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        task = _make_task(prompt="line1\nline2\nline3")
        cmd = runner._build_command(task)
        # The actual prompt in argv should still have newlines
        assert "line1\nline2\nline3" in cmd

    @pytest.mark.asyncio
    async def test_execute_escapes_newlines_in_logs(self, monkeypatch, tmp_path, caplog):
        """When task has newlines in prompt, logs should show escaped version."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings
        import logging

        monkeypatch.setattr(settings, "agent_commands", {"test": "echo ok"})
        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])

        async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None, git_diff=None):
            task = _make_task(id=task_id, status=TaskStatus.COMPLETED)
            task.exit_code = exit_code
            return task

        import app.core.runner as runner_mod
        monkeypatch.setattr(runner_mod, "complete_task", fake_complete)

        runner = AgentRunner()
        task = _make_task(agent="test", project_dir=str(tmp_path), prompt="inject\nfake log entry")

        with caplog.at_level(logging.DEBUG, logger="app.core.runner"):
            await runner._execute(task)

        # Verify no raw newlines leaked into log messages
        for record in caplog.records:
            if "inject" in record.message:
                assert "\n" not in record.message, "Raw newline leaked into log"
                assert "\\n" in record.message, "Newline should be escaped"


# ── RACE-002: advance_chain failure resilience ─────────────────────


class TestAdvanceChainResilience:
    """Verify _after_complete doesn't crash when advance_chain fails."""

    @pytest.mark.asyncio
    async def test_advance_chain_exception_swallowed(self, monkeypatch):
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        async def noop_reenqueue(task):
            return None

        async def exploding_advance(task):
            raise RuntimeError("DB connection lost")

        monkeypatch.setattr(runner_mod, "maybe_reenqueue", noop_reenqueue)
        monkeypatch.setattr(runner_mod, "advance_chain", exploding_advance)

        runner = AgentRunner()
        task = _make_task(status=TaskStatus.COMPLETED)
        task.chain_id = 42
        task.chain_step = 0
        task.repeat_remaining = None
        task.repeat_until = None

        # Should NOT raise despite advance_chain failure
        await runner._after_complete(task)


# ── SEC-008: /tmp removed from allowed_project_dirs ────────────────


class TestDefaultAllowedDirs:
    """Verify /tmp is not in the default allowed project directories."""

    def test_tmp_not_in_defaults(self):
        from app.config.settings import Settings

        s = Settings(telegram_bot_token="test")
        for d in s.allowed_project_dirs:
            expanded = os.path.expanduser(d)
            assert "/tmp" not in expanded, f"/tmp found in allowed_project_dirs: {d}"

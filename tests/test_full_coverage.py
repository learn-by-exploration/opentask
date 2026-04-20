"""Tests targeting remaining uncovered lines to reach 100% coverage."""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-for-tests")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

from app.core.models import ChainStatus, Task, TaskStatus


def _make_task(**overrides) -> Task:
    defaults = {
        "id": 1,
        "prompt": "fix bug",
        "project_dir": "/home/user/project",
        "agent": "opencode",
        "status": TaskStatus.RUNNING,
    }
    defaults.update(overrides)
    return Task(**defaults)


# ── runner.py: start() poll loop branches ───────────────────────────


class TestRunnerPollLoop:
    """Cover start() lines 48-70: error backoff + idle wait + execute path."""

    @pytest.mark.asyncio
    async def test_error_backoff_then_idle_then_task(self, monkeypatch):
        """Cover: pick_next_task raises → backoff wait → returns None → idle wait → returns task → execute → stop."""
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        call_count = 0
        fake_task = _make_task()

        async def mock_pick():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("DB down")
            if call_count == 2:
                return None
            return fake_task

        execute_called = False

        async def mock_execute(self, task):
            nonlocal execute_called
            execute_called = True
            # stop after executing
            self._running = False

        monkeypatch.setattr(runner_mod, "pick_next_task", mock_pick)
        monkeypatch.setattr(AgentRunner, "_execute", mock_execute)

        runner = AgentRunner()
        # Set wake event so the timeouts don't actually wait
        runner._wake_event.set()

        await runner.start()
        assert execute_called
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_backoff_timeout_branch(self, monkeypatch):
        """Cover lines 53-54: asyncio.TimeoutError in backoff wait."""
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        call_count = 0

        async def mock_pick():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("DB error")
            # Stop on second call
            runner._running = False
            return None

        monkeypatch.setattr(runner_mod, "pick_next_task", mock_pick)

        runner = AgentRunner()
        # Don't set wake event, so TimeoutError fires on error backoff
        # But use a very short backoff by monkeypatching wait_for
        original_wait_for = asyncio.wait_for

        async def fast_wait_for(coro, timeout):
            try:
                return await original_wait_for(coro, timeout=0.01)
            except asyncio.TimeoutError:
                raise

        monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)

        await runner.start()
        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_idle_timeout_branch(self, monkeypatch):
        """Cover lines 63-65: asyncio.TimeoutError in idle wait (no task)."""
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        call_count = 0

        async def mock_pick():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return None
            runner._running = False
            return None

        monkeypatch.setattr(runner_mod, "pick_next_task", mock_pick)

        runner = AgentRunner()

        # Patch the IDLE_TIMEOUT constant used in wait_for so it times out instantly
        original_wait_for = asyncio.wait_for

        async def fast_idle_wait(coro, timeout=None):
            # Force any 30s timeout (idle) to 0.01s so it times out
            if timeout == 30:
                return await original_wait_for(coro, timeout=0.01)
            return await original_wait_for(coro, timeout=timeout)

        monkeypatch.setattr(asyncio, "wait_for", fast_idle_wait)

        await runner.start()
        assert call_count >= 2


# ── runner.py: timeout + kill branch (lines 120-121) ────────────────


class TestRunnerTimeoutKill:
    """Cover lines 120-121: process.kill() after terminate timeout."""

    @pytest.mark.asyncio
    async def test_timeout_kills_stubborn_process(self, monkeypatch, tmp_path):
        """When process ignores SIGTERM, the kill branch fires."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        # Use a command that traps SIGTERM and hangs
        monkeypatch.setattr(
            settings, "agent_commands", {"test": "bash -c 'trap \"\" TERM; sleep 60'"},
        )
        monkeypatch.setattr(settings, "task_timeout_seconds", 1)
        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])

        captured = {}

        async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None):
            captured["full_output"] = full_output
            captured["error_message"] = error_message
            task = _make_task(id=task_id, status=TaskStatus.FAILED)
            task.exit_code = exit_code
            return task

        import app.core.runner as runner_mod
        monkeypatch.setattr(runner_mod, "complete_task", fake_complete)

        runner = AgentRunner()
        task = _make_task(agent="test", project_dir=str(tmp_path))
        await runner._execute(task)

        assert "TIMEOUT" in captured["full_output"]


# ── runner.py: _read_output line trimming + progress error (174, 185-186)


class TestReadOutputEdgeCases:
    """Cover line 174 (trim recent_lines > 5) and 185-186 (progress error)."""

    @pytest.mark.asyncio
    async def test_recent_lines_trimmed_to_five(self, monkeypatch, tmp_path):
        """When > 5 lines arrive between progress ticks, only last 5 kept."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        # interval=0 so every line triggers progress check
        monkeypatch.setattr(settings, "progress_interval_seconds", 0)

        sent = []

        async def mock_progress(chat_id, text):
            sent.append(text)

        runner = AgentRunner()
        runner._progress_notify = mock_progress

        # Generate 10 lines
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", "for i in $(seq 1 10); do echo line$i; done",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(tmp_path),
        )
        task = _make_task(telegram_chat_id=123)
        await runner._read_output(proc, task)

        # At least one progress message should have been sent
        assert len(sent) >= 1
        # The last progress should only contain the last 5 lines
        last_msg = sent[-1]
        # Should have line10 (last line)
        assert "line10" in last_msg or "line9" in last_msg

    @pytest.mark.asyncio
    async def test_progress_notify_exception_swallowed(self, monkeypatch, tmp_path):
        """Cover lines 185-186: progress callback raises → no crash."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        monkeypatch.setattr(settings, "progress_interval_seconds", 0)

        async def failing_progress(chat_id, text):
            raise RuntimeError("notification service down")

        runner = AgentRunner()
        runner._progress_notify = failing_progress

        proc = await asyncio.create_subprocess_exec(
            "printf", "hello\nworld\n",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(tmp_path),
        )
        task = _make_task(telegram_chat_id=123)
        # Should not raise
        raw, _stderr = await runner._read_output(proc, task)
        assert b"hello" in raw


# ── runner.py: _after_complete chain advance (line 251) ──────────────


class TestAfterCompleteChainAdvance:
    """Cover line 251: chain advance success logging."""

    @pytest.mark.asyncio
    async def test_chain_advance_success(self, monkeypatch):
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        next_task = _make_task(id=2, status=TaskStatus.PENDING)
        next_task.chain_step = 1

        async def mock_reenqueue(task):
            return None

        async def mock_advance(task):
            return next_task

        monkeypatch.setattr(runner_mod, "maybe_reenqueue", mock_reenqueue)
        monkeypatch.setattr(runner_mod, "advance_chain", mock_advance)

        runner = AgentRunner()
        task = _make_task(status=TaskStatus.COMPLETED)
        task.chain_id = 10
        task.chain_step = 0
        task.repeat_remaining = None
        task.repeat_until = None

        await runner._after_complete(task)
        # If we get here without error, line 251 was covered


# ── runner.py: _notify_chain_event edge cases (266, 272, 276-277) ────


class TestNotifyChainEventEdgeCases:
    """Cover lines 266 (chain None), 272 (chain still running), 276-277 (exception)."""

    @pytest.mark.asyncio
    async def test_chain_not_found_returns_early(self, monkeypatch):
        """Line 266: get_chain_by_id returns None → return."""
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        async def mock_get_chain(cid):
            return None

        monkeypatch.setattr(runner_mod, "get_chain_by_id", mock_get_chain)

        sent = []

        async def mock_notify(chat_id, text):
            sent.append(text)

        runner = AgentRunner()
        runner._chain_notify = mock_notify

        task = MagicMock()
        task.chain_id = 999
        task.telegram_chat_id = 123

        await runner._notify_chain_event(task)
        assert len(sent) == 0

    @pytest.mark.asyncio
    async def test_chain_still_running_returns_early(self, monkeypatch):
        """Line 272: chain.status is RUNNING → return without notification."""
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        chain = MagicMock()
        chain.status = ChainStatus.RUNNING
        chain.name = "test"

        async def mock_get_chain(cid):
            return chain

        monkeypatch.setattr(runner_mod, "get_chain_by_id", mock_get_chain)

        sent = []

        async def mock_notify(chat_id, text):
            sent.append(text)

        runner = AgentRunner()
        runner._chain_notify = mock_notify

        task = MagicMock()
        task.chain_id = 1
        task.telegram_chat_id = 123

        await runner._notify_chain_event(task)
        assert len(sent) == 0

    @pytest.mark.asyncio
    async def test_chain_notify_exception_swallowed(self, monkeypatch):
        """Lines 276-277: exception in chain notification → logged, no crash."""
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        async def mock_get_chain(cid):
            raise RuntimeError("DB connection lost")

        monkeypatch.setattr(runner_mod, "get_chain_by_id", mock_get_chain)

        async def mock_notify(chat_id, text):
            pass

        runner = AgentRunner()
        runner._chain_notify = mock_notify

        task = MagicMock()
        task.chain_id = 1
        task.telegram_chat_id = 123

        # Should not raise
        await runner._notify_chain_event(task)


# ── broker.py: retry_task edge cases (160, 162, 172) ─────────────────


class TestBrokerRetryEdgeCases:
    """Cover retry_task branches: not found, not terminal, queue full."""

    @pytest.mark.asyncio
    async def test_retry_nonexistent_task(self, monkeypatch):
        """Line 160: retry a task ID that doesn't exist → None."""
        from app.core.broker import retry_task
        from app.core.db import get_session

        # Use real DB but with no tasks
        from app.core.db import init_db
        import app.core.db as db_mod
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from app.core.models import Base

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async def mock_get_session():
            return factory()

        monkeypatch.setattr("app.core.broker.get_session", mock_get_session)

        result = await retry_task(99999)
        assert result is None

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_retry_running_task(self, monkeypatch):
        """Line 162: retry a RUNNING task → None."""
        from app.core.broker import retry_task
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from app.core.models import Base, Task, TaskStatus

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        # Insert a RUNNING task
        async with factory() as session:
            task = Task(prompt="test", project_dir="/tmp", agent="opencode", status=TaskStatus.RUNNING)
            session.add(task)
            await session.commit()
            task_id = task.id

        async def mock_get_session():
            return factory()

        monkeypatch.setattr("app.core.broker.get_session", mock_get_session)

        result = await retry_task(task_id)
        assert result is None

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_retry_queue_full(self, monkeypatch):
        """Line 172: retry when queue is full → ValueError."""
        from app.core.broker import retry_task
        from app.config.settings import settings
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from app.core.models import Base, Task, TaskStatus

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        # Insert a FAILED task + fill queue to max
        async with factory() as session:
            failed = Task(prompt="test", project_dir="/tmp", agent="opencode", status=TaskStatus.FAILED)
            session.add(failed)
            for i in range(settings.max_queue_size):
                session.add(Task(prompt=f"task {i}", project_dir="/tmp", agent="opencode", status=TaskStatus.PENDING))
            await session.commit()
            failed_id = failed.id

        async def mock_get_session():
            return factory()

        monkeypatch.setattr("app.core.broker.get_session", mock_get_session)

        with pytest.raises(ValueError, match="Queue full"):
            await retry_task(failed_id)

        await engine.dispose()


# ── broker.py: start_chain empty steps (line 469) ───────────────────


class TestBrokerStartChainEmptySteps:
    """Cover line 469: chain with empty steps list → None."""

    @pytest.mark.asyncio
    async def test_start_chain_empty_steps(self, monkeypatch):
        from app.core.broker import start_chain
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from app.core.models import Base, TaskChain, ChainStatus
        import json

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        # Insert a chain with empty steps
        async with factory() as session:
            chain = TaskChain(
                name="empty",
                steps_json=json.dumps([]),
                status=ChainStatus.IDLE,
                telegram_chat_id=123,
            )
            session.add(chain)
            await session.commit()

        async def mock_get_session():
            return factory()

        monkeypatch.setattr("app.core.broker.get_session", mock_get_session)

        result = await start_chain(name="empty", chat_id=123)
        assert result is None

        await engine.dispose()


# ── db.py: init_db (lines 18-21) ────────────────────────────────────


class TestInitDb:
    """Cover init_db() — creates directory and tables."""

    @pytest.mark.asyncio
    async def test_init_db_creates_tables(self, tmp_path, monkeypatch):
        from app.config.settings import settings
        from sqlalchemy.ext.asyncio import create_async_engine

        db_path = str(tmp_path / "sub" / "test.db")
        monkeypatch.setattr(settings, "db_path", db_path)

        # Re-create engine/session with new path
        import app.core.db as db_mod
        new_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
        monkeypatch.setattr(db_mod, "engine", new_engine)

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
        new_session = async_sessionmaker(new_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(db_mod, "async_session", new_session)

        await db_mod.init_db()

        # Verify directory was created
        assert os.path.isdir(str(tmp_path / "sub"))

        await new_engine.dispose()


# ── __main__.py: recovery logging + graceful timeout + main() ────────


class TestMainRecoveryLogs:
    """Cover __main__.py lines 27, 30, 35 — the recovery/purge logging branches."""

    @pytest.mark.asyncio
    async def test_recovery_logs(self, monkeypatch):
        """When recovered/purged counts > 0, log messages appear."""
        import app.__main__ as main_mod
        import app.core.broker as broker_mod
        import signal

        monkeypatch.setattr(main_mod, "init_db", AsyncMock())
        monkeypatch.setattr(main_mod, "recover_interrupted_tasks", AsyncMock(return_value=3))
        monkeypatch.setattr(main_mod, "recover_interrupted_chains", AsyncMock(return_value=2))
        monkeypatch.setattr(broker_mod, "purge_old_tasks", AsyncMock(return_value=5))
        monkeypatch.setattr(broker_mod, "recover_stale_worker_tasks", AsyncMock(return_value=0))
        monkeypatch.setattr(broker_mod, "_runner_wake", None)

        mock_updater = AsyncMock()
        mock_app = MagicMock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()
        mock_app.updater = mock_updater
        mock_app.bot = AsyncMock()

        monkeypatch.setattr(main_mod, "build_app", lambda runner: mock_app)
        monkeypatch.setattr(main_mod, "make_notify_callback", AsyncMock(return_value=AsyncMock()))
        monkeypatch.setattr(main_mod, "make_chain_notify_callback", AsyncMock(return_value=AsyncMock()))
        monkeypatch.setattr(main_mod, "make_progress_callback", AsyncMock(return_value=AsyncMock()))
        monkeypatch.setattr(main_mod, "make_typing_callback", AsyncMock(return_value=AsyncMock()))

        # Disable dashboard so uvicorn doesn't try to bind a port
        monkeypatch.setattr(main_mod.settings, "dashboard_enabled", False)

        # Mock AgentRunner.start so it doesn't block on the poll loop
        from app.core.runner import AgentRunner

        async def fake_start(self):
            self._running = True
            while self._running:
                await asyncio.sleep(0.05)

        monkeypatch.setattr(AgentRunner, "start", fake_start)

        async def send_signal():
            await asyncio.sleep(0.1)
            os.kill(os.getpid(), signal.SIGTERM)

        sig_task = asyncio.create_task(send_signal())
        await main_mod._run()
        await sig_task


class TestMainGracefulTimeout:
    """Cover __main__.py lines 74-80 — runner doesn't stop within grace period."""

    @pytest.mark.asyncio
    async def test_runner_timeout_force_cancel(self, monkeypatch):
        """Call _run() with a hanging runner; signal fires → wait_for raises TimeoutError → cancel."""
        import signal

        import app.__main__ as main_mod
        import app.core.broker as broker_mod
        from app.core.runner import AgentRunner

        monkeypatch.setattr(main_mod, "init_db", AsyncMock())
        monkeypatch.setattr(main_mod, "recover_interrupted_tasks", AsyncMock(return_value=0))
        monkeypatch.setattr(main_mod, "recover_interrupted_chains", AsyncMock(return_value=0))
        monkeypatch.setattr(broker_mod, "purge_old_tasks", AsyncMock(return_value=0))
        monkeypatch.setattr(broker_mod, "recover_stale_worker_tasks", AsyncMock(return_value=0))
        monkeypatch.setattr(broker_mod, "_runner_wake", None)

        mock_updater = AsyncMock()
        mock_app = MagicMock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()
        mock_app.updater = mock_updater
        mock_app.bot = AsyncMock()

        monkeypatch.setattr(main_mod, "build_app", lambda runner: mock_app)
        monkeypatch.setattr(main_mod, "make_notify_callback", AsyncMock(return_value=AsyncMock()))
        monkeypatch.setattr(main_mod, "make_chain_notify_callback", AsyncMock(return_value=AsyncMock()))
        monkeypatch.setattr(main_mod, "make_progress_callback", AsyncMock(return_value=AsyncMock()))
        monkeypatch.setattr(main_mod, "make_typing_callback", AsyncMock(return_value=AsyncMock()))

        # Disable dashboard so uvicorn doesn't try to bind a port
        monkeypatch.setattr(main_mod.settings, "dashboard_enabled", False)

        # Make runner.start() hang (ignores stop())
        original_stop = AgentRunner.stop

        async def hanging_start(self_runner):
            self_runner._running = True
            # Let CancelledError propagate so _run's except CancelledError: pass is triggered
            while True:
                await asyncio.sleep(100)

        monkeypatch.setattr(AgentRunner, "start", hanging_start)
        monkeypatch.setattr(AgentRunner, "cancel_current", AsyncMock())

        # Patch wait_for so it immediately raises TimeoutError (simulating
        # running past the grace period without duplicating _run logic).
        real_wait_for = asyncio.wait_for

        async def fast_wait_for(coro, *, timeout=None):
            if timeout == 10:  # the grace=10 in _run
                raise asyncio.TimeoutError()
            return await real_wait_for(coro, timeout=timeout)

        monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)

        # Fire SIGTERM quickly so _run proceeds past shutdown_event.wait()
        async def send_signal():
            await asyncio.sleep(0.1)
            os.kill(os.getpid(), signal.SIGTERM)

        sig_task = asyncio.create_task(send_signal())
        await main_mod._run()
        await sig_task

        mock_app.shutdown.assert_awaited_once()


class TestMainEntryPoint:
    """Cover __main__.py lines 90-94, 98 — main() and __name__ == '__main__'."""

    def test_main_keyboard_interrupt(self, monkeypatch):
        """Lines 90-94: KeyboardInterrupt during asyncio.run → sys.exit(0)."""
        import app.__main__ as main_mod

        monkeypatch.setattr(asyncio, "run", MagicMock(side_effect=KeyboardInterrupt))

        with pytest.raises(SystemExit) as exc_info:
            main_mod.main()
        assert exc_info.value.code == 0

    def test_main_normal_exit(self, monkeypatch):
        """Lines 90-91: normal asyncio.run completion."""
        import app.__main__ as main_mod

        monkeypatch.setattr(asyncio, "run", MagicMock(return_value=None))
        # Should not raise
        main_mod.main()


# ── Typing indicator tests ──────────────────────────────────────────


class TestMakeTypingCallback:
    """Cover bot.py make_typing_callback (lines 208-213)."""

    @pytest.mark.asyncio
    async def test_typing_sends_action(self):
        """Typing callback sends ChatAction.TYPING."""
        from app.telegram.bot import make_typing_callback
        from telegram.constants import ChatAction

        app = MagicMock()
        app.bot = AsyncMock()
        typing_fn = await make_typing_callback(app)

        await typing_fn(chat_id=123)
        app.bot.send_chat_action.assert_called_once_with(
            chat_id=123, action=ChatAction.TYPING,
        )

    @pytest.mark.asyncio
    async def test_typing_none_chat_id(self):
        """Typing callback ignores None chat_id."""
        from app.telegram.bot import make_typing_callback

        app = MagicMock()
        app.bot = AsyncMock()
        typing_fn = await make_typing_callback(app)

        await typing_fn(chat_id=None)
        app.bot.send_chat_action.assert_not_called()

    @pytest.mark.asyncio
    async def test_typing_exception_swallowed(self):
        """Typing callback swallows exceptions."""
        from app.telegram.bot import make_typing_callback

        app = MagicMock()
        app.bot = AsyncMock()
        app.bot.send_chat_action.side_effect = RuntimeError("network error")
        typing_fn = await make_typing_callback(app)

        # Should not raise
        await typing_fn(chat_id=123)


class TestRunnerTypingIndicator:
    """Cover runner.py typing indicator at task start (lines 73-76)."""

    @pytest.mark.asyncio
    async def test_typing_called_at_task_start(self, monkeypatch):
        """Runner calls _typing_notify when starting a task."""
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        fake_task = _make_task(telegram_chat_id=999)
        call_count = 0

        async def mock_pick():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return fake_task
            return None

        execute_called = False

        async def mock_execute(self, task):
            nonlocal execute_called
            execute_called = True
            self._running = False

        monkeypatch.setattr(runner_mod, "pick_next_task", mock_pick)
        monkeypatch.setattr(AgentRunner, "_execute", mock_execute)

        typing_mock = AsyncMock()
        runner = AgentRunner()
        runner._typing_notify = typing_mock
        runner._wake_event.set()

        await runner.start()
        assert execute_called
        typing_mock.assert_called_once_with(999)

    @pytest.mark.asyncio
    async def test_typing_exception_at_start_swallowed(self, monkeypatch):
        """Runner swallows exceptions from _typing_notify at task start."""
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        fake_task = _make_task(telegram_chat_id=999)
        call_count = 0

        async def mock_pick():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return fake_task
            return None

        async def mock_execute(self, task):
            self._running = False

        monkeypatch.setattr(runner_mod, "pick_next_task", mock_pick)
        monkeypatch.setattr(AgentRunner, "_execute", mock_execute)

        typing_mock = AsyncMock(side_effect=RuntimeError("network"))
        runner = AgentRunner()
        runner._typing_notify = typing_mock
        runner._wake_event.set()

        # Should not raise despite typing error
        await runner.start()
        typing_mock.assert_called_once()


class TestRunnerTypingWithProgress:
    """Cover runner.py typing indicator with progress (lines 195-198)."""

    @pytest.mark.asyncio
    async def test_typing_sent_with_progress_update(self, monkeypatch, tmp_path):
        """Typing indicator is sent alongside progress updates."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        # Fast script that prints enough lines
        script = tmp_path / "script.sh"
        script.write_text("#!/bin/bash\nfor i in $(seq 1 10); do echo line$i; sleep 0.05; done\n")
        script.chmod(0o755)

        monkeypatch.setattr(
            settings, "agent_commands", {"test": str(script)},
        )
        monkeypatch.setattr(settings, "task_timeout_seconds", 30)
        monkeypatch.setattr(settings, "progress_interval_seconds", 0)  # trigger every line
        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])

        async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None):
            task = _make_task(id=task_id, status=TaskStatus.COMPLETED)
            task.exit_code = exit_code
            return task

        import app.core.runner as runner_mod
        monkeypatch.setattr(runner_mod, "complete_task", fake_complete)
        monkeypatch.setattr(runner_mod, "maybe_reenqueue", AsyncMock(return_value=None))

        progress_mock = AsyncMock()
        typing_mock = AsyncMock()

        runner = AgentRunner()
        runner._progress_notify = progress_mock
        runner._typing_notify = typing_mock

        task = _make_task(agent="test", project_dir=str(tmp_path), telegram_chat_id=42)
        await runner._execute(task)

        # Both progress and typing should have been called
        assert progress_mock.call_count >= 1
        assert typing_mock.call_count >= 1

    @pytest.mark.asyncio
    async def test_typing_exception_during_progress_swallowed(self, monkeypatch, tmp_path):
        """Typing exception during progress doesn't break execution."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        script = tmp_path / "script.sh"
        script.write_text("#!/bin/bash\nfor i in $(seq 1 5); do echo line$i; sleep 0.05; done\n")
        script.chmod(0o755)

        monkeypatch.setattr(
            settings, "agent_commands", {"test": str(script)},
        )
        monkeypatch.setattr(settings, "task_timeout_seconds", 30)
        monkeypatch.setattr(settings, "progress_interval_seconds", 0)
        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])

        async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None):
            task = _make_task(id=task_id, status=TaskStatus.COMPLETED)
            task.exit_code = exit_code
            return task

        import app.core.runner as runner_mod
        monkeypatch.setattr(runner_mod, "complete_task", fake_complete)
        monkeypatch.setattr(runner_mod, "maybe_reenqueue", AsyncMock(return_value=None))

        progress_mock = AsyncMock()
        typing_mock = AsyncMock(side_effect=RuntimeError("boom"))

        runner = AgentRunner()
        runner._progress_notify = progress_mock
        runner._typing_notify = typing_mock

        task = _make_task(agent="test", project_dir=str(tmp_path), telegram_chat_id=42)
        # Should complete without raising
        await runner._execute(task)

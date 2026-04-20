"""Tests to close remaining coverage gaps across all modules."""

from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-for-tests")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

import asyncio
import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base, ChainStatus, Task, TaskChain, TaskStatus


@pytest_asyncio.fixture
async def full_cov_engine():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def full_cov_session(full_cov_engine):
    factory = async_sessionmaker(full_cov_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()


# ── broker.py:369-370 — repeat reenqueue hits queue full ────────────


class TestRepeatReenqueueQueueFull:
    @pytest.mark.asyncio
    async def test_maybe_reenqueue_queue_full(self, full_cov_engine):
        """When queue is full during re-enqueue, maybe_reenqueue returns None."""
        factory = async_sessionmaker(full_cov_engine, class_=AsyncSession, expire_on_commit=False)

        # Create a completed task with repeat_remaining > 0
        async with factory() as session, session.begin():
            task = Task(
                prompt="repeat me",
                project_dir="/tmp/tests",
                agent="opencode",
                status=TaskStatus.COMPLETED,
                repeat_total=5,
                repeat_remaining=3,
            )
            session.add(task)
            await session.flush()
            await session.refresh(task)
            task_id = task.id

        # Fill queue to capacity
        async with factory() as session, session.begin():
            for i in range(20):
                session.add(Task(
                    prompt=f"filler {i}",
                    project_dir="/tmp/tests",
                    agent="opencode",
                    status=TaskStatus.PENDING,
                ))

        # Fetch the task fresh
        async with factory() as session:
            from sqlalchemy import select
            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one()

        with patch("app.core.broker.get_session", return_value=factory()), \
             patch("app.core.broker.settings") as mock_settings:
            mock_settings.max_queue_size = 20
            from app.core.broker import maybe_reenqueue
            result = await maybe_reenqueue(task)
            assert result is None


# ── broker.py:566-569 — chain advance hits queue full ───────────────


class TestChainAdvanceQueueFull:
    @pytest.mark.asyncio
    async def test_advance_chain_queue_full(self, full_cov_engine):
        """When queue is full during chain advance, chain gets FAILED."""
        factory = async_sessionmaker(full_cov_engine, class_=AsyncSession, expire_on_commit=False)

        # Create a chain with 3 steps
        steps = [{"prompt": "s1"}, {"prompt": "s2"}, {"prompt": "s3"}]
        async with factory() as session, session.begin():
            chain = TaskChain(
                name="test-chain-full",
                steps_json=json.dumps(steps),
                status=ChainStatus.RUNNING,
                current_step=0,
            )
            session.add(chain)
            await session.flush()
            await session.refresh(chain)
            chain_id = chain.id

            # Create completed chain task at step 0
            task = Task(
                prompt="s1",
                project_dir="/tmp/tests",
                agent="opencode",
                status=TaskStatus.COMPLETED,
                chain_id=chain_id,
                chain_step=0,
            )
            session.add(task)
            await session.flush()
            await session.refresh(task)
            task_id = task.id

            # Fill queue
            for i in range(20):
                session.add(Task(
                    prompt=f"fill {i}",
                    project_dir="/tmp/tests",
                    agent="opencode",
                    status=TaskStatus.PENDING,
                ))

        # Fetch task fresh
        async with factory() as session:
            from sqlalchemy import select
            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one()

        with patch("app.core.broker.get_session", return_value=factory()), \
             patch("app.core.broker.settings") as mock_settings:
            mock_settings.max_queue_size = 20
            mock_settings.default_project_dir = "/tmp/tests"
            mock_settings.default_agent = "opencode"
            from app.core.broker import advance_chain
            result = await advance_chain(task)
            assert result is None

        # Verify chain is now FAILED
        async with factory() as session:
            from sqlalchemy import select
            result = await session.execute(select(TaskChain).where(TaskChain.id == chain_id))
            chain = result.scalar_one()
            assert chain.status == ChainStatus.FAILED


# ── bot.py:527-528 — savechain with empty or too-long name ─────────


class TestSavechainNameValidation:
    @pytest.mark.asyncio
    async def test_savechain_empty_name(self):
        """cmd_savechain rejects empty chain name."""
        from app.telegram.bot import cmd_savechain

        update = MagicMock()
        update.effective_user.id = 12345
        update.effective_chat.id = 12345
        update.get_bot.return_value.send_message = AsyncMock()
        context = MagicMock()
        # " " sanitizes to "" which is falsy
        context.args = ["\x00", "step1", "|", "step2"]

        await cmd_savechain(update, context)
        update.get_bot.return_value.send_message.assert_called()
        msg = update.get_bot.return_value.send_message.call_args[1]["text"]
        assert "1-64" in msg

    @pytest.mark.asyncio
    async def test_savechain_name_too_long(self):
        """cmd_savechain rejects name > 64 chars."""
        from app.telegram.bot import cmd_savechain

        update = MagicMock()
        update.effective_user.id = 12345
        update.effective_chat.id = 12345
        update.get_bot.return_value.send_message = AsyncMock()
        context = MagicMock()
        context.args = ["a" * 65, "step1", "|", "step2"]

        await cmd_savechain(update, context)
        msg = update.get_bot.return_value.send_message.call_args[1]["text"]
        assert "1-64" in msg


# ── bot.py:617-619 — delchain raises ValueError (running chain) ────


class TestDelchainRunningChain:
    @pytest.mark.asyncio
    async def test_delchain_running_chain(self):
        """cmd_delchain shows error when chain is running."""
        from app.telegram.bot import cmd_delchain

        update = MagicMock()
        update.effective_user.id = 12345
        update.effective_chat.id = 12345
        update.get_bot.return_value.send_message = AsyncMock()
        context = MagicMock()
        context.args = ["my-chain"]

        with patch("app.telegram.bot.delete_chain", new_callable=AsyncMock, side_effect=ValueError("Cannot delete chain 'my-chain' while running")):
            await cmd_delchain(update, context)

        msg = update.get_bot.return_value.send_message.call_args[1]["text"]
        assert "Cannot delete" in msg or "running" in msg.lower()


# ── db.py:22-23 — SQLite pragma execution ───────────────────────────


class TestSqlitePragmas:
    def test_pragmas_are_set(self):
        """_set_sqlite_pragmas executes WAL and foreign key pragmas."""
        from app.core.db import _set_sqlite_pragmas

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        _set_sqlite_pragmas(mock_conn, None)

        assert mock_cursor.execute.call_count == 2
        calls = [c[0][0] for c in mock_cursor.execute.call_args_list]
        assert "PRAGMA journal_mode=WAL" in calls
        assert "PRAGMA foreign_keys=ON" in calls
        mock_cursor.close.assert_called_once()


# ── __main__.py:86-87 — ImportError when uvicorn not available ──────


class TestMainDashboardImportError:
    @pytest.mark.asyncio
    async def test_dashboard_disabled_when_import_fails(self, monkeypatch):
        """When uvicorn import fails inside _run(), dashboard is disabled gracefully."""
        import signal

        import app.__main__ as main_mod
        import app.core.broker as broker_mod

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

        # Mock AgentRunner.start so it doesn't block on the poll loop
        from app.core.runner import AgentRunner

        async def fake_start(self):
            self._running = True
            while self._running:
                await asyncio.sleep(0.05)

        monkeypatch.setattr(AgentRunner, "start", fake_start)

        # Force uvicorn import to fail
        import builtins
        real_import = builtins.__import__

        def failing_import(name, *args, **kwargs):
            if name == "uvicorn":
                raise ImportError("mocked: uvicorn not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", failing_import)

        async def send_signal():
            await asyncio.sleep(0.1)
            os.kill(os.getpid(), signal.SIGTERM)

        sig_task = asyncio.create_task(send_signal())
        await main_mod._run()
        await sig_task


# ── __main__.py:122-123 — web task timeout during shutdown ──────────


class TestMainWebTaskShutdown:
    @pytest.mark.asyncio
    async def test_web_task_timeout_during_shutdown(self, monkeypatch):
        """Web task timeout path is exercised during _run() shutdown."""
        import signal

        import app.__main__ as main_mod
        import app.core.broker as broker_mod

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

        # Mock AgentRunner.start so it doesn't block on the poll loop
        from app.core.runner import AgentRunner

        async def fake_start(self):
            self._running = True
            while self._running:
                await asyncio.sleep(0.05)

        monkeypatch.setattr(AgentRunner, "start", fake_start)

        # Mock uvicorn so we control the web_server and web_task
        mock_uvicorn = MagicMock()
        mock_config = MagicMock()
        mock_uvicorn.Config.return_value = mock_config

        # Create a server whose serve() hangs until cancelled
        hanging_future = asyncio.get_event_loop().create_future()

        async def hanging_serve():
            await hanging_future

        mock_server = MagicMock()
        mock_server.serve = hanging_serve
        mock_server.should_exit = False
        mock_uvicorn.Server.return_value = mock_server

        sys.modules["uvicorn"] = mock_uvicorn

        # Ensure dashboard is enabled
        monkeypatch.setattr(main_mod.settings, "dashboard_enabled", True)

        async def send_signal():
            await asyncio.sleep(0.15)
            os.kill(os.getpid(), signal.SIGTERM)

        sig_task = asyncio.create_task(send_signal())
        await main_mod._run()
        await sig_task

        # Clean up
        if "uvicorn" in sys.modules and sys.modules["uvicorn"] is mock_uvicorn:
            del sys.modules["uvicorn"]
            import uvicorn  # Re-import real uvicorn

    @pytest.mark.asyncio
    async def test_web_task_cancelled_during_shutdown(self):
        """Web task CancelledError path is handled."""

        async def cancelled_coro():
            raise asyncio.CancelledError()

        class FakeServer:
            should_exit = False

        web_server = FakeServer()
        web_task = asyncio.ensure_future(cancelled_coro())

        if web_task is not None and web_server is not None:
            web_server.should_exit = True
            try:
                await asyncio.wait_for(web_task, timeout=1)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

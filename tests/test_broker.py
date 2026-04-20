"""Tests for the task broker CRUD operations."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base, Task, TaskStatus


# ── Local fixture: isolated engine + monkeypatched broker ───────────

@pytest_asyncio.fixture
async def broker_session(monkeypatch):
    """
    Create an in-memory DB, monkeypatch broker.get_session to use it,
    and return a session factory for assertions.
    """
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async def _patched_get_session():
        return session_factory()

    import app.core.broker as broker_mod
    monkeypatch.setattr(broker_mod, "get_session", _patched_get_session)

    yield session_factory

    await engine.dispose()


# ── enqueue_task ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enqueue_task_creates_pending_task(broker_session):
    from app.core.broker import enqueue_task

    task = await enqueue_task(
        prompt="fix the bug",
        project_dir="/tmp/proj",
        agent="opencode",
        chat_id=111,
        msg_id=222,
    )

    assert task.id is not None
    assert task.status == TaskStatus.PENDING
    assert task.prompt == "fix the bug"
    assert task.project_dir == "/tmp/proj"
    assert task.agent == "opencode"
    assert task.telegram_chat_id == 111
    assert task.telegram_msg_id == 222


@pytest.mark.asyncio
async def test_enqueue_uses_defaults_when_not_provided(broker_session):
    from app.core.broker import enqueue_task

    task = await enqueue_task(prompt="do something")

    assert task.project_dir  # should be the default from settings
    assert task.agent  # should be the default from settings


@pytest.mark.asyncio
async def test_enqueue_raises_when_queue_full(broker_session, monkeypatch):
    from app.core.broker import enqueue_task
    from app.config.settings import settings

    monkeypatch.setattr(settings, "max_queue_size", 2)

    await enqueue_task(prompt="task 1")
    await enqueue_task(prompt="task 2")

    with pytest.raises(ValueError, match="Queue full"):
        await enqueue_task(prompt="task 3")


# ── pick_next_task ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pick_next_task_returns_oldest_pending(broker_session):
    from app.core.broker import enqueue_task, pick_next_task

    t1 = await enqueue_task(prompt="first")
    t2 = await enqueue_task(prompt="second")

    picked = await pick_next_task()
    assert picked is not None
    assert picked.id == t1.id
    assert picked.status == TaskStatus.RUNNING
    assert picked.started_at is not None


@pytest.mark.asyncio
async def test_pick_next_task_returns_none_when_empty(broker_session):
    from app.core.broker import pick_next_task

    result = await pick_next_task()
    assert result is None


@pytest.mark.asyncio
async def test_pick_skips_running_tasks(broker_session):
    from app.core.broker import enqueue_task, pick_next_task

    await enqueue_task(prompt="first")
    await enqueue_task(prompt="second")

    # Pick first → now RUNNING
    first = await pick_next_task()
    assert first.prompt == "first"

    # Pick again → should get second, not first
    second = await pick_next_task()
    assert second is not None
    assert second.prompt == "second"


# ── complete_task ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_complete_task_success(broker_session):
    from app.core.broker import complete_task, enqueue_task, pick_next_task

    await enqueue_task(prompt="test task")
    task = await pick_next_task()

    updated = await complete_task(
        task_id=task.id,
        exit_code=0,
        output_summary="All good",
        full_output="Detailed output here",
    )

    assert updated is not None
    assert updated.status == TaskStatus.COMPLETED
    assert updated.exit_code == 0
    assert updated.output_summary == "All good"
    assert updated.completed_at is not None
    assert updated.duration_seconds is not None


@pytest.mark.asyncio
async def test_complete_task_failure(broker_session):
    from app.core.broker import complete_task, enqueue_task, pick_next_task

    await enqueue_task(prompt="bad task")
    task = await pick_next_task()

    updated = await complete_task(
        task_id=task.id,
        exit_code=1,
        output_summary="Error occurred",
        full_output="stack trace",
        error_message="Exit code 1",
    )

    assert updated.status == TaskStatus.FAILED
    assert updated.exit_code == 1
    assert updated.error_message == "Exit code 1"


@pytest.mark.asyncio
async def test_complete_nonexistent_task_returns_none(broker_session):
    from app.core.broker import complete_task

    result = await complete_task(
        task_id=9999, exit_code=0, output_summary="x", full_output="x"
    )
    assert result is None


# ── cancel_running_task ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_running_task(broker_session):
    from app.core.broker import cancel_running_task, enqueue_task, pick_next_task

    await enqueue_task(prompt="long task")
    await pick_next_task()  # mark as RUNNING

    cancelled = await cancel_running_task()
    assert cancelled is not None
    assert cancelled.status == TaskStatus.CANCELLED
    assert cancelled.completed_at is not None


@pytest.mark.asyncio
async def test_cancel_returns_none_when_nothing_running(broker_session):
    from app.core.broker import cancel_running_task

    result = await cancel_running_task()
    assert result is None


# ── query helpers ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_running_task(broker_session):
    from app.core.broker import enqueue_task, get_running_task, pick_next_task

    assert await get_running_task() is None
    await enqueue_task(prompt="a task")
    await pick_next_task()
    running = await get_running_task()
    assert running is not None
    assert running.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_get_pending_tasks(broker_session):
    from app.core.broker import enqueue_task, get_pending_tasks

    await enqueue_task(prompt="one")
    await enqueue_task(prompt="two")
    pending = await get_pending_tasks()
    assert len(pending) == 2


@pytest.mark.asyncio
async def test_get_recent_tasks_respects_limit(broker_session):
    from app.core.broker import enqueue_task, get_recent_tasks

    for i in range(5):
        await enqueue_task(prompt=f"task {i}")

    recent = await get_recent_tasks(limit=3)
    assert len(recent) == 3


@pytest.mark.asyncio
async def test_get_task_by_id(broker_session):
    from app.core.broker import enqueue_task, get_task_by_id

    task = await enqueue_task(prompt="find me")
    found = await get_task_by_id(task.id)
    assert found is not None
    assert found.prompt == "find me"


@pytest.mark.asyncio
async def test_get_task_by_id_not_found(broker_session):
    from app.core.broker import get_task_by_id

    assert await get_task_by_id(9999) is None


# ── recover_interrupted_tasks ───────────────────────────────────────

@pytest.mark.asyncio
async def test_recover_interrupted_tasks(broker_session):
    from app.core.broker import (
        enqueue_task,
        get_task_by_id,
        pick_next_task,
        recover_interrupted_tasks,
    )

    await enqueue_task(prompt="will be orphaned")
    task = await pick_next_task()  # now RUNNING

    # Simulate a crash — the task remains RUNNING
    count = await recover_interrupted_tasks()
    assert count == 1

    recovered = await get_task_by_id(task.id)
    assert recovered.status == TaskStatus.FAILED
    assert recovered.error_message is not None


# ── runner wake notification ────────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_calls_runner_wake(broker_session, monkeypatch):
    from app.core import broker as broker_mod
    from app.core.broker import enqueue_task

    called = False

    def mock_wake():
        nonlocal called
        called = True

    monkeypatch.setattr(broker_mod, "_runner_wake", mock_wake)
    await enqueue_task(prompt="wake test")
    assert called


@pytest.mark.asyncio
async def test_retry_calls_runner_wake(broker_session, monkeypatch):
    from app.core import broker as broker_mod
    from app.core.broker import complete_task, enqueue_task, pick_next_task, retry_task

    called = False

    def mock_wake():
        nonlocal called
        called = True

    await enqueue_task(prompt="will fail")
    task = await pick_next_task()
    await complete_task(
        task_id=task.id, exit_code=1,
        output_summary="err", full_output="err", error_message="fail",
    )

    monkeypatch.setattr(broker_mod, "_runner_wake", mock_wake)
    retried = await retry_task(task.id)
    assert retried is not None
    assert called


@pytest.mark.asyncio
async def test_enqueue_repeat_calls_runner_wake(broker_session, monkeypatch):
    from app.core import broker as broker_mod
    from app.core.broker import enqueue_repeat_task

    called = False

    def mock_wake():
        nonlocal called
        called = True

    monkeypatch.setattr(broker_mod, "_runner_wake", mock_wake)
    task = await enqueue_repeat_task(prompt="repeat wake", repeat_count=3)
    assert task is not None
    assert called

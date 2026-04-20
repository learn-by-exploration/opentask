"""Tests for Phase 1 fixes: purge bulk delete, cancel by ID, atomic complete."""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base, Task, TaskStatus, _utcnow


# ── Local fixture: isolated engine + monkeypatched broker ───────────

@pytest_asyncio.fixture
async def broker_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )

    async def _patched_get_session():
        return session_factory()

    import app.core.broker as broker_mod
    monkeypatch.setattr(broker_mod, "get_session", _patched_get_session)

    yield session_factory

    await engine.dispose()


# ── cancel_task_by_id ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_task_by_id_pending(broker_session):
    """Cancelling a PENDING task returns the task with CANCELLED status."""
    from app.core.broker import cancel_task_by_id, enqueue_task

    task = await enqueue_task(prompt="do stuff", chat_id=1)
    assert task.status == TaskStatus.PENDING

    cancelled = await cancel_task_by_id(task.id)
    assert cancelled is not None
    assert cancelled.id == task.id
    assert cancelled.status == TaskStatus.CANCELLED
    assert cancelled.completed_at is not None


@pytest.mark.asyncio
async def test_cancel_task_by_id_not_found(broker_session):
    """Cancelling a non-existent ID returns None."""
    from app.core.broker import cancel_task_by_id

    result = await cancel_task_by_id(99999)
    assert result is None


@pytest.mark.asyncio
async def test_cancel_task_by_id_not_pending(broker_session):
    """Cancelling a RUNNING task by ID returns None (only PENDING allowed)."""
    from app.core.broker import cancel_task_by_id, enqueue_task, pick_next_task

    task = await enqueue_task(prompt="do stuff", chat_id=1)
    running = await pick_next_task()
    assert running is not None
    assert running.status == TaskStatus.RUNNING

    result = await cancel_task_by_id(running.id)
    assert result is None


# ── complete_task atomic ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_complete_task_atomic(broker_session):
    """Completing a RUNNING task sets COMPLETED and duration."""
    from app.core.broker import complete_task, enqueue_task, pick_next_task

    task = await enqueue_task(prompt="build it", chat_id=1)
    running = await pick_next_task()
    assert running is not None

    completed = await complete_task(
        task_id=running.id,
        exit_code=0,
        output_summary="All good",
        full_output="detailed output",
    )
    assert completed is not None
    assert completed.status == TaskStatus.COMPLETED
    assert completed.exit_code == 0
    assert completed.output_summary == "All good"
    assert completed.completed_at is not None


@pytest.mark.asyncio
async def test_complete_task_already_cancelled(broker_session):
    """If a task was cancelled, complete_task should NOT overwrite to COMPLETED."""
    from app.core.broker import cancel_running_task, complete_task, enqueue_task, pick_next_task

    task = await enqueue_task(prompt="race me", chat_id=1)
    running = await pick_next_task()
    assert running is not None

    # Cancel the task (simulates /cancel racing with completion)
    cancelled = await cancel_running_task()
    assert cancelled is not None
    assert cancelled.status == TaskStatus.CANCELLED

    # Now try to complete the already-cancelled task
    result = await complete_task(
        task_id=running.id,
        exit_code=0,
        output_summary="done",
        full_output="output",
    )
    # Should return the task but NOT overwrite CANCELLED → COMPLETED
    assert result is not None
    assert result.status == TaskStatus.CANCELLED


# ── purge_old_tasks (bulk DELETE) ───────────────────────────────────

@pytest.mark.asyncio
async def test_purge_bulk_delete(broker_session):
    """Old completed tasks are purged via bulk DELETE."""
    from app.core.broker import purge_old_tasks

    session_factory = broker_session
    async with session_factory() as session, session.begin():
        old_time = _utcnow() - timedelta(days=10)
        for i in range(5):
            session.add(Task(
                prompt=f"old task {i}",
                project_dir="/tmp",
                agent="test",
                status=TaskStatus.COMPLETED,
                completed_at=old_time,
            ))

    count = await purge_old_tasks(days=7)
    assert count == 5

    # Verify they're gone
    async with session_factory() as session:
        result = await session.execute(select(Task))
        remaining = result.scalars().all()
        assert len(remaining) == 0


@pytest.mark.asyncio
async def test_purge_keeps_recent(broker_session):
    """Recent completed tasks are NOT purged."""
    from app.core.broker import purge_old_tasks

    session_factory = broker_session
    async with session_factory() as session, session.begin():
        recent_time = _utcnow() - timedelta(days=1)
        session.add(Task(
            prompt="recent task",
            project_dir="/tmp",
            agent="test",
            status=TaskStatus.COMPLETED,
            completed_at=recent_time,
        ))

    count = await purge_old_tasks(days=7)
    assert count == 0

    # Verify it's still there
    async with session_factory() as session:
        result = await session.execute(select(Task))
        remaining = result.scalars().all()
        assert len(remaining) == 1

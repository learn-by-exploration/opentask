"""Tests for task chains and repeat features."""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base, ChainStatus, Task, TaskStatus, _utcnow


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


# ── Repeat: enqueue_repeat_task ─────────────────────────────────────

@pytest.mark.asyncio
async def test_enqueue_repeat_task_with_count(broker_session):
    from app.core.broker import enqueue_repeat_task

    task = await enqueue_repeat_task(
        prompt="run tests", repeat_count=5, project_dir="/tmp",
    )
    assert task.repeat_total == 5
    assert task.repeat_remaining == 5
    assert task.repeat_until is None
    assert task.status == TaskStatus.PENDING


@pytest.mark.asyncio
async def test_enqueue_repeat_task_with_until(broker_session):
    from app.core.broker import enqueue_repeat_task

    deadline = _utcnow() + timedelta(hours=2)
    task = await enqueue_repeat_task(
        prompt="health check", repeat_until=deadline,
    )
    assert task.repeat_until == deadline
    assert task.repeat_total is None
    assert task.repeat_remaining is None


@pytest.mark.asyncio
async def test_enqueue_repeat_task_with_both(broker_session):
    from app.core.broker import enqueue_repeat_task

    deadline = _utcnow() + timedelta(hours=1)
    task = await enqueue_repeat_task(
        prompt="stress test", repeat_count=10, repeat_until=deadline,
    )
    assert task.repeat_total == 10
    assert task.repeat_remaining == 10
    assert task.repeat_until == deadline


@pytest.mark.asyncio
async def test_enqueue_repeat_task_requires_at_least_one(broker_session):
    from app.core.broker import enqueue_repeat_task

    with pytest.raises(ValueError, match="Provide repeat_count"):
        await enqueue_repeat_task(prompt="no repeat param")


@pytest.mark.asyncio
async def test_enqueue_repeat_task_count_must_be_positive(broker_session):
    from app.core.broker import enqueue_repeat_task

    with pytest.raises(ValueError, match="must be >= 1"):
        await enqueue_repeat_task(prompt="bad", repeat_count=0)


# ── Repeat: maybe_reenqueue ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_maybe_reenqueue_decrements_remaining(broker_session):
    from app.core.broker import enqueue_repeat_task, maybe_reenqueue, pick_next_task, complete_task

    task = await enqueue_repeat_task(prompt="qa loop", repeat_count=3)
    picked = await pick_next_task()
    completed = await complete_task(picked.id, 0, "ok", "output")

    requeued = await maybe_reenqueue(completed)
    assert requeued is not None
    assert requeued.repeat_remaining == 2
    assert requeued.repeat_total == 3
    assert requeued.status == TaskStatus.PENDING


@pytest.mark.asyncio
async def test_maybe_reenqueue_stops_at_zero(broker_session):
    from app.core.broker import enqueue_repeat_task, maybe_reenqueue, pick_next_task, complete_task

    task = await enqueue_repeat_task(prompt="final run", repeat_count=1)
    picked = await pick_next_task()
    completed = await complete_task(picked.id, 0, "ok", "output")

    requeued = await maybe_reenqueue(completed)
    assert requeued is None


@pytest.mark.asyncio
async def test_maybe_reenqueue_respects_deadline(broker_session):
    from app.core.broker import enqueue_repeat_task, maybe_reenqueue, pick_next_task, complete_task

    future = _utcnow() + timedelta(hours=1)
    task = await enqueue_repeat_task(prompt="until deadline", repeat_until=future)
    picked = await pick_next_task()
    completed = await complete_task(picked.id, 0, "ok", "output")

    requeued = await maybe_reenqueue(completed)
    assert requeued is not None
    assert requeued.repeat_until == future


@pytest.mark.asyncio
async def test_maybe_reenqueue_stops_after_deadline(broker_session):
    """Deadline-only repeat: past deadline should NOT re-enqueue."""
    pass  # Covered by test_maybe_reenqueue_deadline_only_past


@pytest.mark.asyncio
async def test_maybe_reenqueue_deadline_only_past(broker_session):
    from app.core.broker import enqueue_repeat_task, maybe_reenqueue, pick_next_task, complete_task

    past = _utcnow() - timedelta(hours=1)
    task = await enqueue_repeat_task(prompt="expired", repeat_until=past)
    picked = await pick_next_task()
    completed = await complete_task(picked.id, 0, "ok", "output")

    requeued = await maybe_reenqueue(completed)
    assert requeued is None


@pytest.mark.asyncio
async def test_maybe_reenqueue_no_repeat_fields(broker_session):
    """Normal tasks without repeat fields should not re-enqueue."""
    from app.core.broker import enqueue_task, maybe_reenqueue, pick_next_task, complete_task

    task = await enqueue_task(prompt="one-shot")
    picked = await pick_next_task()
    completed = await complete_task(picked.id, 0, "ok", "output")

    requeued = await maybe_reenqueue(completed)
    assert requeued is None


# ── Chain: save_chain ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_chain_basic(broker_session):
    from app.core.broker import save_chain

    chain = await save_chain(
        name="deploy",
        steps=[
            {"prompt": "run tests"},
            {"prompt": "build"},
            {"prompt": "deploy"},
        ],
    )
    assert chain.name == "deploy"
    assert chain.total_steps == 3
    assert chain.status == ChainStatus.IDLE
    assert chain.steps[0]["prompt"] == "run tests"


@pytest.mark.asyncio
async def test_save_chain_upserts(broker_session):
    from app.core.broker import save_chain, get_chain_by_name

    await save_chain(name="test", steps=[{"prompt": "v1"}])
    await save_chain(name="test", steps=[{"prompt": "v2"}, {"prompt": "v3"}])

    chain = await get_chain_by_name("test")
    assert chain.total_steps == 2
    assert chain.steps[0]["prompt"] == "v2"


@pytest.mark.asyncio
async def test_save_chain_validates_empty(broker_session):
    from app.core.broker import save_chain

    with pytest.raises(ValueError, match="at least one"):
        await save_chain(name="empty", steps=[])


@pytest.mark.asyncio
async def test_save_chain_validates_missing_prompt(broker_session):
    from app.core.broker import save_chain

    with pytest.raises(ValueError, match="missing 'prompt'"):
        await save_chain(name="bad", steps=[{"agent": "opencode"}])


# ── Chain: list + get + delete ──────────────────────────────────────

@pytest.mark.asyncio
async def test_list_chains(broker_session):
    from app.core.broker import save_chain, list_chains

    await save_chain(name="a", steps=[{"prompt": "x"}])
    await save_chain(name="b", steps=[{"prompt": "y"}])

    chains = await list_chains()
    assert len(chains) == 2


@pytest.mark.asyncio
async def test_get_chain_by_name(broker_session):
    from app.core.broker import save_chain, get_chain_by_name

    await save_chain(name="findme", steps=[{"prompt": "x"}])
    found = await get_chain_by_name("findme")
    assert found is not None
    assert found.name == "findme"


@pytest.mark.asyncio
async def test_get_chain_by_name_not_found(broker_session):
    from app.core.broker import get_chain_by_name

    assert await get_chain_by_name("nope") is None


@pytest.mark.asyncio
async def test_delete_chain(broker_session):
    from app.core.broker import save_chain, delete_chain, get_chain_by_name

    await save_chain(name="todelete", steps=[{"prompt": "x"}])
    assert await delete_chain("todelete") is True
    assert await get_chain_by_name("todelete") is None


@pytest.mark.asyncio
async def test_delete_chain_not_found(broker_session):
    from app.core.broker import delete_chain

    assert await delete_chain("nope") is False


# ── Chain: start_chain ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_chain_enqueues_first_step(broker_session):
    from app.core.broker import save_chain, start_chain, get_chain_by_name

    await save_chain(
        name="ci",
        steps=[{"prompt": "lint"}, {"prompt": "test"}, {"prompt": "deploy"}],
    )

    task = await start_chain("ci", chat_id=12345)
    assert task is not None
    assert task.prompt == "lint"
    assert task.chain_step == 0
    assert task.status == TaskStatus.PENDING

    chain = await get_chain_by_name("ci")
    assert chain.status == ChainStatus.RUNNING
    assert chain.current_step == 0


@pytest.mark.asyncio
async def test_start_chain_not_found(broker_session):
    from app.core.broker import start_chain

    assert await start_chain("nope") is None


# ── Chain: advance_chain ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_advance_chain_to_next_step(broker_session):
    from app.core.broker import save_chain, start_chain, advance_chain, pick_next_task, complete_task

    await save_chain(
        name="pipeline",
        steps=[{"prompt": "step1"}, {"prompt": "step2"}, {"prompt": "step3"}],
    )

    first = await start_chain("pipeline")
    picked = await pick_next_task()
    completed = await complete_task(picked.id, 0, "ok", "output")

    next_task = await advance_chain(completed)
    assert next_task is not None
    assert next_task.prompt == "step2"
    assert next_task.chain_step == 1


@pytest.mark.asyncio
async def test_advance_chain_completes_at_end(broker_session):
    from app.core.broker import (
        save_chain, start_chain, advance_chain, pick_next_task,
        complete_task, get_chain_by_name,
    )

    await save_chain(name="short", steps=[{"prompt": "only"}])

    await start_chain("short")
    picked = await pick_next_task()
    completed = await complete_task(picked.id, 0, "ok", "output")

    next_task = await advance_chain(completed)
    assert next_task is None

    chain = await get_chain_by_name("short")
    assert chain.status == ChainStatus.COMPLETED


@pytest.mark.asyncio
async def test_advance_chain_fails_on_task_failure(broker_session):
    from app.core.broker import (
        save_chain, start_chain, advance_chain, pick_next_task,
        complete_task, get_chain_by_name,
    )

    await save_chain(
        name="fragile",
        steps=[{"prompt": "risky"}, {"prompt": "never reaches here"}],
    )

    await start_chain("fragile")
    picked = await pick_next_task()
    completed = await complete_task(picked.id, 1, "err", "trace", "failed")

    next_task = await advance_chain(completed)
    assert next_task is None

    chain = await get_chain_by_name("fragile")
    assert chain.status == ChainStatus.FAILED


@pytest.mark.asyncio
async def test_advance_chain_full_run(broker_session):
    """Run a 3-step chain from start to completion."""
    from app.core.broker import (
        save_chain, start_chain, advance_chain, pick_next_task,
        complete_task, get_chain_by_name,
    )

    await save_chain(
        name="full",
        steps=[{"prompt": "s1"}, {"prompt": "s2"}, {"prompt": "s3"}],
    )

    await start_chain("full")

    for i, expected_prompt in enumerate(["s1", "s2", "s3"]):
        picked = await pick_next_task()
        assert picked is not None, f"Step {i} should have a pending task"
        assert picked.prompt == expected_prompt
        completed = await complete_task(picked.id, 0, "ok", "output")
        next_task = await advance_chain(completed)

        if i < 2:
            assert next_task is not None
        else:
            assert next_task is None

    chain = await get_chain_by_name("full")
    assert chain.status == ChainStatus.COMPLETED


@pytest.mark.asyncio
async def test_advance_chain_non_chain_task_returns_none(broker_session):
    """A task without chain_id should not trigger chain advancement."""
    from app.core.broker import enqueue_task, advance_chain, pick_next_task, complete_task

    task = await enqueue_task(prompt="solo")
    picked = await pick_next_task()
    completed = await complete_task(picked.id, 0, "ok", "output")

    result = await advance_chain(completed)
    assert result is None


# ── Phase 1 fixes ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_task_preserves_cancelled_status(broker_session):
    """complete_task should NOT overwrite CANCELLED with FAILED."""
    from app.core.broker import cancel_running_task, enqueue_task, pick_next_task, complete_task

    task = await enqueue_task(prompt="will be cancelled")
    await pick_next_task()
    cancelled = await cancel_running_task()
    assert cancelled.status == TaskStatus.CANCELLED

    # Runner calls complete_task after subprocess dies
    result = await complete_task(cancelled.id, -15, "killed", "", "SIGTERM")
    assert result is not None
    assert result.status == TaskStatus.CANCELLED  # NOT FAILED


@pytest.mark.asyncio
async def test_maybe_reenqueue_skips_failed_task(broker_session):
    """Failed tasks should NOT be re-enqueued even if repeat_remaining > 1."""
    from app.core.broker import enqueue_repeat_task, maybe_reenqueue, pick_next_task, complete_task

    task = await enqueue_repeat_task(prompt="flaky", repeat_count=5)
    picked = await pick_next_task()
    # Simulate failure (exit code 1)
    completed = await complete_task(picked.id, 1, "err", "trace", "failed")

    requeued = await maybe_reenqueue(completed)
    assert requeued is None


@pytest.mark.asyncio
async def test_maybe_reenqueue_count_one_until_future(broker_session):
    """Edge case: repeat_count=1 (exhausted) but repeat_until in future.

    Should STILL re-enqueue because the deadline hasn't passed.
    """
    from app.core.broker import enqueue_repeat_task, maybe_reenqueue, pick_next_task, complete_task

    future = _utcnow() + timedelta(hours=2)
    task = await enqueue_repeat_task(
        prompt="edge case", repeat_count=1, repeat_until=future,
    )
    picked = await pick_next_task()
    completed = await complete_task(picked.id, 0, "ok", "output")

    requeued = await maybe_reenqueue(completed)
    assert requeued is not None
    assert requeued.repeat_remaining == 0  # count exhausted
    assert requeued.repeat_until == future  # deadline still active


@pytest.mark.asyncio
async def test_start_chain_rejects_already_running(broker_session):
    """Starting a chain that's already RUNNING should raise ValueError."""
    from app.core.broker import save_chain, start_chain

    await save_chain(name="ci", steps=[{"prompt": "lint"}, {"prompt": "test"}])
    await start_chain("ci", chat_id=1)

    with pytest.raises(ValueError, match="already running"):
        await start_chain("ci", chat_id=1)


# ── Phase 2 fixes ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recover_interrupted_chains(broker_session):
    """RUNNING chains should be recovered (→ FAILED) on startup."""
    from app.core.broker import save_chain, start_chain, recover_interrupted_chains, get_chain_by_name

    await save_chain(name="orphan", steps=[{"prompt": "x"}, {"prompt": "y"}])
    await start_chain("orphan")

    chain = await get_chain_by_name("orphan")
    assert chain.status == ChainStatus.RUNNING

    count = await recover_interrupted_chains()
    assert count == 1

    chain = await get_chain_by_name("orphan")
    assert chain.status == ChainStatus.FAILED
    assert chain.completed_at is not None


@pytest.mark.asyncio
async def test_recover_interrupted_chains_skips_idle(broker_session):
    """IDLE chains should NOT be affected by recovery."""
    from app.core.broker import save_chain, recover_interrupted_chains, get_chain_by_name

    await save_chain(name="idle", steps=[{"prompt": "x"}])
    count = await recover_interrupted_chains()
    assert count == 0

    chain = await get_chain_by_name("idle")
    assert chain.status == ChainStatus.IDLE


# ── Phase 3 fixes ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_purge_old_tasks(broker_session):
    """purge_old_tasks should delete old terminal tasks, keep recent ones."""
    from app.core.broker import enqueue_task, pick_next_task, complete_task, purge_old_tasks, get_recent_tasks

    task = await enqueue_task(prompt="old task")
    picked = await pick_next_task()
    completed = await complete_task(picked.id, 0, "ok", "output")

    # Hack completed_at to 60 days ago
    factory = broker_session
    async with factory() as session, session.begin():
        from sqlalchemy import select as sel
        from app.core.models import Task as T
        result = await session.execute(sel(T).where(T.id == completed.id))
        t = result.scalar_one()
        t.completed_at = _utcnow() - timedelta(days=60)

    # Purge tasks older than 30 days
    count = await purge_old_tasks(days=30)
    assert count == 1

    tasks = await get_recent_tasks()
    assert len(tasks) == 0


@pytest.mark.asyncio
async def test_purge_old_tasks_keeps_recent(broker_session):
    """Tasks completed within the retention window should NOT be purged."""
    from app.core.broker import enqueue_task, pick_next_task, complete_task, purge_old_tasks, get_recent_tasks

    task = await enqueue_task(prompt="recent")
    picked = await pick_next_task()
    await complete_task(picked.id, 0, "ok", "output")

    count = await purge_old_tasks(days=30)
    assert count == 0

    tasks = await get_recent_tasks()
    assert len(tasks) == 1

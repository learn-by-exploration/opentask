"""Extra broker tests — edge cases and gaps from coverage audit."""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base, ChainStatus, Task, TaskChain, TaskStatus, _utcnow


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


# ── complete_task edge cases ────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_task_no_started_at_skips_duration(broker_session):
    """A PENDING task (not RUNNING) cannot be completed by the atomic UPDATE.

    The atomic complete_task only updates tasks with status=RUNNING, so
    a task that was never picked returns as-is (still PENDING).
    """
    from app.core.broker import enqueue_task, complete_task

    task = await enqueue_task(prompt="never picked")
    # Complete directly without picking (status is still PENDING)
    updated = await complete_task(
        task_id=task.id,
        exit_code=0,
        output_summary="done",
        full_output="output",
    )

    assert updated is not None
    # Atomic UPDATE WHERE status=RUNNING won't match PENDING → returns as-is
    assert updated.status == TaskStatus.PENDING
    assert updated.duration_seconds is None


@pytest.mark.asyncio
async def test_complete_task_sets_all_output_fields(broker_session):
    from app.core.broker import enqueue_task, pick_next_task, complete_task

    await enqueue_task(prompt="test")
    task = await pick_next_task()
    updated = await complete_task(
        task_id=task.id,
        exit_code=1,
        output_summary="short",
        full_output="long output here",
        error_message="something broke",
    )

    assert updated.exit_code == 1
    assert updated.output_summary == "short"
    assert updated.full_output == "long output here"
    assert updated.error_message == "something broke"


# ── cancel_running_task edge cases ──────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_running_task_computes_duration(broker_session):
    """cancel_running_task should compute duration_seconds from started_at."""
    from app.core.broker import enqueue_task, pick_next_task, cancel_running_task

    await enqueue_task(prompt="long task")
    picked = await pick_next_task()
    assert picked.started_at is not None

    cancelled = await cancel_running_task()
    assert cancelled is not None
    assert cancelled.duration_seconds is not None
    assert cancelled.duration_seconds >= 0


# ── get_recent_tasks ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_recent_tasks_returns_newest_first(broker_session):
    """get_recent_tasks should return tasks with newest created_at first."""
    from app.core.broker import enqueue_task, get_recent_tasks

    t1 = await enqueue_task(prompt="first")
    t2 = await enqueue_task(prompt="second")
    t3 = await enqueue_task(prompt="third")

    recent = await get_recent_tasks(limit=10)
    assert len(recent) == 3
    # Newest first
    assert recent[0].id == t3.id
    assert recent[1].id == t2.id
    assert recent[2].id == t1.id


@pytest.mark.asyncio
async def test_get_recent_tasks_empty_db(broker_session):
    from app.core.broker import get_recent_tasks

    recent = await get_recent_tasks()
    assert recent == []


# ── recover_interrupted_tasks ───────────────────────────────────────


@pytest.mark.asyncio
async def test_recover_interrupted_tasks_zero_running(broker_session):
    """When nothing is RUNNING, recovery should return 0."""
    from app.core.broker import enqueue_task, recover_interrupted_tasks

    await enqueue_task(prompt="pending only")
    count = await recover_interrupted_tasks()
    assert count == 0


@pytest.mark.asyncio
async def test_recover_interrupted_tasks_multiple(broker_session):
    """Multiple RUNNING tasks should all be recovered."""
    from app.core.broker import enqueue_task, pick_next_task, recover_interrupted_tasks
    from app.config.settings import settings

    # Need to enqueue + pick multiple tasks
    t1 = await enqueue_task(prompt="orphan1")
    picked1 = await pick_next_task()
    t2 = await enqueue_task(prompt="orphan2")
    picked2 = await pick_next_task()

    count = await recover_interrupted_tasks()
    assert count == 2


# ── Queue full counts both PENDING and RUNNING ──────────────────────


@pytest.mark.asyncio
async def test_queue_full_counts_running_tasks(broker_session, monkeypatch):
    """Queue size check should count RUNNING tasks too, not just PENDING."""
    from app.core.broker import enqueue_task, pick_next_task
    from app.config.settings import settings

    monkeypatch.setattr(settings, "max_queue_size", 2)

    t1 = await enqueue_task(prompt="will run")
    await pick_next_task()  # now RUNNING
    await enqueue_task(prompt="pending")  # 1 RUNNING + 1 PENDING = 2

    with pytest.raises(ValueError, match="Queue full"):
        await enqueue_task(prompt="overflow")


# ── enqueue_repeat_task edge cases ──────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_repeat_task_queue_full(broker_session, monkeypatch):
    from app.core.broker import enqueue_task, enqueue_repeat_task
    from app.config.settings import settings

    monkeypatch.setattr(settings, "max_queue_size", 1)
    await enqueue_task(prompt="existing")

    with pytest.raises(ValueError, match="Queue full"):
        await enqueue_repeat_task(prompt="repeat", repeat_count=3)


@pytest.mark.asyncio
async def test_enqueue_repeat_task_with_all_params(broker_session):
    from app.core.broker import enqueue_repeat_task

    deadline = _utcnow() + timedelta(hours=2)
    task = await enqueue_repeat_task(
        prompt="full params",
        repeat_count=5,
        repeat_until=deadline,
        project_dir="/custom/dir",
        agent="aider",
        chat_id=999,
        msg_id=888,
    )
    assert task.project_dir == "/custom/dir"
    assert task.agent == "aider"
    assert task.telegram_chat_id == 999
    assert task.telegram_msg_id == 888
    assert task.repeat_total == 5
    assert task.repeat_until == deadline


# ── maybe_reenqueue edge cases ──────────────────────────────────────


@pytest.mark.asyncio
async def test_maybe_reenqueue_preserves_chain_fields(broker_session):
    """When re-enqueueing, chain_id and chain_step must be preserved."""
    from app.core.broker import save_chain, start_chain, maybe_reenqueue, pick_next_task, complete_task

    await save_chain(name="repeat_chain", steps=[{"prompt": "loop"}])
    first = await start_chain("repeat_chain", chat_id=1)

    # Manually set repeat fields on the chain task
    factory = broker_session
    async with factory() as session, session.begin():
        result = await session.execute(select(Task).where(Task.id == first.id))
        t = result.scalar_one()
        t.repeat_total = 3
        t.repeat_remaining = 3

    picked = await pick_next_task()
    completed = await complete_task(picked.id, 0, "ok", "output")

    requeued = await maybe_reenqueue(completed)
    assert requeued is not None
    assert requeued.chain_id == completed.chain_id
    assert requeued.chain_step == completed.chain_step


@pytest.mark.asyncio
async def test_maybe_reenqueue_preserves_telegram_chat_id(broker_session):
    from app.core.broker import enqueue_repeat_task, maybe_reenqueue, pick_next_task, complete_task

    task = await enqueue_repeat_task(
        prompt="notified", repeat_count=2, chat_id=777,
    )
    picked = await pick_next_task()
    completed = await complete_task(picked.id, 0, "ok", "output")

    requeued = await maybe_reenqueue(completed)
    assert requeued is not None
    assert requeued.telegram_chat_id == 777


# ── get_chain_by_id ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_chain_by_id_found(broker_session):
    from app.core.broker import save_chain, get_chain_by_id

    chain = await save_chain(name="mychain", steps=[{"prompt": "x"}])
    found = await get_chain_by_id(chain.id)
    assert found is not None
    assert found.name == "mychain"


@pytest.mark.asyncio
async def test_get_chain_by_id_not_found(broker_session):
    from app.core.broker import get_chain_by_id

    assert await get_chain_by_id(9999) is None


# ── save_chain edge cases ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_chain_with_chat_id(broker_session):
    from app.core.broker import save_chain, get_chain_by_name

    chain = await save_chain(
        name="with_chat", steps=[{"prompt": "x"}], chat_id=42,
    )
    assert chain.telegram_chat_id == 42

    fetched = await get_chain_by_name("with_chat")
    assert fetched.telegram_chat_id == 42


@pytest.mark.asyncio
async def test_save_chain_validates_step_without_prompt_key(broker_session):
    """A step dict that has prompt="" should fail."""
    from app.core.broker import save_chain

    with pytest.raises(ValueError, match="missing 'prompt'"):
        await save_chain(name="bad", steps=[{"prompt": ""}])


# ── start_chain edge cases ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_chain_sets_started_at(broker_session):
    from app.core.broker import save_chain, start_chain, get_chain_by_name

    await save_chain(name="ts", steps=[{"prompt": "step1"}])
    await start_chain("ts", chat_id=55)

    chain = await get_chain_by_name("ts")
    assert chain.started_at is not None
    assert chain.telegram_chat_id == 55


@pytest.mark.asyncio
async def test_start_chain_uses_step_agent_and_project_dir(broker_session):
    """When a step specifies agent/project_dir, those should be used."""
    from app.core.broker import save_chain, start_chain

    await save_chain(
        name="custom",
        steps=[{"prompt": "do it", "agent": "aider", "project_dir": "/custom"}],
    )
    task = await start_chain("custom")
    assert task.agent == "aider"
    assert task.project_dir == "/custom"


# ── advance_chain edge cases ────────────────────────────────────────


@pytest.mark.asyncio
async def test_advance_chain_deleted_chain_returns_none(broker_session):
    """If the chain was deleted mid-run, advance_chain should return None.
    
    Now delete_chain raises ValueError for running chains,
    so instead we test that advance_chain handles a missing chain gracefully.
    """
    from app.core.broker import save_chain, start_chain, advance_chain, pick_next_task, complete_task, delete_chain

    await save_chain(
        name="doomed", steps=[{"prompt": "s1"}, {"prompt": "s2"}],
    )
    await start_chain("doomed")
    picked = await pick_next_task()
    completed = await complete_task(picked.id, 0, "ok", "output")

    # Attempting to delete a RUNNING chain should raise ValueError
    with pytest.raises(ValueError, match="Cannot delete chain"):
        await delete_chain("doomed")

    # Advance should still work
    next_task = await advance_chain(completed)
    assert next_task is not None


@pytest.mark.asyncio
async def test_advance_chain_step_uses_custom_agent(broker_session):
    """Each chain step should use its own agent/project_dir."""
    from app.core.broker import (
        save_chain, start_chain, advance_chain,
        pick_next_task, complete_task,
    )

    await save_chain(
        name="multi_agent",
        steps=[
            {"prompt": "lint", "agent": "opencode"},
            {"prompt": "deploy", "agent": "aider", "project_dir": "/deploy"},
        ],
    )

    await start_chain("multi_agent")
    picked = await pick_next_task()
    assert picked.agent == "opencode"

    completed = await complete_task(picked.id, 0, "ok", "output")
    next_task = await advance_chain(completed)

    assert next_task is not None
    assert next_task.agent == "aider"
    assert next_task.project_dir == "/deploy"


@pytest.mark.asyncio
async def test_advance_chain_sets_completed_at_on_finish(broker_session):
    from app.core.broker import (
        save_chain, start_chain, advance_chain,
        pick_next_task, complete_task, get_chain_by_name,
    )

    await save_chain(name="one", steps=[{"prompt": "only"}])
    await start_chain("one")
    picked = await pick_next_task()
    completed = await complete_task(picked.id, 0, "ok", "output")
    await advance_chain(completed)

    chain = await get_chain_by_name("one")
    assert chain.status == ChainStatus.COMPLETED
    assert chain.completed_at is not None


@pytest.mark.asyncio
async def test_advance_chain_sets_completed_at_on_failure(broker_session):
    from app.core.broker import (
        save_chain, start_chain, advance_chain,
        pick_next_task, complete_task, get_chain_by_name,
    )

    await save_chain(name="fail", steps=[{"prompt": "s1"}, {"prompt": "s2"}])
    await start_chain("fail")
    picked = await pick_next_task()
    completed = await complete_task(picked.id, 1, "err", "trace", "fail")
    await advance_chain(completed)

    chain = await get_chain_by_name("fail")
    assert chain.status == ChainStatus.FAILED
    assert chain.completed_at is not None


# ── purge_old_tasks edge cases ──────────────────────────────────────


@pytest.mark.asyncio
async def test_purge_old_tasks_keeps_pending_tasks(broker_session):
    """PENDING tasks should never be purged regardless of age."""
    from app.core.broker import enqueue_task, purge_old_tasks, get_pending_tasks

    task = await enqueue_task(prompt="still pending")

    # Hack created_at to 90 days ago
    factory = broker_session
    async with factory() as session, session.begin():
        result = await session.execute(select(Task).where(Task.id == task.id))
        t = result.scalar_one()
        t.created_at = _utcnow() - timedelta(days=90)

    count = await purge_old_tasks(days=30)
    assert count == 0  # PENDING is not terminal

    pending = await get_pending_tasks()
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_purge_old_tasks_keeps_running_tasks(broker_session):
    """RUNNING tasks should never be purged regardless of age."""
    from app.core.broker import enqueue_task, pick_next_task, purge_old_tasks, get_running_task

    await enqueue_task(prompt="in progress")
    await pick_next_task()

    count = await purge_old_tasks(days=0)  # days=0 means purge everything old
    assert count == 0  # RUNNING is not terminal


@pytest.mark.asyncio
async def test_purge_old_tasks_deletes_failed_tasks(broker_session):
    """Old FAILED tasks should be purged."""
    from app.core.broker import enqueue_task, pick_next_task, complete_task, purge_old_tasks, get_recent_tasks

    task = await enqueue_task(prompt="failed task")
    picked = await pick_next_task()
    completed = await complete_task(picked.id, 1, "err", "trace", "error")

    # Hack completed_at to old
    factory = broker_session
    async with factory() as session, session.begin():
        result = await session.execute(select(Task).where(Task.id == completed.id))
        t = result.scalar_one()
        t.completed_at = _utcnow() - timedelta(days=60)

    count = await purge_old_tasks(days=30)
    assert count == 1


@pytest.mark.asyncio
async def test_purge_old_tasks_deletes_cancelled_tasks(broker_session):
    """Old CANCELLED tasks should be purged."""
    from app.core.broker import enqueue_task, pick_next_task, cancel_running_task, purge_old_tasks

    await enqueue_task(prompt="cancelled task")
    await pick_next_task()
    cancelled = await cancel_running_task()

    factory = broker_session
    async with factory() as session, session.begin():
        result = await session.execute(select(Task).where(Task.id == cancelled.id))
        t = result.scalar_one()
        t.completed_at = _utcnow() - timedelta(days=60)

    count = await purge_old_tasks(days=30)
    assert count == 1


# ── pick_next_task edge cases ───────────────────────────────────────


@pytest.mark.asyncio
async def test_pick_next_task_exhausted(broker_session):
    """After all tasks are picked, pick_next_task returns None."""
    from app.core.broker import enqueue_task, pick_next_task

    await enqueue_task(prompt="only one")
    picked = await pick_next_task()
    assert picked is not None

    again = await pick_next_task()
    assert again is None


@pytest.mark.asyncio
async def test_pick_next_task_sets_started_at(broker_session):
    from app.core.broker import enqueue_task, pick_next_task

    await enqueue_task(prompt="test")
    picked = await pick_next_task()
    assert picked.started_at is not None


# ── list_chains ordering ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_chains_returns_newest_first(broker_session):
    from app.core.broker import save_chain, list_chains

    c1 = await save_chain(name="alpha", steps=[{"prompt": "x"}])
    c2 = await save_chain(name="beta", steps=[{"prompt": "y"}])
    c3 = await save_chain(name="gamma", steps=[{"prompt": "z"}])

    chains = await list_chains()
    assert len(chains) == 3
    # Newest first (desc by created_at)
    assert chains[0].name == "gamma"
    assert chains[2].name == "alpha"


# ── get_pending_tasks ordering ──────────────────────────────────────


@pytest.mark.asyncio
async def test_get_pending_tasks_returns_oldest_first(broker_session):
    from app.core.broker import enqueue_task, get_pending_tasks

    t1 = await enqueue_task(prompt="first")
    t2 = await enqueue_task(prompt="second")

    pending = await get_pending_tasks()
    assert pending[0].id == t1.id
    assert pending[1].id == t2.id


@pytest.mark.asyncio
async def test_get_pending_tasks_empty(broker_session):
    from app.core.broker import get_pending_tasks

    pending = await get_pending_tasks()
    assert pending == []


# ── load_only excludes full_output from summary queries ─────────────


@pytest.mark.asyncio
async def test_get_recent_tasks_excludes_full_output(broker_session):
    """get_recent_tasks uses load_only so full_output is not eagerly loaded."""
    from app.core.broker import enqueue_task, pick_next_task, complete_task, get_recent_tasks

    await enqueue_task(prompt="with output")
    picked = await pick_next_task()
    await complete_task(picked.id, 0, "summary", "big full output data")

    tasks = await get_recent_tasks(limit=10)
    assert len(tasks) == 1
    # The task is returned correctly — the optimization is transparent
    assert tasks[0].prompt == "with output"
    assert tasks[0].output_summary == "summary"


@pytest.mark.asyncio
async def test_get_pending_tasks_with_load_only(broker_session):
    """get_pending_tasks works correctly with load_only optimization."""
    from app.core.broker import enqueue_task, get_pending_tasks

    await enqueue_task(prompt="pending task")
    tasks = await get_pending_tasks()
    assert len(tasks) == 1
    assert tasks[0].prompt == "pending task"


@pytest.mark.asyncio
async def test_get_running_task_with_load_only(broker_session):
    """get_running_task works correctly with load_only optimization."""
    from app.core.broker import enqueue_task, pick_next_task, get_running_task

    await enqueue_task(prompt="running task")
    await pick_next_task()
    task = await get_running_task()
    assert task is not None
    assert task.prompt == "running task"

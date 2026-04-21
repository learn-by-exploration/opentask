"""Task broker — CRUD operations for the task queue."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import delete as sa_delete, func, select, update
from sqlalchemy.orm import load_only

from app.config.settings import settings
from app.core.db import get_session
from app.core.models import ChatPrefs, ChainStatus, Recipe, Task, TaskChain, TaskStatus, _utcnow

logger = logging.getLogger(__name__)

_TASK_SUMMARY_COLUMNS = (
    Task.id, Task.prompt, Task.project_dir, Task.agent, Task.status,
    Task.output_summary, Task.exit_code, Task.error_message,
    Task.telegram_chat_id, Task.telegram_msg_id,
    Task.chain_id, Task.chain_step,
    Task.repeat_total, Task.repeat_remaining, Task.repeat_until,
    Task.assigned_to, Task.worker_id, Task.heartbeat_at,
    Task.priority, Task.retry_count, Task.max_retries, Task.git_diff,
    Task.model,
    Task.created_at, Task.started_at, Task.completed_at, Task.duration_seconds,
)

_runner_wake: Callable[[], None] | None = None
_on_worker_complete: Callable[[Task], Any] | None = None


def set_runner_wake(fn: Callable[[], None] | None) -> None:
    """Register a callback to wake the runner when a new task is enqueued."""
    global _runner_wake
    _runner_wake = fn


def set_worker_complete_callback(fn: Callable[[Task], Any] | None) -> None:
    """Register a callback for when a remote worker completes a task.

    This mirrors the runner's _after_complete — it triggers Telegram
    notifications, chain advancement, repeat re-enqueue, and auto-retry.
    """
    global _on_worker_complete
    _on_worker_complete = fn


async def enqueue_task(
    prompt: str,
    project_dir: str | None = None,
    agent: str | None = None,
    chat_id: int | None = None,
    msg_id: int | None = None,
    model: str | None = None,
    assigned_to: str | None = None,
) -> Task:
    """Add a new task to the queue. Raises ValueError if the queue is full.

    If *assigned_to* is set, only a remote worker with that id can claim it.
    """
    session = await get_session()
    async with session, session.begin():
        count_result = await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
        )
        active_count = count_result.scalar() or 0
        if active_count >= settings.max_queue_size:
            raise ValueError(
                f"Queue full ({active_count}/{settings.max_queue_size})"
            )

        task = Task(
            prompt=prompt,
            project_dir=project_dir or settings.default_project_dir,
            agent=agent or settings.default_agent,
            model=model or settings.default_model or None,
            assigned_to=assigned_to,
            status=TaskStatus.PENDING,
            telegram_chat_id=chat_id,
            telegram_msg_id=msg_id,
        )
        session.add(task)
        await session.flush()
        await session.refresh(task)
    if _runner_wake:
        _runner_wake()
    return task


async def switch_task_agent(task_id: int, new_agent: str) -> Task | None:
    """Switch the agent for a PENDING task. Returns updated task or None."""
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            select(Task).where(Task.id == task_id, Task.status == TaskStatus.PENDING)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return None
        task.agent = new_agent
        await session.flush()
        await session.refresh(task)
        return task


async def switch_task_model(task_id: int, new_model: str) -> Task | None:
    """Switch the model for a PENDING task. Returns updated task or None."""
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            select(Task).where(Task.id == task_id, Task.status == TaskStatus.PENDING)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return None
        task.model = new_model if new_model else None
        await session.flush()
        await session.refresh(task)
        return task


async def switch_task_worker(task_id: int, worker_name: str) -> Task | None:
    """Switch the assigned worker for a PENDING task. Empty string = local."""
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            select(Task).where(Task.id == task_id, Task.status == TaskStatus.PENDING)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return None
        task.assigned_to = worker_name if worker_name else None
        await session.flush()
        await session.refresh(task)
        return task


async def enqueue_followup(
    parent_task_id: int,
    prompt: str,
    chat_id: int | None = None,
    msg_id: int | None = None,
) -> Task:
    """Create a follow-up task that continues the session of a parent task.

    Inherits agent, project_dir, and model from the parent.
    Raises ValueError if parent not found, not completed, or queue is full.
    """
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(select(Task).where(Task.id == parent_task_id))
        parent = result.scalar_one_or_none()
        if parent is None:
            raise ValueError(f"Task #{parent_task_id} not found")
        if parent.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise ValueError(f"Task #{parent_task_id} is {parent.status.value} — only completed/failed tasks can be continued")

        count_result = await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
        )
        active_count = count_result.scalar() or 0
        if active_count >= settings.max_queue_size:
            raise ValueError(
                f"Queue full ({active_count}/{settings.max_queue_size})"
            )

        task = Task(
            prompt=prompt,
            project_dir=parent.project_dir,
            agent=parent.agent,
            model=parent.model,
            parent_task_id=parent.id,
            status=TaskStatus.PENDING,
            telegram_chat_id=chat_id or parent.telegram_chat_id,
            telegram_msg_id=msg_id,
        )
        session.add(task)
        await session.flush()
        await session.refresh(task)
    if _runner_wake:
        _runner_wake()
    return task


async def pick_next_task() -> Task | None:
    """Pick the oldest PENDING task and mark it RUNNING.

    Skips tasks that are assigned to a specific remote worker (assigned_to
    is set) — those are claimed via the Worker API instead.
    """
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            select(Task)
            .where(
                Task.status == TaskStatus.PENDING,
                Task.assigned_to.is_(None),  # skip remote-assigned tasks
            )
            .order_by(Task.priority.desc(), Task.created_at.asc())
            .limit(1)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return None

        task.status = TaskStatus.RUNNING
        task.started_at = _utcnow()
        return task


# ── Worker API (multi-machine) ──────────────────────────────────

WORKER_HEARTBEAT_TIMEOUT = 120  # seconds before a worker is considered dead


async def worker_claim_task(worker_id: str) -> Task | None:
    """Atomically claim the next PENDING task for a remote worker.

    A worker can claim:
    - Tasks explicitly assigned to it (assigned_to == worker_id)
    - Tasks with no assignment (assigned_to IS NULL) that aren't grabbed by local runner

    Tasks assigned to a *different* worker are never claimed.
    Returns the task with status set to RUNNING, or None.
    """
    if not worker_id or len(worker_id) > 128:
        raise ValueError("worker_id must be 1-128 characters")

    session = await get_session()
    async with session, session.begin():
        # Prefer tasks explicitly assigned to this worker, then unassigned
        result = await session.execute(
            select(Task)
            .where(
                Task.status == TaskStatus.PENDING,
                (Task.assigned_to == worker_id) | (Task.assigned_to.is_(None)),
            )
            .order_by(
                # Prioritize tasks assigned to this worker
                (Task.assigned_to == worker_id).desc(),
                Task.created_at.asc(),
            )
            .limit(1)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return None

        task.status = TaskStatus.RUNNING
        task.started_at = _utcnow()
        task.worker_id = worker_id
        task.heartbeat_at = _utcnow()
        return task


async def worker_submit_result(
    task_id: int,
    worker_id: str,
    exit_code: int,
    output_summary: str,
    full_output: str,
    error_message: str | None = None,
) -> Task | None:
    """Submit the result of a task executed by a remote worker.

    Only accepts results from the worker that claimed the task.
    Returns the updated task, or None if not found / not owned.
    """
    now = _utcnow()
    new_status = TaskStatus.COMPLETED if exit_code == 0 else TaskStatus.FAILED

    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == TaskStatus.RUNNING,
                Task.worker_id == worker_id,
            )
            .values(
                status=new_status,
                exit_code=exit_code,
                output_summary=output_summary,
                full_output=full_output,
                error_message=error_message,
                completed_at=now,
            )
        )
        if result.rowcount == 0:
            return None

        fetch = await session.execute(select(Task).where(Task.id == task_id))
        task = fetch.scalar_one_or_none()
        if task and task.started_at:
            task.duration_seconds = int((now - task.started_at).total_seconds())

    # Post-completion: trigger the same logic the local runner uses
    if task is not None:
        await _handle_worker_post_completion(task)

    return task


async def _handle_worker_post_completion(task: Task) -> None:
    """Run post-completion logic for worker-submitted tasks.

    Mirrors the runner's _after_complete: auto-retry, notify, repeat, chain.
    """
    # Auto-retry transient failures
    if task.status == TaskStatus.FAILED:
        retried = await auto_retry_task(task.id)
        if retried:
            logger.info(
                "Worker auto-retry: task #%d → re-enqueued as #%d",
                task.id, retried.id,
            )
            if _on_worker_complete:
                try:
                    await _on_worker_complete(task)
                except Exception:
                    logger.exception("Worker notify failed for task #%d", task.id)
            return

    # Notify via Telegram
    if _on_worker_complete:
        try:
            await _on_worker_complete(task)
        except Exception:
            logger.exception("Worker notify failed for task #%d", task.id)

    # Repeat re-enqueue
    requeued = await maybe_reenqueue(task)
    if requeued:
        logger.info("Worker repeat: task #%d → re-enqueued as #%d", task.id, requeued.id)
        return

    # Chain advancement
    if task.chain_id is not None:
        try:
            next_task = await advance_chain(task)
            if next_task:
                logger.info(
                    "Worker chain: task #%d → next step #%d",
                    task.id, next_task.id,
                )
        except Exception:
            logger.exception("Worker advance_chain failed for task #%d", task.id)


async def worker_heartbeat(task_id: int, worker_id: str) -> bool:
    """Update the heartbeat timestamp for a running task owned by a worker.

    Returns True if updated, False if task not found or not owned.
    """
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == TaskStatus.RUNNING,
                Task.worker_id == worker_id,
            )
            .values(heartbeat_at=_utcnow())
        )
        return result.rowcount > 0


async def recover_stale_worker_tasks() -> int:
    """Recover tasks from workers that stopped sending heartbeats.

    Tasks running on a worker whose heartbeat is older than
    WORKER_HEARTBEAT_TIMEOUT seconds are reset to PENDING so they
    can be re-claimed.
    """
    from datetime import timedelta

    cutoff = _utcnow() - timedelta(seconds=WORKER_HEARTBEAT_TIMEOUT)
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            update(Task)
            .where(
                Task.status == TaskStatus.RUNNING,
                Task.worker_id.isnot(None),
                Task.heartbeat_at < cutoff,
            )
            .values(
                status=TaskStatus.PENDING,
                started_at=None,
                worker_id=None,
                heartbeat_at=None,
            )
        )
        count = result.rowcount
        if count:
            logger.warning("Recovered %d stale worker task(s)", count)
        return count


async def list_active_workers() -> list[dict]:
    """Return info about workers with currently running tasks."""
    session = await get_session()
    async with session:
        result = await session.execute(
            select(Task)
            .where(
                Task.status == TaskStatus.RUNNING,
                Task.worker_id.isnot(None),
            )
        )
        tasks = result.scalars().all()
        workers: dict[str, dict] = {}
        for t in tasks:
            wid = t.worker_id
            if wid not in workers:
                workers[wid] = {
                    "worker_id": wid,
                    "tasks": [],
                    "last_heartbeat": None,
                }
            workers[wid]["tasks"].append(t.id)
            hb = t.heartbeat_at
            if hb and (workers[wid]["last_heartbeat"] is None or hb > workers[wid]["last_heartbeat"]):
                workers[wid]["last_heartbeat"] = hb

        return [
            {
                **w,
                "last_heartbeat": w["last_heartbeat"].isoformat() if w["last_heartbeat"] else None,
            }
            for w in workers.values()
        ]


async def complete_task(
    task_id: int,
    exit_code: int,
    output_summary: str,
    full_output: str,
    error_message: str | None = None,
    git_diff: str | None = None,
) -> Task | None:
    """Mark a task as completed or failed based on exit code.

    Uses atomic UPDATE WHERE status=RUNNING to avoid race with cancel.
    """
    now = _utcnow()
    new_status = TaskStatus.COMPLETED if exit_code == 0 else TaskStatus.FAILED

    session = await get_session()
    async with session, session.begin():
        # Atomic: only update if still RUNNING (avoids race with cancel)
        result = await session.execute(
            update(Task)
            .where(Task.id == task_id, Task.status == TaskStatus.RUNNING)
            .values(
                status=new_status,
                exit_code=exit_code,
                output_summary=output_summary,
                full_output=full_output,
                error_message=error_message,
                git_diff=git_diff,
                completed_at=now,
            )
        )
        if result.rowcount == 0:
            # Task was likely cancelled — fetch current state
            fetch = await session.execute(select(Task).where(Task.id == task_id))
            return fetch.scalar_one_or_none()

        # Re-fetch to get updated task with all fields
        fetch = await session.execute(select(Task).where(Task.id == task_id))
        task = fetch.scalar_one_or_none()
        if task and task.started_at:
            task.duration_seconds = int((now - task.started_at).total_seconds())
        return task


async def cancel_task_by_id(task_id: int) -> Task | None:
    """Cancel a specific PENDING task by ID. Returns the task or None."""
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            select(Task).where(Task.id == task_id, Task.status == TaskStatus.PENDING)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return None
        task.status = TaskStatus.CANCELLED
        task.completed_at = _utcnow()
        return task


async def retry_task(task_id: int) -> Task | None:
    """Re-enqueue a FAILED or CANCELLED task with the same prompt/agent/project_dir.

    Returns the newly created PENDING task, or None if source task not found or not terminal.
    """
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(select(Task).where(Task.id == task_id))
        source = result.scalar_one_or_none()
        if source is None:
            return None
        if source.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            return None

        # Check queue capacity
        count_result = await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
        )
        active_count = count_result.scalar() or 0
        if active_count >= settings.max_queue_size:
            raise ValueError(
                f"Queue full ({active_count}/{settings.max_queue_size})"
            )

        new_task = Task(
            prompt=source.prompt,
            project_dir=source.project_dir,
            agent=source.agent,
            model=source.model,
            status=TaskStatus.PENDING,
            telegram_chat_id=source.telegram_chat_id,
        )
        session.add(new_task)
        await session.flush()
        await session.refresh(new_task)
    if _runner_wake:
        _runner_wake()
    return new_task


async def cancel_running_task() -> Task | None:
    """Cancel the currently running task."""
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            select(Task).where(Task.status == TaskStatus.RUNNING)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return None

        task.status = TaskStatus.CANCELLED
        task.completed_at = _utcnow()
        if task.started_at:
            task.duration_seconds = int(
                (task.completed_at - task.started_at).total_seconds()
            )
        return task


async def get_running_task() -> Task | None:
    session = await get_session()
    async with session:
        result = await session.execute(
            select(Task)
            .where(Task.status == TaskStatus.RUNNING)
            .options(load_only(*_TASK_SUMMARY_COLUMNS))
        )
        return result.scalar_one_or_none()


async def get_pending_tasks() -> list[Task]:
    session = await get_session()
    async with session:
        result = await session.execute(
            select(Task)
            .where(Task.status == TaskStatus.PENDING)
            .order_by(Task.priority.desc(), Task.created_at.asc())
            .options(load_only(*_TASK_SUMMARY_COLUMNS))
        )
        return list(result.scalars().all())


async def get_recent_tasks(limit: int = 10) -> list[Task]:
    session = await get_session()
    async with session:
        result = await session.execute(
            select(Task)
            .order_by(Task.created_at.desc())
            .limit(limit)
            .options(load_only(*_TASK_SUMMARY_COLUMNS))
        )
        return list(result.scalars().all())


async def get_task_by_id(task_id: int) -> Task | None:
    session = await get_session()
    async with session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        return result.scalar_one_or_none()


async def recover_interrupted_tasks() -> int:
    """Mark orphaned RUNNING tasks as FAILED on startup (e.g. after laptop sleep)."""
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            update(Task)
            .where(Task.status == TaskStatus.RUNNING)
            .values(
                status=TaskStatus.FAILED,
                error_message="Interrupted — recovered on restart",
                completed_at=_utcnow(),
            )
        )
        count = result.rowcount
        if count:
            logger.warning("Recovered %d interrupted task(s)", count)
        return count


# ── Repeat support ──────────────────────────────────────────────────


async def enqueue_repeat_task(
    prompt: str,
    repeat_count: int | None = None,
    repeat_until: datetime | None = None,
    project_dir: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    chat_id: int | None = None,
    msg_id: int | None = None,
) -> Task:
    """Enqueue a task that will repeat N times or until a deadline.

    Either repeat_count or repeat_until must be given (or both).
    """
    if repeat_count is None and repeat_until is None:
        raise ValueError("Provide repeat_count or repeat_until (or both)")
    if repeat_count is not None and repeat_count < 1:
        raise ValueError("repeat_count must be >= 1")
    if repeat_count is not None and repeat_count > 1000:
        raise ValueError("repeat_count must be <= 1000")
    if repeat_until is not None:
        from datetime import timedelta
        max_deadline = _utcnow() + timedelta(hours=24)
        if repeat_until > max_deadline:
            raise ValueError("repeat_until cannot be more than 24 hours from now")

    session = await get_session()
    async with session, session.begin():
        count_result = await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
        )
        active_count = count_result.scalar() or 0
        if active_count >= settings.max_queue_size:
            raise ValueError(
                f"Queue full ({active_count}/{settings.max_queue_size})"
            )

        task = Task(
            prompt=prompt,
            project_dir=project_dir or settings.default_project_dir,
            agent=agent or settings.default_agent,
            model=model or settings.default_model or None,
            status=TaskStatus.PENDING,
            telegram_chat_id=chat_id,
            telegram_msg_id=msg_id,
            repeat_total=repeat_count,
            repeat_remaining=repeat_count,
            repeat_until=repeat_until,
        )
        session.add(task)
        await session.flush()
        await session.refresh(task)
    if _runner_wake:
        _runner_wake()
    return task


async def maybe_reenqueue(task: Task) -> Task | None:
    """After a task completes, re-enqueue it if repeat conditions remain.

    Returns the newly enqueued task, or None if no repeat needed.
    Skips re-enqueue on FAILED tasks (chain/user can retry manually).
    """
    # Don't repeat failed or cancelled tasks
    if task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
        return None

    # Decrement count first
    new_remaining = None
    if task.repeat_remaining is not None:
        new_remaining = task.repeat_remaining - 1

    # Check both conditions with updated count
    should_repeat = False

    if new_remaining is not None and new_remaining > 0:
        should_repeat = True

    if task.repeat_until is not None and _utcnow() < task.repeat_until:
        should_repeat = True

    if not should_repeat:
        return None

    session = await get_session()
    async with session, session.begin():
        # Check queue capacity before reenqueue
        count_result = await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
        )
        active_count = count_result.scalar() or 0
        if active_count >= settings.max_queue_size:
            logger.warning("Repeat skipped — queue full (%d/%d)", active_count, settings.max_queue_size)
            return None

        new_task = Task(
            prompt=task.prompt,
            project_dir=task.project_dir,
            agent=task.agent,
            model=task.model,
            status=TaskStatus.PENDING,
            telegram_chat_id=task.telegram_chat_id,
            repeat_total=task.repeat_total,
            repeat_remaining=new_remaining,
            repeat_until=task.repeat_until,
            chain_id=task.chain_id,
            chain_step=task.chain_step,
        )
        session.add(new_task)
        await session.flush()
        await session.refresh(new_task)
        logger.info(
            "Re-enqueued task (remaining=%s, until=%s) → #%d",
            new_remaining, task.repeat_until, new_task.id,
        )
        return new_task


# ── Chain support ───────────────────────────────────────────────────


async def save_chain(
    name: str,
    steps: list[dict],
    chat_id: int | None = None,
) -> TaskChain:
    """Save a named task chain. Each step is {prompt, agent?, project_dir?}."""
    if not steps:
        raise ValueError("Chain must have at least one step")

    if len(steps) > 50:
        raise ValueError("Chain cannot exceed 50 steps")
    for i, step in enumerate(steps):
        if not step.get("prompt"):
            raise ValueError(f"Step {i} missing 'prompt'")
        if len(step["prompt"]) > settings.max_prompt_len:
            raise ValueError(f"Step {i} prompt exceeds {settings.max_prompt_len} chars")

    session = await get_session()
    async with session, session.begin():
        # Upsert: delete old chain with same name
        existing = await session.execute(
            select(TaskChain).where(TaskChain.name == name)
        )
        old = existing.scalar_one_or_none()
        if old is not None:
            await session.delete(old)
            await session.flush()

        chain = TaskChain(
            name=name,
            steps_json=json.dumps(steps),
            status=ChainStatus.IDLE,
            telegram_chat_id=chat_id,
        )
        session.add(chain)
        await session.flush()
        await session.refresh(chain)
        return chain


async def list_chains() -> list[TaskChain]:
    session = await get_session()
    async with session:
        result = await session.execute(
            select(TaskChain).order_by(TaskChain.created_at.desc())
        )
        return list(result.scalars().all())


async def get_chain_by_name(name: str) -> TaskChain | None:
    session = await get_session()
    async with session:
        result = await session.execute(
            select(TaskChain).where(TaskChain.name == name)
        )
        return result.scalar_one_or_none()


async def get_chain_by_id(chain_id: int) -> TaskChain | None:
    session = await get_session()
    async with session:
        result = await session.execute(
            select(TaskChain).where(TaskChain.id == chain_id)
        )
        return result.scalar_one_or_none()


async def delete_chain(name: str) -> bool:
    """Delete a chain by name. Returns True if deleted. Raises ValueError if running."""
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            select(TaskChain).where(TaskChain.name == name)
        )
        chain = result.scalar_one_or_none()
        if chain is None:
            return False
        if chain.status == ChainStatus.RUNNING:
            raise ValueError(f"Cannot delete chain '{name}' while running")
        await session.delete(chain)
        return True


async def start_chain(name: str, chat_id: int | None = None) -> Task | None:
    """Start running a chain from step 0. Returns the first enqueued task."""
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            select(TaskChain).where(TaskChain.name == name)
        )
        chain = result.scalar_one_or_none()
        if chain is None:
            return None

        steps = chain.steps
        if not steps:
            return None

        if chain.status == ChainStatus.RUNNING:
            raise ValueError(f"Chain '{name}' is already running")

        chain.status = ChainStatus.RUNNING
        chain.current_step = 0
        chain.started_at = _utcnow()
        chain.telegram_chat_id = chat_id or chain.telegram_chat_id

        step = steps[0]
        task = Task(
            prompt=step["prompt"],
            project_dir=step.get("project_dir") or settings.default_project_dir,
            agent=step.get("agent") or settings.default_agent,
            model=step.get("model") or None,
            status=TaskStatus.PENDING,
            telegram_chat_id=chain.telegram_chat_id,
            chain_id=chain.id,
            chain_step=0,
        )
        session.add(task)
        await session.flush()
        await session.refresh(task)
        logger.info("Started chain '%s' → task #%d (step 0/%d)", name, task.id, len(steps))
        return task


async def advance_chain(task: Task) -> Task | None:
    """After a chain-linked task completes, enqueue the next step.

    Returns the next task, or None if chain is done/failed.
    """
    if task.chain_id is None:
        return None

    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            select(TaskChain).where(TaskChain.id == task.chain_id)
        )
        chain = result.scalar_one_or_none()
        if chain is None:
            return None

        steps = chain.steps
        next_idx = (task.chain_step or 0) + 1

        # Chain failed if the current step failed or was cancelled
        if task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            chain.status = ChainStatus.FAILED if task.status == TaskStatus.FAILED else ChainStatus.CANCELLED
            chain.completed_at = _utcnow()
            logger.warning("Chain '%s' %s at step %d", chain.name, task.status.value, task.chain_step or 0)
            return None

        # Chain completed if no more steps
        if next_idx >= len(steps):
            chain.status = ChainStatus.COMPLETED
            chain.completed_at = _utcnow()
            chain.current_step = next_idx
            logger.info("Chain '%s' completed all %d steps", chain.name, len(steps))
            return None

        # Advance
        chain.current_step = next_idx

        # Check queue capacity before creating next task
        count_result = await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
        )
        active_count = count_result.scalar() or 0
        if active_count >= settings.max_queue_size:
            chain.status = ChainStatus.FAILED
            chain.completed_at = _utcnow()
            logger.warning("Chain '%s' paused at step %d — queue full", chain.name, next_idx)
            return None

        step = steps[next_idx]
        next_task = Task(
            prompt=step["prompt"],
            project_dir=step.get("project_dir") or settings.default_project_dir,
            agent=step.get("agent") or settings.default_agent,
            model=step.get("model") or None,
            status=TaskStatus.PENDING,
            telegram_chat_id=chain.telegram_chat_id,
            chain_id=chain.id,
            chain_step=next_idx,
        )
        session.add(next_task)
        await session.flush()
        await session.refresh(next_task)
        logger.info(
            "Chain '%s' step %d/%d → task #%d",
            chain.name, next_idx, len(steps), next_task.id,
        )
        return next_task


# ── Recovery & maintenance ──────────────────────────────────────────


async def recover_interrupted_chains() -> int:
    """Mark orphaned RUNNING chains as FAILED on startup."""
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            update(TaskChain)
            .where(TaskChain.status == ChainStatus.RUNNING)
            .values(
                status=ChainStatus.FAILED,
                completed_at=_utcnow(),
            )
        )
        count = result.rowcount
        if count:
            logger.warning("Recovered %d interrupted chain(s)", count)
        return count


async def purge_old_tasks(days: int = 30) -> int:
    """Delete tasks older than *days* that are in a terminal state.

    Returns the number of rows deleted.
    """
    from datetime import timedelta

    cutoff = _utcnow() - timedelta(days=days)
    terminal = [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]

    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            sa_delete(Task).where(
                Task.status.in_(terminal),
                Task.completed_at < cutoff,
            )
        )
        count = result.rowcount
        if count:
            logger.info("Purged %d tasks older than %d days", count, days)
        return count


# ── Chat preferences persistence ────────────────────────────────────


async def get_chat_prefs(chat_id: int) -> dict:
    """Return {"project_dir": ..., "agent": ..., "model": ...} for a chat. Missing keys → None."""
    session = await get_session()
    async with session:
        result = await session.execute(
            select(ChatPrefs).where(ChatPrefs.chat_id == chat_id)
        )
        prefs = result.scalar_one_or_none()
        if prefs is None:
            return {"project_dir": None, "agent": None, "model": None}
        return {"project_dir": prefs.project_dir, "agent": prefs.agent, "model": prefs.model}


async def set_chat_pref(chat_id: int, *, project_dir: str | None = None, agent: str | None = None, model: str | None = None) -> None:
    """Upsert a single chat preference."""
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            select(ChatPrefs).where(ChatPrefs.chat_id == chat_id)
        )
        prefs = result.scalar_one_or_none()
        if prefs is None:
            prefs = ChatPrefs(chat_id=chat_id)
            session.add(prefs)
        if project_dir is not None:
            prefs.project_dir = project_dir
        if agent is not None:
            prefs.agent = agent
        if model is not None:
            prefs.model = model
        prefs.updated_at = _utcnow()


# ── Priority / bump ─────────────────────────────────────────────────


async def bump_task(task_id: int) -> Task | None:
    """Bump a PENDING task to highest priority (max priority among pending + 1).

    Returns updated task or None if not found / not pending.
    """
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            select(Task).where(Task.id == task_id, Task.status == TaskStatus.PENDING)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return None
        # Find the current max priority among pending tasks
        max_result = await session.execute(
            select(func.max(Task.priority))
            .select_from(Task)
            .where(Task.status == TaskStatus.PENDING)
        )
        max_prio = max_result.scalar() or 0
        task.priority = max(max_prio + 1, task.priority + 1)
        await session.flush()
        await session.refresh(task)
        return task


# ── Search ───────────────────────────────────────────────────────────


async def search_tasks(query: str, limit: int = 20) -> list[Task]:
    """Search tasks by prompt text (case-insensitive LIKE match)."""
    session = await get_session()
    async with session:
        result = await session.execute(
            select(Task)
            .where(Task.prompt.ilike(f"%{query}%"))
            .order_by(Task.created_at.desc())
            .limit(limit)
            .options(load_only(*_TASK_SUMMARY_COLUMNS))
        )
        return list(result.scalars().all())


# ── Auto-retry ───────────────────────────────────────────────────────


async def auto_retry_task(task_id: int) -> Task | None:
    """Auto-retry a FAILED task if it has retries remaining.

    Returns the newly created task, or None if no retries left or task not eligible.
    """
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(select(Task).where(Task.id == task_id))
        source = result.scalar_one_or_none()
        if source is None:
            return None
        if source.status != TaskStatus.FAILED:
            return None
        if source.retry_count >= source.max_retries:
            return None
        # Check for transient failure: exit_code < 0 (crashes, signals, timeouts)
        if source.exit_code is not None and source.exit_code >= 0:
            return None  # normal failures (user errors) don't auto-retry

        # Check queue capacity
        count_result = await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
        )
        active_count = count_result.scalar() or 0
        if active_count >= settings.max_queue_size:
            return None  # silently skip if queue is full

        new_task = Task(
            prompt=source.prompt,
            project_dir=source.project_dir,
            agent=source.agent,
            model=source.model,
            status=TaskStatus.PENDING,
            telegram_chat_id=source.telegram_chat_id,
            priority=source.priority,
            retry_count=source.retry_count + 1,
            max_retries=source.max_retries,
            assigned_to=source.assigned_to,
        )
        session.add(new_task)
        await session.flush()
        await session.refresh(new_task)
    if _runner_wake:
        _runner_wake()
    return new_task


# ── Recipe CRUD ──────────────────────────────────────────────────────


async def save_recipe(
    name: str,
    triggers: list[str],
    agent: str | None = None,
    model: str | None = None,
    project_dir: str | None = None,
    setup_commands: list[str] | None = None,
    skills: list[str] | None = None,
    prompt_prefix: str | None = None,
    prompt_suffix: str | None = None,
    chat_id: int | None = None,
) -> Recipe:
    """Save or update a recipe.  Raises ValueError on invalid input."""
    if not name or len(name) > 128:
        raise ValueError("Recipe name must be 1-128 characters")
    if not triggers:
        raise ValueError("Recipe must have at least one trigger keyword")
    if len(triggers) > 50:
        raise ValueError("Recipe cannot have more than 50 triggers")
    for t in triggers:
        if not t or len(t) > 200:
            raise ValueError("Each trigger must be 1-200 characters")
    if setup_commands and len(setup_commands) > 20:
        raise ValueError("Recipe cannot have more than 20 setup commands")
    if skills and len(skills) > 20:
        raise ValueError("Recipe cannot have more than 20 skills")
    if prompt_prefix and len(prompt_prefix) > settings.max_prompt_len:
        raise ValueError(f"prompt_prefix exceeds {settings.max_prompt_len} chars")
    if prompt_suffix and len(prompt_suffix) > settings.max_prompt_len:
        raise ValueError(f"prompt_suffix exceeds {settings.max_prompt_len} chars")

    session = await get_session()
    async with session, session.begin():
        # Upsert: delete old recipe with same name
        existing = await session.execute(
            select(Recipe).where(Recipe.name == name)
        )
        old = existing.scalar_one_or_none()
        if old is not None:
            await session.delete(old)
            await session.flush()

        recipe = Recipe(
            name=name,
            triggers_json=json.dumps(triggers),
            agent=agent,
            model=model,
            project_dir=project_dir,
            setup_commands_json=json.dumps(setup_commands or []),
            skills_json=json.dumps(skills or []),
            prompt_prefix=prompt_prefix,
            prompt_suffix=prompt_suffix,
            telegram_chat_id=chat_id,
        )
        session.add(recipe)
        await session.flush()
        await session.refresh(recipe)
        return recipe


async def list_recipes() -> list[Recipe]:
    """Return all recipes ordered by creation time."""
    session = await get_session()
    async with session:
        result = await session.execute(
            select(Recipe).order_by(Recipe.created_at.desc())
        )
        return list(result.scalars().all())


async def get_recipe_by_name(name: str) -> Recipe | None:
    session = await get_session()
    async with session:
        result = await session.execute(
            select(Recipe).where(Recipe.name == name)
        )
        return result.scalar_one_or_none()


async def get_recipe_by_id(recipe_id: int) -> Recipe | None:
    session = await get_session()
    async with session:
        result = await session.execute(
            select(Recipe).where(Recipe.id == recipe_id)
        )
        return result.scalar_one_or_none()


async def delete_recipe(name: str) -> bool:
    """Delete a recipe by name. Returns True if deleted."""
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            select(Recipe).where(Recipe.name == name)
        )
        recipe = result.scalar_one_or_none()
        if recipe is None:
            return False
        await session.delete(recipe)
        return True


async def match_recipe(prompt: str) -> Recipe | None:
    """Find the first recipe whose triggers match the prompt (case-insensitive).

    Returns the recipe with the most trigger matches, or None.
    """
    recipes = await list_recipes()
    if not recipes:
        return None

    prompt_lower = prompt.lower()
    best_recipe = None
    best_score = 0

    for recipe in recipes:
        score = sum(1 for t in recipe.triggers if t.lower() in prompt_lower)
        if score > best_score:
            best_score = score
            best_recipe = recipe

    return best_recipe if best_score > 0 else None


async def _apply_recipe_to_task(
    task_id: int,
    recipe: Recipe,
    enriched_prompt: str,
) -> Task | None:
    """Apply recipe overrides (agent, project_dir, enriched prompt) to a PENDING task."""
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            select(Task).where(Task.id == task_id, Task.status == TaskStatus.PENDING)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return None
        task.prompt = enriched_prompt
        if recipe.agent:
            task.agent = recipe.agent
        if recipe.model:
            task.model = recipe.model
        if recipe.project_dir:
            task.project_dir = recipe.project_dir
        await session.flush()
        await session.refresh(task)
        return task


async def install_gallery_recipe(name: str, chat_id: int | None = None) -> Recipe | None:
    """Install a single recipe from the gallery. Returns the Recipe or None if not found."""
    from app.core.gallery import get_gallery_item

    item = get_gallery_item(name)
    if item is None:
        return None

    kwargs = dict(item["recipe"])
    kwargs["name"] = name
    kwargs["chat_id"] = chat_id
    return await save_recipe(**kwargs)


async def install_gallery_category(category: str, chat_id: int | None = None) -> list[Recipe]:
    """Install all recipes from a gallery category. Returns installed recipes."""
    from app.core.gallery import get_gallery_by_category

    items = get_gallery_by_category(category)
    installed: list[Recipe] = []
    for item in items:
        kwargs = dict(item["recipe"])
        kwargs["name"] = item["name"]
        kwargs["chat_id"] = chat_id
        recipe = await save_recipe(**kwargs)
        installed.append(recipe)
    return installed


async def install_all_gallery_recipes(chat_id: int | None = None) -> list[Recipe]:
    """Install every recipe from the gallery. Returns all installed recipes."""
    from app.core.gallery import GALLERY

    installed: list[Recipe] = []
    for item in GALLERY:
        kwargs = dict(item["recipe"])
        kwargs["name"] = item["name"]
        kwargs["chat_id"] = chat_id
        recipe = await save_recipe(**kwargs)
        installed.append(recipe)
    return installed

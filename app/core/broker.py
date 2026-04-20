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
    Task.created_at, Task.started_at, Task.completed_at, Task.duration_seconds,
)

_runner_wake: Callable[[], None] | None = None


def set_runner_wake(fn: Callable[[], None] | None) -> None:
    """Register a callback to wake the runner when a new task is enqueued."""
    global _runner_wake
    _runner_wake = fn


async def enqueue_task(
    prompt: str,
    project_dir: str | None = None,
    agent: str | None = None,
    chat_id: int | None = None,
    msg_id: int | None = None,
) -> Task:
    """Add a new task to the queue. Raises ValueError if the queue is full."""
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


async def pick_next_task() -> Task | None:
    """Pick the oldest PENDING task and mark it RUNNING."""
    session = await get_session()
    async with session, session.begin():
        result = await session.execute(
            select(Task)
            .where(Task.status == TaskStatus.PENDING)
            .order_by(Task.created_at.asc())
            .limit(1)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return None

        task.status = TaskStatus.RUNNING
        task.started_at = _utcnow()
        return task


async def complete_task(
    task_id: int,
    exit_code: int,
    output_summary: str,
    full_output: str,
    error_message: str | None = None,
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
            .order_by(Task.created_at.asc())
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
        if len(step["prompt"]) > 2000:
            raise ValueError(f"Step {i} prompt exceeds 2000 chars")

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
    """Return {"project_dir": ..., "agent": ...} for a chat. Missing keys → None."""
    session = await get_session()
    async with session:
        result = await session.execute(
            select(ChatPrefs).where(ChatPrefs.chat_id == chat_id)
        )
        prefs = result.scalar_one_or_none()
        if prefs is None:
            return {"project_dir": None, "agent": None}
        return {"project_dir": prefs.project_dir, "agent": prefs.agent}


async def set_chat_pref(chat_id: int, *, project_dir: str | None = None, agent: str | None = None) -> None:
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
        prefs.updated_at = _utcnow()


# ── Recipe CRUD ──────────────────────────────────────────────────────


async def save_recipe(
    name: str,
    triggers: list[str],
    agent: str | None = None,
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
    if prompt_prefix and len(prompt_prefix) > 2000:
        raise ValueError("prompt_prefix exceeds 2000 chars")
    if prompt_suffix and len(prompt_suffix) > 2000:
        raise ValueError("prompt_suffix exceeds 2000 chars")

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
        if recipe.project_dir:
            task.project_dir = recipe.project_dir
        await session.flush()
        await session.refresh(task)
        return task

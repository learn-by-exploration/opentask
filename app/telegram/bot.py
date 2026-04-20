"""Telegram bot — handles commands, message routing, and notifications."""

from __future__ import annotations

import io
import logging
import os
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config.settings import settings
from app.core.broker import (
    advance_chain,
    cancel_running_task,
    cancel_task_by_id,
    delete_chain,
    enqueue_repeat_task,
    enqueue_task,
    get_chain_by_name,
    get_chat_prefs,
    get_pending_tasks,
    get_recent_tasks,
    get_running_task,
    get_task_by_id,
    list_chains,
    retry_task,
    save_chain,
    set_chat_pref,
    start_chain,
    switch_task_agent,
)
from app.core.models import ChainStatus, Task, TaskStatus

logger = logging.getLogger(__name__)

# ── Mutable session state per chat ──────────────────────────────────

_chat_project_dir: dict[int, str] = {}
_chat_agent: dict[int, str] = {}
_runner_ref = None  # set by build_app() to enable /cancel subprocess kill

MAX_MSG_LEN = 4096
MAX_PROMPT_LEN = 2000


# ── Auth guard ──────────────────────────────────────────────────────

def auth_required(func: Any) -> Any:
    """Decorator: silently ignore messages from non-allowed users."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in settings.allowed_user_ids:
            logger.warning("Unauthorized access attempt from user %s", user_id)
            return
        return await func(update, context)

    return wrapper


# ── Helpers ─────────────────────────────────────────────────────────

def _chat_id(update: Update) -> int:
    return update.effective_chat.id  # type: ignore[union-attr]


async def _load_prefs(chat_id: int) -> None:
    """Load persisted prefs into cache if not already loaded."""
    if chat_id in _chat_project_dir or chat_id in _chat_agent:
        return  # already in cache
    prefs = await get_chat_prefs(chat_id)
    if prefs["project_dir"]:
        _chat_project_dir[chat_id] = prefs["project_dir"]
    if prefs["agent"]:
        _chat_agent[chat_id] = prefs["agent"]


def _project_dir(chat_id: int) -> str:
    return _chat_project_dir.get(chat_id, settings.default_project_dir)


def _agent(chat_id: int) -> str:
    return _chat_agent.get(chat_id, settings.default_agent)


def _status_emoji(status: TaskStatus) -> str:
    return {
        TaskStatus.PENDING: "📋",
        TaskStatus.RUNNING: "🔄",
        TaskStatus.COMPLETED: "✅",
        TaskStatus.FAILED: "❌",
        TaskStatus.CANCELLED: "🚫",
    }.get(status, "❓")


def _is_allowed_project_dir(path: str) -> bool:
    expanded = os.path.expanduser(path)
    real = os.path.realpath(expanded)
    for allowed in settings.allowed_project_dirs_list:
        allowed_real = os.path.realpath(os.path.expanduser(allowed))
        if real == allowed_real or real.startswith(allowed_real + os.sep):
            return True
    return False


async def _send(update: Update, text: str) -> None:
    """Send a message, splitting at MAX_MSG_LEN if needed."""
    chat_id = _chat_id(update)
    for i in range(0, len(text), MAX_MSG_LEN):
        await update.get_bot().send_message(
            chat_id=chat_id,
            text=text[i : i + MAX_MSG_LEN],
        )


def _format_task(task: Task) -> str:
    emoji = _status_emoji(task.status)
    dur = f" ({task.duration_seconds}s)" if task.duration_seconds else ""
    return f"{emoji} #{task.id} [{task.agent}] {task.prompt[:60]}{dur}"


# ── Notification callback (called by runner) ────────────────────────

async def make_notify_callback(
    app: Application,
) -> Any:
    """Return a callback the runner can use to notify task completion."""

    async def _notify(task: Task) -> None:
        if task.telegram_chat_id is None:
            logger.warning("Task #%d has no chat_id, skipping notification", task.id)
            return

        emoji = _status_emoji(task.status)
        text = f"{emoji} Task #{task.id} {task.status.value}\n"
        if task.output_summary:
            text += f"\n{task.output_summary}"
        if task.error_message:
            text += f"\n⚠️ {task.error_message}"

        for i in range(0, len(text), MAX_MSG_LEN):
            await app.bot.send_message(
                chat_id=task.telegram_chat_id,
                text=text[i : i + MAX_MSG_LEN],
            )
        if task.telegram_msg_id:
            try:
                await app.bot.edit_message_text(
                    chat_id=task.telegram_chat_id,
                    message_id=task.telegram_msg_id,
                    text=f"{emoji} #{task.id} — {task.status.value}",
                )
            except Exception:
                pass  # original message may have been deleted

    return _notify


async def make_chain_notify_callback(
    app: Application,
) -> Any:
    """Return a callback to send chain-level status messages."""

    async def _chain_notify(chat_id: int | None, text: str) -> None:
        if chat_id is None:
            return
        for i in range(0, len(text), MAX_MSG_LEN):
            await app.bot.send_message(chat_id=chat_id, text=text[i : i + MAX_MSG_LEN])

    return _chain_notify


async def make_progress_callback(
    app: Application,
) -> Any:
    """Return a callback to send task progress updates."""

    async def _progress_notify(chat_id: int | None, text: str) -> None:
        if chat_id is None:
            return
        for i in range(0, len(text), MAX_MSG_LEN):
            await app.bot.send_message(
                chat_id=chat_id,
                text=text[i : i + MAX_MSG_LEN],
                parse_mode="Markdown",
            )

    return _progress_notify


async def make_typing_callback(
    app: Application,
) -> Any:
    """Return a callback to send a typing indicator to a chat."""

    async def _typing_notify(chat_id: int | None) -> None:
        if chat_id is None:
            return
        try:
            await app.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            logger.debug("Typing indicator failed for chat %s", chat_id)

    return _typing_notify


# ── Command handlers ────────────────────────────────────────────────

@auth_required
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(update, "👋 TaskPilot ready. Send any text to create a task.")


@auth_required
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📖 *TaskPilot Commands*\n\n"
        "/status — current task status\n"
        "/queue — pending tasks\n"
        "/history [N] — recent tasks\n"
        "/cancel [id] — cancel running or pending task\n"
        "/retry <id> — re-queue a failed task\n"
        "/project <path> — set project dir\n"
        "/agent <name> — set agent\n"
        "/output <id> — get full output of a task\n\n"
        "*Repeat:*\n"
        "/repeat <N> <prompt> — repeat N times\n"
        "/repeat until:HH:MM <prompt> — repeat until time\n\n"
        "*Chains:*\n"
        "/savechain <name> step1 | step2 | step3\n"
        "/chain <name> — run a saved chain\n"
        "/chains — list all chains\n"
        "/delchain <name> — delete a chain\n\n"
        "/help — this message\n\n"
        "Or just send any text to queue a task."
    )
    await _send(update, text)


@auth_required
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _load_prefs(_chat_id(update))
    task = await get_running_task()
    if task is None:
        await _send(update, "💤 No task running.")
    else:
        elapsed = ""
        if task.started_at:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            delta = int((now - task.started_at).total_seconds())
            mins, secs = divmod(delta, 60)
            elapsed = f" — {mins}m{secs:02d}s elapsed" if mins else f" — {secs}s elapsed"
        await _send(update, f"🔄 Running: {_format_task(task)}{elapsed}")


@auth_required
async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _load_prefs(_chat_id(update))
    tasks = await get_pending_tasks()
    if not tasks:
        await _send(update, "📭 Queue is empty.")
    else:
        lines = ["📦 *Pending tasks:*"] + [_format_task(t) for t in tasks]
        await _send(update, "\n".join(lines))


@auth_required
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    limit = 10
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 50))
        except ValueError:
            pass
    tasks = await get_recent_tasks(limit=limit)
    if not tasks:
        await _send(update, "📭 No tasks yet.")
    else:
        lines = [f"📜 *Recent tasks (last {len(tasks)}):*"] + [_format_task(t) for t in tasks]
        await _send(update, "\n".join(lines))


@auth_required
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /cancel <id> — cancel a specific pending or running task
    if context.args:
        try:
            task_id = int(context.args[0])
        except ValueError:
            await _send(update, "Invalid task ID.")
            return
        task = await cancel_task_by_id(task_id)
        if task is not None:
            await _send(update, f"🚫 Cancelled pending task #{task.id}")
            return
        # Check if it's the currently running task
        running = await get_running_task()
        if running and running.id == task_id:
            cancelled = await cancel_running_task()
            if _runner_ref is not None:
                await _runner_ref.cancel_current()
            if cancelled:
                await _send(update, f"🚫 Cancelled running task #{cancelled.id}")
                return
        await _send(update, f"Task #{task_id} not found or already completed.")
        return

    # /cancel — cancel the running task
    task = await cancel_running_task()
    if task is not None and _runner_ref is not None:
        await _runner_ref.cancel_current()
    if task is None:
        await _send(update, "💤 Nothing to cancel.")
    else:
        await _send(update, f"🚫 Cancelled task #{task.id}")


@auth_required
async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        path = context.args[0]
        if not _is_allowed_project_dir(path):
            await _send(update, f"⚠️ Directory not in allowed paths. Allowed: {', '.join(settings.allowed_project_dirs_list)}")
            return
        expanded = os.path.expanduser(path)
        if not os.path.isdir(expanded):
            await _send(update, f"⚠️ Directory not found: `{path}`")
            return
        chat_id = _chat_id(update)
        _chat_project_dir[chat_id] = path
        await set_chat_pref(chat_id, project_dir=path)
        await _send(update, f"📁 Project dir set to: `{path}`")
    else:
        await _send(update, f"📁 Current: `{_project_dir(_chat_id(update))}`")


@auth_required
async def cmd_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    available = ", ".join(settings.agent_commands.keys())
    if context.args:
        name = context.args[0]
        if name not in settings.agent_commands:
            await _send(update, f"❓ Unknown agent. Available: {available}")
            return
        chat_id = _chat_id(update)
        _chat_agent[chat_id] = name
        await set_chat_pref(chat_id, agent=name)
        await _send(update, f"🤖 Agent set to: `{name}`")
    else:
        await _send(
            update,
            f"🤖 Current: `{_agent(_chat_id(update))}`\nAvailable: {available}",
        )


@auth_required
async def cmd_output(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _send(update, "Usage: /output <task_id>")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await _send(update, "Invalid task ID.")
        return

    task = await get_task_by_id(task_id)
    if task is None:
        await _send(update, f"Task #{task_id} not found.")
    elif not task.full_output:
        await _send(update, f"Task #{task_id} has no output yet.")
    elif len(task.full_output) > MAX_MSG_LEN:
        # Send as file to avoid flooding chat
        buf = io.BytesIO(task.full_output.encode("utf-8"))
        buf.name = f"task_{task_id}_output.txt"
        await update.get_bot().send_document(
            chat_id=_chat_id(update),
            document=buf,
            caption=f"📄 Output for #{task_id} ({len(task.full_output)} chars)",
        )
    else:
        await _send(update, f"📄 Output for #{task_id}:\n\n{task.full_output}")


# ── Retry command ───────────────────────────────────────────────────

@auth_required
async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-queue a failed or cancelled task: /retry <id>"""
    if not context.args:
        await _send(update, "Usage: /retry <task_id>")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await _send(update, "Invalid task ID.")
        return

    try:
        new_task = await retry_task(task_id)
    except ValueError as e:
        await _send(update, f"⚠️ {e}")
        return

    if new_task is None:
        await _send(update, f"Task #{task_id} not found or not failed/cancelled.")
    else:
        await _send(
            update,
            f"🔄 Retried #{task_id} → new task #{new_task.id} ({new_task.agent})",
        )


# ── Repeat commands ─────────────────────────────────────────────────

@auth_required
async def cmd_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Repeat a task N times: /repeat <N> <prompt>"""
    await _load_prefs(_chat_id(update))
    if not context.args or len(context.args) < 2:
        await _send(
            update,
            "Usage:\n"
            "  /repeat <N> <prompt> — repeat N times\n"
            "  /repeat until:<HH:MM> <prompt> — repeat until time (UTC)\n"
            "  /repeat <N> until:<HH:MM> <prompt> — both",
        )
        return

    args = list(context.args)
    repeat_count = None
    repeat_until = None

    # Parse repeat count
    try:
        repeat_count = int(args[0])
        args.pop(0)
    except ValueError:
        pass

    # Parse until:<HH:MM>
    if args and args[0].startswith("until:"):
        from datetime import datetime, timedelta, timezone

        time_str = args[0].split(":", 1)[1]
        try:
            parts = time_str.split(":")
            if len(parts) not in (1, 2):
                raise ValueError("Expected HH or HH:MM")
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            if not (0 <= h <= 23) or not (0 <= m <= 59):
                raise ValueError("Hour must be 0-23, minute 0-59")
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            deadline = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if deadline <= now:
                deadline += timedelta(days=1)
            repeat_until = deadline
            args.pop(0)
        except (ValueError, IndexError) as exc:
            await _send(update, f"Invalid time format. Use until:HH:MM (24h, UTC). ({exc})")
            return

    if repeat_count is None and repeat_until is None:
        await _send(update, "Provide a repeat count (number) or until:<HH:MM>.")
        return

    prompt = _sanitize_text(" ".join(args))
    if not prompt:
        await _send(update, "Missing prompt after repeat options.")
        return
    if len(prompt) > MAX_PROMPT_LEN:
        await _send(update, f"⚠️ Prompt too long ({len(prompt)} chars). Max is {MAX_PROMPT_LEN}.")
        return

    chat_id = _chat_id(update)
    try:
        task = await enqueue_repeat_task(
            prompt=prompt,
            repeat_count=repeat_count,
            repeat_until=repeat_until,
            project_dir=_project_dir(chat_id),
            agent=_agent(chat_id),
            chat_id=chat_id,
        )
    except ValueError as e:
        await _send(update, f"⚠️ {e}")
        return

    parts = []
    if repeat_count:
        parts.append(f"{repeat_count}x")
    if repeat_until:
        parts.append(f"until {repeat_until.strftime('%H:%M')} UTC")
    label = " ".join(parts)

    await _send(update, f"🔁 Queued #{task.id} — repeat {label}\n📋 {prompt[:80]}")


# ── Chain commands ──────────────────────────────────────────────────

@auth_required
async def cmd_savechain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save a chain: /savechain <name> step1 | step2 | step3"""
    await _load_prefs(_chat_id(update))
    if not context.args or len(context.args) < 2:
        await _send(
            update,
            "Usage: /savechain <name> step1 prompt | step2 prompt | step3 prompt\n"
            "Steps are separated by |",
        )
        return

    name = _sanitize_text(context.args[0])
    if not name or len(name) > 64:
        await _send(update, "⚠️ Chain name must be 1-64 characters.")
        return
    rest = " ".join(context.args[1:])
    raw_steps = [_sanitize_text(s) for s in rest.split("|") if s.strip()]

    if not raw_steps:
        await _send(update, "No steps found. Separate with |")
        return

    chat_id = _chat_id(update)
    steps = []
    for raw in raw_steps:
        steps.append({
            "prompt": raw,
            "agent": _agent(chat_id),
            "project_dir": _project_dir(chat_id),
        })

    try:
        chain = await save_chain(name=name, steps=steps, chat_id=chat_id)
    except ValueError as e:
        await _send(update, f"⚠️ {e}")
        return

    lines = [f"💾 Chain '{chain.name}' saved ({chain.total_steps} steps):"]
    for i, step in enumerate(chain.steps):
        lines.append(f"  {i+1}. {step['prompt'][:60]}")
    await _send(update, "\n".join(lines))


@auth_required
async def cmd_chain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run a saved chain: /chain <name>"""
    await _load_prefs(_chat_id(update))
    if not context.args:
        await _send(update, "Usage: /chain <name>\nSee /chains for available chains.")
        return

    name = context.args[0]
    chat_id = _chat_id(update)

    try:
        task = await start_chain(name=name, chat_id=chat_id)
    except ValueError as e:
        await _send(update, f"⚠️ {e}")
        return

    if task is None:
        await _send(update, f"❓ Chain '{name}' not found or empty. See /chains")
        return

    chain = await get_chain_by_name(name)
    total = chain.total_steps if chain else "?"
    await _send(update, f"⛓️ Chain '{name}' started — step 1/{total} → task #{task.id}")


@auth_required
async def cmd_chains(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List saved chains."""
    chains = await list_chains()

    if not chains:
        await _send(update, "📭 No chains saved. Use /savechain to create one.")
        return

    status_emoji = {
        ChainStatus.IDLE: "💤",
        ChainStatus.RUNNING: "🔄",
        ChainStatus.COMPLETED: "✅",
        ChainStatus.FAILED: "❌",
        ChainStatus.CANCELLED: "🚫",
    }

    lines = ["⛓️ *Saved chains:*"]
    for c in chains:
        emoji = status_emoji.get(c.status, "❓")
        lines.append(f"  {emoji} {c.name} ({c.total_steps} steps) — {c.status.value}")
    await _send(update, "\n".join(lines))


@auth_required
async def cmd_delchain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a chain: /delchain <name>"""
    if not context.args:
        await _send(update, "Usage: /delchain <name>")
        return

    name = context.args[0]
    try:
        deleted = await delete_chain(name)
    except ValueError as e:
        await _send(update, f"⚠️ {e}")
        return
    if deleted:
        await _send(update, f"🗑️ Chain '{name}' deleted.")
    else:
        await _send(update, f"❓ Chain '{name}' not found.")


# ── Text message → enqueue ──────────────────────────────────────────

def _sanitize_text(text: str) -> str:
    """Strip null bytes and control characters from user input."""
    return text.replace("\x00", "").strip()


@auth_required
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any plain text → auto-queue with default agent, show switch buttons."""
    await _load_prefs(_chat_id(update))
    prompt = _sanitize_text(update.message.text or "")  # type: ignore[union-attr]
    if not prompt:
        return
    if len(prompt) > MAX_PROMPT_LEN:
        await _send(update, f"⚠️ Prompt too long ({len(prompt)} chars). Max is {MAX_PROMPT_LEN}.")
        return

    chat_id = _chat_id(update)
    current_agent = _agent(chat_id)

    try:
        task = await enqueue_task(
            prompt=prompt,
            project_dir=_project_dir(chat_id),
            agent=current_agent,
            chat_id=chat_id,
            msg_id=update.message.message_id,  # type: ignore[union-attr]
        )
    except ValueError:
        await _send(update, "⚠️ Queue is full. Wait for tasks to complete.")
        return
    except Exception:
        logger.exception("Failed to enqueue task")
        await _send(update, "⚠️ Failed to queue task. Check server logs.")
        return

    project_display = task.project_dir.replace(os.path.expanduser('~'), '~')

    # Build switch buttons for other agents
    other_agents = [a for a in settings.agent_commands if a != current_agent]
    if other_agents:
        buttons = [
            InlineKeyboardButton(f"↻ {name}", callback_data=f"switch:{task.id}:{name}")
            for name in other_agents
        ]
        keyboard = InlineKeyboardMarkup([buttons])
        await update.message.reply_text(  # type: ignore[union-attr]
            f"📋 Queued #{task.id} (`{task.agent}`) in `{project_display}`\n"
            f"_Switch agent before it starts:_",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(  # type: ignore[union-attr]
            f"📋 Queued #{task.id} (`{task.agent}`) in `{project_display}`",
            parse_mode=ParseMode.MARKDOWN,
        )


@auth_required
async def handle_agent_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button press to switch agent on a pending task."""
    query = update.callback_query
    await query.answer()  # type: ignore[union-attr]

    data = query.data or ""  # type: ignore[union-attr]
    if not data.startswith("switch:"):
        return

    # Parse switch:<task_id>:<agent_name>
    parts = data.split(":", 2)
    if len(parts) != 3:
        return
    try:
        task_id = int(parts[1])
    except ValueError:
        return
    new_agent = parts[2]

    if new_agent not in settings.agent_commands:
        await query.edit_message_text("❓ Unknown agent.")  # type: ignore[union-attr]
        return

    updated = await switch_task_agent(task_id, new_agent)
    if updated:
        project_display = updated.project_dir.replace(os.path.expanduser('~'), '~')
        await query.edit_message_text(  # type: ignore[union-attr]
            f"📋 Switched #{updated.id} → `{updated.agent}` in `{project_display}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await query.edit_message_text(  # type: ignore[union-attr]
            f"⚠️ Task #{task_id} already started or not found — can't switch."
        )


# ── Build the Application ───────────────────────────────────────────

def build_app(runner=None) -> Application:
    """Create and configure the Telegram Application."""
    global _runner_ref
    _runner_ref = runner

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("agent", cmd_agent))
    app.add_handler(CommandHandler("output", cmd_output))
    app.add_handler(CommandHandler("retry", cmd_retry))
    app.add_handler(CommandHandler("repeat", cmd_repeat))
    app.add_handler(CommandHandler("savechain", cmd_savechain))
    app.add_handler(CommandHandler("chain", cmd_chain))
    app.add_handler(CommandHandler("chains", cmd_chains))
    app.add_handler(CommandHandler("delchain", cmd_delchain))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_agent_callback, pattern=r"^switch:"))

    return app

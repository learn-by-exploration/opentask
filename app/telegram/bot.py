"""Telegram bot — handles commands, message routing, and notifications."""

from __future__ import annotations

import io
import logging
import os
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
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
    _apply_recipe_to_task,
    advance_chain,
    bump_task,
    cancel_running_task,
    cancel_task_by_id,
    delete_chain,
    delete_recipe,
    enqueue_followup,
    enqueue_repeat_task,
    enqueue_task,
    get_chain_by_name,
    get_chat_prefs,
    get_pending_tasks,
    get_recent_tasks,
    get_recipe_by_name,
    get_running_task,
    get_task_by_id,
    install_all_gallery_recipes,
    install_gallery_category,
    install_gallery_recipe,
    list_chains,
    list_recipes,
    match_recipe,
    retry_task,
    save_chain,
    save_recipe,
    search_tasks,
    set_chat_pref,
    start_chain,
    switch_task_agent,
    switch_task_model,
    switch_task_worker,
)
from app.core.models import ChainStatus, Task, TaskStatus

logger = logging.getLogger(__name__)

# ── Mutable session state per chat ──────────────────────────────────

_chat_project_dir: dict[int, str] = {}
_chat_agent: dict[int, str] = {}
_chat_model: dict[int, str] = {}
_chat_followup: dict[int, int] = {}  # chat_id → parent_task_id for follow-up mode
_runner_ref = None  # set by build_app() to enable /cancel subprocess kill

MAX_MSG_LEN = 4096


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
    """Load persisted prefs into cache if not already fetched from DB.

    Tracked by a ``_loaded`` set so we hit the DB exactly once per
    chat_id per process lifetime.  Values set by explicit commands
    (``/agent``, ``/model``, ``/project``) are never overwritten.
    """
    _loaded: set | None = getattr(_load_prefs, "_loaded", None)  # type: ignore[attr-defined]
    if _loaded is None:
        _loaded = set()
        _load_prefs._loaded = _loaded  # type: ignore[attr-defined]

    if chat_id in _loaded:
        return

    prefs = await get_chat_prefs(chat_id)
    if prefs["project_dir"] and chat_id not in _chat_project_dir:
        _chat_project_dir[chat_id] = prefs["project_dir"]
    if prefs["agent"] and chat_id not in _chat_agent:
        _chat_agent[chat_id] = prefs["agent"]
    if prefs.get("model") and chat_id not in _chat_model:
        _chat_model[chat_id] = prefs["model"]
    _loaded.add(chat_id)


def _project_dir(chat_id: int) -> str:
    return _chat_project_dir.get(chat_id, settings.default_project_dir)


def _agent(chat_id: int) -> str:
    return _chat_agent.get(chat_id, settings.default_agent)


def _model(chat_id: int) -> str:
    """Return current model for the chat, or empty string for agent default."""
    return _chat_model.get(chat_id, settings.default_model)


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


async def _send(update: Update, text: str, parse_mode: str | None = ParseMode.MARKDOWN, reply_markup=None) -> None:
    """Send a message, splitting at MAX_MSG_LEN if needed."""
    chat_id = _chat_id(update)
    chunks = [text[i : i + MAX_MSG_LEN] for i in range(0, len(text), MAX_MSG_LEN)]
    for idx, chunk in enumerate(chunks):
        # Only attach reply_markup to the last chunk
        markup = reply_markup if idx == len(chunks) - 1 else None
        try:
            await update.get_bot().send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=parse_mode,
                reply_markup=markup,
            )
        except Exception:
            # Fallback to plain text if markdown fails (e.g. unmatched backticks)
            await update.get_bot().send_message(
                chat_id=chat_id,
                text=chunk,
                reply_markup=markup,
            )


def _format_task(task: Task) -> str:
    emoji = _status_emoji(task.status)
    dur = f"  `{task.duration_seconds}s`" if task.duration_seconds else ""
    prompt_short = task.prompt[:60].replace('`', "'")
    return f"{emoji} *#{task.id}*  `{task.agent}`  {prompt_short}{dur}"


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
        dur = f"  ({task.duration_seconds}s)" if task.duration_seconds else ""
        status_label = task.status.value.upper()
        text = f"{emoji} *Task #{task.id} — {status_label}*{dur}\n"
        text += f"`{task.agent}`"
        if task.model:
            text += f"  model: `{task.model}`"
        text += "\n"
        if task.output_summary:
            # Wrap summary in a clean block
            summary = task.output_summary.replace('`', "'")
            text += f"\n```\n{summary}\n```\n"
        if task.error_message:
            err = task.error_message.replace('`', "'")
            text += f"\n⚠️ `{err}`\n"
        # Git diff summary for successful tasks
        git_diff = getattr(task, "git_diff", None)
        if git_diff and isinstance(git_diff, str):
            diff_text = git_diff.replace('`', "'")
            text += f"\n📝 *Files changed:*\n```\n{diff_text}\n```\n"
        # Auto-retry indicator
        retry_count = getattr(task, "retry_count", 0)
        max_retries = getattr(task, "max_retries", 1)
        if isinstance(retry_count, int) and retry_count > 0:
            text += f"\n🔄 _Auto-retry attempt {retry_count}/{max_retries}_\n"

        # Post-completion action buttons
        buttons = []
        if task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            buttons.append(InlineKeyboardButton("🔄 Retry", callback_data=f"taskretry:{task.id}"))
        buttons.append(InlineKeyboardButton("📤 Full Output", callback_data=f"taskoutput:{task.id}"))
        buttons.append(InlineKeyboardButton("💬 Follow Up", callback_data=f"taskfollowup:{task.id}"))
        keyboard = InlineKeyboardMarkup([buttons])

        # Auto-continue conversation mode for follow-up tasks
        # Only continue if the task actually succeeded — failed follow-ups
        # should break the chain so users don't get stuck in a failing loop.
        if task.parent_task_id and task.telegram_chat_id:
            if task.status == TaskStatus.COMPLETED:
                _chat_followup[task.telegram_chat_id] = task.id
                text += "\n💬 _Conversation active — just type your next message._"
            else:
                # Break follow-up chain on failure
                _chat_followup.pop(task.telegram_chat_id, None)
                text += "\n⚠️ _Follow-up ended — task failed. Use 💬 Follow Up to retry._"

        for i in range(0, len(text), MAX_MSG_LEN):
            # Only attach keyboard to the last chunk
            reply_markup = keyboard if i + MAX_MSG_LEN >= len(text) else None
            try:
                await app.bot.send_message(
                    chat_id=task.telegram_chat_id,
                    text=text[i : i + MAX_MSG_LEN],
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                await app.bot.send_message(
                    chat_id=task.telegram_chat_id,
                    text=text[i : i + MAX_MSG_LEN],
                    reply_markup=reply_markup,
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

# Persistent reply keyboard shown at bottom of chat
_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("/status"), KeyboardButton("/queue"), KeyboardButton("/history")],
        [KeyboardButton("/cancel"), KeyboardButton("/help")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


@auth_required
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _load_prefs(_chat_id(update))
    chat_id = _chat_id(update)
    agent = _agent(chat_id)
    project = _project_dir(chat_id).replace(os.path.expanduser('~'), '~')
    text = (
        f"👋 *TaskPilot ready*\n\n"
        f"🤖 Agent: `{agent}`\n"
        f"📁 Project: `{project}`\n\n"
        "Send any text to create a task, or tap a button:"
    )
    inline_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Status", callback_data="menu:status"),
            InlineKeyboardButton("📦 Queue", callback_data="menu:queue"),
            InlineKeyboardButton("📜 History", callback_data="menu:history"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings"),
            InlineKeyboardButton("❓ Help", callback_data="menu:help"),
        ],
    ])
    # Send persistent reply keyboard first, then inline keyboard
    await update.get_bot().send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=_REPLY_KEYBOARD,
        parse_mode=ParseMode.MARKDOWN,
    )
    await update.get_bot().send_message(
        chat_id=chat_id,
        text="Quick actions:",
        reply_markup=inline_keyboard,
    )


@auth_required
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📖 *TaskPilot Commands*\n\n"
        "/status — current task status\n"
        "/queue — pending tasks\n"
        "/history [N] — recent tasks\n"
        "/cancel [id] — cancel running or pending task\n"
        "/retry <id> — re-queue a failed task\n"
        "/continue <id> <prompt> — follow-up on a completed task\n"
        "/cancel\\_followup — exit follow-up mode\n"
        "/project <path> — set project dir\n"
        "/agent <name> — set agent\n"
        "/model <name> — set model (sonnet, opus, etc.)\n"
        "/setmodel <id> <name> — set model for a pending task\n"
        "/output <id> — get full output of a task\n\n"
        "*Repeat:*\n"
        "/repeat <N> <prompt> — repeat N times\n"
        "/repeat until:HH:MM <prompt> — repeat until time\n\n"
        "*Chains:*\n"
        "/savechain <name> step1 | step2 | step3\n"
        "/chain <name> — run a saved chain\n"
        "/chains — list all chains\n"
        "/delchain <name> — delete a chain\n\n"
        "*Recipes:*\n"
        "/addrecipe <name> triggers:kw1,kw2 [agent:name] [model:name]\n"
        "/recipes — list recipes\n"
        "/recipe <name> — show recipe details\n"
        "/delrecipe <name> — delete a recipe\n"
        "/gallery — browse & install pre-made recipes\n\n"
        "*Queue management:*\n"
        "/bump <id> — move a task to front of queue\n"
        "/search <query> — search tasks by prompt text\n\n"
        "/help — this message\n\n"
        "Or just send any text to queue a task.\n"
        "💡 Tap *Follow Up* on any completed task to continue the discussion."
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
            elapsed = f"  ⏱ `{mins}m{secs:02d}s`" if mins else f"  ⏱ `{secs}s`"
        prompt_short = task.prompt[:80].replace('`', "'")
        model_tag = f"  model: `{task.model}`" if task.model else ""
        worker_tag = f"  → @{task.assigned_to}" if task.assigned_to else ""
        await _send(
            update,
            f"🔄 *Running — Task #{task.id}*{elapsed}\n"
            f"`{task.agent}`{model_tag}{worker_tag}\n"
            f"_{prompt_short}_",
        )


@auth_required
async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _load_prefs(_chat_id(update))
    tasks = await get_pending_tasks()
    if not tasks:
        await _send(update, "📭 Queue is empty.")
    else:
        lines = ["📦 *Pending tasks:*\n"]
        for t in tasks:
            lines.append(_format_task(t))
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
        lines = [f"📜 *Recent tasks (last {len(tasks)}):*\n"]
        for t in tasks:
            lines.append(_format_task(t))
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
async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or show the model override for the current chat."""
    if context.args:
        name = context.args[0]
        if name.lower() in ("none", "default", "reset", "clear"):
            chat_id = _chat_id(update)
            _chat_model.pop(chat_id, None)
            await set_chat_pref(chat_id, model="")
            await _send(update, "🧠 Model reset to agent default")
            return
        chat_id = _chat_id(update)
        _chat_model[chat_id] = name
        await set_chat_pref(chat_id, model=name)
        await _send(update, f"🧠 Model set to: `{name}`")
    else:
        current = _model(_chat_id(update))
        if current:
            await _send(update, f"🧠 Current model: `{current}`\nUse `/model none` to reset to agent default")
        else:
            await _send(update, "🧠 Using agent default model\nUse `/model <name>` to override (e.g. `sonnet`, `opus`, `anthropic/claude-sonnet-4`)")


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
            caption=f"📄 Full output for *#{task_id}* ({len(task.full_output)} chars)",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        escaped = task.full_output.replace('`', "'")
        await _send(update, f"📄 *Output for #{task_id}:*\n\n```\n{escaped}\n```")


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


# ── Bump command ────────────────────────────────────────────────────

@auth_required
async def cmd_bump(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bump a pending task to the front of the queue: /bump <id>"""
    if not context.args:
        await _send(update, "Usage: /bump <task\\_id>")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await _send(update, "Invalid task ID.")
        return

    task = await bump_task(task_id)
    if task is None:
        await _send(update, f"⚠️ Task #{task_id} not found or not pending.")
    else:
        await _send(update, f"⬆️ *#{task.id}* bumped to priority `{task.priority}`")


# ── Search command ──────────────────────────────────────────────────

@auth_required
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search tasks by prompt text: /search <query>"""
    if not context.args:
        await _send(update, "Usage: /search <query>")
        return

    query = " ".join(context.args)
    tasks = await search_tasks(query, limit=10)
    if not tasks:
        await _send(update, f"🔍 No tasks matching _{query}_")
        return

    lines = [f"🔍 *Search results for* _{query}_:\n"]
    for t in tasks:
        lines.append(_format_task(t))
    await _send(update, "\n".join(lines))


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
    if len(prompt) > settings.max_prompt_len:
        await _send(update, f"⚠️ Prompt too long ({len(prompt)} chars). Max is {settings.max_prompt_len}.")
        return

    chat_id = _chat_id(update)
    try:
        task = await enqueue_repeat_task(
            prompt=prompt,
            repeat_count=repeat_count,
            repeat_until=repeat_until,
            project_dir=_project_dir(chat_id),
            agent=_agent(chat_id),
            model=_model(chat_id) or None,
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
    current_model = _model(chat_id)
    for raw in raw_steps:
        step = {
            "prompt": raw,
            "agent": _agent(chat_id),
            "project_dir": _project_dir(chat_id),
        }
        if current_model:
            step["model"] = current_model
        steps.append(step)

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


# ── Follow-up / continue commands ───────────────────────────────────


@auth_required
async def cmd_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/continue <task_id> <prompt> — send a follow-up to a completed task."""
    parts = (update.message.text or "").split(None, 2)  # type: ignore[union-attr]
    if len(parts) < 3:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Usage: `/continue <task_id> <prompt>`", parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        parent_id = int(parts[1])
    except ValueError:
        await _send(update, "⚠️ Invalid task ID.")
        return

    prompt = _sanitize_text(parts[2])
    if not prompt:
        await _send(update, "⚠️ Prompt cannot be empty.")
        return
    if len(prompt) > settings.max_prompt_len:
        await _send(update, f"⚠️ Prompt too long ({len(prompt)} chars). Max is {settings.max_prompt_len}.")
        return

    chat_id = _chat_id(update)
    try:
        task = await enqueue_followup(
            parent_task_id=parent_id,
            prompt=prompt,
            chat_id=chat_id,
            msg_id=update.message.message_id,  # type: ignore[union-attr]
        )
    except ValueError as e:
        await _send(update, f"⚠️ {e}")
        return

    await update.message.reply_text(  # type: ignore[union-attr]
        f"💬 Follow-up #{task.id} → #{parent_id} (`{task.agent}`)\n"
        f"_Continues the previous session._",
        parse_mode=ParseMode.MARKDOWN,
    )


@auth_required
async def cmd_cancel_followup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel_followup — exit follow-up mode."""
    chat_id = _chat_id(update)
    parent_id = _chat_followup.pop(chat_id, None)
    if parent_id:
        await _send(update, f"✅ Exited follow-up mode (was following task #{parent_id}).")
    else:
        await _send(update, "ℹ️ Not in follow-up mode.")


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
    if len(prompt) > settings.max_prompt_len:
        await _send(update, f"⚠️ Prompt too long ({len(prompt)} chars). Max is {settings.max_prompt_len}.")
        return

    chat_id = _chat_id(update)

    # Follow-up mode: if active, route to follow-up instead of new task
    parent_id = _chat_followup.get(chat_id)
    if parent_id:
        try:
            task = await enqueue_followup(
                parent_task_id=parent_id,
                prompt=prompt,
                chat_id=chat_id,
                msg_id=update.message.message_id,  # type: ignore[union-attr]
            )
        except ValueError as e:
            await _send(update, f"⚠️ {e}")
            return
        # Update conversation pointer to the new task
        _chat_followup[chat_id] = task.id
        await update.message.reply_text(  # type: ignore[union-attr]
            f"💬 Follow-up #{task.id} → #{parent_id} (`{task.agent}`)\n"
            f"_Conversation active — keep typing or /cancel\\_followup to exit._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    current_agent = _agent(chat_id)
    current_model = _model(chat_id)

    # Parse @worker_name hint from the prompt (e.g. "@server2 fix the bug")
    assigned_to = None
    clean_prompt = prompt
    if prompt.startswith("@"):
        parts = prompt.split(None, 1)
        if len(parts) >= 2:
            assigned_to = parts[0][1:]  # strip the @ prefix
            clean_prompt = parts[1]
        # if only "@server2" with no task text, treat whole thing as prompt

    try:
        task = await enqueue_task(
            prompt=clean_prompt,
            project_dir=_project_dir(chat_id),
            agent=current_agent,
            chat_id=chat_id,
            msg_id=update.message.message_id,  # type: ignore[union-attr]
            model=current_model or None,
            assigned_to=assigned_to,
        )
    except ValueError:
        await _send(update, "⚠️ Queue is full. Wait for tasks to complete.")
        return
    except Exception:
        logger.exception("Failed to enqueue task")
        await _send(update, "⚠️ Failed to queue task. Check server logs.")
        return

    project_display = task.project_dir.replace(os.path.expanduser('~'), '~')
    model_display = f"  model: `{task.model}`" if task.model else ""
    worker_display = f"  → `{task.assigned_to}`" if task.assigned_to else ""

    # Check for recipe match
    recipe = await match_recipe(clean_prompt)

    # Build switch buttons for other agents
    other_agents = [a for a in settings.agent_commands if a != current_agent]
    agent_buttons = []
    if other_agents:
        agent_buttons = [
            InlineKeyboardButton(f"↻ {name}", callback_data=f"switch:{task.id}:{name}")
            for name in other_agents
        ]

    # Build direct model choice buttons (no two-step — task starts fast)
    model_choices = ["sonnet", "opus", "haiku"]
    model_buttons = [
        InlineKeyboardButton(f"🧠 {m}", callback_data=f"modelswitch:{task.id}:{m}")
        for m in model_choices
    ]

    # Build worker/server buttons from known_workers setting
    worker_names = settings.known_workers_list
    worker_buttons = []
    if worker_names:
        if not assigned_to:
            # Show "🖥 local" as current, plus each remote worker
            worker_buttons = [
                InlineKeyboardButton(
                    f"🖥 {w}", callback_data=f"workerswitch:{task.id}:{w}"
                )
                for w in worker_names
            ]
        else:
            # Already assigned — show local + other workers
            worker_buttons = [
                InlineKeyboardButton(
                    "🖥 local", callback_data=f"workerswitch:{task.id}:__local__"
                )
            ]
            worker_buttons += [
                InlineKeyboardButton(
                    f"🖥 {w}", callback_data=f"workerswitch:{task.id}:{w}"
                )
                for w in worker_names if w != assigned_to
            ]

    if recipe:
        # Offer to apply recipe
        recipe_buttons = [
            InlineKeyboardButton(
                f"🧪 Use '{recipe.name}'",
                callback_data=f"recipeuse:{task.id}:{recipe.name}",
            ),
            InlineKeyboardButton("⏭ Skip", callback_data=f"recipeskip:{task.id}"),
        ]
        rows = [recipe_buttons]
        if agent_buttons:
            rows.append(agent_buttons)
        rows.append(model_buttons)
        if worker_buttons:
            rows.append(worker_buttons)
        keyboard = InlineKeyboardMarkup(rows)
        await update.message.reply_text(  # type: ignore[union-attr]
            f"📋 *Queued #{task.id}*  `{task.agent}`{model_display}{worker_display}\n"
            f"📂 `{project_display}`\n\n"
            f"🧪 Recipe *{recipe.name}* matched — apply it?",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        rows = []
        if agent_buttons:
            rows.append(agent_buttons)
        rows.append(model_buttons)
        if worker_buttons:
            rows.append(worker_buttons)
        keyboard = InlineKeyboardMarkup(rows)
        prompt_short = task.prompt[:80].replace('`', "'")
        await update.message.reply_text(  # type: ignore[union-attr]
            f"📋 *Queued #{task.id}*  `{task.agent}`{model_display}{worker_display}\n"
            f"📂 `{project_display}`\n\n"
            f"_{prompt_short}_",
            reply_markup=keyboard,
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
            f"✅ *#{updated.id}* switched → `{updated.agent}`\n📂 `{project_display}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await query.edit_message_text(  # type: ignore[union-attr]
            f"⚠️ Task #{task_id} already started or not found — can't switch."
        )


@auth_required
async def handle_model_set_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 🧠 Model button — show model choices for a pending task."""
    query = update.callback_query
    await query.answer()  # type: ignore[union-attr]

    data = query.data or ""  # type: ignore[union-attr]
    # modelset:<task_id>
    parts = data.split(":", 1)
    if len(parts) != 2:
        return
    try:
        task_id = int(parts[1])
    except ValueError:
        return

    task = await get_task_by_id(task_id)
    if task is None or task.status != TaskStatus.PENDING:
        await query.edit_message_text(  # type: ignore[union-attr]
            f"⚠️ Task #{task_id} already started or not found."
        )
        return

    # Quick-pick model buttons: common aliases + clear
    model_choices = ["sonnet", "opus", "haiku"]
    buttons = [
        InlineKeyboardButton(f"🧠 {m}", callback_data=f"modelswitch:{task_id}:{m}")
        for m in model_choices
    ]
    buttons.append(
        InlineKeyboardButton("🔄 Default", callback_data=f"modelswitch:{task_id}:__default__")
    )
    project_display = task.project_dir.replace(os.path.expanduser('~'), '~')
    model_display = f"  model: `{task.model}`" if task.model else ""
    await query.edit_message_text(  # type: ignore[union-attr]
        f"🧠 *Task #{task_id}*  `{task.agent}`{model_display}\n"
        f"📂 `{project_display}`\n\n"
        f"_Pick a model:_",
        reply_markup=InlineKeyboardMarkup([buttons]),
        parse_mode=ParseMode.MARKDOWN,
    )


@auth_required
async def handle_model_switch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle model choice button — apply the selected model to a task."""
    query = update.callback_query
    await query.answer()  # type: ignore[union-attr]

    data = query.data or ""  # type: ignore[union-attr]
    # modelswitch:<task_id>:<model_name>
    parts = data.split(":", 2)
    if len(parts) != 3:
        return
    try:
        task_id = int(parts[1])
    except ValueError:
        return
    model_name = parts[2]

    # __default__ means clear the model override
    if model_name == "__default__":
        model_name = ""

    updated = await switch_task_model(task_id, model_name)
    if updated:
        project_display = updated.project_dir.replace(os.path.expanduser('~'), '~')
        model_text = f"model: `{updated.model}`" if updated.model else "model: _agent default_"
        await query.edit_message_text(  # type: ignore[union-attr]
            f"✅ *#{updated.id}*  `{updated.agent}`  {model_text}\n📂 `{project_display}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await query.edit_message_text(  # type: ignore[union-attr]
            f"⚠️ Task #{task_id} already started or not found — can't switch model."
        )


@auth_required
async def handle_worker_switch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 🖥 worker button — reassign a pending task to a different server."""
    query = update.callback_query
    await query.answer()  # type: ignore[union-attr]

    data = query.data or ""  # type: ignore[union-attr]
    # workerswitch:<task_id>:<worker_name>
    parts = data.split(":", 2)
    if len(parts) != 3:
        return
    try:
        task_id = int(parts[1])
    except ValueError:
        return
    worker_name = parts[2]

    # __local__ means clear assignment (run on local machine)
    if worker_name == "__local__":
        worker_name = ""

    updated = await switch_task_worker(task_id, worker_name)
    if updated:
        project_display = updated.project_dir.replace(os.path.expanduser('~'), '~')
        worker_text = f"→ `{updated.assigned_to}`" if updated.assigned_to else "→ _local_"
        await query.edit_message_text(  # type: ignore[union-attr]
            f"✅ *#{updated.id}*  `{updated.agent}`  {worker_text}\n📂 `{project_display}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await query.edit_message_text(  # type: ignore[union-attr]
            f"⚠️ Task #{task_id} already started or not found — can't switch server."
        )


@auth_required
async def cmd_setmodel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a specific model for a pending task: /setmodel <task_id> <model>"""
    if not context.args or len(context.args) < 2:
        await _send(update, "Usage: /setmodel <task\\_id> <model\\_name>")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await _send(update, "Invalid task ID.")
        return
    model_name = context.args[1]
    if model_name.lower() in ("none", "default", "reset", "clear"):
        model_name = ""

    updated = await switch_task_model(task_id, model_name)
    if updated:
        model_text = f"`{updated.model}`" if updated.model else "agent default"
        await _send(update, f"🧠 Task #{task_id} model → {model_text}")
    else:
        await _send(update, f"⚠️ Task #{task_id} already started or not found.")


@auth_required
async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start menu button presses."""
    query = update.callback_query
    await query.answer()  # type: ignore[union-attr]

    data = query.data or ""  # type: ignore[union-attr]
    action = data.split(":", 1)[1] if ":" in data else ""

    chat_id = query.message.chat_id  # type: ignore[union-attr]
    await _load_prefs(chat_id)

    if action == "status":
        task = await get_running_task()
        if task is None:
            text = "💤 No task running."
        else:
            elapsed = ""
            if task.started_at:
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                delta = int((now - task.started_at).total_seconds())
                mins, secs = divmod(delta, 60)
                elapsed = f" — {mins}m{secs:02d}s elapsed" if mins else f" — {secs}s elapsed"
            text = f"🔄 Running: {_format_task(task)}{elapsed}"
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)  # type: ignore[union-attr]

    elif action == "queue":
        tasks = await get_pending_tasks()
        if not tasks:
            text = "📭 Queue is empty."
        else:
            lines = ["📦 *Pending tasks:*"] + [_format_task(t) for t in tasks]
            text = "\n".join(lines)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)  # type: ignore[union-attr]

    elif action == "history":
        tasks = await get_recent_tasks(limit=10)
        if not tasks:
            text = "📭 No tasks yet."
        else:
            lines = [f"📜 *Recent tasks (last {len(tasks)}):*"] + [_format_task(t) for t in tasks]
            text = "\n".join(lines)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)  # type: ignore[union-attr]

    elif action == "help":
        text = (
            "📖 *TaskPilot Commands*\n\n"
            "/status — current task status\n"
            "/queue — pending tasks\n"
            "/history [N] — recent tasks\n"
            "/cancel [id] — cancel running or pending task\n"
            "/retry <id> — re-queue a failed task\n"
            "/continue <id> <prompt> — follow-up on a completed task\n"
            "/cancel\\_followup — exit follow-up mode\n"
            "/project <path> — set project dir\n"
            "/agent <name> — set agent\n"
            "/model <name> — set model (sonnet, opus, etc.)\n"
            "/setmodel <id> <name> — set model for a pending task\n"
            "/output <id> — get full output of a task\n\n"
            "*Repeat:*\n"
            "/repeat <N> <prompt> — repeat N times\n"
            "/repeat until:HH:MM <prompt> — repeat until time\n\n"
            "*Chains:*\n"
            "/savechain <name> step1 | step2 | step3\n"
            "/chain <name> — run a saved chain\n"
            "/chains — list all chains\n"
            "/delchain <name> — delete a chain\n\n"
            "*Recipes:*\n"
            "/addrecipe <name> triggers:kw1,kw2 [agent:name] [model:name]\n"
            "/recipes — list recipes\n"
            "/recipe <name> — show recipe details\n"
            "/delrecipe <name> — delete a recipe\n"
            "/gallery — browse & install pre-made recipes\n\n"
            "*Queue management:*\n"
            "/bump <id> — move a task to front of queue\n"
            "/search <query> — search tasks by prompt text\n\n"
            "Or just send any text to queue a task.\n"
            "💡 Tap *Follow Up* on any completed task to continue the discussion."
        )
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)  # type: ignore[union-attr]

    elif action == "settings":
        agent = _agent(chat_id)
        model = _model(chat_id)
        project = _project_dir(chat_id).replace(os.path.expanduser('~'), '~')
        available_agents = list(settings.agent_commands.keys())

        text = (
            f"⚙️ *Settings*\n\n"
            f"🤖 Agent: `{agent}`\n"
            f"🧠 Model: `{model or 'agent default'}`\n"
            f"📁 Project: `{project}`\n"
        )
        buttons = [
            InlineKeyboardButton(f"{'✅ ' if a == agent else ''}{a}", callback_data=f"setagent:{a}")
            for a in available_agents
        ]
        keyboard = InlineKeyboardMarkup(
            [buttons, [InlineKeyboardButton("◀️ Back", callback_data="menu:back")]]
        )
        await query.edit_message_text(  # type: ignore[union-attr]
            text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "back":
        # Re-show the /start menu
        agent = _agent(chat_id)
        project = _project_dir(chat_id).replace(os.path.expanduser('~'), '~')
        text = (
            f"👋 *TaskPilot ready*\n\n"
            f"🤖 Agent: `{agent}`\n"
            f"📁 Project: `{project}`\n\n"
            "Send any text to create a task, or tap a button:"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📊 Status", callback_data="menu:status"),
                InlineKeyboardButton("📦 Queue", callback_data="menu:queue"),
                InlineKeyboardButton("📜 History", callback_data="menu:history"),
            ],
            [
                InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings"),
                InlineKeyboardButton("❓ Help", callback_data="menu:help"),
            ],
        ])
        await query.edit_message_text(  # type: ignore[union-attr]
            text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN,
        )


@auth_required
async def handle_setagent_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle agent selection from Settings panel."""
    query = update.callback_query
    await query.answer()  # type: ignore[union-attr]

    data = query.data or ""  # type: ignore[union-attr]
    new_agent = data.split(":", 1)[1] if ":" in data else ""

    if new_agent not in settings.agent_commands:
        await query.edit_message_text("❓ Unknown agent.")  # type: ignore[union-attr]
        return

    chat_id = query.message.chat_id  # type: ignore[union-attr]
    _chat_agent[chat_id] = new_agent
    await set_chat_pref(chat_id, agent=new_agent)

    project = _project_dir(chat_id).replace(os.path.expanduser('~'), '~')
    available_agents = list(settings.agent_commands.keys())

    text = (
        f"⚙️ *Settings*\n\n"
        f"🤖 Agent: `{new_agent}` ✓\n"
        f"📁 Project: `{project}`\n"
    )
    buttons = [
        InlineKeyboardButton(f"{'✅ ' if a == new_agent else ''}{a}", callback_data=f"setagent:{a}")
        for a in available_agents
    ]
    keyboard = InlineKeyboardMarkup(
        [buttons, [InlineKeyboardButton("◀️ Back", callback_data="menu:back")]]
    )
    await query.edit_message_text(  # type: ignore[union-attr]
        text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN,
    )


@auth_required
async def handle_task_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle post-completion action buttons (retry, output)."""
    query = update.callback_query
    await query.answer()  # type: ignore[union-attr]

    data = query.data or ""  # type: ignore[union-attr]
    parts = data.split(":", 2)
    if len(parts) != 2:
        return

    action, task_id_str = parts
    try:
        task_id = int(task_id_str)
    except ValueError:
        return

    chat_id = query.message.chat_id  # type: ignore[union-attr]

    if action == "taskretry":
        try:
            new_task = await retry_task(task_id)
        except ValueError as e:
            await query.edit_message_text(f"⚠️ {e}")  # type: ignore[union-attr]
            return
        if new_task is None:
            await query.edit_message_text(  # type: ignore[union-attr]
                f"Task #{task_id} not found or not failed/cancelled."
            )
        else:
            await query.edit_message_text(  # type: ignore[union-attr]
                f"🔄 Retried #{task_id} → new task #{new_task.id} (`{new_task.agent}`)",
                parse_mode=ParseMode.MARKDOWN,
            )

    elif action == "taskoutput":
        task = await get_task_by_id(task_id)
        if task is None:
            await query.edit_message_text(f"Task #{task_id} not found.")  # type: ignore[union-attr]
        elif not task.full_output:
            await query.edit_message_text(  # type: ignore[union-attr]
                f"Task #{task_id} has no output yet."
            )
        elif len(task.full_output) > MAX_MSG_LEN:
            buf = io.BytesIO(task.full_output.encode("utf-8"))
            buf.name = f"task_{task_id}_output.txt"
            await query.message.reply_document(  # type: ignore[union-attr]
                document=buf,
                caption=f"📄 Output for #{task_id} ({len(task.full_output)} chars)",
            )
        else:
            await query.message.reply_text(  # type: ignore[union-attr]
                f"📄 Output for #{task_id}:\n\n{task.full_output}"
            )

    elif action == "taskfollowup":
        task = await get_task_by_id(task_id)
        if task is None:
            await query.edit_message_text(f"Task #{task_id} not found.")  # type: ignore[union-attr]
            return
        if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            await query.edit_message_text(  # type: ignore[union-attr]
                f"⚠️ Task #{task_id} is {task.status.value} — wait for it to finish first."
            )
            return
        # Set follow-up state so next plain text message continues this task
        _chat_followup[chat_id] = task_id
        await query.message.reply_text(  # type: ignore[union-attr]
            f"💬 *Conversation mode* for task #{task_id}\n\n"
            f"Type messages to continue the agent session.\n"
            f"Each reply continues the same conversation.\n"
            f"Send /cancel\\_followup to exit.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ── Recipe commands ─────────────────────────────────────────────────

@auth_required
async def cmd_recipes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all saved recipes."""
    recipes = await list_recipes()
    if not recipes:
        await _send(update, "📭 No recipes saved. Use /addrecipe to create one.")
        return

    lines = ["🧪 *Saved recipes:*"]
    for r in recipes:
        triggers = ", ".join(r.triggers[:5])
        extra = f" (+{len(r.triggers)-5} more)" if len(r.triggers) > 5 else ""
        agent_info = f" [{r.agent}]" if r.agent else ""
        lines.append(f"  • {r.name}{agent_info} — triggers: {triggers}{extra}")
    await _send(update, "\n".join(lines))


@auth_required
async def cmd_addrecipe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a recipe: /addrecipe <name> triggers:kw1,kw2 [agent:name] [prefix:text] [suffix:text] [setup:cmd1|cmd2] [skills:s1,s2]"""
    await _load_prefs(_chat_id(update))
    if not context.args or len(context.args) < 2:
        await _send(
            update,
            "Usage: /addrecipe <name> triggers:kw1,kw2 [agent:name] "
            "[model:name] [prefix:text] [suffix:text] [setup:cmd1|cmd2] [skills:s1,s2]",
        )
        return

    name = _sanitize_text(context.args[0])
    if not name or len(name) > 128:
        await _send(update, "⚠️ Recipe name must be 1-128 characters.")
        return

    rest = " ".join(context.args[1:])
    triggers: list[str] = []
    agent: str | None = None
    model: str | None = None
    project_dir: str | None = None
    prefix: str | None = None
    suffix: str | None = None
    setup_commands: list[str] = []
    skills: list[str] = []

    # Parse key:value pairs from rest
    import re
    # Extract triggers:...
    m = re.search(r'triggers?:([^\s]+)', rest)
    if m:
        triggers = [t.strip() for t in m.group(1).split(",") if t.strip()]
    # Extract agent:...
    m = re.search(r'agent:([^\s]+)', rest)
    if m:
        agent = m.group(1)
    # Extract model:...
    m = re.search(r'model:([^\s]+)', rest)
    if m:
        model = m.group(1)
    # Extract project:...
    m = re.search(r'project:([^\s]+)', rest)
    if m:
        project_dir = m.group(1)
    # Extract prefix:...
    m = re.search(r'prefix:(.+?)(?=\s+\w+:|$)', rest)
    if m:
        prefix = m.group(1).strip()
    # Extract suffix:...
    m = re.search(r'suffix:(.+?)(?=\s+\w+:|$)', rest)
    if m:
        suffix = m.group(1).strip()
    # Extract setup:cmd1|cmd2
    m = re.search(r'setup:(.+?)(?=\s+\w+:|$)', rest)
    if m:
        setup_commands = [c.strip() for c in m.group(1).split("|") if c.strip()]
    # Extract skills:s1,s2
    m = re.search(r'skills?:([^\s]+)', rest)
    if m:
        skills = [s.strip() for s in m.group(1).split(",") if s.strip()]

    if not triggers:
        await _send(update, "⚠️ At least one trigger keyword is required. Use triggers:kw1,kw2")
        return

    chat_id = _chat_id(update)
    try:
        recipe = await save_recipe(
            name=name,
            triggers=triggers,
            agent=agent,
            model=model,
            project_dir=project_dir,
            setup_commands=setup_commands,
            skills=skills,
            prompt_prefix=prefix,
            prompt_suffix=suffix,
            chat_id=chat_id,
        )
    except ValueError as e:
        await _send(update, f"⚠️ {e}")
        return

    lines = [f"🧪 Recipe '{recipe.name}' saved:"]
    lines.append(f"  Triggers: {', '.join(recipe.triggers)}")
    if recipe.agent:
        lines.append(f"  Agent: {recipe.agent}")
    if recipe.model:
        lines.append(f"  Model: {recipe.model}")
    if recipe.setup_commands:
        lines.append(f"  Setup: {len(recipe.setup_commands)} command(s)")
    if recipe.skills:
        lines.append(f"  Skills: {', '.join(recipe.skills)}")
    if recipe.prompt_prefix:
        lines.append(f"  Prefix: {recipe.prompt_prefix[:60]}")
    if recipe.prompt_suffix:
        lines.append(f"  Suffix: {recipe.prompt_suffix[:60]}")
    await _send(update, "\n".join(lines))


@auth_required
async def cmd_delrecipe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a recipe: /delrecipe <name>"""
    if not context.args:
        await _send(update, "Usage: /delrecipe <name>")
        return

    name = context.args[0]
    deleted = await delete_recipe(name)
    if deleted:
        await _send(update, f"🗑️ Recipe '{name}' deleted.")
    else:
        await _send(update, f"❓ Recipe '{name}' not found.")


@auth_required
async def cmd_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show details of a recipe: /recipe <name>"""
    if not context.args:
        await _send(update, "Usage: /recipe <name>")
        return

    name = context.args[0]
    recipe = await get_recipe_by_name(name)
    if recipe is None:
        await _send(update, f"❓ Recipe '{name}' not found.")
        return

    lines = [f"🧪 *Recipe: {recipe.name}*"]
    lines.append(f"  Triggers: {', '.join(recipe.triggers)}")
    if recipe.agent:
        lines.append(f"  Agent: `{recipe.agent}`")
    if recipe.model:
        lines.append(f"  Model: `{recipe.model}`")
    if recipe.project_dir:
        lines.append(f"  Project dir: `{recipe.project_dir}`")
    if recipe.setup_commands:
        lines.append(f"  Setup commands:")
        for cmd in recipe.setup_commands:
            lines.append(f"    `{cmd[:80]}`")
    if recipe.skills:
        lines.append(f"  Skills: {', '.join(recipe.skills)}")
    if recipe.prompt_prefix:
        lines.append(f"  Prefix: {recipe.prompt_prefix[:100]}")
    if recipe.prompt_suffix:
        lines.append(f"  Suffix: {recipe.prompt_suffix[:100]}")
    await _send(update, "\n".join(lines))


# ── Gallery commands ────────────────────────────────────────────────

@auth_required
async def cmd_gallery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Browse the recipe gallery: /gallery [category|all]"""
    from app.core.gallery import GALLERY, get_gallery_by_category, get_gallery_categories, get_gallery_item

    args = context.args or []

    # /gallery install <name> — install a single recipe
    if len(args) >= 2 and args[0] == "install":
        name = args[1]
        item = get_gallery_item(name)
        if not item:
            await _send(update, f"❓ '{name}' not found in gallery. Try /gallery to browse.")
            return
        chat_id = _chat_id(update)
        recipe = await install_gallery_recipe(name, chat_id=chat_id)
        if recipe:
            await _send(update, f"✅ Installed recipe *{recipe.name}* ({item['description']})")
        return

    # /gallery install-all — install everything
    if len(args) >= 1 and args[0] == "install-all":
        chat_id = _chat_id(update)
        installed = await install_all_gallery_recipes(chat_id=chat_id)
        await _send(update, f"✅ Installed *{len(installed)}* recipes from the gallery.")
        return

    # /gallery install-category <cat> — install a category
    if len(args) >= 2 and args[0] == "install-category":
        cat_name = " ".join(args[1:])
        items = get_gallery_by_category(cat_name)
        if not items:
            cats = get_gallery_categories()
            await _send(update, f"❓ Category '{cat_name}' not found. Available: {', '.join(cats)}")
            return
        chat_id = _chat_id(update)
        installed = await install_gallery_category(cat_name, chat_id=chat_id)
        await _send(update, f"✅ Installed *{len(installed)}* recipes from _{cat_name}_.")
        return

    # /gallery <category> — show one category
    if args:
        cat_name = " ".join(args)
        items = get_gallery_by_category(cat_name)
        if not items:
            cats = get_gallery_categories()
            await _send(update, f"❓ Category '{cat_name}' not found. Available: {', '.join(cats)}")
            return
        lines = [f"📦 *{cat_name}* recipes:"]
        for item in items:
            lines.append(f"  • *{item['name']}* — {item['description']}")
        lines.append(f"\n/gallery install-category {cat_name}")
        lines.append("Or: /gallery install <name>")
        await _send(update, "\n".join(lines))
        return

    # /gallery — show all categories with inline install buttons
    categories = get_gallery_categories()
    lines = ["🏪 *Recipe Gallery*\n"]
    for cat in categories:
        items = get_gallery_by_category(cat)
        lines.append(f"*{cat}* ({len(items)} recipes):")
        for item in items:
            lines.append(f"  • `{item['name']}` — {item['description']}")
        lines.append("")

    lines.append("*Install:*")
    lines.append("/gallery install <name> — install one")
    lines.append("/gallery install-category <category> — install a category")
    lines.append("/gallery install-all — install all recipes")

    # Add inline buttons for quick category install
    buttons = []
    for cat in categories:
        items = get_gallery_by_category(cat)
        buttons.append(
            [InlineKeyboardButton(
                f"📥 Install {cat} ({len(items)})",
                callback_data=f"gallerycat:{cat}",
            )]
        )
    buttons.append(
        [InlineKeyboardButton(
            f"📥 Install All ({len(GALLERY)})",
            callback_data="galleryall:",
        )]
    )

    await _send(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


@auth_required
async def handle_gallery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle gallery install callbacks."""
    query = update.callback_query
    await query.answer()  # type: ignore[union-attr]

    data = query.data or ""  # type: ignore[union-attr]
    chat_id = query.message.chat_id  # type: ignore[union-attr]

    if data.startswith("gallerycat:"):
        cat_name = data.split(":", 1)[1]
        installed = await install_gallery_category(cat_name, chat_id=chat_id)
        if installed:
            await query.edit_message_text(  # type: ignore[union-attr]
                f"✅ Installed *{len(installed)}* recipes from _{cat_name}_.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await query.edit_message_text(  # type: ignore[union-attr]
                f"❓ Category '{cat_name}' not found.",
            )

    elif data.startswith("galleryall:"):
        installed = await install_all_gallery_recipes(chat_id=chat_id)
        await query.edit_message_text(  # type: ignore[union-attr]
            f"✅ Installed *{len(installed)}* recipes from the gallery.",
            parse_mode=ParseMode.MARKDOWN,
        )


@auth_required
async def handle_recipe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle recipe confirmation/skip callback from auto-match."""
    query = update.callback_query
    await query.answer()  # type: ignore[union-attr]

    data = query.data or ""  # type: ignore[union-attr]
    # Split only on first colon: "recipeuse:42:ros" → action="recipeuse", rest="42:ros"
    if ":" not in data:
        return

    action, rest = data.split(":", 1)

    chat_id = query.message.chat_id  # type: ignore[union-attr]
    await _load_prefs(chat_id)

    if action == "recipeskip":
        # User chose to skip recipe — task is already queued without enrichment
        try:
            task_id = int(rest)
        except ValueError:
            return
        await query.edit_message_text(  # type: ignore[union-attr]
            f"📋 Task *#{task_id}* queued without recipe.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "recipeuse":
        # Parse recipeuse:<task_id>:<recipe_name>
        sub_parts = rest.split(":", 1)
        if len(sub_parts) != 2:
            return
        try:
            task_id = int(sub_parts[0])
        except ValueError:
            return
        recipe_name = sub_parts[1]

        recipe = await get_recipe_by_name(recipe_name)
        if recipe is None:
            await query.edit_message_text(f"❓ Recipe '{recipe_name}' no longer exists.")  # type: ignore[union-attr]
            return

        # Apply recipe overrides to the task
        task = await get_task_by_id(task_id)
        if task is None or task.status != TaskStatus.PENDING:
            await query.edit_message_text(  # type: ignore[union-attr]
                f"⚠️ Task #{task_id} already started or not found."
            )
            return

        # Enrich the prompt and re-enqueue with recipe settings
        enriched_prompt = _enrich_prompt(task.prompt, recipe)
        await _apply_recipe_to_task(task_id, recipe, enriched_prompt)

        await query.edit_message_text(  # type: ignore[union-attr]
            f"🧪 Recipe *{recipe_name}* applied to task *#{task_id}*",
            parse_mode=ParseMode.MARKDOWN,
        )


def _enrich_prompt(prompt: str, recipe) -> str:
    """Build the enriched prompt with prefix, skill content, and suffix."""
    parts = []

    # Add setup commands as context
    if recipe.setup_commands:
        setup_block = "\n".join(recipe.setup_commands)
        parts.append(f"[Setup commands to run first:\n{setup_block}\n]")

    # Load skill SKILL.md content if available
    if recipe.skills:
        skills_dir = os.path.expanduser(settings.skills_dir)
        for skill_name in recipe.skills:
            skill_path = os.path.join(skills_dir, skill_name, "SKILL.md")
            if os.path.isfile(skill_path):
                try:
                    with open(skill_path, "r") as f:
                        content = f.read(10000)  # Cap at 10K chars per skill
                    parts.append(f"[Skill: {skill_name}]\n{content}\n[/Skill]")
                except OSError:
                    pass

    if recipe.prompt_prefix:
        parts.append(recipe.prompt_prefix)

    parts.append(prompt)

    if recipe.prompt_suffix:
        parts.append(recipe.prompt_suffix)

    return "\n\n".join(parts)


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
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("setmodel", cmd_setmodel))
    app.add_handler(CommandHandler("output", cmd_output))
    app.add_handler(CommandHandler("retry", cmd_retry))
    app.add_handler(CommandHandler("bump", cmd_bump))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("continue", cmd_continue))
    app.add_handler(CommandHandler("cancel_followup", cmd_cancel_followup))
    app.add_handler(CommandHandler("repeat", cmd_repeat))
    app.add_handler(CommandHandler("savechain", cmd_savechain))
    app.add_handler(CommandHandler("chain", cmd_chain))
    app.add_handler(CommandHandler("chains", cmd_chains))
    app.add_handler(CommandHandler("delchain", cmd_delchain))
    app.add_handler(CommandHandler("recipes", cmd_recipes))
    app.add_handler(CommandHandler("addrecipe", cmd_addrecipe))
    app.add_handler(CommandHandler("delrecipe", cmd_delrecipe))
    app.add_handler(CommandHandler("recipe", cmd_recipe))
    app.add_handler(CommandHandler("gallery", cmd_gallery))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_agent_callback, pattern=r"^switch:"))
    app.add_handler(CallbackQueryHandler(handle_model_set_callback, pattern=r"^modelset:"))
    app.add_handler(CallbackQueryHandler(handle_model_switch_callback, pattern=r"^modelswitch:"))
    app.add_handler(CallbackQueryHandler(handle_worker_switch_callback, pattern=r"^workerswitch:"))
    app.add_handler(CallbackQueryHandler(handle_menu_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(handle_setagent_callback, pattern=r"^setagent:"))
    app.add_handler(CallbackQueryHandler(handle_task_action_callback, pattern=r"^task(retry|output|followup):"))
    app.add_handler(CallbackQueryHandler(handle_recipe_callback, pattern=r"^recipe(use|skip):"))
    app.add_handler(CallbackQueryHandler(handle_gallery_callback, pattern=r"^gallery(cat|all):"))

    return app

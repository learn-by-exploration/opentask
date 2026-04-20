# TaskPilot — Architecture Overview

## System Context

```
     ┌─────────┐          Telegram API           ┌─────────────────┐
     │  User   │ ◄──────────────────────────────► │   TaskPilot     │
     │ (Phone) │    (long-polling, outbound only)  │   (Dev Machine) │
     └─────────┘                                   └────────┬────────┘
                                                            │
                                                   ┌────────▼────────┐
                                                   │    OpenCode     │
                                                   │  (subprocess)   │
                                                   └─────────────────┘
```

## Component Architecture

### 1. Config (`app/config/settings.py`)

Pydantic `BaseSettings` loading from `.env`:
- `TELEGRAM_BOT_TOKEN` — from BotFather
- `ALLOWED_USER_IDS` — comma-separated Telegram user IDs
- `DEFAULT_AGENT` — which agent CLI to use (default: "opencode")
- `DEFAULT_PROJECT_DIR` — working directory for agent tasks
- `AGENT_COMMANDS` — JSON mapping agent name → command template
- `TASK_TIMEOUT_SECONDS` — max runtime before kill (default: 1800 = 30min)
- `DB_PATH` — SQLite database file path

### 2. Models (`app/core/models.py`)

Single SQLAlchemy model: `Task`

Tracks the full lifecycle of a queued instruction. Key fields:
- `status` enum: pending → running → completed/failed/cancelled
- `telegram_chat_id` + `telegram_msg_id` for notification routing
- Timestamps for created/started/completed + computed duration

### 3. Database (`app/core/db.py`)

- Async SQLAlchemy engine with aiosqlite
- `init_db()` creates tables and data directory on first run
- `get_session()` async context manager for transactions

### 4. Broker (`app/core/broker.py`)

Task queue operations — the brain of the system:

| Function | Purpose |
|----------|---------|
| `enqueue_task()` | Create task with status=pending |
| `pick_next_task()` | Get oldest pending task, mark as running |
| `complete_task()` | Mark task as completed/failed with output |
| `cancel_running_task()` | Mark running task as cancelled |
| `get_running_task()` | Check if anything is currently running |
| `get_pending_tasks()` | List queue for /queue command |
| `get_recent_tasks()` | Last N tasks for /history command |
| `recover_interrupted_tasks()` | On startup, fail any orphaned "running" tasks |

### 5. Runner (`app/core/runner.py`)

Execution engine that polls for tasks and runs them:

```
Loop (every 2 seconds):
  1. Check if any task is running → skip if yes
  2. Pick next pending task from broker
  3. Build shell command from agent template
  4. Execute via asyncio.create_subprocess_shell
  5. Capture stdout + stderr
  6. On completion/timeout/error → update task via broker
  7. Call notify callback → sends Telegram message
```

**Timeout handling:**
- After `TASK_TIMEOUT_SECONDS`: send SIGTERM
- Wait 5 seconds for graceful shutdown
- If still running: send SIGKILL
- Mark task as failed with timeout error

**Output summarization:**
- Full output stored in `full_output`
- Smart summary for Telegram (max ~500 chars):
  - If output has error-like lines (traceback, Error:, FAIL), prioritize those
  - Otherwise, take last N lines that fit
  - Always indicate if output was truncated

### 6. Telegram Bot (`app/telegram/bot.py`)

Async bot using `python-telegram-bot` library:

**Auth guard:** Every handler checks `message.from_user.id in ALLOWED_USER_IDS`

**Message flow:**
- Plain text message → `enqueue_task()` → reply "📋 Queued #ID"
- When task completes → bot sends "✅ Task #ID completed (45s)\n\n<summary>"
- When task fails → bot sends "❌ Task #ID failed\n\n<error>"

**Commands:** /status, /queue, /history, /cancel, /output, /project, /agent, /help

### 7. Entrypoint (`app/__main__.py`)

Single process running two concurrent loops:
1. **Telegram bot** — polling for user messages
2. **Agent runner** — polling DB for pending tasks

Both share the same async event loop. The runner's notify callback
sends messages through the bot's Telegram API client.

## Data Flow

```
1. User sends "fix the login bug" on Telegram
       │
2. Bot receives message, calls broker.enqueue_task()
       │
3. Broker creates Task(status=pending, prompt="fix the login bug")
       │
4. Runner poll picks up the task, calls broker.pick_next_task()
       │
5. Runner builds command: cd /project && echo 'fix the login bug' | opencode
       │
6. Runner executes subprocess, captures output
       │
7. On completion, runner calls broker.complete_task() with output
       │
8. Runner calls notify_callback(task) → bot sends Telegram message
       │
9. User reads "✅ Task #3 completed (2m 15s)" on phone
       │
10. User sends next task...
```

## Error Recovery

| Scenario | Recovery |
|----------|----------|
| TaskPilot process crashes | On restart: `recover_interrupted_tasks()` fails any orphaned running tasks, then resumes |
| Agent hangs | Timeout → SIGTERM → 5s → SIGKILL → task marked failed |
| Machine sleeps | Same as crash recovery — running task gets failed on next start |
| Network loss | Telegram long-polling auto-reconnects. Tasks in queue survive in SQLite |
| User sends too many tasks | Tasks queue up. One runs at a time. /queue shows pending count |

## Future Considerations

- **Web dashboard**: FastAPI server + simple HTML for browser access
- **Multi-machine**: Machine registry, route tasks to specific machines
- **Streaming**: Send output chunks to Telegram in real-time
- **Task chaining**: "do X, then do Y" as a single compound task
- **File sharing**: Upload/download files via Telegram

# OpenTask — Architecture Overview

## System Context

```
     ┌─────────┐          Telegram API           ┌─────────────────┐
     │  User   │ ◄──────────────────────────────► │   OpenTask      │
     │ (Phone) │    (long-polling, outbound only)  │   (Dev Machine) │
     └─────────┘                                   └────────┬────────┘
                                                            │
                                                  ┌─────────┼─────────┐
                                                  │         │         │
                                           ┌──────▼──┐ ┌────▼────┐ ┌─▼──────────┐
                                           │ OpenCode│ │ SQLite  │ │    Web     │
                                           │ (subproc)│ │   DB    │ │ Dashboard  │
                                           └─────────┘ └─────────┘ └────────────┘
```

## Component Architecture

### 1. Config (`app/config/settings.py`)

Pydantic `BaseSettings` loading from `.env`:
- `TELEGRAM_BOT_TOKEN` — from BotFather
- `ALLOWED_USER_IDS` — comma-separated Telegram user IDs
- `DEFAULT_AGENT` — which agent CLI to use (default: "opencode")
- `DEFAULT_PROJECT_DIR` — working directory for agent tasks
- `ALLOWED_PROJECT_DIRS` — whitelist of directories agents may run in
- `AGENT_COMMANDS` — JSON mapping agent name → command template
- `TASK_TIMEOUT_SECONDS` — max runtime before kill (default: 1800 = 30min)
- `PROGRESS_INTERVAL_SECONDS` — seconds between progress notifications (default: 30)
- `MAX_QUEUE_SIZE` — max pending + running tasks (default: 20)
- `DB_PATH` — SQLite database file path
- `DASHBOARD_ENABLED` / `DASHBOARD_HOST` / `DASHBOARD_PORT` / `DASHBOARD_TOKEN` — web dashboard config

### 2. Models (`app/core/models.py`)

Three SQLAlchemy models:

**Task** — tracks the full lifecycle of a queued instruction:
- `status` enum: pending → running → completed/failed/cancelled
- `telegram_chat_id` + `telegram_msg_id` for notification routing
- Timestamps for created/started/completed + computed duration
- `chain_id` + `chain_step` for linking to a TaskChain
- `repeat_total` / `repeat_remaining` / `repeat_until` for repeat tasks

**TaskChain** — named multi-step sequences:
- `steps_json` — JSON list of step dicts (prompt, agent, project_dir)
- `current_step` / `status` for execution tracking
- Timestamps for lifecycle

**ChatPrefs** — per-chat persistent preferences:
- `project_dir` and `agent` choice persisted to DB, cached in memory

### 3. Database (`app/core/db.py`)

- Async SQLAlchemy engine with aiosqlite
- `init_db()` creates tables and data directory on first run
- `get_session()` async context manager for transactions

### 4. Broker (`app/core/broker.py`)

Task queue operations — the brain of the system:

| Function | Purpose |
|----------|---------|
| `enqueue_task()` | Create task with status=pending, wake runner |
| `pick_next_task()` | Get oldest pending task, mark as running |
| `complete_task()` | Mark task as completed/failed with output |
| `cancel_running_task()` | Mark running task as cancelled |
| `cancel_task_by_id()` | Cancel a specific pending task |
| `get_running_task()` | Check if anything is currently running |
| `get_pending_tasks()` | List queue for /queue command |
| `get_recent_tasks()` | Last N tasks for /history command |
| `get_task_by_id()` | Fetch single task for /output command |
| `retry_task()` | Re-enqueue a failed task |
| `recover_interrupted_tasks()` | On startup, fail any orphaned "running" tasks |
| `recover_interrupted_chains()` | On startup, fail any orphaned "running" chains |
| `purge_old_tasks()` | Remove completed tasks older than N days |
| `save_chain()` | Create a named chain with multiple steps |
| `start_chain()` | Begin executing a chain's first step |
| `advance_chain()` | Move to the next step or complete the chain |
| `list_chains()` | List all saved chains |
| `delete_chain()` | Remove a chain by name |
| `enqueue_repeat_task()` | Enqueue a task that runs N times or until deadline |
| `get_chat_prefs()` | Load per-chat preferences from DB |
| `set_chat_pref()` | Persist a per-chat preference |
| `set_runner_wake()` | Register callback to wake runner on enqueue |

### 5. Runner (`app/core/runner.py`)

Execution engine that polls for tasks and runs them:

```
Loop (every 2 seconds, or woken immediately on enqueue):
  1. Check if any task is running → skip if yes
  2. Pick next pending task from broker
  3. Build shell command from agent template
  4. Execute via asyncio.create_subprocess_shell
  5. Capture stdout + stderr
  6. On completion/timeout/error → update task via broker
  7. Call notify callback → sends Telegram message
  8. If task is part of a chain → advance chain to next step
  9. If task is a repeat → enqueue next iteration
  10. Send progress notifications at configured intervals
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

**Path allowlist:** `/project` validates paths against `ALLOWED_PROJECT_DIRS`

**Per-chat state:** Project dir and agent preferences are persisted via `ChatPrefs` model, cached in memory dicts for fast access.

**Message flow:**
- Plain text message → `enqueue_task()` → reply "📋 Queued #ID"
- When task completes → bot sends "✅ Task #ID completed (45s)\n\n<summary>"
- When task fails → bot sends "❌ Task #ID failed\n\n<error>"
- Progress updates sent periodically while tasks run

**Commands:**

| Command | Action |
|---------|--------|
| *any text* | Queue as a new task |
| `/status` | Show current running task with elapsed time |
| `/queue` | List pending tasks |
| `/history [N]` | Recent completed/failed tasks |
| `/cancel [id]` | Cancel running or specific pending task |
| `/output <id>` | Full output (file attachment if large) |
| `/retry <id>` | Re-queue a failed task |
| `/project <path>` | Change working directory |
| `/agent <name>` | Switch agent |
| `/repeat <N> <prompt>` | Run a task N times |
| `/repeat until:HH:MM <prompt>` | Run until a time |
| `/savechain <name> s1 \| s2 \| s3` | Save a multi-step chain |
| `/chain <name>` | Run a saved chain |
| `/chains` | List all chains |
| `/delchain <name>` | Delete a chain |
| `/help` | Show all commands |

### 7. Web Dashboard (`app/web/dashboard.py`)

FastAPI application running alongside the bot (optional, requires `pip install .[web]`):

**Security:**
- Optional bearer token authentication via `DASHBOARD_TOKEN`
- Security headers: X-Frame-Options, X-Content-Type-Options, Referrer-Policy, CSP
- Timing-safe token comparison via `hmac.compare_digest`

**JSON API:**

| Endpoint | Returns |
|----------|---------|
| `GET /api/health` | Service health check |
| `GET /api/stats` | Queue depth, completed/failed counts, avg duration, chain stats |
| `GET /api/tasks?limit=N` | Recent tasks (max 100) |
| `GET /api/tasks/{id}` | Single task detail with full output |
| `GET /api/queue` | Pending tasks |
| `GET /api/chains` | All chains |
| `GET /api/chains/{id}` | Chain detail with steps |

**HTML Dashboard:**
- Self-contained embedded HTML (no external template dependencies)
- Dark theme with responsive CSS grid layout
- Auto-refresh every 5 seconds via `fetch()` + DOM updates
- Stats cards, running task highlight, queue table, task history, chains view
- Mobile-friendly (responsive breakpoints)

### 8. Entrypoint (`app/__main__.py`)

Startup sequence:
1. `init_db()` — create tables and data directory
2. `recover_interrupted_tasks()` — fail orphaned running tasks
3. `recover_interrupted_chains()` — fail orphaned running chains
4. `purge_old_tasks(days=7)` — clean up old completed tasks
5. Wire up runner ↔ bot callbacks (notify, chain_notify, progress, typing)
6. Start Telegram polling + runner loop + web dashboard concurrently
7. Register SIGTERM/SIGINT handlers for clean shutdown

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

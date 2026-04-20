# OpenTask

**Personal remote AI agent control via Telegram.**

Send a task from your phone → AI agent runs it on your dev machine → get results back on Telegram.

---

## Why This Exists

When you step away from your desk — lunch break, commute, overnight — your dev machine sits idle. OpenTask lets you keep it working:

1. You send a natural language task to a Telegram bot
2. OpenTask queues it and dispatches it to your AI coding agent (OpenCode, Aider, etc.)
3. When the agent finishes, you get a summary back on Telegram
4. You read the result and send the next task — all from your phone

No SSH tunnels, no port forwarding, no VPN. Just Telegram long-polling.

## Architecture

```
┌──────────────┐        ┌──────────────────────────────────────────────┐
│  Your Phone  │        │              Dev Machine                     │
│  (Telegram)  │◄──────►│                                              │
│              │        │  ┌────────┐  ┌────────┐  ┌───────────────┐  │
│  Send task   │        │  │Telegram│  │  Task  │  │    Agent      │  │
│  Get results │        │  │  Bot   │──│ Broker │──│    Runner     │  │
│  /status     │        │  └────────┘  └────────┘  └───────────────┘  │
│  /cancel     │        │       │          │              │            │
│  /queue      │        │       │      ┌───┴───┐    ┌────┴─────┐     │
│  /chain      │        │       │      │SQLite │    │ OpenCode │     │
│              │        │       │      │  DB   │    │ subprocess│    │
└──────────────┘        │       │      └───────┘    └──────────┘     │
                        │  ┌────┴─────────┐                           │
                        │  │ Web Dashboard │                           │
                        │  │  (FastAPI)    │                           │
                        │  └──────────────┘                           │
                        └──────────────────────────────────────────────┘
```

## Features

- **Telegram bot** — queue tasks, monitor progress, get results on your phone
- **Task chains** — define multi-step workflows that run sequentially
- **Repeat tasks** — run a task N times or until a specified time
- **Web dashboard** — real-time monitoring UI with auto-refresh (FastAPI)
- **Persistent preferences** — project dir and agent choice saved per chat
- **Crash recovery** — orphaned running tasks/chains recovered on restart
- **Auto-purge** — old completed tasks cleaned up after 7 days
- **Progress notifications** — periodic updates while tasks run
- **Path allowlist** — restrict agent execution to approved directories
- **879 tests** across 19 test files covering broker, runner, bot, chains, security, and dashboard

## Key Design Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Interface | Telegram bot | Works from any phone, no app to build, no server needed |
| Transport | Long-polling | No public IP / port forwarding required. Works behind NAT, corp firewalls |
| Agent | OpenCode (primary) | User's current AI coding agent. Extensible to others |
| Queue | SQLite | Zero-setup, survives restarts, single-user scale is fine |
| Execution | Subprocess | Simple, isolated, timeout-able. One task at a time |
| Auth | Telegram user ID allowlist | Simple, effective for personal use |
| Dashboard | FastAPI (optional) | Lightweight, async, same process — install with `pip install .[web]` |

## Components

| Component | File | Purpose |
|-----------|------|---------|
| Config | `app/config/settings.py` | Pydantic settings from `.env` |
| Models | `app/core/models.py` | Task, TaskChain, ChatPrefs — SQLAlchemy ORM |
| Broker | `app/core/broker.py` | Task queue CRUD: enqueue, pick, complete, cancel, recover, chains, prefs |
| Runner | `app/core/runner.py` | Subprocess execution with timeout, output capture, summarization |
| Bot | `app/telegram/bot.py` | Telegram commands, message handling, notifications |
| Dashboard | `app/web/dashboard.py` | FastAPI web dashboard with JSON API + embedded HTML UI |
| Entrypoint | `app/__main__.py` | Starts bot + runner + dashboard concurrently |

## Telegram Commands

| Command | Action |
|---------|--------|
| *any text* | Queue as a new task |
| `/status` | Show current running task with elapsed time |
| `/queue` | List pending tasks |
| `/history [N]` | Recent completed/failed tasks (default 10) |
| `/cancel [id]` | Cancel the running task, or a specific pending task by ID |
| `/output <id>` | Get full output of a task (sent as file if large) |
| `/retry <id>` | Re-queue a failed task |
| `/project <path>` | Change working directory (validated against allowlist) |
| `/agent <name>` | Switch agent (opencode, aider, etc.) |
| `/repeat <N> <prompt>` | Run a task N times |
| `/repeat until:HH:MM <prompt>` | Run a task repeatedly until a time |
| `/savechain <name> s1 \| s2 \| s3` | Save a multi-step chain |
| `/chain <name>` | Run a saved chain |
| `/chains` | List all saved chains |
| `/delchain <name>` | Delete a chain |
| `/help` | Show all commands |

## Quick Start

```bash
# 1. Clone
git clone git@github.com:learn-by-exploration/opentask.git
cd opentask

# 2. One-command setup (creates venv, installs deps, runs tests)
bash setup.sh

# 3. Configure
#    Edit .env — add your Telegram bot token and user ID
#    (setup.sh creates .env from the template if it doesn't exist)

# 4. Run
source .venv/bin/activate
python -m app
# Or: make run
```

### Alternative: Manual Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # edit with your config
python -m app
```

### Running in Background

```bash
make run-bg          # start in background (logs → data/bot.log)
make logs            # tail the log
make stop            # stop the bot
make restart         # stop + start
```

### Makefile Commands

| Command | Description |
|---------|-------------|
| `make setup` | Full setup via setup.sh |
| `make install` | Install all dependencies into .venv |
| `make run` | Run in foreground |
| `make run-bg` | Run in background (logs → data/bot.log) |
| `make stop` | Stop background process |
| `make restart` | Stop + start in background |
| `make logs` | Tail the bot log |
| `make test` | Run all tests (verbose) |
| `make test-quick` | Quick test (stop on first failure) |
| `make clean` | Remove caches and build artifacts |

### Prerequisites

- **Python 3.9+** — `python3 --version`
- **An AI coding agent** — at least one of:
  - [OpenCode](https://github.com/opencode-ai/opencode) — `opencode run` (default)
  - [Claude CLI](https://docs.anthropic.com/en/docs/claude-cli) — `claude -p`
- **Telegram account** — for the bot interface

### Getting a Telegram Bot Token

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot`, follow prompts
3. Copy the token to `.env`

### Finding Your Telegram User ID

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram
2. It replies with your numeric user ID
3. Add it to `ALLOWED_USER_IDS` in `.env`

## Web Dashboard

The web dashboard runs alongside the bot at `http://127.0.0.1:8095`:

- **Stats overview** — queue depth, completed/failed counts, average duration
- **Running task** — highlighted card with elapsed time
- **Queue view** — pending tasks
- **Recent tasks** — status, agent, prompt, duration
- **Chains** — all saved chains with status
- **Auto-refresh** — live updates every 5 seconds
- **JSON API** — `/api/stats`, `/api/tasks`, `/api/queue`, `/api/chains`, `/api/health`
- **Optional auth** — set `DASHBOARD_TOKEN` in `.env` for bearer token protection

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | From @BotFather |
| `ALLOWED_USER_IDS` | yes | — | Comma-separated Telegram user IDs |
| `DEFAULT_AGENT` | no | `opencode` | Agent to use by default |
| `DEFAULT_PROJECT_DIR` | no | `~/ai` | Working directory for tasks |
| `ALLOWED_PROJECT_DIRS` | no | `~/ai,~/repos,~/projects` | Directories the agent may run in |
| `AGENT_COMMANDS` | no | *(built-in)* | JSON map of agent→command template |
| `TASK_TIMEOUT_SECONDS` | no | `1800` | Max seconds per task (30 min) |
| `MAX_QUEUE_SIZE` | no | `20` | Max pending+running tasks |
| `PROGRESS_INTERVAL_SECONDS` | no | `30` | Seconds between progress notifications |
| `DB_PATH` | no | `./data/taskpilot.db` | SQLite database path |
| `DASHBOARD_ENABLED` | no | `true` | Enable/disable web dashboard |
| `DASHBOARD_HOST` | no | `127.0.0.1` | Dashboard listen address |
| `DASHBOARD_PORT` | no | `8095` | Dashboard listen port |
| `DASHBOARD_TOKEN` | no | *(empty)* | Bearer token for dashboard auth |

## Project Structure

```
opentask/
├── app/
│   ├── __init__.py
│   ├── __main__.py            # Entrypoint: bot + runner + dashboard
│   ├── config/
│   │   └── settings.py        # Pydantic settings from .env
│   ├── core/
│   │   ├── models.py          # Task, TaskChain, ChatPrefs (SQLAlchemy)
│   │   ├── db.py              # Async engine + session factory (aiosqlite)
│   │   ├── broker.py          # Queue CRUD, chains, prefs, recovery
│   │   └── runner.py          # Agent subprocess execution
│   ├── telegram/
│   │   └── bot.py             # Commands, auth, notifications
│   └── web/
│       └── dashboard.py       # FastAPI dashboard (HTML + JSON API)
├── tests/                     # 879 tests across 19 files
│   ├── conftest.py            # In-memory SQLite fixtures
│   ├── test_broker.py         # Core broker operations
│   ├── test_runner.py         # Command building + summarization
│   ├── test_bot.py            # All Telegram handlers
│   ├── test_chains.py         # Chain CRUD and execution
│   ├── test_web_dashboard.py  # Dashboard API + auth + HTML
│   ├── test_qa_security.py    # Security and edge cases
│   └── ...                    # Additional coverage suites
├── docs/
│   ├── architecture/
│   │   └── overview.md        # Architecture deep-dive
│   └── design/
│       ├── spec.md            # V1 specification
│       └── kickoff-discovery-coverage.md
├── .env.example
├── pyproject.toml
├── CLAUDE.md
└── README.md
```

## Running Tests

```bash
source .venv/bin/activate
make test              # verbose
make test-quick        # stop on first failure
# Or directly: pytest tests/ -v
```

## License

MIT

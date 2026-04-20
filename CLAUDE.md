# OpenTask

Personal Telegram → AI agent bridge. Queue coding tasks from your phone, execute them via OpenCode/Claude on your dev machine.

## Quick Start

```bash
bash setup.sh          # creates venv, installs deps, runs tests
# edit .env with your Telegram bot token + user ID
source .venv/bin/activate
python -m app          # or: make run
```

## Project Layout

```
app/
  config/settings.py    — Pydantic settings from .env (Settings class)
  core/models.py        — Task, TaskChain, ChatPrefs (SQLAlchemy ORM)
  core/db.py            — Async engine + session factory (aiosqlite)
  core/broker.py        — Queue CRUD, chains, prefs, recovery, purge
  core/runner.py        — AgentRunner: subprocess execution with timeout + shell escaping
  telegram/bot.py       — Telegram bot: auth, commands, chains, repeats, notifications
  web/dashboard.py      — FastAPI web dashboard (HTML + JSON API)
  __main__.py           — Entry point: init_db → recover → purge → bot + runner + dashboard
tests/                  — 879 tests across 19 files
  conftest.py           — In-memory SQLite fixtures
  test_broker.py        — Core broker operations
  test_runner.py        — Command building + summarization
  test_bot.py           — All Telegram handlers
  test_chains.py        — Chain CRUD and execution
  test_web_dashboard.py — Dashboard API + auth + HTML
  test_qa_security.py   — Security edge cases
```

## Architecture

- **Single-process**: Telegram bot + task runner + web dashboard run concurrently via asyncio
- **Sequential execution**: One task at a time (runner polls every 2s, woken on enqueue)
- **SQLite persistence**: Tasks survive restarts; orphaned RUNNING tasks/chains recovered on startup
- **Auto-purge**: Completed tasks older than 7 days cleaned on startup
- **Auth**: ALLOWED_USER_IDS allowlist checked on every Telegram handler
- **Path allowlist**: ALLOWED_PROJECT_DIRS restricts where agents can run
- **Shell safety**: Prompts and project dirs shell-escaped via shlex.quote
- **Web dashboard**: FastAPI with optional bearer token auth, security headers, auto-refresh HTML UI

## Key Design Decisions

- `from __future__ import annotations` everywhere + `Optional[T]` in SQLAlchemy `Mapped[]` for Python 3.9 compat
- Naive UTC datetimes (SQLite strips tzinfo) via `_utcnow()` helper
- Agent command templates use `{prompt}` and `{project_dir}` placeholders (no quotes — shlex.quote handles it)
- Each broker function creates its own session (no shared state, clean isolation)
- Bot per-chat state backed by ChatPrefs model (persisted to DB, cached in memory)
- `build_app(runner=...)` passes runner ref so `/cancel` can kill subprocesses
- `process.returncode if returncode is not None else -1` — never use `returncode or -1` (0 is falsy)
- Full output capped at 2MB, prompts at 2000 chars
- SIGTERM/SIGINT handlers signal runner + dashboard to stop cleanly
- Task chains: named multi-step sequences stored as JSON, auto-advance on completion
- Repeat tasks: run N times or until a time deadline
- Runner wake callback: new tasks wake the runner immediately (no poll delay)
- Progress notifications: periodic updates while long tasks run
- Dashboard: embedded HTML (no template deps), JSON API for programmatic access

## Running Tests

```bash
source .venv/bin/activate
make test              # or: pytest tests/ -v
```

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| TELEGRAM_BOT_TOKEN | yes | — | From @BotFather |
| ALLOWED_USER_IDS | yes | — | Comma-separated Telegram user IDs |
| DEFAULT_AGENT | no | opencode | Agent to use by default |
| DEFAULT_PROJECT_DIR | no | ~/ai | Working directory for tasks |
| ALLOWED_PROJECT_DIRS | no | ~/ai,~/repos,~/projects | Allowed working directories |
| AGENT_COMMANDS | no | *(built-in)* | JSON map: agent name → command template |
| TASK_TIMEOUT_SECONDS | no | 1800 | Max seconds per task |
| MAX_QUEUE_SIZE | no | 20 | Max pending+running tasks |
| PROGRESS_INTERVAL_SECONDS | no | 30 | Seconds between progress notifications |
| DB_PATH | no | ./data/taskpilot.db | SQLite database path |
| DASHBOARD_ENABLED | no | true | Enable/disable web dashboard |
| DASHBOARD_HOST | no | 127.0.0.1 | Dashboard listen address |
| DASHBOARD_PORT | no | 8095 | Dashboard listen port |
| DASHBOARD_TOKEN | no | *(empty)* | Bearer token for dashboard auth |

## Pre-Implementation Gate

**Before writing any new code**, complete the kickoff discovery checklist:

1. `gh search repos "<domain keywords>" --sort=stars --limit=10`
2. `gh search code "<key pattern>" --limit=10`
3. Check package registries (pip, npm, etc.)
4. Log top 3 candidates + reuse decision in `docs/design/kickoff-discovery-<task>.md`
5. Template: [`docs/templates/kickoff-discovery.md`](docs/templates/kickoff-discovery.md)

Block implementation until the discovery log exists and has a build/reuse decision.

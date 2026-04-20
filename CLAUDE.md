# TaskPilot

Personal Telegram → AI agent bridge. Queue coding tasks from your phone, execute them via OpenCode/Aider on your dev machine.

## Quick Start

```bash
python3.9 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in TELEGRAM_BOT_TOKEN + ALLOWED_USER_IDS
taskpilot              # or: python -m app
```

## Project Layout

```
app/
  config/settings.py    — Pydantic settings from .env
  core/models.py        — SQLAlchemy Task model + TaskStatus enum
  core/db.py            — Async engine + session factory (SQLite)
  core/broker.py        — Task queue CRUD (enqueue, pick, complete, cancel, recover)
  core/runner.py        — AgentRunner: subprocess execution with timeout + shell escaping
  telegram/bot.py       — Telegram bot: auth, commands, text→task, notifications
  __main__.py           — Entry point: init_db → recover → bot + runner concurrent loop
tests/
  conftest.py           — In-memory SQLite fixtures
  test_broker.py        — 17 tests covering all broker operations
  test_runner.py        — 13 tests covering command building + summarization
```

## Architecture

- **Single-process**: Telegram bot + task runner run concurrently via asyncio
- **Sequential execution**: One task at a time (runner polls every 2s)
- **SQLite persistence**: Tasks survive restarts; orphaned RUNNING tasks recovered on startup
- **Auth**: ALLOWED_USER_IDS allowlist checked on every handler
- **Shell safety**: Prompts and project dirs shell-escaped via shlex.quote

## Key Design Decisions

- `from __future__ import annotations` everywhere + `Optional[T]` in SQLAlchemy `Mapped[]` for Python 3.9 compat
- Naive UTC datetimes (SQLite strips tzinfo) via `_utcnow()` helper
- Agent command templates use `{prompt}` and `{project_dir}` placeholders (no quotes — shlex.quote handles it)
- Each broker function creates its own session (no shared state, clean isolation)
- Bot per-chat state (project_dir, agent) stored in module-level dicts (single-user use case)
- `build_app(runner=...)` passes runner ref so `/cancel` can kill subprocesses
- `process.returncode if returncode is not None else -1` — never use `returncode or -1` (0 is falsy)
- Full output capped at 2MB, prompts at 2000 chars
- SIGTERM/SIGINT handlers signal runner to stop cleanly

## Running Tests

```bash
source .venv/bin/activate
pytest tests/ -v
```

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| TELEGRAM_BOT_TOKEN | yes | — | From @BotFather |
| ALLOWED_USER_IDS | yes | — | Comma-separated Telegram user IDs |
| DEFAULT_AGENT | no | opencode | Agent to use by default |
| DEFAULT_PROJECT_DIR | no | ~/ai | Working directory for tasks |
| TASK_TIMEOUT_SECONDS | no | 1800 | Max seconds per task |
| MAX_QUEUE_SIZE | no | 20 | Max pending+running tasks |
| DB_PATH | no | ./data/taskpilot.db | SQLite database path |

## Pre-Implementation Gate

**Before writing any new code**, complete the kickoff discovery checklist:

1. `gh search repos "<domain keywords>" --sort=stars --limit=10`
2. `gh search code "<key pattern>" --limit=10`
3. Check package registries (pip, npm, etc.)
4. Log top 3 candidates + reuse decision in `docs/design/kickoff-discovery-<task>.md`
5. Template: [`docs/templates/kickoff-discovery.md`](docs/templates/kickoff-discovery.md)

Block implementation until the discovery log exists and has a build/reuse decision.

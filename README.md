# TaskPilot

**Personal remote AI agent control via Telegram.**

Send a task from your phone → AI agent runs it on your dev machine → get results back on Telegram.

---

## Why This Exists

When you step away from your desk — lunch break, commute, overnight — your dev machine sits idle. TaskPilot lets you keep it working:

1. You send a natural language task to a Telegram bot
2. TaskPilot queues it and dispatches it to your AI coding agent (OpenCode, etc.)
3. When the agent finishes, you get a summary back on Telegram
4. You read the result and send the next task — all from your phone

No SSH tunnels, no port forwarding, no VPN. Just Telegram long-polling.

## Architecture

```
┌──────────────┐        ┌──────────────────────────────────────────┐
│  Your Phone  │        │            Dev Machine                   │
│  (Telegram)  │◄──────►│                                          │
│              │        │  ┌────────┐  ┌────────┐  ┌───────────┐  │
│  Send task   │        │  │Telegram│  │  Task  │  │  Agent    │  │
│  Get results │        │  │  Bot   │──│ Broker │──│  Runner   │  │
│  /status     │        │  └────────┘  └────────┘  └───────────┘  │
│  /cancel     │        │                  │            │          │
│  /queue      │        │              ┌───┴───┐   ┌────┴─────┐   │
│              │        │              │SQLite │   │ OpenCode │   │
└──────────────┘        │              │  DB   │   │ subprocess│  │
                        │              └───────┘   └──────────┘   │
                        └──────────────────────────────────────────┘
```

## Key Design Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Interface | Telegram bot | Works from any phone, no app to build, no server needed |
| Transport | Long-polling | No public IP / port forwarding required. Works behind NAT, corp firewalls |
| Agent | OpenCode (primary) | User's current AI coding agent. Extensible to others |
| Queue | SQLite | Zero-setup, survives restarts, single-user scale is fine |
| Execution | Subprocess | Simple, isolated, timeout-able. One task at a time |
| Auth | Telegram user ID allowlist | Simple, effective for personal use |

## Components

| Component | File | Purpose |
|-----------|------|---------|
| Config | `app/config/settings.py` | Pydantic settings from `.env` |
| Models | `app/core/models.py` | SQLAlchemy Task model with full lifecycle |
| Broker | `app/core/broker.py` | Task queue CRUD: enqueue, pick, complete, cancel, recover |
| Runner | `app/core/runner.py` | Subprocess execution with timeout, output capture, summarization |
| Bot | `app/telegram/bot.py` | Telegram commands and message handling |
| Entrypoint | `app/__main__.py` | Starts bot + runner concurrently |

## Telegram Commands

| Command | Action |
|---------|--------|
| *any text* | Queue as a new task |
| `/status` | Show current running task |
| `/queue` | List pending tasks |
| `/history` | Recent completed/failed tasks |
| `/cancel` | Cancel the running task |
| `/output <id>` | Get full output of a task |
| `/project <path>` | Change working directory |
| `/agent <name>` | Switch agent (opencode, copilot, etc.) |
| `/help` | Show all commands |

## Quick Start

```bash
# 1. Clone
git clone <repo-url> ~/ai/taskpilot
cd ~/ai/taskpilot

# 2. Install
pip install -e .

# 3. Configure
cp .env.example .env
# Edit .env — add your Telegram bot token and user ID

# 4. Run
taskpilot
# Or: python -m app
```

### Getting a Telegram Bot Token

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot`, follow prompts
3. Copy the token to `.env`

### Finding Your Telegram User ID

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram
2. It replies with your numeric user ID
3. Add it to `ALLOWED_USER_IDS` in `.env`

## Reference Implementations

Three open-source tools are kept as git submodules in `vendor/` for reference:

| Tool | Stars | Language | Agents | Role |
|------|-------|----------|--------|------|
| [cc-connect](https://github.com/nicholasxuu/cc-connect) | 5.5k | Go | 11 (inc. OpenCode) | Universal backbone reference |
| [vibe-remote](https://github.com/mherod/vibe-remote) | 401 | Python | OpenCode + others | Quick-task web dashboard reference |
| [cc-telegram-bridge](https://github.com/cloveric/cc-telegram-bridge) | 124 | Node/TS | Claude + Codex only | Deep Telegram UX reference |

## Project Structure

```
taskpilot/
├── app/
│   ├── __init__.py
│   ├── __main__.py          # Entrypoint: bot + runner
│   ├── config/
│   │   ├── __init__.py
│   │   └── settings.py      # Pydantic settings
│   ├── core/
│   │   ├── __init__.py
│   │   ├── models.py        # SQLAlchemy Task model
│   │   ├── db.py            # Async DB engine
│   │   ├── broker.py        # Task queue operations
│   │   └── runner.py        # Agent subprocess runner
│   ├── telegram/
│   │   ├── __init__.py
│   │   └── bot.py           # Telegram handlers
│   └── web/
│       └── __init__.py      # Future: optional web dashboard
├── vendor/                   # Git submodules (reference only)
│   ├── cc-connect/
│   ├── vibe-remote/
│   └── cc-telegram-bridge/
├── docs/
│   ├── design/
│   │   └── spec.md          # V1 specification
│   └── architecture/
│       └── overview.md       # Architecture deep-dive
├── tests/
│   └── conftest.py
├── .env.example
├── .gitignore
├── pyproject.toml
└── README.md
```

## License

MIT

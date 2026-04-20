# OpenTask

> **Personal Telegram → AI agent bridge.**
> Queue coding tasks from your phone, execute them on your dev machine (or a fleet of remote workers).

Send a message from your phone → OpenTask dispatches it to your AI coding agent → get results, git diffs, and follow-up conversation — all from Telegram.

No SSH tunnels, no port forwarding, no VPN. Just Telegram long-polling.

## Architecture

```mermaid
graph LR
    subgraph Phone["📱 Your Phone (Telegram)"]
        User["Send task\nGet results\nFollow up"]
    end

    subgraph Brain["🖥️ Brain (Dev Machine)"]
        Bot["Telegram\nBot"]
        Broker["Task\nBroker"]
        Runner["Agent\nRunner"]
        DB[(SQLite DB)]
        Dashboard["Web Dashboard\n(FastAPI)"]
        Agents["OpenCode / Claude\n(subprocess)"]

        Bot --> Broker
        Broker --> Runner
        Broker --> DB
        Runner --> Agents
        Bot --> Dashboard
    end

    subgraph Workers["🌐 Remote Workers"]
        W1["worker.py\n(server2)"]
        W2["worker.py\n(server3)"]
    end

    User <-->|"Long-polling"| Bot
    W1 -->|"Worker API"| Dashboard
    W2 -->|"Worker API"| Dashboard
```

## Task Lifecycle

```mermaid
stateDiagram-v2
    [*] --> PENDING : enqueue
    PENDING --> RUNNING : runner picks task
    RUNNING --> COMPLETED : exit 0
    RUNNING --> FAILED : exit ≠ 0
    FAILED --> PENDING : auto-retry (crash)
    FAILED --> [*] : max retries reached
    COMPLETED --> PENDING : /continue (follow-up)
    PENDING --> CANCELLED : /cancel
    RUNNING --> CANCELLED : /cancel
    COMPLETED --> [*]
    CANCELLED --> [*]
```

## Features

- **Telegram bot** — queue tasks, monitor progress, get results on your phone
- **Follow-up conversations** — tap "Follow Up" on any completed task to continue the discussion
- **Model selection** — switch between models (sonnet, opus, haiku) per chat or per task
- **Recipes** — smart prompt routing with keyword triggers, setup commands, skills, and prompt enrichment
- **Task chains** — multi-step workflows that run sequentially
- **Repeat tasks** — run a task N times or until a specified time
- **Priority & bump** — `/bump` moves a task to the front of the queue
- **Auto-retry** — crashed tasks automatically retry (configurable max retries)
- **Git diff summaries** — completed tasks include a summary of files changed
- **Task search** — find tasks by prompt text
- **Multi-machine workers** — route tasks to remote machines with `@worker` syntax
- **Web dashboard** — real-time monitoring UI with auto-refresh
- **Persistent reply keyboard** — quick-access buttons for Status, Queue, History, Help
- **Crash recovery** — orphaned running tasks/chains recovered on restart
- **Auto-purge** — completed tasks older than 7 days cleaned on startup
- **Shell safety** — all prompts and paths shell-escaped via `shlex.quote`
- **1200+ tests** across 24 test files

## Quick Links

- [Telegram Commands](guide/commands.md) — all 25 bot commands
- [Quick Start](deploy/quickstart.md) — get running in 2 minutes
- [Docker Deployment](deploy/docker.md) — containerized setup
- [Remote Workers](deploy/workers.md) — distribute tasks across machines
- [Environment Variables](reference/env.md) — full configuration reference
- [Dashboard & API](reference/dashboard.md) — web UI and JSON API

## Design Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Interface | Telegram bot | Works from any phone, no app to build |
| Transport | Long-polling | No public IP / port forwarding required |
| Agent | OpenCode + Claude | Extensible via `AGENT_COMMANDS` |
| Queue | SQLite (async) | Zero-setup, survives restarts |
| Execution | Subprocess | Simple, isolated, timeout-able |
| Auth | Telegram user ID allowlist | Simple, effective for personal use |
| Dashboard | FastAPI | Lightweight, async, same process |
| Workers | Pull-based polling | Workers behind NAT can reach the brain |
| Shell safety | `shlex.quote` | All input shell-escaped |
| Follow-ups | Agent `--continue` flag | Resumes agent session |
| Retry | Auto-retry on crash | Automatic recovery from transient failures |
| Git diffs | Post-task `git diff` | See what files changed |

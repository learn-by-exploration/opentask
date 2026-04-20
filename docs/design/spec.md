# OpenTask — Specification

## Problem

When you walk away from your dev machine (lunch, commute, overnight), your AI coding agent sits idle. You want to:
- Get notified on your phone when the current task finishes
- Send the next task without returning to the desk
- Monitor progress and cancel if needed
- Chain multi-step workflows to run sequentially
- Repeat tasks automatically

## Constraints

- **No Claude Code** — user runs OpenCode in VS Code
- **No public IP** — dev machine is behind NAT/firewall
- **Single user** — personal tool, not multi-tenant
- **Single machine** — one dev machine at a time (multi-machine is future scope)
- **Minimal infra** — no Redis, no Postgres, no Docker required

## Solution

A Python daemon that bridges Telegram ↔ AI coding agent via:
1. **Telegram long-polling** — no server, no port forwarding
2. **SQLite task queue** — survives restarts, zero config
3. **Subprocess execution** — runs agent CLI in project directory
4. **Auth by user ID** — only allowed Telegram users can interact
5. **Web dashboard** — optional real-time monitoring via FastAPI

## Scope

### Implemented
- Telegram bot with long-polling
- Text message → enqueue task
- Task queue (pending → running → completed/failed/cancelled)
- OpenCode + Aider agent execution via subprocess
- Output capture and smart summarization (Telegram 4096 char limit)
- Task timeout with graceful shutdown (SIGTERM → SIGKILL)
- Recovery on restart (mark orphaned running tasks/chains as failed)
- Auto-purge of completed tasks older than 7 days
- Commands: /status, /queue, /history, /cancel, /output, /project, /agent, /help, /retry
- Task chains: /savechain, /chain, /chains, /delchain
- Repeat tasks: /repeat N or /repeat until:HH:MM
- Per-chat persistent preferences (project_dir, agent)
- Path allowlist (ALLOWED_PROJECT_DIRS)
- Progress notifications at configurable intervals
- Web dashboard with JSON API + auto-refresh HTML UI
- Optional bearer token auth for dashboard
- 871 tests across 19 files

### Out of Scope (Future)
- Multi-machine support
- Streaming output to Telegram in real-time
- File upload/download via Telegram
- CI/CD integration
- Priority queue
- Task dependencies (beyond chains)

## Data Model

```
Task:
  id: int (primary key, auto)
  prompt: text (the natural language instruction)
  project_dir: text (working directory for agent)
  agent: text (which agent: "opencode", "aider", etc.)
  status: enum (pending, running, completed, failed, cancelled)
  output_summary: text (truncated output for Telegram)
  full_output: text (complete stdout+stderr)
  exit_code: int (nullable)
  error_message: text (nullable)
  telegram_chat_id: int (for sending notifications back)
  telegram_msg_id: int (nullable, for reply threading)
  chain_id: int (nullable, FK to TaskChain)
  chain_step: int (nullable, step index within chain)
  repeat_total: int (nullable, total repeat count)
  repeat_remaining: int (nullable, remaining iterations)
  repeat_until: datetime (nullable, deadline for repeats)
  created_at: datetime
  started_at: datetime (nullable)
  completed_at: datetime (nullable)
  duration_seconds: int (nullable)

TaskChain:
  id: int (primary key, auto)
  name: str (unique, max 128 chars)
  steps_json: text (JSON list of step dicts)
  current_step: int (0-based index)
  status: enum (idle, running, completed, failed, cancelled)
  telegram_chat_id: int (nullable)
  created_at: datetime
  started_at: datetime (nullable)
  completed_at: datetime (nullable)

ChatPrefs:
  chat_id: int (primary key)
  project_dir: str (nullable)
  agent: str (nullable)
  updated_at: datetime
```

## Agent Command Templates

Agents are invoked via shell commands with variable substitution:

```json
{
  "opencode": "opencode -p {prompt} --dir {project_dir}",
  "aider": "aider --message {prompt} --yes --no-git"
}
```

The `{prompt}` and `{project_dir}` are shell-escaped via `shlex.quote` before substitution to prevent injection.

## Task Lifecycle

```
User sends message
       │
       ▼
  ┌─────────┐     Runner polls     ┌─────────┐
  │ PENDING  │ ──────────────────► │ RUNNING  │
  └─────────┘                      └────┬─────┘
                                        │
                              ┌─────────┼──────────┐
                              ▼         ▼          ▼
                        ┌──────────┐ ┌──────┐ ┌──────────┐
                        │COMPLETED │ │FAILED│ │CANCELLED │
                        └──────────┘ └──────┘ └──────────┘
                              │         │
                              ▼         ▼
                     (if chain: advance to next step)
                     (if repeat: enqueue next iteration)
```

## Chain Lifecycle

```
/savechain mychain step1 | step2 | step3
       │
       ▼
  ┌──────┐       /chain mychain       ┌─────────┐
  │ IDLE │ ──────────────────────────► │ RUNNING │
  └──────┘                             └────┬────┘
                                            │
                              ┌──────────── │ ──────────┐
                              ▼             ▼           ▼
                        ┌──────────┐  ┌──────┐  ┌──────────┐
                        │COMPLETED │  │FAILED│  │CANCELLED │
                        └──────────┘  └──────┘  └──────────┘
```

Steps execute sequentially. If any step fails, the chain status is set to FAILED.

## Security

- **Auth**: Only Telegram user IDs in `ALLOWED_USER_IDS` can interact
- **Injection**: Prompts and paths are shell-escaped via `shlex.quote` before command substitution
- **Path allowlist**: `/project` validates against `ALLOWED_PROJECT_DIRS`
- **Secrets**: Bot token and user IDs in `.env`, never committed
- **Dashboard auth**: Optional bearer token with timing-safe comparison
- **Security headers**: X-Frame-Options, X-Content-Type-Options, Referrer-Policy, CSP on all dashboard responses
- **Output cap**: Full output limited to 2MB, prompts to 2000 chars
- **Network**: No inbound ports — Telegram long-polling is outbound only

## Success Criteria

1. Send a task from Telegram, agent runs it, get result back
2. Can monitor running task and queue from phone
3. Can cancel a stuck task
4. Survives machine restart / agent crash
5. Works behind NAT without any port forwarding

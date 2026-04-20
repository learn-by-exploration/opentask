# TaskPilot — V1 Specification

## Problem

When you walk away from your dev machine (lunch, commute, overnight), your AI coding agent sits idle. You want to:
- Get notified on your phone when the current task finishes
- Send the next task without returning to the desk
- Monitor progress and cancel if needed

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

## V1 Scope

### In Scope
- Telegram bot with long-polling
- Text message → enqueue task
- Task queue (pending → running → completed/failed)
- OpenCode agent execution via subprocess
- Output capture and smart summarization (Telegram 4096 char limit)
- Task timeout with graceful shutdown (SIGTERM → SIGKILL)
- Recovery on restart (mark orphaned running tasks as failed)
- Commands: /status, /queue, /history, /cancel, /output, /project, /agent, /help

### Out of Scope (Future)
- Web dashboard
- Multi-machine support
- Streaming output to Telegram in real-time
- File upload/download via Telegram
- CI/CD integration
- Task dependencies / chaining
- Priority queue

## Data Model

```
Task:
  id: int (primary key, auto)
  prompt: text (the natural language instruction)
  project_dir: text (working directory for agent)
  agent: text (which agent: "opencode", etc.)
  status: enum (pending, running, completed, failed, cancelled)
  output_summary: text (truncated output for Telegram)
  full_output: text (complete stdout+stderr)
  exit_code: int (nullable)
  error_message: text (nullable)
  telegram_chat_id: int (for sending notifications back)
  telegram_msg_id: int (nullable, for reply threading)
  created_at: datetime
  started_at: datetime (nullable)
  completed_at: datetime (nullable)
  duration_seconds: float (nullable)
```

## Agent Command Templates

Agents are invoked via shell commands with variable substitution:

```yaml
opencode: "cd {project_dir} && echo '{prompt}' | opencode"
aider: "cd {project_dir} && aider --message '{prompt}'"
```

The `{prompt}` is shell-escaped before substitution to prevent injection.

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
```

## Security

- **Auth**: Only Telegram user IDs in `ALLOWED_USER_IDS` can interact
- **Injection**: Prompts are shell-escaped before command substitution
- **Secrets**: Bot token and user IDs in `.env`, never committed
- **Network**: No inbound ports — Telegram long-polling is outbound only

## Success Criteria

1. Send a task from Telegram, agent runs it, get result back
2. Can monitor running task and queue from phone
3. Can cancel a stuck task
4. Survives machine restart / agent crash
5. Works behind NAT without any port forwarding

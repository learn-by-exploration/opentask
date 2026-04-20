# Environment Variables

All configuration is via environment variables, loaded from `.env`.

## Required

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) |
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs |

## Agent Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_AGENT` | `opencode` | Agent to use by default |
| `DEFAULT_PROJECT_DIR` | `~/ai` | Working directory for tasks |
| `DEFAULT_MODEL` | *(empty)* | Default model (e.g., `sonnet`). Empty = agent default |
| `ALLOWED_PROJECT_DIRS` | `~/ai,~/repos,~/projects` | Directories the agent may run in |
| `AGENT_COMMANDS` | *(built-in)* | JSON map: agent name → command template. Use `{prompt}` placeholder |
| `AGENT_MODEL_FLAGS` | *(built-in)* | JSON map: agent name → model flag (e.g., `--model {model}`) |
| `AGENT_CONTINUE_FLAGS` | *(built-in)* | JSON map: agent name → continue flag (e.g., `--continue`) |

### Default Agent Commands

```json
{
  "opencode": "opencode run {prompt}",
  "claude": "claude -p {prompt} --allowedTools computer mcp --no-input"
}
```

## Task Execution

| Variable | Default | Description |
|----------|---------|-------------|
| `TASK_TIMEOUT_SECONDS` | `1800` | Max seconds per task (30 min) |
| `MAX_QUEUE_SIZE` | `20` | Max pending + running tasks |
| `PROGRESS_INTERVAL_SECONDS` | `30` | Seconds between progress notifications |
| `OUTPUT_SUMMARY_MAX_CHARS` | `500` | Max characters for output summaries in Telegram |

## Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `./data/taskpilot.db` | SQLite database path |
| `SKILLS_DIR` | `~/.taskpilot/skills` | Directory for recipe skill packages |

## Web Dashboard

| Variable | Default | Description |
|----------|---------|-------------|
| `DASHBOARD_ENABLED` | `true` | Enable/disable web dashboard |
| `DASHBOARD_HOST` | `127.0.0.1` | Dashboard listen address |
| `DASHBOARD_PORT` | `8095` | Dashboard listen port |
| `DASHBOARD_TOKEN` | *(empty)* | Bearer token for auth. Empty = no auth |

## Workers

| Variable | Default | Description |
|----------|---------|-------------|
| `KNOWN_WORKERS` | *(empty)* | Comma-separated worker names for Telegram buttons |

For worker-side variables, see [Remote Workers](deploy/workers.md).

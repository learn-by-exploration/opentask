# Docker Deployment

## Quick Start

```bash
docker compose up --build -d
```

## What's Included

The Docker image installs:
- **Python 3.10** (slim)
- **Node.js 20** (for Claude CLI)
- **Claude CLI** via npm

## docker-compose.yml

The compose file mounts several volumes so agents have access to your projects and credentials:

| Mount | Purpose |
|-------|---------|
| `./data` → `/app/data` | SQLite database persistence |
| `/home` → `/home` | Access to project directories |
| `~/.claude` → `/root/.claude` | Claude CLI credentials |
| `~/.config` → `/root/.config` | Agent configuration |
| `~/.taskpilot` → `/root/.taskpilot` | Skills and local config |

The dashboard listens on `0.0.0.0:8095` inside the container (overridden from the default `127.0.0.1`).

## Commands

```bash
# Build and start
docker compose up --build -d

# View logs
docker compose logs -f

# Stop
docker compose down

# Rebuild after code changes
docker compose up --build -d
```

## Environment Variables

Set variables in `.env` (same file as non-Docker). The compose file reads from it automatically.

See [Environment Variables](reference/env.md) for the full reference.

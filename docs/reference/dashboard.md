# Web Dashboard & API

The web dashboard runs alongside the bot at `http://127.0.0.1:8095`.

## Dashboard UI

- **Stats overview** — queue depth, completed/failed counts, average duration
- **Running task** — highlighted card with elapsed time
- **Queue view** — pending tasks with priority levels
- **Recent tasks** — status, agent, model, prompt, duration, git diff
- **Chains** — all saved chains with status
- **Auto-refresh** — live updates every 5 seconds

## Authentication

Set `DASHBOARD_TOKEN` in `.env` to require a bearer token:

```
DASHBOARD_TOKEN=my-secret-token
```

All API requests must include `Authorization: Bearer my-secret-token`.

## JSON API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Health check |
| `/api/stats` | GET | Queue depth, task counts, average duration |
| `/api/tasks` | GET | List recent tasks |
| `/api/tasks/{id}` | GET | Get a specific task |
| `/api/queue` | GET | List pending tasks |
| `/api/chains` | GET | List all chains |
| `/api/chains/{id}` | GET | Get a specific chain |

## Worker API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/worker/claim` | POST | Worker claims the next available task |
| `/api/worker/{id}/result` | POST | Worker submits task results |
| `/api/worker/{id}/heartbeat` | POST | Worker sends periodic heartbeat |
| `/api/workers` | GET | List active workers |

See [Remote Workers](deploy/workers.md) for worker setup details.

## Example: Fetching Stats

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8095/api/stats
```

```json
{
  "pending": 2,
  "running": 1,
  "completed": 42,
  "failed": 3,
  "avg_duration_seconds": 85
}
```

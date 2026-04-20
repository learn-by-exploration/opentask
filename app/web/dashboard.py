"""Web dashboard — FastAPI app for monitoring TaskPilot status."""

from __future__ import annotations

import hmac
import html
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.config.settings import settings
from app.core.broker import (
    get_chain_by_id,
    get_pending_tasks,
    get_recent_tasks,
    get_running_task,
    get_task_by_id,
    list_chains,
)
from app.core.models import ChainStatus, TaskStatus

_log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _task_to_dict(task: Any) -> dict:
    """Convert a Task ORM object to a JSON-safe dict."""
    return {
        "id": task.id,
        "prompt": (task.prompt or "")[:200],
        "project_dir": task.project_dir,
        "agent": task.agent,
        "status": task.status.value,
        "exit_code": task.exit_code,
        "error_message": task.error_message,
        "output_summary": task.output_summary,
        "duration_seconds": task.duration_seconds,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "chain_id": task.chain_id,
        "chain_step": task.chain_step,
        "repeat_total": task.repeat_total,
        "repeat_remaining": task.repeat_remaining,
    }


def _chain_to_dict(chain: Any) -> dict:
    """Convert a TaskChain ORM object to a JSON-safe dict."""
    return {
        "id": chain.id,
        "name": chain.name,
        "status": chain.status.value,
        "current_step": chain.current_step,
        "total_steps": chain.total_steps,
        "created_at": chain.created_at.isoformat() if chain.created_at else None,
        "started_at": chain.started_at.isoformat() if chain.started_at else None,
        "completed_at": chain.completed_at.isoformat() if chain.completed_at else None,
    }


def create_dashboard_app() -> FastAPI:
    """Create the FastAPI dashboard application."""
    app = FastAPI(
        title="TaskPilot Dashboard",
        description="Web monitoring dashboard for TaskPilot",
        version="1.0.0",
        docs_url="/api/docs" if not settings.dashboard_token else None,
        redoc_url=None,
    )

    # ── Optional bearer token auth (inner middleware) ────────────────

    @app.middleware("http")
    async def auth_check(request: Request, call_next):  # type: ignore[no-untyped-def]
        token = settings.dashboard_token
        if token:
            auth = request.headers.get("authorization", "").strip()
            expected = f"Bearer {token}"
            if not hmac.compare_digest(auth, expected):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized"},
                )
        return await call_next(request)

    # ── Security headers (outer middleware — wraps ALL responses) ────

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'"
        )
        return response

    # ── JSON API endpoints ──────────────────────────────────────────

    @app.get("/api/health")
    async def api_health() -> dict:
        return {"status": "ok", "service": "taskpilot"}

    @app.get("/api/stats")
    async def api_stats() -> dict:
        try:
            running = await get_running_task()
            pending = await get_pending_tasks()
            recent = await get_recent_tasks(limit=50)
            chains = await list_chains()
        except Exception:
            _log.exception("Dashboard: error fetching stats")
            return JSONResponse(  # type: ignore[return-value]
                status_code=500,
                content={"detail": "Internal error"},
            )

        completed = sum(1 for t in recent if t.status == TaskStatus.COMPLETED)
        failed = sum(1 for t in recent if t.status == TaskStatus.FAILED)
        cancelled = sum(1 for t in recent if t.status == TaskStatus.CANCELLED)

        avg_duration = 0.0
        durations = [t.duration_seconds for t in recent if t.duration_seconds]
        if durations:
            avg_duration = round(sum(durations) / len(durations), 1)

        return {
            "running": _task_to_dict(running) if running else None,
            "pending_count": len(pending),
            "queue_capacity": settings.max_queue_size,
            "recent_completed": completed,
            "recent_failed": failed,
            "recent_cancelled": cancelled,
            "avg_duration_seconds": avg_duration,
            "chains_total": len(chains),
            "chains_running": sum(1 for c in chains if c.status == ChainStatus.RUNNING),
        }

    @app.get("/api/tasks")
    async def api_tasks(limit: int = Query(default=20, ge=1, le=100)) -> list[dict]:
        try:
            tasks = await get_recent_tasks(limit=limit)
        except Exception:
            _log.exception("Dashboard: error fetching tasks")
            return JSONResponse(  # type: ignore[return-value]
                status_code=500,
                content={"detail": "Internal error"},
            )
        return [_task_to_dict(t) for t in tasks]

    @app.get("/api/tasks/{task_id}")
    async def api_task_detail(task_id: int) -> dict:
        try:
            task = await get_task_by_id(task_id)
        except Exception:
            _log.exception("Dashboard: error fetching task %s", task_id)
            return JSONResponse(  # type: ignore[return-value]
                status_code=500,
                content={"detail": "Internal error"},
            )
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        d = _task_to_dict(task)
        d["full_output"] = task.full_output
        d["prompt"] = task.prompt or ""  # full prompt for detail view
        return d

    @app.get("/api/queue")
    async def api_queue() -> list[dict]:
        try:
            tasks = await get_pending_tasks()
        except Exception:
            _log.exception("Dashboard: error fetching queue")
            return JSONResponse(  # type: ignore[return-value]
                status_code=500,
                content={"detail": "Internal error"},
            )
        return [_task_to_dict(t) for t in tasks]

    @app.get("/api/chains")
    async def api_chains() -> list[dict]:
        try:
            chains = await list_chains()
        except Exception:
            _log.exception("Dashboard: error fetching chains")
            return JSONResponse(  # type: ignore[return-value]
                status_code=500,
                content={"detail": "Internal error"},
            )
        return [_chain_to_dict(c) for c in chains]

    @app.get("/api/chains/{chain_id}")
    async def api_chain_detail(chain_id: int) -> dict:
        try:
            chain = await get_chain_by_id(chain_id)
        except Exception:
            _log.exception("Dashboard: error fetching chain %s", chain_id)
            return JSONResponse(  # type: ignore[return-value]
                status_code=500,
                content={"detail": "Internal error"},
            )
        if chain is None:
            raise HTTPException(status_code=404, detail="Chain not found")
        d = _chain_to_dict(chain)
        d["steps"] = chain.steps
        return d

    # ── HTML dashboard ──────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_page() -> str:
        return _render_dashboard()

    return app


# ── Embedded HTML dashboard (no external template deps) ─────────────


def _esc(text: str | None) -> str:
    """HTML-escape a string, returning empty string for None."""
    if text is None:
        return ""
    return html.escape(str(text))


STATUS_COLORS = {
    "pending": "#6b7280",
    "running": "#3b82f6",
    "completed": "#22c55e",
    "failed": "#ef4444",
    "cancelled": "#f59e0b",
}

STATUS_ICONS = {
    "pending": "&#x1F4CB;",
    "running": "&#x1F504;",
    "completed": "&#x2705;",
    "failed": "&#x274C;",
    "cancelled": "&#x1F6AB;",
}


def _render_dashboard() -> str:
    """Return self-contained HTML with auto-refresh via fetch()."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TaskPilot Dashboard</title>
<style>
  :root { --bg: #0f172a; --surface: #1e293b; --border: #334155; --text: #e2e8f0; --dim: #94a3b8; --accent: #3b82f6; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); }
  .header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 1rem 2rem; display: flex; align-items: center; gap: 1rem; }
  .header h1 { font-size: 1.5rem; font-weight: 600; }
  .header .badge { font-size: 0.75rem; padding: 2px 8px; border-radius: 9999px; background: #22c55e; color: #000; font-weight: 600; }
  .header .refresh-info { margin-left: auto; font-size: 0.8rem; color: var(--dim); }
  .container { max-width: 1200px; margin: 0 auto; padding: 1.5rem; }
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
  .stat-card { background: var(--surface); border: 1px solid var(--border); border-radius: 0.5rem; padding: 1rem; }
  .stat-card .label { font-size: 0.75rem; color: var(--dim); text-transform: uppercase; letter-spacing: 0.05em; }
  .stat-card .value { font-size: 1.8rem; font-weight: 700; margin-top: 0.25rem; }
  .stat-card .sub { font-size: 0.8rem; color: var(--dim); margin-top: 0.25rem; }
  .section { background: var(--surface); border: 1px solid var(--border); border-radius: 0.5rem; margin-bottom: 1.5rem; overflow: hidden; }
  .section-header { padding: 0.75rem 1rem; border-bottom: 1px solid var(--border); font-weight: 600; font-size: 0.9rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th { text-align: left; padding: 0.5rem 1rem; color: var(--dim); font-size: 0.75rem; text-transform: uppercase; border-bottom: 1px solid var(--border); }
  td { padding: 0.5rem 1rem; border-bottom: 1px solid var(--border); vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  .status-badge { display: inline-block; padding: 2px 8px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; }
  .prompt-cell { max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .empty-state { padding: 2rem; text-align: center; color: var(--dim); }
  .running-card { background: linear-gradient(135deg, #1e3a5f, #1e293b); border: 1px solid #3b82f6; border-radius: 0.5rem; padding: 1rem; margin-bottom: 1.5rem; }
  .running-card h3 { color: #60a5fa; margin-bottom: 0.5rem; }
  .running-card .prompt { color: var(--text); font-size: 0.9rem; }
  .running-card .meta { color: var(--dim); font-size: 0.8rem; margin-top: 0.5rem; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  @media (max-width: 640px) { .container { padding: 0.75rem; } .stats-grid { grid-template-columns: repeat(2, 1fr); } }
</style>
</head>
<body>
<div class="header">
  <h1>&#x1F680; TaskPilot</h1>
  <span class="badge" id="health-badge">LIVE</span>
  <span class="refresh-info">Auto-refresh: <span id="countdown">5</span>s</span>
</div>
<div class="container">
  <div id="running-section"></div>
  <div class="stats-grid" id="stats-grid"></div>
  <div class="section">
    <div class="section-header">&#x1F4CB; Queue</div>
    <div id="queue-body"><div class="empty-state">Loading...</div></div>
  </div>
  <div class="section">
    <div class="section-header">&#x1F4DC; Recent Tasks</div>
    <div id="tasks-body"><div class="empty-state">Loading...</div></div>
  </div>
  <div class="section">
    <div class="section-header">&#x26D3;&#xFE0F; Chains</div>
    <div id="chains-body"><div class="empty-state">Loading...</div></div>
  </div>
</div>
<script>
const STATUS_COLORS = {"pending":"#6b7280","running":"#3b82f6","completed":"#22c55e","failed":"#ef4444","cancelled":"#f59e0b"};
const STATUS_ICONS = {"pending":"\\u{1F4CB}","running":"\\u{1F504}","completed":"\\u{2705}","failed":"\\u{274C}","cancelled":"\\u{1F6AB}"};

function esc(s) { if (s === null || s === undefined) return ''; const d = document.createElement('div'); d.textContent = String(s); return d.innerHTML; }

function timeAgo(iso) {
  if (!iso) return '';
  const diff = (Date.now() - new Date(iso + 'Z').getTime()) / 1000;
  if (diff < 60) return Math.round(diff) + 's ago';
  if (diff < 3600) return Math.round(diff/60) + 'm ago';
  if (diff < 86400) return Math.round(diff/3600) + 'h ago';
  return Math.round(diff/86400) + 'd ago';
}

function badge(status) {
  const c = STATUS_COLORS[status] || '#6b7280';
  const i = STATUS_ICONS[status] || '';
  return `<span class="status-badge" style="background:${c}20;color:${c}">${i} ${esc(status)}</span>`;
}

async function refresh() {
  try {
    const results = await Promise.allSettled([
      fetch('/api/stats').then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }),
      fetch('/api/tasks?limit=20').then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }),
      fetch('/api/queue').then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }),
      fetch('/api/chains').then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }),
    ]);

    const stats = results[0].status === 'fulfilled' ? results[0].value : null;
    const tasks = results[1].status === 'fulfilled' ? results[1].value : null;
    const queue = results[2].status === 'fulfilled' ? results[2].value : null;
    const chains = results[3].status === 'fulfilled' ? results[3].value : null;
    const anyFailed = results.some(r => r.status === 'rejected');

    // Running task
    const rs = document.getElementById('running-section');
    if (stats && stats.running) {
      const t = stats.running;
      const elapsed = t.started_at ? timeAgo(t.started_at) : '';
      rs.innerHTML = `<div class="running-card">
        <h3>\\u{1F504} Running Task #${esc(t.id)}</h3>
        <div class="prompt">${esc(t.prompt)}</div>
        <div class="meta">${esc(t.agent)} &bull; started ${elapsed}</div>
      </div>`;
    } else if (stats) {
      rs.innerHTML = '';
    }

    // Stats
    if (stats) {
    document.getElementById('stats-grid').innerHTML = `
      <div class="stat-card"><div class="label">Queue</div><div class="value">${esc(stats.pending_count)}</div><div class="sub">/ ${esc(stats.queue_capacity)} capacity</div></div>
      <div class="stat-card"><div class="label">Completed</div><div class="value" style="color:#22c55e">${esc(stats.recent_completed)}</div><div class="sub">recent</div></div>
      <div class="stat-card"><div class="label">Failed</div><div class="value" style="color:#ef4444">${esc(stats.recent_failed)}</div><div class="sub">recent</div></div>
      <div class="stat-card"><div class="label">Avg Duration</div><div class="value">${esc(stats.avg_duration_seconds)}s</div><div class="sub">recent tasks</div></div>
      <div class="stat-card"><div class="label">Chains</div><div class="value">${esc(stats.chains_total)}</div><div class="sub">${esc(stats.chains_running)} running</div></div>
    `;
    }

    // Queue
    if (queue !== null) {
    const qb = document.getElementById('queue-body');
    if (queue.length === 0) {
      qb.innerHTML = '<div class="empty-state">Queue is empty \\u{1F4ED}</div>';
    } else {
      qb.innerHTML = '<table><tr><th>#</th><th>Agent</th><th>Prompt</th><th>Created</th></tr>' +
        queue.map(t => `<tr><td>${esc(t.id)}</td><td>${esc(t.agent)}</td><td class="prompt-cell">${esc(t.prompt)}</td><td>${timeAgo(t.created_at)}</td></tr>`).join('') + '</table>';
    }
    }

    // Tasks
    if (tasks !== null) {
    const tb = document.getElementById('tasks-body');
    if (tasks.length === 0) {
      tb.innerHTML = '<div class="empty-state">No tasks yet</div>';
    } else {
      tb.innerHTML = '<table><tr><th>#</th><th>Status</th><th>Agent</th><th>Prompt</th><th>Duration</th><th>When</th></tr>' +
        tasks.map(t => `<tr>
          <td><a href="/api/tasks/${esc(t.id)}">${esc(t.id)}</a></td>
          <td>${badge(t.status)}</td>
          <td>${esc(t.agent)}</td>
          <td class="prompt-cell">${esc(t.prompt)}</td>
          <td>${t.duration_seconds ? esc(t.duration_seconds) + 's' : '-'}</td>
          <td>${timeAgo(t.completed_at || t.created_at)}</td>
        </tr>`).join('') + '</table>';
    }
    }

    // Chains
    if (chains !== null) {
    const cb = document.getElementById('chains-body');
    if (chains.length === 0) {
      cb.innerHTML = '<div class="empty-state">No chains saved</div>';
    } else {
      cb.innerHTML = '<table><tr><th>Name</th><th>Status</th><th>Steps</th><th>Progress</th><th>Created</th></tr>' +
        chains.map(c => `<tr>
          <td><a href="/api/chains/${esc(c.id)}">${esc(c.name)}</a></td>
          <td>${badge(c.status)}</td>
          <td>${esc(c.total_steps)}</td>
          <td>${esc(c.current_step)}/${esc(c.total_steps)}</td>
          <td>${timeAgo(c.created_at)}</td>
        </tr>`).join('') + '</table>';
    }
    }

    document.getElementById('health-badge').textContent = anyFailed ? 'PARTIAL' : 'LIVE';
    document.getElementById('health-badge').style.background = anyFailed ? '#f59e0b' : '#22c55e';
  } catch (e) {
    document.getElementById('health-badge').textContent = 'ERROR';
    document.getElementById('health-badge').style.background = '#ef4444';
  }
}

let count = 5;
function tick() {
  count--;
  document.getElementById('countdown').textContent = count;
  if (count <= 0) { refresh().finally(() => { count = 5; scheduleNext(); }); return; }
  scheduleNext();
}
function scheduleNext() { setTimeout(tick, 1000); }
refresh().finally(scheduleNext);
</script>
</body>
</html>"""

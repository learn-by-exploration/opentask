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
    cancel_task_by_id,
    enqueue_task,
    get_chain_by_id,
    get_pending_tasks,
    get_recent_tasks,
    get_running_task,
    get_task_by_id,
    list_active_workers,
    list_chains,
    recover_stale_worker_tasks,
    retry_task,
    search_tasks,
    worker_claim_task,
    worker_heartbeat,
    worker_submit_result,
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
        "assigned_to": getattr(task, "assigned_to", None),
        "worker_id": getattr(task, "worker_id", None),
        "priority": getattr(task, "priority", 0),
        "retry_count": getattr(task, "retry_count", 0),
        "git_diff": getattr(task, "git_diff", None),
        "model": getattr(task, "model", None),
        "fallback_index": getattr(task, "fallback_index", 0),
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

    @app.get("/api/fallbacks")
    async def api_get_fallbacks() -> dict:
        """Return current model fallback chain."""
        return {
            "fallbacks": settings.model_fallbacks_list,
            "count": len(settings.model_fallbacks_list),
        }

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

    @app.get("/api/tasks/search")
    async def api_search_tasks(q: str = Query(..., min_length=1)) -> list[dict]:
        """Search tasks by prompt text."""
        try:
            tasks = await search_tasks(q)
        except Exception:
            _log.exception("Dashboard: error searching tasks")
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

    # ── Task Management API (for OpenClaw / external integrations) ──

    @app.post("/api/tasks")
    async def api_create_task(request: Request) -> JSONResponse:
        """Submit a new task to the queue."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})

        prompt = body.get("prompt", "").strip() if isinstance(body.get("prompt"), str) else ""
        if not prompt:
            return JSONResponse(status_code=400, content={"detail": "prompt is required"})
        if len(prompt) > settings.max_prompt_len:
            return JSONResponse(
                status_code=400,
                content={"detail": f"prompt exceeds {settings.max_prompt_len} chars"},
            )

        project_dir = body.get("project_dir") or None
        agent = body.get("agent") or None
        model = body.get("model") or None
        assigned_to = body.get("assigned_to") or None
        callback_url = body.get("callback_url") or None

        try:
            task = await enqueue_task(
                prompt=prompt,
                project_dir=project_dir,
                agent=agent,
                model=model,
                assigned_to=assigned_to,
            )
        except ValueError as exc:
            return JSONResponse(status_code=409, content={"detail": str(exc)})
        except Exception:
            _log.exception("Dashboard: error creating task")
            return JSONResponse(status_code=500, content={"detail": "Internal error"})

        result = _task_to_dict(task)
        result["prompt"] = task.prompt or ""  # full prompt in create response
        if callback_url:
            result["callback_url"] = callback_url  # echo back for client tracking
        return JSONResponse(status_code=201, content=result)

    @app.post("/api/tasks/{task_id}/cancel")
    async def api_cancel_task(task_id: int) -> JSONResponse:
        """Cancel a PENDING task."""
        try:
            task = await cancel_task_by_id(task_id)
        except Exception:
            _log.exception("Dashboard: error cancelling task %s", task_id)
            return JSONResponse(status_code=500, content={"detail": "Internal error"})
        if task is None:
            return JSONResponse(
                status_code=404,
                content={"detail": "Task not found or not cancellable"},
            )
        return JSONResponse(content=_task_to_dict(task))

    @app.post("/api/tasks/{task_id}/retry")
    async def api_retry_task(task_id: int) -> JSONResponse:
        """Retry a FAILED or CANCELLED task."""
        try:
            task = await retry_task(task_id)
        except Exception:
            _log.exception("Dashboard: error retrying task %s", task_id)
            return JSONResponse(status_code=500, content={"detail": "Internal error"})
        if task is None:
            return JSONResponse(
                status_code=404,
                content={"detail": "Task not found or not retryable"},
            )
        return JSONResponse(status_code=201, content=_task_to_dict(task))

    # ── Worker API (multi-machine) ──────────────────────────────────

    @app.post("/api/worker/claim")
    async def api_worker_claim(request: Request) -> JSONResponse:
        """Atomically claim the next available task for a remote worker.

        Body: {"worker_id": "server2"}
        Returns 200 + task dict, or 204 if nothing to claim.
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})

        worker_id = str(body.get("worker_id", "")).strip()
        if not worker_id or len(worker_id) > 128:
            return JSONResponse(
                status_code=400,
                content={"detail": "worker_id is required (1-128 chars)"},
            )

        # Recover stale tasks before claiming
        await recover_stale_worker_tasks()

        try:
            task = await worker_claim_task(worker_id)
        except ValueError as e:
            return JSONResponse(status_code=400, content={"detail": str(e)})
        except Exception:
            _log.exception("Worker claim error")
            return JSONResponse(status_code=500, content={"detail": "Internal error"})

        if task is None:
            return JSONResponse(status_code=204, content=None)

        d = _task_to_dict(task)
        d["full_output"] = None  # not relevant for claim
        d["prompt"] = task.prompt  # full prompt for execution
        return JSONResponse(status_code=200, content=d)

    @app.post("/api/worker/{task_id}/result")
    async def api_worker_result(task_id: int, request: Request) -> JSONResponse:
        """Submit the result of a task executed by a remote worker.

        Body: {"worker_id": "server2", "exit_code": 0,
               "output_summary": "...", "full_output": "...",
               "error_message": null}
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})

        worker_id = str(body.get("worker_id", "")).strip()
        if not worker_id:
            return JSONResponse(status_code=400, content={"detail": "worker_id required"})

        exit_code = body.get("exit_code")
        if exit_code is None or not isinstance(exit_code, int):
            return JSONResponse(status_code=400, content={"detail": "exit_code (int) required"})

        output_summary = str(body.get("output_summary", ""))[:500]
        full_output = str(body.get("full_output", ""))[:2_000_000]
        error_message = body.get("error_message")
        if error_message is not None:
            error_message = str(error_message)[:2000]

        try:
            task = await worker_submit_result(
                task_id=task_id,
                worker_id=worker_id,
                exit_code=exit_code,
                output_summary=output_summary,
                full_output=full_output,
                error_message=error_message,
            )
        except Exception:
            _log.exception("Worker result error")
            return JSONResponse(status_code=500, content={"detail": "Internal error"})

        if task is None:
            return JSONResponse(
                status_code=404,
                content={"detail": "Task not found, not running, or not owned by this worker"},
            )

        return JSONResponse(status_code=200, content=_task_to_dict(task))

    @app.post("/api/worker/{task_id}/heartbeat")
    async def api_worker_heartbeat(task_id: int, request: Request) -> JSONResponse:
        """Keep-alive signal from a remote worker executing a task.

        Body: {"worker_id": "server2"}
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})

        worker_id = str(body.get("worker_id", "")).strip()
        if not worker_id:
            return JSONResponse(status_code=400, content={"detail": "worker_id required"})

        try:
            ok = await worker_heartbeat(task_id, worker_id)
        except Exception:
            _log.exception("Worker heartbeat error")
            return JSONResponse(status_code=500, content={"detail": "Internal error"})

        if not ok:
            return JSONResponse(
                status_code=404,
                content={"detail": "Task not found or not owned by this worker"},
            )

        return JSONResponse(status_code=200, content={"ok": True})

    @app.get("/api/workers")
    async def api_workers() -> list[dict]:
        """List active workers with their running tasks."""
        try:
            return await list_active_workers()
        except Exception:
            _log.exception("Workers list error")
            return JSONResponse(  # type: ignore[return-value]
                status_code=500,
                content={"detail": "Internal error"},
            )

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
  :root {
    --bg: #0f172a; --surface: #1e293b; --surface2: #273548; --border: #334155;
    --text: #e2e8f0; --dim: #94a3b8; --accent: #3b82f6; --accent-dim: #2563eb;
    --green: #22c55e; --red: #ef4444; --yellow: #f59e0b; --purple: #a855f7;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); }

  /* Header */
  .header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 0.75rem 2rem; display: flex; align-items: center; gap: 1rem; position: sticky; top: 0; z-index: 100; }
  .header h1 { font-size: 1.3rem; font-weight: 700; }
  .badge { font-size: 0.7rem; padding: 2px 8px; border-radius: 9999px; font-weight: 600; }
  .badge-live { background: var(--green); color: #000; }
  .badge-warn { background: var(--yellow); color: #000; }
  .badge-err { background: var(--red); color: #fff; }
  .header-right { margin-left: auto; display: flex; align-items: center; gap: 1rem; font-size: 0.8rem; color: var(--dim); }

  .container { max-width: 1400px; margin: 0 auto; padding: 1.5rem; }

  /* Stats strip */
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 0.75rem; margin-bottom: 1.25rem; }
  .stat-card { background: var(--surface); border: 1px solid var(--border); border-radius: 0.5rem; padding: 0.875rem 1rem; }
  .stat-card .label { font-size: 0.7rem; color: var(--dim); text-transform: uppercase; letter-spacing: 0.05em; }
  .stat-card .value { font-size: 1.6rem; font-weight: 700; margin-top: 0.15rem; }
  .stat-card .sub { font-size: 0.75rem; color: var(--dim); margin-top: 0.15rem; }

  /* Running card */
  .running-card { background: linear-gradient(135deg, #1e3a5f 0%, var(--surface) 100%); border: 1px solid var(--accent); border-radius: 0.5rem; padding: 1rem 1.25rem; margin-bottom: 1.25rem; display: flex; align-items: flex-start; gap: 1rem; }
  .running-pulse { width: 12px; height: 12px; border-radius: 50%; background: var(--accent); margin-top: 4px; animation: pulse 1.5s infinite; flex-shrink: 0; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
  .running-info { flex: 1; min-width: 0; }
  .running-info h3 { color: #60a5fa; font-size: 0.95rem; margin-bottom: 0.3rem; }
  .running-info .prompt { color: var(--text); font-size: 0.85rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .running-info .meta { color: var(--dim); font-size: 0.8rem; margin-top: 0.35rem; display: flex; flex-wrap: wrap; gap: 0.5rem; }
  .running-info .meta span { display: inline-flex; align-items: center; gap: 0.25rem; }
  .running-elapsed { font-size: 1.6rem; font-weight: 700; color: #60a5fa; font-variant-numeric: tabular-nums; flex-shrink: 0; }

  /* Tabs */
  .tabs { display: flex; gap: 0; border-bottom: 1px solid var(--border); margin-bottom: 1.25rem; }
  .tab { padding: 0.6rem 1.25rem; font-size: 0.85rem; font-weight: 500; color: var(--dim); cursor: pointer; border-bottom: 2px solid transparent; transition: all 0.15s; }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab .count { font-size: 0.7rem; background: var(--surface2); padding: 1px 6px; border-radius: 9999px; margin-left: 0.35rem; }

  /* Sections */
  .section { background: var(--surface); border: 1px solid var(--border); border-radius: 0.5rem; overflow: hidden; display: none; }
  .section.active { display: block; }
  table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  th { text-align: left; padding: 0.55rem 1rem; color: var(--dim); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.04em; border-bottom: 1px solid var(--border); background: var(--surface2); position: sticky; top: 0; }
  td { padding: 0.5rem 1rem; border-bottom: 1px solid var(--border); vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(59,130,246,0.04); }
  .prompt-cell { max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .empty-state { padding: 2.5rem; text-align: center; color: var(--dim); font-size: 0.9rem; }
  .status-badge { display: inline-block; padding: 2px 8px; border-radius: 9999px; font-size: 0.72rem; font-weight: 600; white-space: nowrap; }
  .priority-badge { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; background: var(--purple); color: #fff; }
  .diff-badge { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: 500; background: #166534; color: #86efac; }
  .worker-tag { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 0.7rem; background: #1e3a5f; color: #93c5fd; }
  .model-tag { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 0.7rem; background: #3b1f6e; color: #c4b5fd; }
  .retry-tag { display: inline-block; padding: 1px 5px; border-radius: 4px; font-size: 0.65rem; background: #7c2d12; color: #fed7aa; }
  .clickable { cursor: pointer; }
  a { color: var(--accent); text-decoration: none; }

  /* Workers grid */
  .workers-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 0.75rem; padding: 1rem; }
  .worker-card { background: var(--surface2); border: 1px solid var(--border); border-radius: 0.5rem; padding: 0.875rem; }
  .worker-card .wname { font-weight: 600; font-size: 0.9rem; margin-bottom: 0.3rem; }
  .worker-card .wstatus { font-size: 0.8rem; color: var(--dim); }
  .worker-card .wtask { font-size: 0.78rem; color: var(--text); margin-top: 0.3rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* Modal */
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 200; justify-content: center; align-items: flex-start; padding: 3rem 1rem; overflow-y: auto; }
  .modal-overlay.open { display: flex; }
  .modal { background: var(--surface); border: 1px solid var(--border); border-radius: 0.75rem; width: 100%; max-width: 720px; max-height: 85vh; overflow-y: auto; }
  .modal-header { display: flex; align-items: center; justify-content: space-between; padding: 1rem 1.25rem; border-bottom: 1px solid var(--border); position: sticky; top: 0; background: var(--surface); z-index: 1; }
  .modal-header h2 { font-size: 1rem; }
  .modal-close { background: none; border: none; color: var(--dim); font-size: 1.3rem; cursor: pointer; padding: 0.25rem; }
  .modal-close:hover { color: var(--text); }
  .modal-body { padding: 1.25rem; }
  .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem 1.5rem; margin-bottom: 1rem; font-size: 0.85rem; }
  .detail-grid .dkey { color: var(--dim); font-size: 0.75rem; text-transform: uppercase; }
  .detail-grid .dval { font-weight: 500; }
  .detail-output { margin-top: 1rem; }
  .detail-output h4 { font-size: 0.8rem; color: var(--dim); text-transform: uppercase; margin-bottom: 0.5rem; }
  .detail-output pre { background: var(--bg); border: 1px solid var(--border); border-radius: 0.375rem; padding: 0.75rem; font-size: 0.78rem; overflow-x: auto; white-space: pre-wrap; word-break: break-word; max-height: 300px; overflow-y: auto; color: var(--text); font-family: 'SF Mono', 'Fira Code', monospace; }
  .detail-diff { margin-top: 0.75rem; padding: 0.75rem; background: #0d2818; border: 1px solid #166534; border-radius: 0.375rem; font-size: 0.82rem; color: #86efac; }

  @media (max-width: 640px) {
    .container { padding: 0.75rem; }
    .stats-grid { grid-template-columns: repeat(2, 1fr); }
    .running-card { flex-direction: column; }
    .running-elapsed { font-size: 1.2rem; }
    .detail-grid { grid-template-columns: 1fr; }
    .header { padding: 0.75rem 1rem; }
    .tab { padding: 0.5rem 0.75rem; font-size: 0.8rem; }
  }
</style>
</head>
<body>
<div class="header">
  <h1>&#x1F680; TaskPilot</h1>
  <span class="badge badge-live" id="health-badge">LIVE</span>
  <div class="header-right">
    <span>&#x23F1; <span id="countdown">5</span>s</span>
  </div>
</div>
<div class="container">
  <div id="running-section"></div>
  <div class="stats-grid" id="stats-grid"></div>
  <div class="tabs" id="tabs">
    <div class="tab active" data-tab="queue">Queue <span class="count" id="tab-queue-count">0</span></div>
    <div class="tab" data-tab="tasks">Tasks <span class="count" id="tab-tasks-count">0</span></div>
    <div class="tab" data-tab="chains">Chains <span class="count" id="tab-chains-count">0</span></div>
    <div class="tab" data-tab="workers">Workers <span class="count" id="tab-workers-count">0</span></div>
  </div>
  <div class="section active" id="section-queue">
    <div id="queue-body"><div class="empty-state">Loading...</div></div>
  </div>
  <div class="section" id="section-tasks">
    <div id="tasks-body"><div class="empty-state">Loading...</div></div>
  </div>
  <div class="section" id="section-chains">
    <div id="chains-body"><div class="empty-state">Loading...</div></div>
  </div>
  <div class="section" id="section-workers">
    <div id="workers-body"><div class="empty-state">Loading...</div></div>
  </div>
</div>

<!-- Task detail modal -->
<div class="modal-overlay" id="modal-overlay">
  <div class="modal">
    <div class="modal-header">
      <h2 id="modal-title">Task Detail</h2>
      <button class="modal-close" id="modal-close">&times;</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<script>
const STATUS_COLORS = {"pending":"#6b7280","running":"#3b82f6","completed":"#22c55e","failed":"#ef4444","cancelled":"#f59e0b"};
const STATUS_ICONS = {"pending":"\\u{1F4CB}","running":"\\u{1F504}","completed":"\\u{2705}","failed":"\\u{274C}","cancelled":"\\u{1F6AB}"};

function esc(s) { if (s === null || s === undefined) return ''; const d = document.createElement('div'); d.textContent = String(s); return d.innerHTML; }

function fmtDuration(secs) {
  if (!secs && secs !== 0) return '-';
  if (secs < 60) return secs + 's';
  const m = Math.floor(secs / 60), s = secs % 60;
  return m + 'm ' + (s < 10 ? '0' : '') + s + 's';
}

function timeAgo(iso) {
  if (!iso) return '';
  const diff = (Date.now() - new Date(iso + 'Z').getTime()) / 1000;
  if (diff < 60) return Math.round(diff) + 's ago';
  if (diff < 3600) return Math.round(diff / 60) + 'm ago';
  if (diff < 86400) return Math.round(diff / 3600) + 'h ago';
  return Math.round(diff / 86400) + 'd ago';
}

function elapsedSince(iso) {
  if (!iso) return '0:00';
  const diff = Math.max(0, Math.floor((Date.now() - new Date(iso + 'Z').getTime()) / 1000));
  const m = Math.floor(diff / 60), s = diff % 60;
  return m + ':' + (s < 10 ? '0' : '') + s;
}

function badge(status) {
  const c = STATUS_COLORS[status] || '#6b7280';
  const i = STATUS_ICONS[status] || '';
  return '<span class="status-badge" style="background:' + c + '20;color:' + c + '">' + i + ' ' + esc(status) + '</span>';
}

function tags(t) {
  let h = '';
  if (t.model) h += ' <span class="model-tag">' + esc(t.model) + '</span>';
  if (t.assigned_to) h += ' <span class="worker-tag">@' + esc(t.assigned_to) + '</span>';
  if (t.priority > 0) h += ' <span class="priority-badge">P' + esc(t.priority) + '</span>';
  if (t.retry_count > 0) h += ' <span class="retry-tag">retry ' + esc(t.retry_count) + '</span>';
  if (t.git_diff) h += ' <span class="diff-badge">&#x1F4C4; diff</span>';
  return h;
}

// Tabs
let activeTab = 'queue';
document.getElementById('tabs').addEventListener('click', function(e) {
  const tab = e.target.closest('.tab');
  if (!tab) return;
  activeTab = tab.dataset.tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === activeTab));
  document.querySelectorAll('.section').forEach(s => s.classList.toggle('active', s.id === 'section-' + activeTab));
});

// Modal
const overlay = document.getElementById('modal-overlay');
document.getElementById('modal-close').onclick = () => overlay.classList.remove('open');
overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.classList.remove('open'); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') overlay.classList.remove('open'); });

async function showTaskDetail(id) {
  const mb = document.getElementById('modal-body');
  mb.innerHTML = '<div class="empty-state">Loading...</div>';
  document.getElementById('modal-title').textContent = 'Task #' + id;
  overlay.classList.add('open');
  try {
    const r = await fetch('/api/tasks/' + id);
    if (!r.ok) { mb.innerHTML = '<div class="empty-state">Error loading task</div>'; return; }
    const t = await r.json();
    let h = '<div class="detail-grid">';
    h += '<div><div class="dkey">Status</div><div class="dval">' + badge(t.status) + '</div></div>';
    h += '<div><div class="dkey">Agent</div><div class="dval">' + esc(t.agent) + (t.model ? ' <span class="model-tag">' + esc(t.model) + '</span>' : '') + '</div></div>';
    h += '<div><div class="dkey">Project</div><div class="dval">' + esc(t.project_dir) + '</div></div>';
    h += '<div><div class="dkey">Duration</div><div class="dval">' + fmtDuration(t.duration_seconds) + '</div></div>';
    if (t.assigned_to) h += '<div><div class="dkey">Worker</div><div class="dval"><span class="worker-tag">@' + esc(t.assigned_to) + '</span></div></div>';
    if (t.priority > 0) h += '<div><div class="dkey">Priority</div><div class="dval"><span class="priority-badge">P' + esc(t.priority) + '</span></div></div>';
    if (t.retry_count > 0) h += '<div><div class="dkey">Retry</div><div class="dval">' + esc(t.retry_count) + '</div></div>';
    if (t.exit_code != null) h += '<div><div class="dkey">Exit Code</div><div class="dval">' + esc(t.exit_code) + '</div></div>';
    h += '<div><div class="dkey">Created</div><div class="dval">' + esc(t.created_at) + '</div></div>';
    if (t.completed_at) h += '<div><div class="dkey">Completed</div><div class="dval">' + esc(t.completed_at) + '</div></div>';
    h += '</div>';
    h += '<div class="detail-output"><h4>Prompt</h4><pre>' + esc(t.prompt) + '</pre></div>';
    if (t.output_summary) h += '<div class="detail-output"><h4>Output Summary</h4><pre>' + esc(t.output_summary) + '</pre></div>';
    if (t.full_output) h += '<div class="detail-output"><h4>Full Output</h4><pre>' + esc(t.full_output) + '</pre></div>';
    if (t.error_message) h += '<div class="detail-output"><h4>Error</h4><pre style="color:#fca5a5">' + esc(t.error_message) + '</pre></div>';
    if (t.git_diff) h += '<div class="detail-diff">&#x1F4C4; <strong>Git Diff:</strong> ' + esc(t.git_diff) + '</div>';
    mb.innerHTML = h;
  } catch (e) {
    mb.innerHTML = '<div class="empty-state">Failed to load task details</div>';
  }
}

// Running task live timer
let runningStartedAt = null;
function updateRunningTimer() {
  const el = document.getElementById('running-timer');
  if (el && runningStartedAt) el.textContent = elapsedSince(runningStartedAt);
}
function scheduleTimer() { setTimeout(function() { updateRunningTimer(); scheduleTimer(); }, 1000); }
scheduleTimer();

async function refresh() {
  try {
    const results = await Promise.allSettled([
      fetch('/api/stats').then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }),
      fetch('/api/tasks?limit=30').then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }),
      fetch('/api/queue').then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }),
      fetch('/api/chains').then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }),
      fetch('/api/workers').then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }),
    ]);

    const stats = results[0].status === 'fulfilled' ? results[0].value : null;
    const tasks = results[1].status === 'fulfilled' ? results[1].value : [];
    const queue = results[2].status === 'fulfilled' ? results[2].value : [];
    const chains = results[3].status === 'fulfilled' ? results[3].value : [];
    const workers = results[4].status === 'fulfilled' ? results[4].value : [];
    const anyFailed = results.some(r => r.status === 'rejected');

    // Tab counts
    document.getElementById('tab-queue-count').textContent = queue.length;
    document.getElementById('tab-tasks-count').textContent = tasks.length;
    document.getElementById('tab-chains-count').textContent = chains.length;
    document.getElementById('tab-workers-count').textContent = workers.length;

    // Running task
    const rs = document.getElementById('running-section');
    if (stats && stats.running) {
      const t = stats.running;
      runningStartedAt = t.started_at;
      let metaH = '<span>&#x1F916; ' + esc(t.agent) + '</span>';
      if (t.model) metaH += '<span>&#x2699; ' + esc(t.model) + '</span>';
      if (t.assigned_to) metaH += '<span>&#x1F4E1; @' + esc(t.assigned_to) + '</span>';
      metaH += '<span>&#x1F4C2; ' + esc(t.project_dir) + '</span>';
      rs.innerHTML = '<div class="running-card clickable" onclick="showTaskDetail(' + t.id + ')">' +
        '<div class="running-pulse"></div>' +
        '<div class="running-info"><h3>Running Task #' + esc(t.id) + '</h3>' +
        '<div class="prompt">' + esc(t.prompt) + '</div>' +
        '<div class="meta">' + metaH + '</div></div>' +
        '<div class="running-elapsed" id="running-timer">' + elapsedSince(t.started_at) + '</div>' +
        '</div>';
    } else {
      runningStartedAt = null;
      rs.innerHTML = '';
    }

    // Stats
    if (stats) {
      const total = stats.recent_completed + stats.recent_failed;
      const rate = total > 0 ? Math.round(stats.recent_completed / total * 100) : 100;
      const rateColor = rate >= 80 ? '#22c55e' : rate >= 50 ? '#f59e0b' : '#ef4444';
      document.getElementById('stats-grid').innerHTML =
        '<div class="stat-card"><div class="label">Queue</div><div class="value">' + esc(stats.pending_count) + '</div><div class="sub">/ ' + esc(stats.queue_capacity) + ' capacity</div></div>' +
        '<div class="stat-card"><div class="label">Completed</div><div class="value" style="color:#22c55e">' + esc(stats.recent_completed) + '</div><div class="sub">recent</div></div>' +
        '<div class="stat-card"><div class="label">Failed</div><div class="value" style="color:#ef4444">' + esc(stats.recent_failed) + '</div><div class="sub">recent</div></div>' +
        '<div class="stat-card"><div class="label">Success Rate</div><div class="value" style="color:' + rateColor + '">' + rate + '%</div><div class="sub">last ' + total + ' tasks</div></div>' +
        '<div class="stat-card"><div class="label">Avg Duration</div><div class="value">' + fmtDuration(stats.avg_duration_seconds) + '</div><div class="sub">recent</div></div>' +
        '<div class="stat-card"><div class="label">Chains</div><div class="value">' + esc(stats.chains_total) + '</div><div class="sub">' + esc(stats.chains_running) + ' running</div></div>' +
        '<div class="stat-card"><div class="label">Workers</div><div class="value">' + esc(workers.length) + '</div><div class="sub">connected</div></div>';
    }

    // Queue
    const qb = document.getElementById('queue-body');
    if (queue.length === 0) {
      qb.innerHTML = '<div class="empty-state">Queue is empty &#x1F4ED;</div>';
    } else {
      qb.innerHTML = '<table><tr><th>#</th><th>Agent</th><th>Prompt</th><th>Tags</th><th>Created</th></tr>' +
        queue.map(t => '<tr class="clickable" onclick="showTaskDetail(' + t.id + ')">' +
          '<td>' + esc(t.id) + '</td>' +
          '<td>' + esc(t.agent) + '</td>' +
          '<td class="prompt-cell">' + esc(t.prompt) + '</td>' +
          '<td>' + tags(t) + '</td>' +
          '<td>' + timeAgo(t.created_at) + '</td></tr>').join('') + '</table>';
    }

    // Tasks
    const tb = document.getElementById('tasks-body');
    if (tasks.length === 0) {
      tb.innerHTML = '<div class="empty-state">No tasks yet</div>';
    } else {
      tb.innerHTML = '<table><tr><th>#</th><th>Status</th><th>Agent</th><th>Prompt</th><th>Tags</th><th>Duration</th><th>When</th></tr>' +
        tasks.map(t => '<tr class="clickable" onclick="showTaskDetail(' + t.id + ')">' +
          '<td>' + esc(t.id) + '</td>' +
          '<td>' + badge(t.status) + '</td>' +
          '<td>' + esc(t.agent) + '</td>' +
          '<td class="prompt-cell">' + esc(t.prompt) + '</td>' +
          '<td>' + tags(t) + '</td>' +
          '<td>' + fmtDuration(t.duration_seconds) + '</td>' +
          '<td>' + timeAgo(t.completed_at || t.created_at) + '</td></tr>').join('') + '</table>';
    }

    // Chains
    const cb = document.getElementById('chains-body');
    if (chains.length === 0) {
      cb.innerHTML = '<div class="empty-state">No chains saved</div>';
    } else {
      cb.innerHTML = '<table><tr><th>Name</th><th>Status</th><th>Progress</th><th>Created</th></tr>' +
        chains.map(c => '<tr>' +
          '<td>' + esc(c.name) + '</td>' +
          '<td>' + badge(c.status) + '</td>' +
          '<td><div style="display:flex;align-items:center;gap:0.5rem"><div style="flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden"><div style="width:' + (c.total_steps > 0 ? Math.round(c.current_step / c.total_steps * 100) : 0) + '%;height:100%;background:var(--accent);border-radius:3px"></div></div><span style="font-size:0.75rem;color:var(--dim)">' + esc(c.current_step) + '/' + esc(c.total_steps) + '</span></div></td>' +
          '<td>' + timeAgo(c.created_at) + '</td></tr>').join('') + '</table>';
    }

    // Workers
    const wb = document.getElementById('workers-body');
    if (workers.length === 0) {
      wb.innerHTML = '<div class="empty-state">No workers connected &#x1F4E1;</div>';
    } else {
      wb.innerHTML = '<div class="workers-grid">' + workers.map(w =>
        '<div class="worker-card">' +
        '<div class="wname">&#x1F5A5; ' + esc(w.worker_id || w.id || 'unknown') + '</div>' +
        '<div class="wstatus">' + badge(w.status || 'running') + '</div>' +
        (w.task_id ? '<div class="wtask">Task #' + esc(w.task_id) + ': ' + esc(w.prompt || '') + '</div>' : '<div class="wtask" style="color:var(--dim)">Idle</div>') +
        '</div>').join('') + '</div>';
    }

    const hb = document.getElementById('health-badge');
    hb.textContent = anyFailed ? 'PARTIAL' : 'LIVE';
    hb.className = 'badge ' + (anyFailed ? 'badge-warn' : 'badge-live');
  } catch (e) {
    const hb = document.getElementById('health-badge');
    hb.textContent = 'ERROR';
    hb.className = 'badge badge-err';
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

"""Tests for the Task Management API endpoints (OpenClaw integration)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config.settings import settings
from app.core.db import init_db
from app.core.models import Base
from app.web.dashboard import create_dashboard_app


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    """Reset the DB before each test so tasks don't leak."""
    from app.core.db import engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture
async def api_client():
    """Create test client with no auth token."""
    original_token = settings.dashboard_token
    settings.dashboard_token = ""
    app = create_dashboard_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    settings.dashboard_token = original_token


@pytest_asyncio.fixture
async def auth_client():
    """Create test client with auth token."""
    original_token = settings.dashboard_token
    settings.dashboard_token = "test-secret"
    app = create_dashboard_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    settings.dashboard_token = original_token


# ── POST /api/tasks ─────────────────────────────────────────────────


class TestCreateTask:
    @pytest.mark.asyncio
    async def test_create_task_minimal(self, api_client):
        resp = await api_client.post("/api/tasks", json={"prompt": "fix the bug"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "pending"
        assert data["prompt"] == "fix the bug"
        assert data["agent"] == settings.default_agent
        assert "id" in data

    @pytest.mark.asyncio
    async def test_create_task_with_all_fields(self, api_client):
        resp = await api_client.post("/api/tasks", json={
            "prompt": "refactor auth module",
            "project_dir": "~/repos/myapp",
            "agent": "claude",
            "model": "sonnet",
            "assigned_to": "worker1",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["agent"] == "claude"
        assert data["model"] == "anthropic/claude-sonnet-4"
        assert data["assigned_to"] == "worker1"

    @pytest.mark.asyncio
    async def test_create_task_empty_prompt(self, api_client):
        resp = await api_client.post("/api/tasks", json={"prompt": ""})
        assert resp.status_code == 400
        assert "required" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_task_missing_prompt(self, api_client):
        resp = await api_client.post("/api/tasks", json={"agent": "claude"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_task_whitespace_prompt(self, api_client):
        resp = await api_client.post("/api/tasks", json={"prompt": "   "})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_task_prompt_too_long(self, api_client):
        long = "x" * (settings.max_prompt_len + 1)
        resp = await api_client.post("/api/tasks", json={"prompt": long})
        assert resp.status_code == 400
        assert "exceeds" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_task_invalid_json(self, api_client):
        resp = await api_client.post(
            "/api/tasks",
            content="not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_task_queue_full(self, api_client):
        for i in range(settings.max_queue_size):
            resp = await api_client.post("/api/tasks", json={"prompt": f"fill {i}"})
            assert resp.status_code == 201, f"task {i} failed: {resp.json()}"
        resp = await api_client.post("/api/tasks", json={"prompt": "overflow"})
        assert resp.status_code == 409
        assert "full" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_create_task_callback_url_echoed(self, api_client):
        resp = await api_client.post("/api/tasks", json={
            "prompt": "do it",
            "callback_url": "http://localhost:18789/callback",
        })
        assert resp.status_code == 201
        assert resp.json()["callback_url"] == "http://localhost:18789/callback"

    @pytest.mark.asyncio
    async def test_create_task_requires_auth(self, auth_client):
        resp = await auth_client.post("/api/tasks", json={"prompt": "test"})
        assert resp.status_code == 401

        resp = await auth_client.post(
            "/api/tasks",
            json={"prompt": "test"},
            headers={"Authorization": "Bearer test-secret"},
        )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_create_task_non_string_prompt(self, api_client):
        resp = await api_client.post("/api/tasks", json={"prompt": 12345})
        assert resp.status_code == 400


# ── POST /api/tasks/{id}/cancel ─────────────────────────────────────


class TestCancelTaskApi:
    @pytest.mark.asyncio
    async def test_cancel_pending_task(self, api_client):
        create = await api_client.post("/api/tasks", json={"prompt": "cancel me"})
        task_id = create.json()["id"]
        resp = await api_client.post(f"/api/tasks/{task_id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_task(self, api_client):
        resp = await api_client.post("/api/tasks/99999/cancel")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_cancel_already_cancelled(self, api_client):
        create = await api_client.post("/api/tasks", json={"prompt": "cancel twice"})
        task_id = create.json()["id"]
        await api_client.post(f"/api/tasks/{task_id}/cancel")
        resp = await api_client.post(f"/api/tasks/{task_id}/cancel")
        assert resp.status_code == 404


# ── POST /api/tasks/{id}/retry ──────────────────────────────────────


class TestRetryTaskApi:
    @pytest.mark.asyncio
    async def test_retry_cancelled_task(self, api_client):
        create = await api_client.post("/api/tasks", json={"prompt": "retry me"})
        task_id = create.json()["id"]
        await api_client.post(f"/api/tasks/{task_id}/cancel")
        resp = await api_client.post(f"/api/tasks/{task_id}/retry")
        assert resp.status_code == 201
        assert resp.json()["status"] == "pending"
        assert resp.json()["id"] != task_id  # new task

    @pytest.mark.asyncio
    async def test_retry_pending_task_fails(self, api_client):
        create = await api_client.post("/api/tasks", json={"prompt": "still pending"})
        task_id = create.json()["id"]
        resp = await api_client.post(f"/api/tasks/{task_id}/retry")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_retry_nonexistent(self, api_client):
        resp = await api_client.post("/api/tasks/99999/retry")
        assert resp.status_code == 404


# ── GET /api/tasks/search ───────────────────────────────────────────


class TestSearchTasksApi:
    @pytest.mark.asyncio
    async def test_search_finds_task(self, api_client):
        await api_client.post("/api/tasks", json={"prompt": "fix login bug"})
        resp = await api_client.get("/api/tasks/search", params={"q": "login"})
        assert resp.status_code == 200
        assert len(resp.json()) >= 1
        assert "login" in resp.json()[0]["prompt"].lower()

    @pytest.mark.asyncio
    async def test_search_no_results(self, api_client):
        resp = await api_client.get("/api/tasks/search", params={"q": "zzz_nonexistent_zzz"})
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_search_requires_query(self, api_client):
        resp = await api_client.get("/api/tasks/search")
        assert resp.status_code == 422  # FastAPI validation error


# ── Integration: full lifecycle via API ─────────────────────────────


class TestTaskLifecycleApi:
    @pytest.mark.asyncio
    async def test_create_check_cancel_retry(self, api_client):
        # Create
        resp = await api_client.post("/api/tasks", json={
            "prompt": "lifecycle test",
            "agent": "claude",
        })
        assert resp.status_code == 201
        task_id = resp.json()["id"]

        # Check detail
        resp = await api_client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"

        # Appears in queue
        resp = await api_client.get("/api/queue")
        assert any(t["id"] == task_id for t in resp.json())

        # Cancel
        resp = await api_client.post(f"/api/tasks/{task_id}/cancel")
        assert resp.status_code == 200

        # No longer in queue
        resp = await api_client.get("/api/queue")
        assert not any(t["id"] == task_id for t in resp.json())

        # Retry creates new task
        resp = await api_client.post(f"/api/tasks/{task_id}/retry")
        assert resp.status_code == 201
        new_id = resp.json()["id"]
        assert new_id != task_id

        # New task in queue
        resp = await api_client.get("/api/queue")
        assert any(t["id"] == new_id for t in resp.json())

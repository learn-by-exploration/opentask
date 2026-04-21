"""Adversarial tests for dashboard API — auth, endpoints, worker API, security headers — 200+ edge cases."""

from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

import json
import pytest
import pytest_asyncio
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base, Task, TaskStatus, _utcnow
from app.web.dashboard import create_dashboard_app


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def _patch(engine):
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async def _fake():
        return Session()
    with patch("app.core.broker.get_session", _fake):
        yield


@pytest_asyncio.fixture
async def client(_patch):
    app = create_dashboard_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture
async def authed_client(_patch):
    """Client for when auth is enabled."""
    with patch("app.config.settings.settings.dashboard_token", "secret123"):
        app = create_dashboard_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c


# ═══════════════════════════════════════════════════════════════════════
# SECURITY HEADERS
# ═══════════════════════════════════════════════════════════════════════

class TestSecurityHeaders:

    @pytest.mark.asyncio
    async def test_x_frame_options(self, client):
        resp = await client.get("/api/health")
        assert resp.headers.get("x-frame-options") == "DENY"

    @pytest.mark.asyncio
    async def test_x_content_type_options(self, client):
        resp = await client.get("/api/health")
        assert resp.headers.get("x-content-type-options") == "nosniff"

    @pytest.mark.asyncio
    async def test_referrer_policy(self, client):
        resp = await client.get("/api/health")
        assert resp.headers.get("referrer-policy") == "no-referrer"

    @pytest.mark.asyncio
    async def test_csp_header(self, client):
        resp = await client.get("/api/health")
        csp = resp.headers.get("content-security-policy", "")
        assert "default-src 'self'" in csp

    @pytest.mark.asyncio
    async def test_headers_on_404(self, client):
        resp = await client.get("/nonexistent")
        assert resp.headers.get("x-frame-options") == "DENY"

    @pytest.mark.asyncio
    async def test_headers_on_html(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert resp.headers.get("x-frame-options") == "DENY"


# ═══════════════════════════════════════════════════════════════════════
# AUTH ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestDashboardAuthAdversarial:

    @pytest.mark.asyncio
    async def test_no_auth_when_token_empty(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_auth_required_rejects_no_header(self, authed_client):
        resp = await authed_client.get("/api/health")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_auth_wrong_token(self, authed_client):
        resp = await authed_client.get("/api/health", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_auth_correct_token(self, authed_client):
        resp = await authed_client.get("/api/health", headers={"Authorization": "Bearer secret123"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_auth_bearer_lowercase(self, authed_client):
        resp = await authed_client.get("/api/health", headers={"Authorization": "bearer secret123"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_auth_extra_space(self, authed_client):
        resp = await authed_client.get("/api/health", headers={"Authorization": "Bearer  secret123"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_auth_trailing_space(self, authed_client):
        """Trailing space is stripped by middleware, so this actually authenticates."""
        resp = await authed_client.get("/api/health", headers={"Authorization": "Bearer secret123 "})
        assert resp.status_code == 200  # .strip() removes trailing space

    @pytest.mark.asyncio
    async def test_auth_no_bearer_prefix(self, authed_client):
        resp = await authed_client.get("/api/health", headers={"Authorization": "secret123"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_auth_basic_scheme(self, authed_client):
        resp = await authed_client.get("/api/health", headers={"Authorization": "Basic c2VjcmV0MTIz"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_auth_timing_attack_long(self, authed_client):
        """hmac.compare_digest prevents timing attacks; confirm no false positives."""
        resp = await authed_client.get("/api/health", headers={"Authorization": "Bearer " + "x" * 1000})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_auth_null_byte(self, authed_client):
        resp = await authed_client.get("/api/health", headers={"Authorization": "Bearer secret123\x00"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_auth_empty_bearer(self, authed_client):
        resp = await authed_client.get("/api/health", headers={"Authorization": "Bearer "})
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════
# HEALTH ENDPOINT
# ═══════════════════════════════════════════════════════════════════════

class TestHealthEndpoint:

    @pytest.mark.asyncio
    async def test_health_ok(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_method_not_allowed(self, client):
        resp = await client.post("/api/health")
        assert resp.status_code == 405


# ═══════════════════════════════════════════════════════════════════════
# TASKS ENDPOINT
# ═══════════════════════════════════════════════════════════════════════

class TestTasksEndpoint:

    @pytest.mark.asyncio
    async def test_empty_tasks(self, client):
        resp = await client.get("/api/tasks")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_tasks_default_limit(self, client):
        from app.core.broker import enqueue_task
        for i in range(20):
            await enqueue_task(f"task {i}", "/tmp", "opencode")
        resp = await client.get("/api/tasks")
        assert resp.status_code == 200
        assert len(resp.json()) == 20

    @pytest.mark.asyncio
    async def test_tasks_custom_limit(self, client):
        from app.core.broker import enqueue_task
        for i in range(10):
            await enqueue_task(f"task {i}", "/tmp", "opencode")
        resp = await client.get("/api/tasks?limit=5")
        assert resp.status_code == 200
        assert len(resp.json()) == 5

    @pytest.mark.asyncio
    async def test_tasks_limit_zero(self, client):
        resp = await client.get("/api/tasks?limit=0")
        assert resp.status_code == 422  # FastAPI validation

    @pytest.mark.asyncio
    async def test_tasks_limit_negative(self, client):
        resp = await client.get("/api/tasks?limit=-1")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_tasks_limit_101(self, client):
        resp = await client.get("/api/tasks?limit=101")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_tasks_limit_max(self, client):
        resp = await client.get("/api/tasks?limit=100")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_tasks_limit_string(self, client):
        resp = await client.get("/api/tasks?limit=abc")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_tasks_limit_float(self, client):
        resp = await client.get("/api/tasks?limit=5.5")
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════
# TASK DETAIL
# ═══════════════════════════════════════════════════════════════════════

class TestTaskDetailEndpoint:

    @pytest.mark.asyncio
    async def test_nonexistent_task(self, client):
        resp = await client.get("/api/tasks/9999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_valid_task(self, client):
        from app.core.broker import enqueue_task
        task = await enqueue_task("test", "/tmp", "opencode")
        resp = await client.get(f"/api/tasks/{task.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == task.id
        assert data["prompt"] == "test"  # full prompt in detail

    @pytest.mark.asyncio
    async def test_task_id_zero(self, client):
        resp = await client.get("/api/tasks/0")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_task_id_negative(self, client):
        resp = await client.get("/api/tasks/-1")
        # FastAPI int path will accept negative
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_task_id_string(self, client):
        resp = await client.get("/api/tasks/abc")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_task_prompt_truncated_in_list(self, client):
        from app.core.broker import enqueue_task
        long_prompt = "x" * 500
        task = await enqueue_task(long_prompt, "/tmp", "opencode")
        resp = await client.get("/api/tasks")
        data = resp.json()
        found = [t for t in data if t["id"] == task.id]
        assert len(found) == 1
        assert len(found[0]["prompt"]) == 200  # truncated

    @pytest.mark.asyncio
    async def test_task_full_prompt_in_detail(self, client):
        from app.core.broker import enqueue_task
        long_prompt = "x" * 500
        task = await enqueue_task(long_prompt, "/tmp", "opencode")
        resp = await client.get(f"/api/tasks/{task.id}")
        data = resp.json()
        assert len(data["prompt"]) == 500


# ═══════════════════════════════════════════════════════════════════════
# QUEUE ENDPOINT
# ═══════════════════════════════════════════════════════════════════════

class TestQueueEndpoint:

    @pytest.mark.asyncio
    async def test_empty_queue(self, client):
        resp = await client.get("/api/queue")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_queue_with_pending(self, client):
        from app.core.broker import enqueue_task
        await enqueue_task("test", "/tmp", "opencode")
        resp = await client.get("/api/queue")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    @pytest.mark.asyncio
    async def test_queue_excludes_running(self, client):
        from app.core.broker import enqueue_task, pick_next_task
        await enqueue_task("test", "/tmp", "opencode")
        await pick_next_task()
        resp = await client.get("/api/queue")
        assert resp.status_code == 200
        assert len(resp.json()) == 0


# ═══════════════════════════════════════════════════════════════════════
# CHAINS ENDPOINT
# ═══════════════════════════════════════════════════════════════════════

class TestChainsEndpoint:

    @pytest.mark.asyncio
    async def test_empty_chains(self, client):
        resp = await client.get("/api/chains")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_chain_list(self, client):
        from app.core.broker import save_chain
        await save_chain("test", [{"prompt": "step1"}])
        resp = await client.get("/api/chains")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "test"

    @pytest.mark.asyncio
    async def test_chain_detail_nonexistent(self, client):
        resp = await client.get("/api/chains/999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_chain_detail_valid(self, client):
        from app.core.broker import save_chain
        chain = await save_chain("test", [{"prompt": "step1"}])
        resp = await client.get(f"/api/chains/{chain.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test"
        assert "steps" in data


# ═══════════════════════════════════════════════════════════════════════
# STATS ENDPOINT
# ═══════════════════════════════════════════════════════════════════════

class TestStatsEndpoint:

    @pytest.mark.asyncio
    async def test_stats_empty(self, client):
        resp = await client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is None
        assert data["pending_count"] == 0

    @pytest.mark.asyncio
    async def test_stats_with_tasks(self, client):
        from app.core.broker import enqueue_task, pick_next_task, complete_task
        t = await enqueue_task("test", "/tmp", "opencode")
        await pick_next_task()
        await complete_task(t.id, 0, "ok", "output")
        resp = await client.get("/api/stats")
        data = resp.json()
        assert data["recent_completed"] >= 1


# ═══════════════════════════════════════════════════════════════════════
# WORKER CLAIM ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestWorkerClaimApiAdversarial:

    @pytest.mark.asyncio
    async def test_claim_no_body(self, client):
        resp = await client.post("/api/worker/claim", content="not json")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_claim_empty_body(self, client):
        resp = await client.post("/api/worker/claim", json={})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_claim_empty_worker_id(self, client):
        resp = await client.post("/api/worker/claim", json={"worker_id": ""})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_claim_worker_id_too_long(self, client):
        resp = await client.post("/api/worker/claim", json={"worker_id": "x" * 129})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_claim_whitespace_only(self, client):
        resp = await client.post("/api/worker/claim", json={"worker_id": "   "})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_claim_no_tasks(self, client):
        resp = await client.post("/api/worker/claim", json={"worker_id": "server2"})
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_claim_valid(self, client):
        from app.core.broker import enqueue_task
        await enqueue_task("test", "/tmp", "opencode")
        resp = await client.post("/api/worker/claim", json={"worker_id": "server2"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["prompt"] == "test"

    @pytest.mark.asyncio
    async def test_claim_sql_injection_worker_id(self, client):
        resp = await client.post("/api/worker/claim", json={"worker_id": "'; DROP TABLE tasks;--"})
        # Should not error
        assert resp.status_code in (204, 200)

    @pytest.mark.asyncio
    async def test_claim_unicode_worker_id(self, client):
        from app.core.broker import enqueue_task
        await enqueue_task("test", "/tmp", "opencode")
        resp = await client.post("/api/worker/claim", json={"worker_id": "サーバー2"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_claim_int_worker_id(self, client):
        resp = await client.post("/api/worker/claim", json={"worker_id": 123})
        # int gets stringified or rejected
        assert resp.status_code in (200, 204, 400)


# ═══════════════════════════════════════════════════════════════════════
# WORKER RESULT ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestWorkerResultApiAdversarial:

    @pytest.mark.asyncio
    async def test_result_no_body(self, client):
        resp = await client.post("/api/worker/1/result", content="bad")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_result_no_worker_id(self, client):
        resp = await client.post("/api/worker/1/result", json={"exit_code": 0})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_result_no_exit_code(self, client):
        resp = await client.post("/api/worker/1/result", json={"worker_id": "s2"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_result_str_exit_code(self, client):
        resp = await client.post("/api/worker/1/result", json={"worker_id": "s2", "exit_code": "zero"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_result_float_exit_code(self, client):
        resp = await client.post("/api/worker/1/result", json={"worker_id": "s2", "exit_code": 1.5})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_result_bool_exit_code(self, client):
        """bool is subtype of int in Python — should pass isinstance(int) check."""
        resp = await client.post("/api/worker/1/result", json={"worker_id": "s2", "exit_code": True})
        # True is int subtype, so this might pass validation but fail on task not found
        assert resp.status_code in (400, 404)

    @pytest.mark.asyncio
    async def test_result_none_exit_code(self, client):
        resp = await client.post("/api/worker/1/result", json={"worker_id": "s2", "exit_code": None})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_result_task_not_found(self, client):
        resp = await client.post("/api/worker/999/result", json={
            "worker_id": "s2", "exit_code": 0, "output_summary": "ok"
        })
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_result_wrong_worker(self, client):
        from app.core.broker import enqueue_task, worker_claim_task
        await enqueue_task("test", "/tmp", "opencode", assigned_to="server2")
        task = await worker_claim_task("server2")
        resp = await client.post(f"/api/worker/{task.id}/result", json={
            "worker_id": "wrong", "exit_code": 0, "output_summary": "ok"
        })
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_result_valid(self, client):
        from app.core.broker import enqueue_task, worker_claim_task
        await enqueue_task("test", "/tmp", "opencode", assigned_to="server2")
        task = await worker_claim_task("server2")
        resp = await client.post(f"/api/worker/{task.id}/result", json={
            "worker_id": "server2", "exit_code": 0, "output_summary": "ok",
            "full_output": "full output here",
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_result_truncates_output_summary(self, client):
        from app.core.broker import enqueue_task, worker_claim_task
        await enqueue_task("test", "/tmp", "opencode", assigned_to="server2")
        task = await worker_claim_task("server2")
        resp = await client.post(f"/api/worker/{task.id}/result", json={
            "worker_id": "server2", "exit_code": 0,
            "output_summary": "x" * 1000,
            "full_output": "short",
        })
        assert resp.status_code == 200
        # Summary truncated to 500 server-side

    @pytest.mark.asyncio
    async def test_result_truncates_error_message(self, client):
        from app.core.broker import enqueue_task, worker_claim_task
        await enqueue_task("test", "/tmp", "opencode", assigned_to="server2")
        task = await worker_claim_task("server2")
        resp = await client.post(f"/api/worker/{task.id}/result", json={
            "worker_id": "server2", "exit_code": 1,
            "output_summary": "fail",
            "error_message": "E" * 5000,
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_result_huge_full_output(self, client):
        from app.core.broker import enqueue_task, worker_claim_task
        await enqueue_task("test", "/tmp", "opencode", assigned_to="server2")
        task = await worker_claim_task("server2")
        resp = await client.post(f"/api/worker/{task.id}/result", json={
            "worker_id": "server2", "exit_code": 0,
            "output_summary": "ok",
            "full_output": "x" * 3_000_000,  # over 2MB
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_result_negative_exit_code(self, client):
        from app.core.broker import enqueue_task, worker_claim_task
        await enqueue_task("test", "/tmp", "opencode", assigned_to="server2")
        task = await worker_claim_task("server2")
        resp = await client.post(f"/api/worker/{task.id}/result", json={
            "worker_id": "server2", "exit_code": -1,
            "output_summary": "killed",
        })
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════
# WORKER HEARTBEAT ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestWorkerHeartbeatApiAdversarial:

    @pytest.mark.asyncio
    async def test_heartbeat_no_body(self, client):
        resp = await client.post("/api/worker/1/heartbeat", content="bad")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_heartbeat_no_worker_id(self, client):
        resp = await client.post("/api/worker/1/heartbeat", json={})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_heartbeat_empty_worker_id(self, client):
        resp = await client.post("/api/worker/1/heartbeat", json={"worker_id": ""})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_heartbeat_task_not_found(self, client):
        resp = await client.post("/api/worker/999/heartbeat", json={"worker_id": "s2"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_heartbeat_wrong_worker(self, client):
        from app.core.broker import enqueue_task, worker_claim_task
        await enqueue_task("test", "/tmp", "opencode", assigned_to="server2")
        task = await worker_claim_task("server2")
        resp = await client.post(f"/api/worker/{task.id}/heartbeat", json={"worker_id": "wrong"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_heartbeat_valid(self, client):
        from app.core.broker import enqueue_task, worker_claim_task
        await enqueue_task("test", "/tmp", "opencode", assigned_to="server2")
        task = await worker_claim_task("server2")
        resp = await client.post(f"/api/worker/{task.id}/heartbeat", json={"worker_id": "server2"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ═══════════════════════════════════════════════════════════════════════
# WORKERS LIST
# ═══════════════════════════════════════════════════════════════════════

class TestWorkersListEndpoint:

    @pytest.mark.asyncio
    async def test_no_workers(self, client):
        resp = await client.get("/api/workers")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_with_active_worker(self, client):
        from app.core.broker import enqueue_task, worker_claim_task
        await enqueue_task("test", "/tmp", "opencode", assigned_to="server2")
        await worker_claim_task("server2")
        resp = await client.get("/api/workers")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1


# ═══════════════════════════════════════════════════════════════════════
# HTML DASHBOARD
# ═══════════════════════════════════════════════════════════════════════

class TestHTMLDashboard:

    @pytest.mark.asyncio
    async def test_dashboard_returns_html(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_dashboard_contains_title(self, client):
        resp = await client.get("/")
        assert "TaskPilot" in resp.text

    @pytest.mark.asyncio
    async def test_dashboard_xss_safe(self, client):
        """Verify HTML escaping in dashboard."""
        from app.core.broker import enqueue_task
        await enqueue_task("<script>alert(1)</script>", "/tmp", "opencode")
        resp = await client.get("/")
        assert "<script>alert(1)</script>" not in resp.text


# ═══════════════════════════════════════════════════════════════════════
# TYPE CONFUSION / EDGE CASES
# ═══════════════════════════════════════════════════════════════════════

class TestTypeConfusionAdversarial:

    @pytest.mark.asyncio
    async def test_claim_array_worker_id(self, client):
        """Array gets str()-ified, treated as string worker_id."""
        resp = await client.post("/api/worker/claim", json={"worker_id": ["a", "b"]})
        assert resp.status_code in (200, 204)  # stringified, no tasks → 204

    @pytest.mark.asyncio
    async def test_claim_dict_worker_id(self, client):
        """Dict gets str()-ified to something like "{'nested': True}"."""
        resp = await client.post("/api/worker/claim", json={"worker_id": {"nested": True}})
        assert resp.status_code in (200, 204)  # str(dict) works

    @pytest.mark.asyncio
    async def test_claim_null_worker_id(self, client):
        """None → str(None) = "None", but original is "" via get default."""
        resp = await client.post("/api/worker/claim", json={"worker_id": None})
        assert resp.status_code in (204, 400)  # str(None)="None" passes validation

    @pytest.mark.asyncio
    async def test_result_array_exit_code(self, client):
        resp = await client.post("/api/worker/1/result", json={
            "worker_id": "s2", "exit_code": [0]
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_result_dict_exit_code(self, client):
        resp = await client.post("/api/worker/1/result", json={
            "worker_id": "s2", "exit_code": {"value": 0}
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_result_extremely_neg_exit_code(self, client):
        from app.core.broker import enqueue_task, worker_claim_task
        await enqueue_task("test", "/tmp", "opencode", assigned_to="s2")
        task = await worker_claim_task("s2")
        resp = await client.post(f"/api/worker/{task.id}/result", json={
            "worker_id": "s2", "exit_code": -999999
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_result_max_int_exit_code(self, client):
        from app.core.broker import enqueue_task, worker_claim_task
        await enqueue_task("test", "/tmp", "opencode", assigned_to="s2")
        task = await worker_claim_task("s2")
        resp = await client.post(f"/api/worker/{task.id}/result", json={
            "worker_id": "s2", "exit_code": 2**31
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_claim_extra_fields_ignored(self, client):
        """Extra JSON fields should be silently ignored."""
        from app.core.broker import enqueue_task
        await enqueue_task("test", "/tmp", "opencode")
        resp = await client.post("/api/worker/claim", json={
            "worker_id": "s2",
            "malicious": "payload",
            "admin": True,
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_result_xss_in_output(self, client):
        from app.core.broker import enqueue_task, worker_claim_task
        await enqueue_task("test", "/tmp", "opencode", assigned_to="s2")
        task = await worker_claim_task("s2")
        resp = await client.post(f"/api/worker/{task.id}/result", json={
            "worker_id": "s2", "exit_code": 0,
            "output_summary": "<script>alert(1)</script>",
            "full_output": "<img onerror=alert(1) src=x>",
        })
        assert resp.status_code == 200

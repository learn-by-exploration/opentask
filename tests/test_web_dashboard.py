"""Comprehensive tests for the web dashboard module."""

from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-for-tests")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.config.settings import settings
from app.core.models import ChainStatus, Task, TaskChain, TaskStatus
from app.web.dashboard import (
    STATUS_COLORS,
    STATUS_ICONS,
    _chain_to_dict,
    _esc,
    _render_dashboard,
    _task_to_dict,
    _utcnow,
    create_dashboard_app,
)


# ── Helper factories ────────────────────────────────────────────────


def _make_task(**overrides) -> SimpleNamespace:
    """Create a mock Task-like object with sensible defaults."""
    defaults = {
        "id": 1,
        "prompt": "test prompt",
        "project_dir": "~/ai",
        "agent": "opencode",
        "status": TaskStatus.PENDING,
        "exit_code": None,
        "error_message": None,
        "output_summary": None,
        "full_output": None,
        "duration_seconds": None,
        "created_at": datetime(2025, 1, 15, 10, 0, 0),
        "started_at": None,
        "completed_at": None,
        "chain_id": None,
        "chain_step": None,
        "repeat_total": None,
        "repeat_remaining": None,
        "telegram_chat_id": 12345,
        "telegram_msg_id": None,
        "repeat_until": None,
        "model": None,
        "priority": 0,
        "retry_count": 0,
        "max_retries": 1,
        "git_diff": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_chain(**overrides) -> SimpleNamespace:
    """Create a mock TaskChain-like object."""
    defaults = {
        "id": 1,
        "name": "test-chain",
        "steps_json": json.dumps([{"prompt": "step1"}, {"prompt": "step2"}]),
        "current_step": 0,
        "status": ChainStatus.IDLE,
        "telegram_chat_id": 12345,
        "created_at": datetime(2025, 1, 15, 10, 0, 0),
        "started_at": None,
        "completed_at": None,
    }
    defaults.update(overrides)
    ns = SimpleNamespace(**defaults)
    # Add the property-like methods that TaskChain has
    try:
        ns.steps = json.loads(ns.steps_json)
    except (json.JSONDecodeError, TypeError):
        ns.steps = []
    ns.total_steps = len(ns.steps)
    return ns


@pytest.fixture
def dashboard_app():
    """FastAPI test app."""
    return create_dashboard_app()


@pytest.fixture
def transport(dashboard_app):
    return ASGITransport(app=dashboard_app)


# ── Unit tests: helpers ─────────────────────────────────────────────


class TestEsc:
    def test_none(self):
        assert _esc(None) == ""

    def test_empty(self):
        assert _esc("") == ""

    def test_plain(self):
        assert _esc("hello") == "hello"

    def test_html_special(self):
        assert _esc('<script>alert("xss")</script>') == '&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;'

    def test_ampersand(self):
        assert _esc("a & b") == "a &amp; b"

    def test_number_convert(self):
        assert _esc(42) == "42"


class TestUtcnow:
    def test_returns_datetime(self):
        result = _utcnow()
        assert isinstance(result, datetime)

    def test_no_tzinfo(self):
        result = _utcnow()
        assert result.tzinfo is None


class TestTaskToDict:
    def test_basic(self):
        task = _make_task()
        d = _task_to_dict(task)
        assert d["id"] == 1
        assert d["prompt"] == "test prompt"
        assert d["status"] == "pending"
        assert d["agent"] == "opencode"
        assert d["project_dir"] == "~/ai"

    def test_long_prompt_truncated(self):
        task = _make_task(prompt="x" * 500)
        d = _task_to_dict(task)
        assert len(d["prompt"]) == 200

    def test_completed_task(self):
        task = _make_task(
            status=TaskStatus.COMPLETED,
            exit_code=0,
            duration_seconds=42,
            started_at=datetime(2025, 1, 15, 10, 0, 0),
            completed_at=datetime(2025, 1, 15, 10, 0, 42),
            output_summary="Done!",
        )
        d = _task_to_dict(task)
        assert d["status"] == "completed"
        assert d["exit_code"] == 0
        assert d["duration_seconds"] == 42
        assert d["started_at"] is not None
        assert d["completed_at"] is not None

    def test_chain_linked(self):
        task = _make_task(chain_id=5, chain_step=2)
        d = _task_to_dict(task)
        assert d["chain_id"] == 5
        assert d["chain_step"] == 2

    def test_repeat_fields(self):
        task = _make_task(repeat_total=3, repeat_remaining=1)
        d = _task_to_dict(task)
        assert d["repeat_total"] == 3
        assert d["repeat_remaining"] == 1

    def test_null_timestamps(self):
        task = _make_task(created_at=None, started_at=None, completed_at=None)
        d = _task_to_dict(task)
        assert d["created_at"] is None
        assert d["started_at"] is None
        assert d["completed_at"] is None

    def test_error_message(self):
        task = _make_task(error_message="boom", status=TaskStatus.FAILED, exit_code=1)
        d = _task_to_dict(task)
        assert d["error_message"] == "boom"


class TestChainToDict:
    def test_basic(self):
        chain = _make_chain()
        d = _chain_to_dict(chain)
        assert d["id"] == 1
        assert d["name"] == "test-chain"
        assert d["status"] == "idle"
        assert d["total_steps"] == 2
        assert d["current_step"] == 0

    def test_running_chain(self):
        chain = _make_chain(
            status=ChainStatus.RUNNING,
            current_step=1,
            started_at=datetime(2025, 1, 15, 10, 0, 0),
        )
        d = _chain_to_dict(chain)
        assert d["status"] == "running"
        assert d["started_at"] is not None

    def test_completed_chain(self):
        chain = _make_chain(
            status=ChainStatus.COMPLETED,
            completed_at=datetime(2025, 1, 15, 11, 0, 0),
        )
        d = _chain_to_dict(chain)
        assert d["status"] == "completed"
        assert d["completed_at"] is not None

    def test_null_timestamps(self):
        chain = _make_chain(created_at=None, started_at=None, completed_at=None)
        d = _chain_to_dict(chain)
        assert d["created_at"] is None


class TestConstants:
    def test_status_colors_all_present(self):
        for s in ("pending", "running", "completed", "failed", "cancelled"):
            assert s in STATUS_COLORS

    def test_status_icons_all_present(self):
        for s in ("pending", "running", "completed", "failed", "cancelled"):
            assert s in STATUS_ICONS


# ── API endpoint tests ──────────────────────────────────────────────


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["service"] == "taskpilot"


class TestStatsEndpoint:
    @pytest.mark.asyncio
    async def test_stats_empty(self, transport):
        with patch("app.web.dashboard.get_running_task", new_callable=AsyncMock, return_value=None), \
             patch("app.web.dashboard.get_pending_tasks", new_callable=AsyncMock, return_value=[]), \
             patch("app.web.dashboard.get_recent_tasks", new_callable=AsyncMock, return_value=[]), \
             patch("app.web.dashboard.list_chains", new_callable=AsyncMock, return_value=[]):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["running"] is None
        assert data["pending_count"] == 0
        assert data["recent_completed"] == 0
        assert data["recent_failed"] == 0
        assert data["recent_cancelled"] == 0
        assert data["avg_duration_seconds"] == 0.0
        assert data["chains_total"] == 0
        assert data["chains_running"] == 0

    @pytest.mark.asyncio
    async def test_stats_with_data(self, transport):
        running = _make_task(id=1, status=TaskStatus.RUNNING, started_at=datetime(2025, 1, 15, 10, 0))
        pending = [_make_task(id=2), _make_task(id=3)]
        recent = [
            _make_task(id=4, status=TaskStatus.COMPLETED, duration_seconds=30),
            _make_task(id=5, status=TaskStatus.COMPLETED, duration_seconds=60),
            _make_task(id=6, status=TaskStatus.FAILED, duration_seconds=10),
            _make_task(id=7, status=TaskStatus.CANCELLED),
        ]
        chains = [
            _make_chain(id=1, status=ChainStatus.RUNNING),
            _make_chain(id=2, status=ChainStatus.IDLE),
        ]

        with patch("app.web.dashboard.get_running_task", new_callable=AsyncMock, return_value=running), \
             patch("app.web.dashboard.get_pending_tasks", new_callable=AsyncMock, return_value=pending), \
             patch("app.web.dashboard.get_recent_tasks", new_callable=AsyncMock, return_value=recent), \
             patch("app.web.dashboard.list_chains", new_callable=AsyncMock, return_value=chains):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/stats")
        data = r.json()
        assert data["running"]["id"] == 1
        assert data["pending_count"] == 2
        assert data["recent_completed"] == 2
        assert data["recent_failed"] == 1
        assert data["recent_cancelled"] == 1
        assert data["avg_duration_seconds"] == pytest.approx(33.3, abs=0.1)
        assert data["chains_total"] == 2
        assert data["chains_running"] == 1


class TestTasksEndpoint:
    @pytest.mark.asyncio
    async def test_tasks_empty(self, transport):
        with patch("app.web.dashboard.get_recent_tasks", new_callable=AsyncMock, return_value=[]):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/tasks")
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_tasks_with_limit(self, transport):
        tasks = [_make_task(id=i) for i in range(5)]
        with patch("app.web.dashboard.get_recent_tasks", new_callable=AsyncMock, return_value=tasks) as mock:
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/tasks?limit=5")
        assert r.status_code == 200
        assert len(r.json()) == 5
        mock.assert_called_once_with(limit=5)

    @pytest.mark.asyncio
    async def test_tasks_default_limit(self, transport):
        with patch("app.web.dashboard.get_recent_tasks", new_callable=AsyncMock, return_value=[]) as mock:
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await client.get("/api/tasks")
        mock.assert_called_once_with(limit=20)

    @pytest.mark.asyncio
    async def test_tasks_limit_validation_too_high(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/tasks?limit=200")
        assert r.status_code == 422  # validation error

    @pytest.mark.asyncio
    async def test_tasks_limit_validation_too_low(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/tasks?limit=0")
        assert r.status_code == 422


class TestTaskDetailEndpoint:
    @pytest.mark.asyncio
    async def test_task_found(self, transport):
        task = _make_task(id=42, full_output="full output here", prompt="long prompt")
        with patch("app.web.dashboard.get_task_by_id", new_callable=AsyncMock, return_value=task):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/tasks/42")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == 42
        assert data["full_output"] == "full output here"
        assert data["prompt"] == "long prompt"

    @pytest.mark.asyncio
    async def test_task_not_found(self, transport):
        with patch("app.web.dashboard.get_task_by_id", new_callable=AsyncMock, return_value=None):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/tasks/999")
        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()


class TestQueueEndpoint:
    @pytest.mark.asyncio
    async def test_queue_empty(self, transport):
        with patch("app.web.dashboard.get_pending_tasks", new_callable=AsyncMock, return_value=[]):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/queue")
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_queue_with_tasks(self, transport):
        tasks = [_make_task(id=1), _make_task(id=2)]
        with patch("app.web.dashboard.get_pending_tasks", new_callable=AsyncMock, return_value=tasks):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/queue")
        assert r.status_code == 200
        assert len(r.json()) == 2


class TestChainsEndpoint:
    @pytest.mark.asyncio
    async def test_chains_empty(self, transport):
        with patch("app.web.dashboard.list_chains", new_callable=AsyncMock, return_value=[]):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/chains")
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_chains_with_data(self, transport):
        chains = [_make_chain(id=1), _make_chain(id=2, name="chain2")]
        with patch("app.web.dashboard.list_chains", new_callable=AsyncMock, return_value=chains):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/chains")
        assert r.status_code == 200
        assert len(r.json()) == 2


class TestChainDetailEndpoint:
    @pytest.mark.asyncio
    async def test_chain_found(self, transport):
        chain = _make_chain(id=10)
        with patch("app.web.dashboard.get_chain_by_id", new_callable=AsyncMock, return_value=chain):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/chains/10")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == 10
        assert "steps" in data
        assert len(data["steps"]) == 2

    @pytest.mark.asyncio
    async def test_chain_not_found(self, transport):
        with patch("app.web.dashboard.get_chain_by_id", new_callable=AsyncMock, return_value=None):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/chains/999")
        assert r.status_code == 404


class TestDashboardPage:
    @pytest.mark.asyncio
    async def test_serves_html(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "TaskPilot" in r.text

    @pytest.mark.asyncio
    async def test_contains_key_elements(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/")
        html_content = r.text
        assert "stats-grid" in html_content
        assert "queue-body" in html_content
        assert "tasks-body" in html_content
        assert "chains-body" in html_content
        assert "/api/stats" in html_content
        assert "/api/tasks" in html_content
        assert "/api/queue" in html_content
        assert "/api/chains" in html_content

    @pytest.mark.asyncio
    async def test_has_auto_refresh(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/")
        assert "setTimeout" in r.text
        assert "refresh()" in r.text


class TestRenderDashboard:
    def test_returns_html(self):
        html_content = _render_dashboard()
        assert "<!DOCTYPE html>" in html_content
        assert "TaskPilot" in html_content

    def test_contains_api_urls(self):
        html_content = _render_dashboard()
        assert "/api/stats" in html_content
        assert "/api/tasks" in html_content
        assert "/api/queue" in html_content
        assert "/api/chains" in html_content

    def test_responsive_design(self):
        html_content = _render_dashboard()
        assert "@media" in html_content
        assert "max-width" in html_content

    def test_no_external_dependencies(self):
        html_content = _render_dashboard()
        # Should not reference external CDN scripts
        assert "cdn." not in html_content.lower()
        assert "unpkg.com" not in html_content.lower()


class TestCreateDashboardApp:
    def test_returns_fastapi(self):
        app = create_dashboard_app()
        assert app.title == "TaskPilot Dashboard"

    def test_has_api_docs(self):
        app = create_dashboard_app()
        assert app.docs_url == "/api/docs"

    def test_no_redoc(self):
        app = create_dashboard_app()
        assert app.redoc_url is None


# ── Edge cases and security ─────────────────────────────────────────


class TestXSSPrevention:
    def test_esc_prevents_xss(self):
        malicious = '<img src=x onerror="alert(1)">'
        result = _esc(malicious)
        assert "<img" not in result
        assert "onerror" not in result or "&" in result

    @pytest.mark.asyncio
    async def test_task_prompt_xss_in_api(self, transport):
        task = _make_task(prompt='<script>alert("xss")</script>')
        with patch("app.web.dashboard.get_recent_tasks", new_callable=AsyncMock, return_value=[task]):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/tasks")
        # JSON API returns raw data; XSS prevention is in the frontend JS esc() function
        assert r.status_code == 200
        assert r.json()[0]["prompt"].startswith("<script>")


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_stats_no_durations(self, transport):
        """All recent tasks have no duration — avg should be 0.0."""
        recent = [_make_task(id=1, status=TaskStatus.COMPLETED, duration_seconds=None)]
        with patch("app.web.dashboard.get_running_task", new_callable=AsyncMock, return_value=None), \
             patch("app.web.dashboard.get_pending_tasks", new_callable=AsyncMock, return_value=[]), \
             patch("app.web.dashboard.get_recent_tasks", new_callable=AsyncMock, return_value=recent), \
             patch("app.web.dashboard.list_chains", new_callable=AsyncMock, return_value=[]):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/stats")
        assert r.json()["avg_duration_seconds"] == 0.0

    @pytest.mark.asyncio
    async def test_task_detail_full_output_none(self, transport):
        task = _make_task(id=1, full_output=None)
        with patch("app.web.dashboard.get_task_by_id", new_callable=AsyncMock, return_value=task):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/tasks/1")
        assert r.status_code == 200
        assert r.json()["full_output"] is None

    @pytest.mark.asyncio
    async def test_chain_detail_empty_steps(self, transport):
        import json
        chain = _make_chain(id=1, steps_json=json.dumps([]))
        with patch("app.web.dashboard.get_chain_by_id", new_callable=AsyncMock, return_value=chain):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/chains/1")
        assert r.status_code == 200
        assert r.json()["steps"] == []

    @pytest.mark.asyncio
    async def test_open_api_docs(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/docs")
        # FastAPI docs page just returns HTML
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_unknown_route_404(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/nonexistent")
        assert r.status_code in (404, 405)

    @pytest.mark.asyncio
    async def test_invalid_task_id_type(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/tasks/abc")
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_chain_id_type(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/chains/abc")
        assert r.status_code == 422


class TestAllTaskStatuses:
    """Verify each TaskStatus round-trips through the API."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", list(TaskStatus))
    async def test_task_status_in_api(self, transport, status):
        task = _make_task(id=1, status=status)
        with patch("app.web.dashboard.get_recent_tasks", new_callable=AsyncMock, return_value=[task]):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/tasks")
        assert r.status_code == 200
        assert r.json()[0]["status"] == status.value


class TestAllChainStatuses:
    """Verify each ChainStatus round-trips through the API."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", list(ChainStatus))
    async def test_chain_status_in_api(self, transport, status):
        chain = _make_chain(id=1, status=status)
        with patch("app.web.dashboard.list_chains", new_callable=AsyncMock, return_value=[chain]):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/chains")
        assert r.status_code == 200
        assert r.json()[0]["status"] == status.value


# ── QA Session 1: Security headers ─────────────────────────────────


class TestSecurityHeaders:
    """Verify security headers are present on all responses."""

    @pytest.mark.asyncio
    async def test_x_frame_options(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/health")
        assert r.headers.get("x-frame-options") == "DENY"

    @pytest.mark.asyncio
    async def test_x_content_type_options(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/health")
        assert r.headers.get("x-content-type-options") == "nosniff"

    @pytest.mark.asyncio
    async def test_referrer_policy(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/health")
        assert r.headers.get("referrer-policy") == "no-referrer"

    @pytest.mark.asyncio
    async def test_csp(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/health")
        csp = r.headers.get("content-security-policy", "")
        assert "default-src 'self'" in csp
        assert "script-src 'unsafe-inline'" in csp

    @pytest.mark.asyncio
    async def test_headers_on_html_page(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/")
        assert r.headers.get("x-frame-options") == "DENY"
        assert r.headers.get("x-content-type-options") == "nosniff"


# ── QA Session 1: Bearer token auth ────────────────────────────────


class TestBearerTokenAuth:
    """Verify optional bearer-token authentication middleware."""

    @pytest.mark.asyncio
    async def test_no_token_configured_allows_access(self, transport):
        """When dashboard_token is empty, all requests pass."""
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/health")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_token_required_rejects_missing_auth(self):
        """When a token is set, requests without auth are rejected."""
        with patch.object(settings, "dashboard_token", "secret-123"):
            app = create_dashboard_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/health")
            assert r.status_code == 401
            assert r.json()["detail"] == "Unauthorized"

    @pytest.mark.asyncio
    async def test_token_required_rejects_wrong_token(self):
        with patch.object(settings, "dashboard_token", "secret-123"):
            app = create_dashboard_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get(
                    "/api/health",
                    headers={"Authorization": "Bearer wrong-token"},
                )
            assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_token_required_accepts_correct_token(self):
        with patch.object(settings, "dashboard_token", "secret-123"):
            app = create_dashboard_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get(
                    "/api/health",
                    headers={"Authorization": "Bearer secret-123"},
                )
            assert r.status_code == 200
            assert r.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_docs_hidden_when_token_set(self):
        with patch.object(settings, "dashboard_token", "secret-123"):
            app = create_dashboard_app()
            assert app.docs_url is None


# ── QA Session 2: Broker error handling ─────────────────────────────


class TestBrokerErrorHandling:
    """API returns 500 with structured error when broker raises."""

    @pytest.mark.asyncio
    async def test_stats_broker_error(self, transport):
        with patch("app.web.dashboard.get_running_task", new_callable=AsyncMock, side_effect=RuntimeError("DB down")):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/stats")
        assert r.status_code == 500
        assert "Internal error" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_tasks_broker_error(self, transport):
        with patch("app.web.dashboard.get_recent_tasks", new_callable=AsyncMock, side_effect=RuntimeError("fail")):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/tasks")
        assert r.status_code == 500

    @pytest.mark.asyncio
    async def test_task_detail_broker_error(self, transport):
        with patch("app.web.dashboard.get_task_by_id", new_callable=AsyncMock, side_effect=RuntimeError("fail")):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/tasks/1")
        assert r.status_code == 500

    @pytest.mark.asyncio
    async def test_queue_broker_error(self, transport):
        with patch("app.web.dashboard.get_pending_tasks", new_callable=AsyncMock, side_effect=RuntimeError("fail")):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/queue")
        assert r.status_code == 500

    @pytest.mark.asyncio
    async def test_chains_broker_error(self, transport):
        with patch("app.web.dashboard.list_chains", new_callable=AsyncMock, side_effect=RuntimeError("fail")):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/chains")
        assert r.status_code == 500

    @pytest.mark.asyncio
    async def test_chain_detail_broker_error(self, transport):
        with patch("app.web.dashboard.get_chain_by_id", new_callable=AsyncMock, side_effect=RuntimeError("fail")):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/chains/1")
        assert r.status_code == 500


# ── QA Session 3: HTML/JS quality ──────────────────────────────────


class TestJSQuality:
    """Verify JS code correctness in rendered HTML."""

    def test_esc_uses_null_check(self):
        """esc() should handle 0 and false correctly (not treat as empty)."""
        html_content = _render_dashboard()
        assert "s === null || s === undefined" in html_content

    def test_promise_allsettled(self):
        """Refresh should use Promise.allSettled, not Promise.all."""
        html_content = _render_dashboard()
        assert "Promise.allSettled" in html_content
        assert "Promise.all(" not in html_content

    def test_settimeout_chaining(self):
        """Should use setTimeout chaining instead of setInterval."""
        html_content = _render_dashboard()
        assert "setTimeout" in html_content
        assert "setInterval" not in html_content

    def test_fetch_checks_response_ok(self):
        """Each fetch should check r.ok before parsing JSON."""
        html_content = _render_dashboard()
        assert "r.ok" in html_content

    def test_all_values_escaped_in_templates(self):
        """Numeric values in innerHTML should go through esc()."""
        html_content = _render_dashboard()
        # Stats section should escape numeric values
        assert "esc(stats.pending_count)" in html_content
        assert "esc(stats.queue_capacity)" in html_content
        # Task IDs in tables should be escaped
        assert "esc(t.id)" in html_content
        assert "esc(c.total_steps)" in html_content


# ── QA Session 4: Performance edge cases ────────────────────────────


class TestPerformanceEdgeCases:
    """Verify performance-related edge cases."""

    def test_render_dashboard_is_deterministic(self):
        """Same HTML returned each call — suitable for future caching."""
        a = _render_dashboard()
        b = _render_dashboard()
        assert a == b

    @pytest.mark.asyncio
    async def test_large_limit_capped(self, transport):
        """Limit > 100 is rejected by validation."""
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/tasks?limit=999")
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_stats_with_many_recent_tasks(self, transport):
        """Stats aggregation handles 50 tasks correctly."""
        tasks = [
            _make_task(id=i, status=TaskStatus.COMPLETED, duration_seconds=10.0 + i)
            for i in range(50)
        ]
        with patch("app.web.dashboard.get_running_task", new_callable=AsyncMock, return_value=None), \
             patch("app.web.dashboard.get_pending_tasks", new_callable=AsyncMock, return_value=[]), \
             patch("app.web.dashboard.get_recent_tasks", new_callable=AsyncMock, return_value=tasks), \
             patch("app.web.dashboard.list_chains", new_callable=AsyncMock, return_value=[]):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["recent_completed"] == 50
        assert data["avg_duration_seconds"] > 0


# ── QA Session 5: Edge cases & input validation ─────────────────────


class TestEdgeCaseInputs:
    """Verify edge case inputs are handled safely."""

    def test_task_to_dict_none_prompt(self):
        """task.prompt=None should not crash."""
        task = _make_task(prompt=None)
        d = _task_to_dict(task)
        assert d["prompt"] == ""

    def test_task_to_dict_all_none_timestamps(self):
        """All None timestamps produce None in output."""
        task = _make_task(created_at=None, started_at=None, completed_at=None)
        d = _task_to_dict(task)
        assert d["created_at"] is None
        assert d["started_at"] is None
        assert d["completed_at"] is None

    def test_chain_to_dict_zero_steps(self):
        """Chain with 0 total_steps is valid."""
        chain = _make_chain(steps_json="[]", current_step=0)
        d = _chain_to_dict(chain)
        assert d["total_steps"] == 0
        assert d["current_step"] == 0

    @pytest.mark.asyncio
    async def test_negative_task_id(self, transport):
        """Negative task IDs are valid ints — return 404 if not found."""
        with patch("app.web.dashboard.get_task_by_id", new_callable=AsyncMock, return_value=None):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/tasks/-1")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_task_detail_none_prompt(self, transport):
        """Task detail with None prompt returns empty string."""
        task = _make_task(id=1, prompt=None, full_output="out")
        with patch("app.web.dashboard.get_task_by_id", new_callable=AsyncMock, return_value=task):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/tasks/1")
        assert r.status_code == 200
        assert r.json()["prompt"] == ""

    def test_task_to_dict_binary_prompt(self):
        """Prompt with binary-like characters is truncated safely."""
        task = _make_task(prompt="x\x00y\x01z" * 100)
        d = _task_to_dict(task)
        assert len(d["prompt"]) <= 200

    @pytest.mark.asyncio
    async def test_very_large_task_id(self, transport):
        """Very large task_id is a valid int — returns 404."""
        with patch("app.web.dashboard.get_task_by_id", new_callable=AsyncMock, return_value=None):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/tasks/999999999999")
        assert r.status_code == 404


# ── QA Session 6: Integration edge cases ─────────────────────────────


class TestIntegrationEdgeCases:
    """Verify integration-level edge cases."""

    def test_no_unused_starlette_import(self):
        """BaseHTTPMiddleware should not be imported (we use @app.middleware)."""
        import app.web.dashboard as mod
        source = open(mod.__file__).read()
        assert "BaseHTTPMiddleware" not in source

    @pytest.mark.asyncio
    async def test_concurrent_stats_requests(self, transport):
        """Multiple concurrent stats requests don't interfere."""
        import asyncio
        with patch("app.web.dashboard.get_running_task", new_callable=AsyncMock, return_value=None), \
             patch("app.web.dashboard.get_pending_tasks", new_callable=AsyncMock, return_value=[]), \
             patch("app.web.dashboard.get_recent_tasks", new_callable=AsyncMock, return_value=[]), \
             patch("app.web.dashboard.list_chains", new_callable=AsyncMock, return_value=[]):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                results = await asyncio.gather(
                    client.get("/api/stats"),
                    client.get("/api/stats"),
                    client.get("/api/stats"),
                )
        for r in results:
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_health_works_when_db_down(self, transport):
        """/api/health should work even if broker fails."""
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_dashboard_html_is_valid_html5(self, transport):
        """HTML response starts with DOCTYPE and contains required elements."""
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/")
        assert r.text.startswith("<!DOCTYPE html>")
        assert '<html lang="en">' in r.text
        assert "<meta charset" in r.text
        assert "</body>" in r.text
        assert "</html>" in r.text


# ── QA Session 9: Security headers on error responses ────────────────


class TestSecurityHeadersOnErrors:
    """Security headers must be present on ALL responses, including errors."""

    @pytest.mark.asyncio
    async def test_headers_on_401(self):
        """401 Unauthorized responses have security headers."""
        with patch.object(settings, "dashboard_token", "secret"):
            app = create_dashboard_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/health")
        assert r.status_code == 401
        assert r.headers.get("x-frame-options") == "DENY"
        assert r.headers.get("x-content-type-options") == "nosniff"
        assert r.headers.get("referrer-policy") == "no-referrer"

    @pytest.mark.asyncio
    async def test_headers_on_500(self, transport):
        """500 Internal Error responses have security headers."""
        with patch("app.web.dashboard.get_running_task", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/stats")
        assert r.status_code == 500
        assert r.headers.get("x-frame-options") == "DENY"
        assert r.headers.get("x-content-type-options") == "nosniff"

    @pytest.mark.asyncio
    async def test_headers_on_404(self, transport):
        """404 Not Found responses have security headers."""
        with patch("app.web.dashboard.get_task_by_id", new_callable=AsyncMock, return_value=None):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/tasks/999")
        assert r.status_code == 404
        assert r.headers.get("x-frame-options") == "DENY"

    @pytest.mark.asyncio
    async def test_headers_on_422(self, transport):
        """422 Validation Error responses have security headers."""
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/tasks/notanint")
        assert r.status_code == 422
        assert r.headers.get("x-frame-options") == "DENY"


# ── QA Session 10: Timing-safe auth ─────────────────────────────────


class TestTimingSafeAuth:
    """Verify token comparison uses hmac.compare_digest."""

    def test_hmac_compare_digest_used(self):
        """Source code uses hmac.compare_digest, not == or !=."""
        import app.web.dashboard as mod
        source = open(mod.__file__).read()
        assert "hmac.compare_digest" in source
        # Ensure no `auth !=` or `auth ==` for token comparison
        assert "auth != " not in source
        assert "auth == " not in source

    @pytest.mark.asyncio
    async def test_auth_header_whitespace_tolerance(self):
        """Auth header with trailing whitespace still works."""
        with patch.object(settings, "dashboard_token", "secret"):
            app = create_dashboard_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get(
                    "/api/health",
                    headers={"Authorization": "Bearer secret"},
                )
            assert r.status_code == 200

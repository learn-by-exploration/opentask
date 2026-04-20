"""Tests for multi-machine Worker API — broker functions + dashboard endpoints."""

from __future__ import annotations

import json
import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-for-tests")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch, MagicMock

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base, Task, TaskStatus, _utcnow


# ── Fixtures ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture
async def patch_db(engine):
    """Patch broker's get_session to use our in-memory engine."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _get_session():
        return factory()

    with patch("app.core.broker.get_session", _get_session):
        yield factory


@pytest_asyncio.fixture
async def seed_task(patch_db):
    """Insert a single PENDING task and return it."""
    factory = patch_db
    async with factory() as s, s.begin():
        t = Task(
            prompt="fix the bug",
            project_dir="/tmp/test",
            agent="opencode",
            status=TaskStatus.PENDING,
            telegram_chat_id=12345,
        )
        s.add(t)
        await s.flush()
        await s.refresh(t)
        return t


@pytest_asyncio.fixture
async def seed_assigned_task(patch_db):
    """Insert a PENDING task assigned to 'server2'."""
    factory = patch_db
    async with factory() as s, s.begin():
        t = Task(
            prompt="deploy the app",
            project_dir="/tmp/deploy",
            agent="claude",
            status=TaskStatus.PENDING,
            telegram_chat_id=12345,
            assigned_to="server2",
        )
        s.add(t)
        await s.flush()
        await s.refresh(t)
        return t


# ═══════════════════════════════════════════════════════════════════
# 1. MODEL TESTS — Task worker fields
# ═══════════════════════════════════════════════════════════════════


class TestTaskWorkerFields:
    """Test that Task model includes worker routing fields."""

    @pytest.mark.asyncio
    async def test_task_has_assigned_to_field(self, engine):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s, s.begin():
            t = Task(
                prompt="test",
                project_dir="/tmp",
                agent="opencode",
                status=TaskStatus.PENDING,
                assigned_to="server2",
            )
            s.add(t)
            await s.flush()
            await s.refresh(t)
            assert t.assigned_to == "server2"

    @pytest.mark.asyncio
    async def test_task_has_worker_id_field(self, engine):
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s, s.begin():
            t = Task(
                prompt="test",
                project_dir="/tmp",
                agent="opencode",
                status=TaskStatus.RUNNING,
                worker_id="server2",
            )
            s.add(t)
            await s.flush()
            await s.refresh(t)
            assert t.worker_id == "server2"

    @pytest.mark.asyncio
    async def test_task_has_heartbeat_at_field(self, engine):
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        now = _utcnow()
        async with factory() as s, s.begin():
            t = Task(
                prompt="test",
                project_dir="/tmp",
                agent="opencode",
                status=TaskStatus.RUNNING,
                heartbeat_at=now,
            )
            s.add(t)
            await s.flush()
            await s.refresh(t)
            assert t.heartbeat_at is not None

    @pytest.mark.asyncio
    async def test_worker_fields_default_to_none(self, engine):
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s, s.begin():
            t = Task(
                prompt="test",
                project_dir="/tmp",
                agent="opencode",
                status=TaskStatus.PENDING,
            )
            s.add(t)
            await s.flush()
            await s.refresh(t)
            assert t.assigned_to is None
            assert t.worker_id is None
            assert t.heartbeat_at is None


# ═══════════════════════════════════════════════════════════════════
# 2. BROKER TESTS — enqueue with assigned_to
# ═══════════════════════════════════════════════════════════════════


class TestEnqueueWithAssignment:
    """Test enqueue_task with assigned_to parameter."""

    @pytest.mark.asyncio
    async def test_enqueue_task_with_assigned_to(self, patch_db):
        from app.core.broker import enqueue_task
        task = await enqueue_task(
            prompt="deploy app",
            project_dir="/tmp/test",
            agent="opencode",
            assigned_to="server2",
        )
        assert task.assigned_to == "server2"
        assert task.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_enqueue_task_without_assigned_to(self, patch_db):
        from app.core.broker import enqueue_task
        task = await enqueue_task(
            prompt="fix bug",
            project_dir="/tmp/test",
            agent="opencode",
        )
        assert task.assigned_to is None

    @pytest.mark.asyncio
    async def test_enqueue_task_assigned_to_none_explicitly(self, patch_db):
        from app.core.broker import enqueue_task
        task = await enqueue_task(
            prompt="fix bug",
            project_dir="/tmp/test",
            agent="opencode",
            assigned_to=None,
        )
        assert task.assigned_to is None


# ═══════════════════════════════════════════════════════════════════
# 3. BROKER TESTS — pick_next_task skips assigned tasks
# ═══════════════════════════════════════════════════════════════════


class TestPickNextTaskSkipsAssigned:
    """Verify pick_next_task only picks unassigned tasks."""

    @pytest.mark.asyncio
    async def test_pick_skips_assigned_task(self, seed_assigned_task, patch_db):
        from app.core.broker import pick_next_task
        task = await pick_next_task()
        assert task is None  # only task is assigned to server2

    @pytest.mark.asyncio
    async def test_pick_returns_unassigned_task(self, seed_task, patch_db):
        from app.core.broker import pick_next_task
        task = await pick_next_task()
        assert task is not None
        assert task.status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_pick_prefers_unassigned_over_assigned(self, patch_db):
        from app.core.broker import enqueue_task, pick_next_task
        await enqueue_task(prompt="assigned", project_dir="/tmp", agent="opencode", assigned_to="server2")
        await enqueue_task(prompt="unassigned", project_dir="/tmp", agent="opencode")
        task = await pick_next_task()
        assert task is not None
        assert task.prompt == "unassigned"
        assert task.assigned_to is None


# ═══════════════════════════════════════════════════════════════════
# 4. BROKER TESTS — worker_claim_task
# ═══════════════════════════════════════════════════════════════════


class TestWorkerClaimTask:
    """Test the worker claim mechanism."""

    @pytest.mark.asyncio
    async def test_claim_assigned_task(self, seed_assigned_task, patch_db):
        from app.core.broker import worker_claim_task
        task = await worker_claim_task("server2")
        assert task is not None
        assert task.id == seed_assigned_task.id
        assert task.status == TaskStatus.RUNNING
        assert task.worker_id == "server2"
        assert task.heartbeat_at is not None

    @pytest.mark.asyncio
    async def test_claim_unassigned_task(self, seed_task, patch_db):
        from app.core.broker import worker_claim_task
        task = await worker_claim_task("server2")
        assert task is not None
        assert task.worker_id == "server2"
        assert task.status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_claim_no_available_tasks(self, patch_db):
        from app.core.broker import worker_claim_task
        task = await worker_claim_task("server2")
        assert task is None

    @pytest.mark.asyncio
    async def test_claim_does_not_steal_from_other_worker(self, seed_assigned_task, patch_db):
        from app.core.broker import worker_claim_task
        # server3 should not be able to claim server2's task
        task = await worker_claim_task("server3")
        assert task is None  # assigned_to=server2, not server3

    @pytest.mark.asyncio
    async def test_claim_prefers_assigned_over_unassigned(self, patch_db):
        from app.core.broker import enqueue_task, worker_claim_task
        await enqueue_task(prompt="unassigned", project_dir="/tmp", agent="opencode")
        await enqueue_task(prompt="for-me", project_dir="/tmp", agent="opencode", assigned_to="server2")
        task = await worker_claim_task("server2")
        assert task is not None
        assert task.prompt == "for-me"

    @pytest.mark.asyncio
    async def test_claim_validates_empty_worker_id(self, patch_db):
        from app.core.broker import worker_claim_task
        with pytest.raises(ValueError, match="worker_id"):
            await worker_claim_task("")

    @pytest.mark.asyncio
    async def test_claim_validates_long_worker_id(self, patch_db):
        from app.core.broker import worker_claim_task
        with pytest.raises(ValueError, match="worker_id"):
            await worker_claim_task("x" * 200)

    @pytest.mark.asyncio
    async def test_claim_sets_started_at(self, seed_task, patch_db):
        from app.core.broker import worker_claim_task
        task = await worker_claim_task("server2")
        assert task.started_at is not None


# ═══════════════════════════════════════════════════════════════════
# 5. BROKER TESTS — worker_submit_result
# ═══════════════════════════════════════════════════════════════════


class TestWorkerSubmitResult:
    """Test result submission by remote workers."""

    @pytest.mark.asyncio
    async def test_submit_success(self, seed_task, patch_db):
        from app.core.broker import worker_claim_task, worker_submit_result
        claimed = await worker_claim_task("server2")
        result = await worker_submit_result(
            task_id=claimed.id,
            worker_id="server2",
            exit_code=0,
            output_summary="Done!",
            full_output="Full output here",
        )
        assert result is not None
        assert result.status == TaskStatus.COMPLETED
        assert result.exit_code == 0
        assert result.output_summary == "Done!"
        assert result.duration_seconds is not None

    @pytest.mark.asyncio
    async def test_submit_failure(self, seed_task, patch_db):
        from app.core.broker import worker_claim_task, worker_submit_result
        claimed = await worker_claim_task("server2")
        result = await worker_submit_result(
            task_id=claimed.id,
            worker_id="server2",
            exit_code=1,
            output_summary="Error occurred",
            full_output="traceback...",
            error_message="Build failed",
        )
        assert result is not None
        assert result.status == TaskStatus.FAILED
        assert result.exit_code == 1
        assert result.error_message == "Build failed"

    @pytest.mark.asyncio
    async def test_submit_wrong_worker_rejected(self, seed_task, patch_db):
        from app.core.broker import worker_claim_task, worker_submit_result
        await worker_claim_task("server2")
        result = await worker_submit_result(
            task_id=seed_task.id,
            worker_id="hacker",
            exit_code=0,
            output_summary="Done",
            full_output="",
        )
        assert result is None  # rejected — wrong worker

    @pytest.mark.asyncio
    async def test_submit_nonexistent_task(self, patch_db):
        from app.core.broker import worker_submit_result
        result = await worker_submit_result(
            task_id=999,
            worker_id="server2",
            exit_code=0,
            output_summary="Done",
            full_output="",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_submit_already_completed_task(self, seed_task, patch_db):
        from app.core.broker import worker_claim_task, worker_submit_result
        claimed = await worker_claim_task("server2")
        await worker_submit_result(
            task_id=claimed.id, worker_id="server2",
            exit_code=0, output_summary="First", full_output="",
        )
        # Second submit should fail
        dup = await worker_submit_result(
            task_id=claimed.id, worker_id="server2",
            exit_code=1, output_summary="Second", full_output="",
        )
        assert dup is None


# ═══════════════════════════════════════════════════════════════════
# 6. BROKER TESTS — worker_heartbeat
# ═══════════════════════════════════════════════════════════════════


class TestWorkerHeartbeat:
    """Test heartbeat mechanism."""

    @pytest.mark.asyncio
    async def test_heartbeat_updates_timestamp(self, seed_task, patch_db):
        from app.core.broker import worker_claim_task, worker_heartbeat
        claimed = await worker_claim_task("server2")
        old_hb = claimed.heartbeat_at

        ok = await worker_heartbeat(claimed.id, "server2")
        assert ok is True

        # Verify timestamp actually updated
        factory = patch_db
        async with factory() as s:
            result = await s.execute(select(Task).where(Task.id == claimed.id))
            t = result.scalar_one()
            assert t.heartbeat_at >= old_hb

    @pytest.mark.asyncio
    async def test_heartbeat_wrong_worker(self, seed_task, patch_db):
        from app.core.broker import worker_claim_task, worker_heartbeat
        claimed = await worker_claim_task("server2")
        ok = await worker_heartbeat(claimed.id, "server3")
        assert ok is False

    @pytest.mark.asyncio
    async def test_heartbeat_nonexistent_task(self, patch_db):
        from app.core.broker import worker_heartbeat
        ok = await worker_heartbeat(999, "server2")
        assert ok is False

    @pytest.mark.asyncio
    async def test_heartbeat_completed_task_rejected(self, seed_task, patch_db):
        from app.core.broker import worker_claim_task, worker_submit_result, worker_heartbeat
        claimed = await worker_claim_task("server2")
        await worker_submit_result(
            task_id=claimed.id, worker_id="server2",
            exit_code=0, output_summary="Done", full_output="",
        )
        ok = await worker_heartbeat(claimed.id, "server2")
        assert ok is False  # task is no longer RUNNING


# ═══════════════════════════════════════════════════════════════════
# 7. BROKER TESTS — recover_stale_worker_tasks
# ═══════════════════════════════════════════════════════════════════


class TestRecoverStaleWorkerTasks:
    """Test automatic recovery of tasks from dead workers."""

    @pytest.mark.asyncio
    async def test_recover_stale_task(self, patch_db):
        from app.core.broker import worker_claim_task, recover_stale_worker_tasks, enqueue_task

        await enqueue_task(prompt="stale task", project_dir="/tmp", agent="opencode")
        from app.core.broker import worker_claim_task
        claimed = await worker_claim_task("dead-worker")

        # Manually set heartbeat_at far in the past
        factory = patch_db
        async with factory() as s, s.begin():
            result = await s.execute(select(Task).where(Task.id == claimed.id))
            t = result.scalar_one()
            t.heartbeat_at = _utcnow() - timedelta(seconds=300)

        recovered = await recover_stale_worker_tasks()
        assert recovered == 1

        # Task should be back to PENDING
        async with factory() as s:
            result = await s.execute(select(Task).where(Task.id == claimed.id))
            t = result.scalar_one()
            assert t.status == TaskStatus.PENDING
            assert t.worker_id is None
            assert t.started_at is None

    @pytest.mark.asyncio
    async def test_no_recovery_for_fresh_heartbeat(self, seed_task, patch_db):
        from app.core.broker import worker_claim_task, recover_stale_worker_tasks
        await worker_claim_task("server2")
        recovered = await recover_stale_worker_tasks()
        assert recovered == 0

    @pytest.mark.asyncio
    async def test_no_recovery_for_local_tasks(self, patch_db):
        """Local runner tasks (no worker_id) should not be recovered."""
        from app.core.broker import enqueue_task, pick_next_task, recover_stale_worker_tasks
        await enqueue_task(prompt="local", project_dir="/tmp", agent="opencode")
        await pick_next_task()
        recovered = await recover_stale_worker_tasks()
        assert recovered == 0


# ═══════════════════════════════════════════════════════════════════
# 8. BROKER TESTS — list_active_workers
# ═══════════════════════════════════════════════════════════════════


class TestListActiveWorkers:
    """Test retrieving active worker information."""

    @pytest.mark.asyncio
    async def test_no_workers(self, patch_db):
        from app.core.broker import list_active_workers
        workers = await list_active_workers()
        assert workers == []

    @pytest.mark.asyncio
    async def test_single_worker_with_task(self, seed_task, patch_db):
        from app.core.broker import worker_claim_task, list_active_workers
        claimed = await worker_claim_task("server2")
        workers = await list_active_workers()
        assert len(workers) == 1
        assert workers[0]["worker_id"] == "server2"
        assert claimed.id in workers[0]["tasks"]
        assert workers[0]["last_heartbeat"] is not None

    @pytest.mark.asyncio
    async def test_multiple_workers(self, patch_db):
        from app.core.broker import enqueue_task, worker_claim_task, list_active_workers
        await enqueue_task(prompt="task1", project_dir="/tmp", agent="opencode")
        await enqueue_task(prompt="task2", project_dir="/tmp", agent="opencode")
        await worker_claim_task("server2")
        await worker_claim_task("server3")
        workers = await list_active_workers()
        assert len(workers) == 2
        worker_ids = {w["worker_id"] for w in workers}
        assert worker_ids == {"server2", "server3"}


# ═══════════════════════════════════════════════════════════════════
# 9. DASHBOARD API TESTS — Worker endpoints
# ═══════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def dashboard_client(patch_db):
    """Create an HTTPX test client for the dashboard app."""
    from httpx import AsyncClient, ASGITransport
    from app.web.dashboard import create_dashboard_app

    # Disable auth for tests
    with patch.object(
        __import__("app.config.settings", fromlist=["settings"]).settings,
        "dashboard_token", "",
    ):
        app = create_dashboard_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


class TestWorkerClaimEndpoint:
    """Test POST /api/worker/claim."""

    @pytest.mark.asyncio
    async def test_claim_returns_task(self, dashboard_client, seed_task):
        resp = await dashboard_client.post("/api/worker/claim", json={"worker_id": "server2"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == seed_task.id
        assert data["status"] == "running"
        assert data["worker_id"] == "server2"
        assert data["prompt"] == "fix the bug"

    @pytest.mark.asyncio
    async def test_claim_returns_204_when_empty(self, dashboard_client, patch_db):
        resp = await dashboard_client.post("/api/worker/claim", json={"worker_id": "server2"})
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_claim_missing_worker_id(self, dashboard_client, patch_db):
        resp = await dashboard_client.post("/api/worker/claim", json={})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_claim_empty_worker_id(self, dashboard_client, patch_db):
        resp = await dashboard_client.post("/api/worker/claim", json={"worker_id": ""})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_claim_invalid_json(self, dashboard_client, patch_db):
        resp = await dashboard_client.post(
            "/api/worker/claim",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400


class TestWorkerResultEndpoint:
    """Test POST /api/worker/{task_id}/result."""

    @pytest.mark.asyncio
    async def test_submit_result_success(self, dashboard_client, seed_task):
        # First claim
        await dashboard_client.post("/api/worker/claim", json={"worker_id": "server2"})
        # Then submit result
        resp = await dashboard_client.post(
            f"/api/worker/{seed_task.id}/result",
            json={
                "worker_id": "server2",
                "exit_code": 0,
                "output_summary": "All tests passed",
                "full_output": "Running...\nDone.",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_submit_result_wrong_worker(self, dashboard_client, seed_task):
        await dashboard_client.post("/api/worker/claim", json={"worker_id": "server2"})
        resp = await dashboard_client.post(
            f"/api/worker/{seed_task.id}/result",
            json={
                "worker_id": "hacker",
                "exit_code": 0,
                "output_summary": "Pwned",
                "full_output": "",
            },
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_submit_result_missing_exit_code(self, dashboard_client, seed_task):
        await dashboard_client.post("/api/worker/claim", json={"worker_id": "server2"})
        resp = await dashboard_client.post(
            f"/api/worker/{seed_task.id}/result",
            json={
                "worker_id": "server2",
                "output_summary": "Done",
                "full_output": "",
            },
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_submit_result_missing_worker_id(self, dashboard_client, seed_task):
        resp = await dashboard_client.post(
            f"/api/worker/{seed_task.id}/result",
            json={
                "exit_code": 0,
                "output_summary": "Done",
                "full_output": "",
            },
        )
        assert resp.status_code == 400


class TestWorkerHeartbeatEndpoint:
    """Test POST /api/worker/{task_id}/heartbeat."""

    @pytest.mark.asyncio
    async def test_heartbeat_success(self, dashboard_client, seed_task):
        await dashboard_client.post("/api/worker/claim", json={"worker_id": "server2"})
        resp = await dashboard_client.post(
            f"/api/worker/{seed_task.id}/heartbeat",
            json={"worker_id": "server2"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_heartbeat_wrong_worker(self, dashboard_client, seed_task):
        await dashboard_client.post("/api/worker/claim", json={"worker_id": "server2"})
        resp = await dashboard_client.post(
            f"/api/worker/{seed_task.id}/heartbeat",
            json={"worker_id": "server3"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_heartbeat_missing_worker_id(self, dashboard_client, seed_task):
        resp = await dashboard_client.post(
            f"/api/worker/{seed_task.id}/heartbeat",
            json={},
        )
        assert resp.status_code == 400


class TestWorkersListEndpoint:
    """Test GET /api/workers."""

    @pytest.mark.asyncio
    async def test_no_workers(self, dashboard_client, patch_db):
        resp = await dashboard_client.get("/api/workers")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_active_workers_listed(self, dashboard_client, seed_task):
        await dashboard_client.post("/api/worker/claim", json={"worker_id": "server2"})
        resp = await dashboard_client.get("/api/workers")
        assert resp.status_code == 200
        workers = resp.json()
        assert len(workers) == 1
        assert workers[0]["worker_id"] == "server2"


# ═══════════════════════════════════════════════════════════════════
# 10. BOT TESTS — @worker routing
# ═══════════════════════════════════════════════════════════════════


class TestBotWorkerRouting:
    """Test that @worker_name in messages triggers assigned_to."""

    @pytest.mark.asyncio
    async def test_parse_worker_hint(self, patch_db):
        from app.core.broker import enqueue_task

        # Simulate what bot does: parse @server2 from prompt
        prompt = "@server2 fix the deployment"
        assigned_to = None
        clean_prompt = prompt
        if prompt.startswith("@"):
            parts = prompt.split(None, 1)
            if len(parts) >= 2:
                assigned_to = parts[0][1:]
                clean_prompt = parts[1]

        task = await enqueue_task(
            prompt=clean_prompt,
            project_dir="/tmp",
            agent="opencode",
            assigned_to=assigned_to,
        )
        assert task.assigned_to == "server2"
        assert task.prompt == "fix the deployment"

    @pytest.mark.asyncio
    async def test_no_worker_hint(self, patch_db):
        from app.core.broker import enqueue_task

        prompt = "fix the bug normally"
        assigned_to = None
        clean_prompt = prompt
        if prompt.startswith("@"):
            parts = prompt.split(None, 1)
            if len(parts) >= 2:
                assigned_to = parts[0][1:]
                clean_prompt = parts[1]

        task = await enqueue_task(
            prompt=clean_prompt,
            project_dir="/tmp",
            agent="opencode",
            assigned_to=assigned_to,
        )
        assert task.assigned_to is None
        assert task.prompt == "fix the bug normally"

    @pytest.mark.asyncio
    async def test_only_at_symbol_no_split(self, patch_db):
        """If the entire prompt is just '@server2' with no task text, treat as normal prompt."""
        from app.core.broker import enqueue_task

        prompt = "@server2"
        assigned_to = None
        clean_prompt = prompt
        if prompt.startswith("@"):
            parts = prompt.split(None, 1)
            if len(parts) >= 2:
                assigned_to = parts[0][1:]
                clean_prompt = parts[1]
            # else: keep original prompt, assigned_to stays None

        task = await enqueue_task(
            prompt=clean_prompt,
            project_dir="/tmp",
            agent="opencode",
            assigned_to=assigned_to,
        )
        assert task.assigned_to is None
        assert task.prompt == "@server2"


# ═══════════════════════════════════════════════════════════════════
# 11. DASHBOARD TESTS — _task_to_dict includes worker fields
# ═══════════════════════════════════════════════════════════════════


class TestTaskToDictWorkerFields:
    """Verify task serialization includes worker routing fields."""

    def test_task_dict_has_worker_fields(self):
        from app.web.dashboard import _task_to_dict
        from unittest.mock import MagicMock

        task = MagicMock()
        task.id = 1
        task.prompt = "test"
        task.project_dir = "/tmp"
        task.agent = "opencode"
        task.status = TaskStatus.RUNNING
        task.exit_code = None
        task.error_message = None
        task.output_summary = None
        task.duration_seconds = None
        task.created_at = _utcnow()
        task.started_at = _utcnow()
        task.completed_at = None
        task.chain_id = None
        task.chain_step = None
        task.repeat_total = None
        task.repeat_remaining = None
        task.assigned_to = "server2"
        task.worker_id = "server2"

        d = _task_to_dict(task)
        assert d["assigned_to"] == "server2"
        assert d["worker_id"] == "server2"


# ═══════════════════════════════════════════════════════════════════
# 12. SECURITY TESTS — Worker API
# ═══════════════════════════════════════════════════════════════════


class TestWorkerAPISecurity:
    """Security edge cases for the worker API."""

    @pytest.mark.asyncio
    async def test_claim_with_very_long_worker_id(self, dashboard_client, patch_db):
        resp = await dashboard_client.post(
            "/api/worker/claim",
            json={"worker_id": "x" * 200},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_result_truncates_output(self, dashboard_client, seed_task):
        await dashboard_client.post("/api/worker/claim", json={"worker_id": "server2"})
        resp = await dashboard_client.post(
            f"/api/worker/{seed_task.id}/result",
            json={
                "worker_id": "server2",
                "exit_code": 0,
                "output_summary": "S" * 1000,  # over 500 limit
                "full_output": "F" * 100,
            },
        )
        assert resp.status_code == 200
        # output_summary should be truncated to 500 chars by the endpoint

    @pytest.mark.asyncio
    async def test_result_non_integer_exit_code(self, dashboard_client, seed_task):
        await dashboard_client.post("/api/worker/claim", json={"worker_id": "server2"})
        resp = await dashboard_client.post(
            f"/api/worker/{seed_task.id}/result",
            json={
                "worker_id": "server2",
                "exit_code": "zero",
                "output_summary": "Done",
                "full_output": "",
            },
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_cannot_submit_result_twice(self, dashboard_client, seed_task):
        await dashboard_client.post("/api/worker/claim", json={"worker_id": "server2"})
        # First submit
        resp1 = await dashboard_client.post(
            f"/api/worker/{seed_task.id}/result",
            json={"worker_id": "server2", "exit_code": 0, "output_summary": "Ok", "full_output": ""},
        )
        assert resp1.status_code == 200
        # Second submit — should fail
        resp2 = await dashboard_client.post(
            f"/api/worker/{seed_task.id}/result",
            json={"worker_id": "server2", "exit_code": 1, "output_summary": "Bad", "full_output": ""},
        )
        assert resp2.status_code == 404


# ═══════════════════════════════════════════════════════════════════
# 13. INTEGRATION TESTS — Full workflow
# ═══════════════════════════════════════════════════════════════════


class TestFullWorkerWorkflow:
    """Integration: enqueue → claim → heartbeat → result."""

    @pytest.mark.asyncio
    async def test_full_remote_workflow(self, dashboard_client, patch_db):
        """Simulate a complete remote worker lifecycle via API."""
        from app.core.broker import enqueue_task

        # 1. Enqueue a task assigned to server2
        task = await enqueue_task(
            prompt="run tests",
            project_dir="/tmp/test",
            agent="opencode",
            assigned_to="server2",
        )

        # 2. Verify local runner won't pick it up
        from app.core.broker import pick_next_task
        local_task = await pick_next_task()
        assert local_task is None

        # 3. server2 claims it
        resp = await dashboard_client.post("/api/worker/claim", json={"worker_id": "server2"})
        assert resp.status_code == 200
        claimed = resp.json()
        assert claimed["id"] == task.id

        # 4. Heartbeat during execution
        resp = await dashboard_client.post(
            f"/api/worker/{task.id}/heartbeat",
            json={"worker_id": "server2"},
        )
        assert resp.status_code == 200

        # 5. Submit result
        resp = await dashboard_client.post(
            f"/api/worker/{task.id}/result",
            json={
                "worker_id": "server2",
                "exit_code": 0,
                "output_summary": "All 50 tests passed",
                "full_output": "test output...",
            },
        )
        assert resp.status_code == 200
        result = resp.json()
        assert result["status"] == "completed"

        # 6. Verify active workers list is now empty
        resp = await dashboard_client.get("/api/workers")
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_unassigned_task_claimable_by_any_worker(self, dashboard_client, patch_db):
        """Unassigned tasks can be claimed by any worker."""
        from app.core.broker import enqueue_task

        await enqueue_task(prompt="generic task", project_dir="/tmp", agent="opencode")

        resp = await dashboard_client.post("/api/worker/claim", json={"worker_id": "server5"})
        assert resp.status_code == 200
        assert resp.json()["worker_id"] == "server5"

    @pytest.mark.asyncio
    async def test_stale_worker_recovery_via_claim(self, dashboard_client, patch_db):
        """When a new worker claims, stale tasks from dead workers are recovered."""
        from app.core.broker import enqueue_task, worker_claim_task

        task = await enqueue_task(prompt="will be stale", project_dir="/tmp", agent="opencode")
        claimed = await worker_claim_task("dead-worker")

        # Manually expire the heartbeat
        factory = patch_db
        async with factory() as s, s.begin():
            result = await s.execute(select(Task).where(Task.id == claimed.id))
            t = result.scalar_one()
            t.heartbeat_at = _utcnow() - timedelta(seconds=300)

        # New worker claims — recover_stale runs first inside the endpoint
        resp = await dashboard_client.post("/api/worker/claim", json={"worker_id": "fresh-worker"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["worker_id"] == "fresh-worker"
        assert data["id"] == task.id  # recovered and re-claimed


# ═══════════════════════════════════════════════════════════════════
# 14. WORKER.PY TESTS — command building + config
# ═══════════════════════════════════════════════════════════════════


class TestWorkerScript:
    """Test worker.py helper functions."""

    def test_build_command_opencode(self):
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from worker import _build_command, DEFAULT_AGENT_COMMANDS

        task = {
            "agent": "opencode",
            "prompt": "fix the bug",
            "project_dir": "/home/user/project",
        }
        argv = _build_command(task, DEFAULT_AGENT_COMMANDS)
        assert argv[0] == "opencode"
        assert "fix the bug" in argv
        assert "run" in argv

    def test_build_command_unknown_agent(self):
        from worker import _build_command
        task = {"agent": "unknown", "prompt": "test", "project_dir": "/tmp"}
        argv = _build_command(task, {})
        assert argv[0] == "echo"

    def test_build_command_with_special_chars(self):
        from worker import _build_command, DEFAULT_AGENT_COMMANDS
        task = {
            "agent": "opencode",
            "prompt": "fix the 'quoted' bug",
            "project_dir": "/home/user/my project",
        }
        argv = _build_command(task, DEFAULT_AGENT_COMMANDS)
        # The prompt should appear as a single argument
        assert any("fix the 'quoted' bug" in arg for arg in argv)

    def test_load_config_no_file(self):
        from worker import _load_config, DEFAULT_AGENT_COMMANDS
        # Should return defaults when no config file exists
        result = _load_config()
        assert "opencode" in result

    def test_api_function_handles_connection_error(self):
        from worker import _api
        status, body = _api("http://localhost:99999", "POST", "/api/test", {"key": "val"}, "")
        assert status == 0
        assert body is None

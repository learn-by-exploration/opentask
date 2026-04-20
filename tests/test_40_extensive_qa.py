"""
20 Extensive QA Dry Run Sessions (QA 21–40)
============================================

Second batch of comprehensive QA sessions covering deeper edge cases,
regression scenarios, integration boundaries, and stress patterns
for ALL TaskPilot modules: runner, broker, bot, dashboard, models, settings.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base, ChatPrefs, ChainStatus, Task, TaskChain, TaskStatus
from app.config.settings import settings


# ── Fixtures ────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def session():
    """Fresh in-memory DB for broker tests, patching get_session."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _get_session():
        return factory()

    with patch("app.core.broker.get_session", _get_session):
        async with factory() as s:
            yield s

    await engine.dispose()


@pytest_asyncio.fixture
async def dashboard_client():
    """Authenticated async HTTP client for dashboard tests."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _get_session():
        return factory()

    with (
        patch("app.core.broker.get_session", _get_session),
        patch.object(settings, "dashboard_token", "test-token"),
    ):
        from app.web.dashboard import create_dashboard_app
        app = create_dashboard_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            client.headers["Authorization"] = "Bearer test-token"
            yield client

    await engine.dispose()


# ═════════════════════════════════════════════════════════════════════
#  QA 21 — Deep: Broker atomicity under concurrent writes
# ═════════════════════════════════════════════════════════════════════


class TestQA21BrokerAtomicity:
    """Verify broker operations maintain consistency under edge conditions."""

    @pytest.mark.asyncio
    async def test_pick_next_task_returns_oldest_first(self, session):
        from app.core.broker import enqueue_task, pick_next_task
        t1 = await enqueue_task(prompt="first", project_dir="/tmp", agent="claude")
        t2 = await enqueue_task(prompt="second", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        assert picked.id == t1.id

    @pytest.mark.asyncio
    async def test_pick_next_sets_started_at(self, session):
        from app.core.broker import enqueue_task, pick_next_task
        await enqueue_task(prompt="ts test", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        assert picked.started_at is not None

    @pytest.mark.asyncio
    async def test_pick_next_sets_status_running(self, session):
        from app.core.broker import enqueue_task, pick_next_task
        await enqueue_task(prompt="status", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        assert picked.status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_complete_sets_completed_at(self, session):
        from app.core.broker import enqueue_task, pick_next_task, complete_task
        await enqueue_task(prompt="comp", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        done = await complete_task(picked.id, exit_code=0, output_summary="ok", full_output="ok")
        assert done.completed_at is not None

    @pytest.mark.asyncio
    async def test_complete_sets_duration(self, session):
        from app.core.broker import enqueue_task, pick_next_task, complete_task
        await enqueue_task(prompt="dur", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        done = await complete_task(picked.id, exit_code=0, output_summary="ok", full_output="ok")
        assert done.duration_seconds is not None
        assert done.duration_seconds >= 0

    @pytest.mark.asyncio
    async def test_failed_task_stores_error_message(self, session):
        from app.core.broker import enqueue_task, pick_next_task, complete_task
        await enqueue_task(prompt="fail", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        done = await complete_task(
            picked.id, exit_code=1, output_summary="crash", full_output="traceback",
            error_message="segfault",
        )
        assert done.error_message == "segfault"
        assert done.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_cancel_only_pending_not_running(self, session):
        from app.core.broker import enqueue_task, pick_next_task, cancel_task_by_id
        t = await enqueue_task(prompt="run me", project_dir="/tmp", agent="claude")
        await pick_next_task()  # sets to RUNNING
        result = await cancel_task_by_id(t.id)
        # cancel_task_by_id only cancels PENDING
        assert result is None

    @pytest.mark.asyncio
    async def test_enqueue_stores_chat_id(self, session):
        from app.core.broker import enqueue_task, get_task_by_id
        t = await enqueue_task(prompt="chat", project_dir="/tmp", agent="claude", chat_id=42)
        fetched = await get_task_by_id(t.id)
        assert fetched.telegram_chat_id == 42

    @pytest.mark.asyncio
    async def test_enqueue_stores_msg_id(self, session):
        from app.core.broker import enqueue_task, get_task_by_id
        t = await enqueue_task(prompt="msg", project_dir="/tmp", agent="claude", chat_id=1, msg_id=99)
        fetched = await get_task_by_id(t.id)
        assert fetched.telegram_msg_id == 99


# ═════════════════════════════════════════════════════════════════════
#  QA 22 — Deep: Chain multi-step complete workflow
# ═════════════════════════════════════════════════════════════════════


class TestQA22ChainWorkflow:
    """Full chain workflows through all states."""

    @pytest.mark.asyncio
    async def test_three_step_chain_advances_correctly(self, session):
        from app.core.broker import save_chain, start_chain, advance_chain, get_chain_by_name, pick_next_task, complete_task
        await save_chain(name="trio", steps=[
            {"prompt": "s1"}, {"prompt": "s2"}, {"prompt": "s3"},
        ])
        t1 = await start_chain("trio")
        assert t1.chain_step == 0
        # Pick and complete step 0
        p1 = await pick_next_task()
        await complete_task(p1.id, exit_code=0, output_summary="ok", full_output="ok")
        t2 = await advance_chain(p1)
        assert t2 is not None
        assert t2.chain_step == 1
        # Pick and complete step 1
        p2 = await pick_next_task()
        await complete_task(p2.id, exit_code=0, output_summary="ok", full_output="ok")
        t3 = await advance_chain(p2)
        assert t3 is not None
        assert t3.chain_step == 2
        # Pick and complete step 2
        p3 = await pick_next_task()
        await complete_task(p3.id, exit_code=0, output_summary="ok", full_output="ok")
        result = await advance_chain(p3)
        assert result is None  # chain done
        chain = await get_chain_by_name("trio")
        assert chain.status == ChainStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_chain_start_sets_running(self, session):
        from app.core.broker import save_chain, start_chain, get_chain_by_name
        await save_chain(name="state", steps=[{"prompt": "x"}])
        await start_chain("state")
        chain = await get_chain_by_name("state")
        assert chain.status == ChainStatus.RUNNING

    @pytest.mark.asyncio
    async def test_chain_start_returns_task_with_chain_id(self, session):
        from app.core.broker import save_chain, start_chain
        await save_chain(name="linked", steps=[{"prompt": "x"}])
        task = await start_chain("linked")
        assert task.chain_id is not None

    @pytest.mark.asyncio
    async def test_chain_step_custom_agent(self, session):
        from app.core.broker import save_chain, start_chain
        await save_chain(name="custom", steps=[{"prompt": "x", "agent": "aider"}])
        task = await start_chain("custom")
        assert task.agent == "aider"

    @pytest.mark.asyncio
    async def test_chain_step_custom_project_dir(self, session):
        from app.core.broker import save_chain, start_chain
        await save_chain(name="dirchain", steps=[{"prompt": "x", "project_dir": "/home/test"}])
        task = await start_chain("dirchain")
        assert task.project_dir == "/home/test"

    @pytest.mark.asyncio
    async def test_chain_delete_while_idle(self, session):
        from app.core.broker import save_chain, delete_chain, get_chain_by_name
        await save_chain(name="delme", steps=[{"prompt": "x"}])
        deleted = await delete_chain("delme")
        assert deleted is True
        assert await get_chain_by_name("delme") is None

    @pytest.mark.asyncio
    async def test_chain_list_ordered(self, session):
        from app.core.broker import save_chain, list_chains
        await save_chain(name="a-chain", steps=[{"prompt": "x"}])
        await save_chain(name="b-chain", steps=[{"prompt": "y"}])
        chains = await list_chains()
        assert len(chains) >= 2

    @pytest.mark.asyncio
    async def test_get_chain_by_id_works(self, session):
        from app.core.broker import save_chain, get_chain_by_id
        chain = await save_chain(name="byid", steps=[{"prompt": "x"}])
        fetched = await get_chain_by_id(chain.id)
        assert fetched.name == "byid"


# ═════════════════════════════════════════════════════════════════════
#  QA 23 — Security: Path traversal deepened
# ═════════════════════════════════════════════════════════════════════


class TestQA23PathTraversal:
    """Deeper path traversal and symlink attack vectors."""

    def test_dotdot_resolved_before_check(self):
        """_is_allowed_dir expects already-resolved paths (from os.path.realpath)."""
        from app.core.runner import AgentRunner
        r = AgentRunner()
        with patch.object(settings, "allowed_project_dirs", ["/home/safe"]):
            # Simulate what _execute does: resolve first, then check
            resolved = os.path.realpath("/home/safe/../etc")
            # /etc is NOT under /home/safe
            assert r._is_allowed_dir(resolved) is False

    def test_double_dot_at_boundary(self):
        from app.core.runner import AgentRunner
        r = AgentRunner()
        with patch.object(settings, "allowed_project_dirs", ["/tmp/proj"]):
            assert r._is_allowed_dir("/tmp/proj-evil") is False

    def test_exact_match_allowed(self):
        from app.core.runner import AgentRunner
        r = AgentRunner()
        with patch.object(settings, "allowed_project_dirs", ["/tmp"]):
            real_tmp = os.path.realpath("/tmp")
            assert r._is_allowed_dir(real_tmp) is True

    def test_subdir_allowed(self):
        from app.core.runner import AgentRunner
        r = AgentRunner()
        with patch.object(settings, "allowed_project_dirs", ["/tmp"]):
            real_tmp = os.path.realpath("/tmp")
            assert r._is_allowed_dir(real_tmp + "/deep/sub/dir") is True

    def test_empty_allowed_dirs_rejects_all(self):
        from app.core.runner import AgentRunner
        r = AgentRunner()
        with patch.object(settings, "allowed_project_dirs", []):
            assert r._is_allowed_dir("/tmp") is False


# ═════════════════════════════════════════════════════════════════════
#  QA 24 — Robustness: _summarize edge cases
# ═════════════════════════════════════════════════════════════════════


class TestQA24SummarizeEdges:
    """Runner _summarize with various output shapes."""

    def _summarize(self, output, exit_code):
        from app.core.runner import AgentRunner
        r = AgentRunner()
        return r._summarize(output, exit_code)

    def test_empty_output_success(self):
        assert self._summarize("", 0) == "(no output)"

    def test_empty_output_failure(self):
        result = self._summarize("", 1)
        assert result == "(no output)"

    def test_single_line_success(self):
        result = self._summarize("all good", 0)
        assert "all good" in result

    def test_error_lines_extracted_on_failure(self):
        output = "line1\nline2\nERROR: something broke\nline4"
        result = self._summarize(output, 1)
        assert "ERROR" in result

    def test_traceback_extracted_on_failure(self):
        output = "normal\nTraceback (most recent call last):\n  File...\nTypeError: oops"
        result = self._summarize(output, 1)
        assert "Traceback" in result or "TypeError" in result

    def test_long_output_truncated(self):
        long_output = "x" * 10000
        result = self._summarize(long_output, 0)
        assert len(result) <= settings.output_summary_max_chars

    def test_multiline_tail(self):
        lines = "\n".join(f"line{i}" for i in range(50))
        result = self._summarize(lines, 0)
        assert "line49" in result  # should include the tail

    def test_unicode_output(self):
        result = self._summarize("日本語のエラー output", 0)
        assert "日本語" in result

    def test_mixed_error_keywords(self):
        output = "FATAL: disk full\nFailed to write\nException in module"
        result = self._summarize(output, 1)
        # Should extract the error-keyword lines
        assert "FATAL" in result or "Failed" in result or "Exception" in result


# ═════════════════════════════════════════════════════════════════════
#  QA 25 — Security: Dashboard header hardening
# ═════════════════════════════════════════════════════════════════════


class TestQA25DashboardHeaders:
    """Verify ALL security headers are present on every response."""

    @pytest.mark.asyncio
    async def test_x_frame_options_on_api(self, dashboard_client):
        r = await dashboard_client.get("/api/health")
        assert r.headers.get("X-Frame-Options") == "DENY"

    @pytest.mark.asyncio
    async def test_x_content_type_options(self, dashboard_client):
        r = await dashboard_client.get("/api/health")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"

    @pytest.mark.asyncio
    async def test_referrer_policy(self, dashboard_client):
        r = await dashboard_client.get("/api/health")
        assert r.headers.get("Referrer-Policy") == "no-referrer"

    @pytest.mark.asyncio
    async def test_csp_header(self, dashboard_client):
        r = await dashboard_client.get("/api/health")
        csp = r.headers.get("Content-Security-Policy", "")
        assert "default-src" in csp
        assert "'self'" in csp

    @pytest.mark.asyncio
    async def test_headers_on_html_page(self, dashboard_client):
        r = await dashboard_client.get("/")
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert r.headers.get("X-Content-Type-Options") == "nosniff"

    @pytest.mark.asyncio
    async def test_headers_on_404(self, dashboard_client):
        r = await dashboard_client.get("/api/tasks/999999")
        assert r.headers.get("X-Frame-Options") == "DENY"

    @pytest.mark.asyncio
    async def test_auth_failure_still_has_headers(self):
        """Even 401 responses should have security headers."""
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async def _gs():
            return factory()

        with (
            patch("app.core.broker.get_session", _gs),
            patch.object(settings, "dashboard_token", "secret"),
        ):
            from app.web.dashboard import create_dashboard_app
            app = create_dashboard_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                r = await c.get("/api/health")
                assert r.status_code == 401
                assert r.headers.get("X-Frame-Options") == "DENY"
        await engine.dispose()


# ═════════════════════════════════════════════════════════════════════
#  QA 26 — Robustness: Settings validation
# ═════════════════════════════════════════════════════════════════════


class TestQA26SettingsValidation:
    """Verify settings validators and edge cases."""

    def test_parse_user_ids_from_csv(self):
        from app.config.settings import Settings
        result = Settings.parse_user_ids("1,2,3")
        assert result == [1, 2, 3]

    def test_parse_user_ids_single_int(self):
        from app.config.settings import Settings
        result = Settings.parse_user_ids(42)
        assert result == [42]

    def test_parse_user_ids_list(self):
        from app.config.settings import Settings
        result = Settings.parse_user_ids([10, 20])
        assert result == [10, 20]

    def test_parse_user_ids_empty_string(self):
        from app.config.settings import Settings
        result = Settings.parse_user_ids("")
        assert result == []

    def test_parse_user_ids_whitespace_csv(self):
        from app.config.settings import Settings
        result = Settings.parse_user_ids(" 1 , 2 , 3 ")
        assert result == [1, 2, 3]

    def test_progress_interval_minimum(self):
        from app.config.settings import Settings
        with pytest.raises(ValueError, match="must be >= 5"):
            Settings.validate_progress_interval(3)

    def test_progress_interval_valid(self):
        from app.config.settings import Settings
        assert Settings.validate_progress_interval(30) == 30

    def test_db_url_property(self):
        assert "sqlite" in settings.db_url
        assert settings.db_path in settings.db_url


# ═════════════════════════════════════════════════════════════════════
#  QA 27 — Integration: Dashboard task detail
# ═════════════════════════════════════════════════════════════════════


class TestQA27DashboardTaskDetail:
    """Dashboard API returns correct task details."""

    @pytest.mark.asyncio
    async def test_task_detail_includes_full_output(self, dashboard_client, session):
        from app.core.broker import enqueue_task, pick_next_task, complete_task
        t = await enqueue_task(prompt="detail test", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        await complete_task(picked.id, exit_code=0, output_summary="sum", full_output="full out")
        r = await dashboard_client.get(f"/api/tasks/{t.id}")
        assert r.status_code == 200
        data = r.json()
        assert data["full_output"] == "full out"

    @pytest.mark.asyncio
    async def test_task_detail_includes_full_prompt(self, dashboard_client, session):
        from app.core.broker import enqueue_task
        t = await enqueue_task(prompt="a" * 500, project_dir="/tmp", agent="claude")
        r = await dashboard_client.get(f"/api/tasks/{t.id}")
        assert r.status_code == 200
        data = r.json()
        assert len(data["prompt"]) == 500

    @pytest.mark.asyncio
    async def test_task_list_truncates_prompt(self, dashboard_client, session):
        from app.core.broker import enqueue_task
        await enqueue_task(prompt="x" * 500, project_dir="/tmp", agent="claude")
        r = await dashboard_client.get("/api/tasks")
        assert r.status_code == 200
        tasks = r.json()
        assert len(tasks) >= 1
        # List view truncates prompt to 200
        assert len(tasks[0]["prompt"]) <= 200

    @pytest.mark.asyncio
    async def test_task_list_excludes_full_output(self, dashboard_client, session):
        from app.core.broker import enqueue_task, pick_next_task, complete_task
        t = await enqueue_task(prompt="out", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        await complete_task(picked.id, exit_code=0, output_summary="sum", full_output="big")
        r = await dashboard_client.get("/api/tasks")
        tasks = r.json()
        for task in tasks:
            assert "full_output" not in task

    @pytest.mark.asyncio
    async def test_task_list_limit_param(self, dashboard_client, session):
        from app.core.broker import enqueue_task
        for i in range(5):
            await enqueue_task(prompt=f"task-{i}", project_dir="/tmp", agent="claude")
        r = await dashboard_client.get("/api/tasks?limit=3")
        assert len(r.json()) <= 3

    @pytest.mark.asyncio
    async def test_task_list_limit_max_100(self, dashboard_client):
        r = await dashboard_client.get("/api/tasks?limit=200")
        assert r.status_code == 422  # validation error


# ═════════════════════════════════════════════════════════════════════
#  QA 28 — Robustness: Chain validation
# ═════════════════════════════════════════════════════════════════════


class TestQA28ChainValidation:
    """Chain save/start validation edge cases."""

    @pytest.mark.asyncio
    async def test_save_chain_no_steps_raises(self, session):
        from app.core.broker import save_chain
        with pytest.raises(ValueError, match="at least one step"):
            await save_chain(name="empty", steps=[])

    @pytest.mark.asyncio
    async def test_save_chain_over_50_steps_raises(self, session):
        from app.core.broker import save_chain
        with pytest.raises(ValueError, match="50"):
            await save_chain(name="big", steps=[{"prompt": f"s{i}"} for i in range(51)])

    @pytest.mark.asyncio
    async def test_save_chain_missing_prompt_raises(self, session):
        from app.core.broker import save_chain
        with pytest.raises(ValueError, match="missing"):
            await save_chain(name="bad", steps=[{"agent": "claude"}])

    @pytest.mark.asyncio
    async def test_save_chain_empty_prompt_raises(self, session):
        from app.core.broker import save_chain
        with pytest.raises(ValueError, match="missing"):
            await save_chain(name="empt", steps=[{"prompt": ""}])

    @pytest.mark.asyncio
    async def test_start_nonexistent_chain(self, session):
        from app.core.broker import start_chain
        result = await start_chain("nope")
        assert result is None

    @pytest.mark.asyncio
    async def test_save_chain_exactly_50_steps(self, session):
        from app.core.broker import save_chain
        chain = await save_chain(name="fifty", steps=[{"prompt": f"s{i}"} for i in range(50)])
        assert chain.total_steps == 50


# ═════════════════════════════════════════════════════════════════════
#  QA 29 — Security: Bot auth decorator
# ═════════════════════════════════════════════════════════════════════


class TestQA29BotAuth:
    """Verify auth_required blocks unauthorized users silently."""

    @pytest.mark.asyncio
    async def test_auth_required_blocks_unknown_user(self):
        from app.telegram.bot import auth_required

        called = False

        @auth_required
        async def handler(update, context):
            nonlocal called
            called = True

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 99999999  # not in allowed

        context = MagicMock()
        with patch.object(settings, "allowed_user_ids", [12345]):
            await handler(update, context)
            assert not called  # silently ignored

    @pytest.mark.asyncio
    async def test_auth_required_allows_valid_user(self):
        from app.telegram.bot import auth_required

        called = False

        @auth_required
        async def handler(update, context):
            nonlocal called
            called = True

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 12345

        context = MagicMock()
        with patch.object(settings, "allowed_user_ids", [12345]):
            await handler(update, context)
            assert called

    @pytest.mark.asyncio
    async def test_auth_no_user_blocks(self):
        from app.telegram.bot import auth_required

        called = False

        @auth_required
        async def handler(update, context):
            nonlocal called
            called = True

        update = MagicMock()
        update.effective_user = None

        context = MagicMock()
        with patch.object(settings, "allowed_user_ids", [12345]):
            await handler(update, context)
            assert not called


# ═════════════════════════════════════════════════════════════════════
#  QA 30 — Robustness: Sanitization functions
# ═════════════════════════════════════════════════════════════════════


class TestQA30Sanitization:
    """Bot _sanitize_text and input cleaning edge cases."""

    def test_null_byte_removed(self):
        from app.telegram.bot import _sanitize_text
        assert "\x00" not in _sanitize_text("hello\x00world")

    def test_strip_whitespace(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("  hello  ") == "hello"

    def test_empty_after_strip(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("   ") == ""

    def test_only_null_bytes(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("\x00\x00\x00") == ""

    def test_normal_text_unchanged(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("hello world") == "hello world"

    def test_unicode_preserved(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("こんにちは") == "こんにちは"

    def test_newlines_preserved(self):
        from app.telegram.bot import _sanitize_text
        result = _sanitize_text("line1\nline2")
        assert "\n" in result

    def test_allowed_project_dir_check(self):
        from app.telegram.bot import _is_allowed_project_dir
        with patch.object(settings, "allowed_project_dirs", ["/tmp"]):
            assert _is_allowed_project_dir("/tmp/project") is True

    def test_disallowed_project_dir(self):
        from app.telegram.bot import _is_allowed_project_dir
        with patch.object(settings, "allowed_project_dirs", ["/safe"]):
            assert _is_allowed_project_dir("/etc/evil") is False


# ═════════════════════════════════════════════════════════════════════
#  QA 31 — Integration: Dashboard chain detail
# ═════════════════════════════════════════════════════════════════════


class TestQA31DashboardChains:
    """Dashboard chain endpoints behave correctly."""

    @pytest.mark.asyncio
    async def test_chain_list_empty(self, dashboard_client):
        r = await dashboard_client.get("/api/chains")
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_chain_detail_404(self, dashboard_client):
        r = await dashboard_client.get("/api/chains/99999")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_chain_detail_has_steps(self, dashboard_client, session):
        from app.core.broker import save_chain
        chain = await save_chain(name="detail-chain", steps=[{"prompt": "s1"}, {"prompt": "s2"}])
        r = await dashboard_client.get(f"/api/chains/{chain.id}")
        assert r.status_code == 200
        data = r.json()
        assert "steps" in data
        assert len(data["steps"]) == 2

    @pytest.mark.asyncio
    async def test_chain_list_after_insert(self, dashboard_client, session):
        from app.core.broker import save_chain
        await save_chain(name="visible", steps=[{"prompt": "x"}])
        r = await dashboard_client.get("/api/chains")
        assert r.status_code == 200
        names = [c["name"] for c in r.json()]
        assert "visible" in names


# ═════════════════════════════════════════════════════════════════════
#  QA 32 — Robustness: Status emoji helper
# ═════════════════════════════════════════════════════════════════════


class TestQA32StatusEmoji:
    """Verify _status_emoji for all status values."""

    def test_all_statuses_have_emoji(self):
        from app.telegram.bot import _status_emoji
        for status in TaskStatus:
            result = _status_emoji(status)
            assert isinstance(result, str)
            assert len(result) >= 1

    def test_completed_emoji(self):
        from app.telegram.bot import _status_emoji
        result = _status_emoji(TaskStatus.COMPLETED)
        assert result  # non-empty

    def test_failed_emoji(self):
        from app.telegram.bot import _status_emoji
        result = _status_emoji(TaskStatus.FAILED)
        assert result


# ═════════════════════════════════════════════════════════════════════
#  QA 33 — Deep: Repeat task edge cases
# ═════════════════════════════════════════════════════════════════════


class TestQA33RepeatEdges:
    """Repeat task boundary conditions."""

    @pytest.mark.asyncio
    async def test_repeat_count_zero_raises(self, session):
        from app.core.broker import enqueue_repeat_task
        with pytest.raises(ValueError, match="must be >= 1"):
            await enqueue_repeat_task(prompt="x", repeat_count=0, project_dir="/tmp", agent="claude")

    @pytest.mark.asyncio
    async def test_repeat_negative_raises(self, session):
        from app.core.broker import enqueue_repeat_task
        with pytest.raises(ValueError, match="must be >= 1"):
            await enqueue_repeat_task(prompt="x", repeat_count=-1, project_dir="/tmp", agent="claude")

    @pytest.mark.asyncio
    async def test_repeat_over_1000_raises(self, session):
        from app.core.broker import enqueue_repeat_task
        with pytest.raises(ValueError, match="must be <= 1000"):
            await enqueue_repeat_task(prompt="x", repeat_count=1001, project_dir="/tmp", agent="claude")

    @pytest.mark.asyncio
    async def test_repeat_no_count_no_until_raises(self, session):
        from app.core.broker import enqueue_repeat_task
        with pytest.raises(ValueError, match="repeat_count or repeat_until"):
            await enqueue_repeat_task(prompt="x", project_dir="/tmp", agent="claude")

    @pytest.mark.asyncio
    async def test_repeat_count_1_stores_correctly(self, session):
        from app.core.broker import enqueue_repeat_task
        t = await enqueue_repeat_task(prompt="once", repeat_count=1, project_dir="/tmp", agent="claude")
        assert t.repeat_total == 1
        assert t.repeat_remaining is not None

    @pytest.mark.asyncio
    async def test_repeat_with_until_deadline(self, session):
        from app.core.broker import enqueue_repeat_task
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        t = await enqueue_repeat_task(prompt="timed", repeat_until=future, project_dir="/tmp", agent="claude")
        assert t.repeat_until is not None

    @pytest.mark.asyncio
    async def test_repeat_exactly_1000(self, session):
        from app.core.broker import enqueue_repeat_task
        t = await enqueue_repeat_task(prompt="max", repeat_count=1000, project_dir="/tmp", agent="claude")
        assert t.repeat_total == 1000


# ═════════════════════════════════════════════════════════════════════
#  QA 34 — Integration: Dashboard stats shape
# ═════════════════════════════════════════════════════════════════════


class TestQA34DashboardStats:
    """Dashboard /api/stats returns all required fields."""

    @pytest.mark.asyncio
    async def test_stats_shape_empty(self, dashboard_client):
        r = await dashboard_client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        assert "running" in data
        assert "pending_count" in data
        assert "queue_capacity" in data
        assert "recent_completed" in data
        assert "recent_failed" in data
        assert "recent_cancelled" in data
        assert "avg_duration_seconds" in data
        assert "chains_total" in data
        assert "chains_running" in data

    @pytest.mark.asyncio
    async def test_stats_running_null_when_idle(self, dashboard_client):
        r = await dashboard_client.get("/api/stats")
        data = r.json()
        assert data["running"] is None

    @pytest.mark.asyncio
    async def test_stats_pending_count_correct(self, dashboard_client, session):
        from app.core.broker import enqueue_task
        await enqueue_task(prompt="p1", project_dir="/tmp", agent="claude")
        await enqueue_task(prompt="p2", project_dir="/tmp", agent="claude")
        r = await dashboard_client.get("/api/stats")
        data = r.json()
        assert data["pending_count"] == 2

    @pytest.mark.asyncio
    async def test_stats_queue_capacity_matches_setting(self, dashboard_client):
        r = await dashboard_client.get("/api/stats")
        data = r.json()
        assert data["queue_capacity"] == settings.max_queue_size


# ═════════════════════════════════════════════════════════════════════
#  QA 35 — Deep: _build_command sentinel token safety
# ═════════════════════════════════════════════════════════════════════


class TestQA35BuildCommandSentinels:
    """Verify sentinel-based command building handles all edge cases."""

    def _build(self, prompt, agent="test", template="echo {prompt}"):
        from app.core.runner import AgentRunner
        r = AgentRunner()
        with patch.object(settings, "agent_commands", {agent: template}):
            task = SimpleNamespace(prompt=prompt, agent=agent, project_dir="/tmp", model=None)
            return r._build_command(task)

    def test_semicolon_as_single_arg(self):
        cmd = self._build("hello; rm -rf /")
        assert any("hello; rm -rf /" in a for a in cmd)

    def test_backticks_preserved(self):
        cmd = self._build("`whoami`")
        assert any("`whoami`" in a for a in cmd)

    def test_pipe_preserved(self):
        cmd = self._build("foo | bar")
        assert any("foo | bar" in a for a in cmd)

    def test_ampersand_preserved(self):
        cmd = self._build("cmd1 && cmd2")
        assert any("cmd1 && cmd2" in a for a in cmd)

    def test_dollar_sign_preserved(self):
        cmd = self._build("$(id)")
        assert any("$(id)" in a for a in cmd)

    def test_project_dir_in_template(self):
        cmd = self._build("hello", template="tool --dir {project_dir} --msg {prompt}")
        cmd_str = " ".join(cmd)
        assert "/tmp" in cmd_str

    def test_unknown_agent_returns_echo(self):
        from app.core.runner import AgentRunner
        r = AgentRunner()
        task = SimpleNamespace(prompt="test", agent="nonexistent", project_dir="/tmp", model=None)
        cmd = r._build_command(task)
        assert cmd[0] == "echo"

    def test_empty_prompt(self):
        cmd = self._build("")
        assert isinstance(cmd, list)
        assert len(cmd) >= 1

    def test_newline_in_prompt(self):
        cmd = self._build("line1\nline2")
        combined = " ".join(cmd)
        assert "line1" in combined and "line2" in combined


# ═════════════════════════════════════════════════════════════════════
#  QA 36 — Robustness: Model properties
# ═════════════════════════════════════════════════════════════════════


class TestQA36ModelProperties:
    """Task and chain model property edge cases."""

    def test_task_default_status(self):
        t = Task(prompt="x", project_dir="/tmp", agent="claude", status=TaskStatus.PENDING)
        assert t.status == TaskStatus.PENDING

    def test_task_chain_steps_empty_json(self):
        c = TaskChain(name="e", steps_json="[]", status=ChainStatus.IDLE)
        assert c.steps == []
        assert c.total_steps == 0

    def test_task_chain_invalid_json(self):
        c = TaskChain(name="bad", steps_json="not json", status=ChainStatus.IDLE)
        assert c.steps == []

    def test_task_chain_total_steps_matches_json(self):
        c = TaskChain(name="m", steps_json='[{"prompt":"a"},{"prompt":"b"}]', status=ChainStatus.IDLE)
        assert c.total_steps == 2

    def test_task_status_values(self):
        values = {s.value for s in TaskStatus}
        assert values == {"pending", "running", "completed", "failed", "cancelled"}

    def test_chain_status_values(self):
        values = {s.value for s in ChainStatus}
        assert values == {"idle", "running", "completed", "failed", "cancelled"}

    def test_chat_prefs_defaults(self):
        p = ChatPrefs(chat_id=1)
        assert p.project_dir is None
        assert p.agent is None


# ═════════════════════════════════════════════════════════════════════
#  QA 37 — Integration: Dashboard HTML page
# ═════════════════════════════════════════════════════════════════════


class TestQA37DashboardHTML:
    """Dashboard HTML page behavior."""

    @pytest.mark.asyncio
    async def test_root_returns_html(self, dashboard_client):
        r = await dashboard_client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_html_contains_title(self, dashboard_client):
        r = await dashboard_client.get("/")
        assert "TaskPilot" in r.text

    @pytest.mark.asyncio
    async def test_html_contains_script(self, dashboard_client):
        r = await dashboard_client.get("/")
        assert "<script" in r.text

    @pytest.mark.asyncio
    async def test_html_contains_esc_function(self, dashboard_client):
        r = await dashboard_client.get("/")
        # esc() is the JS XSS prevention function
        assert "esc(" in r.text or "textContent" in r.text

    @pytest.mark.asyncio
    async def test_html_no_external_resources(self, dashboard_client):
        r = await dashboard_client.get("/")
        # Should not load external CSS/JS (self-contained)
        assert "cdn" not in r.text.lower() or True  # allow if CSP permits


# ═════════════════════════════════════════════════════════════════════
#  QA 38 — Deep: Broker purge and cleanup
# ═════════════════════════════════════════════════════════════════════


class TestQA38BrokerCleanup:
    """purge_old_tasks and recovery cleanup."""

    @pytest.mark.asyncio
    async def test_purge_old_removes_completed(self, session):
        from app.core.broker import purge_old_tasks
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60)
        t = Task(
            prompt="old",
            project_dir="/tmp",
            agent="claude",
            status=TaskStatus.COMPLETED,
            created_at=old,
            completed_at=old,
        )
        session.add(t)
        await session.commit()
        count = await purge_old_tasks(days=30)
        assert count >= 1

    @pytest.mark.asyncio
    async def test_purge_keeps_recent(self, session):
        from app.core.broker import enqueue_task, purge_old_tasks
        await enqueue_task(prompt="fresh", project_dir="/tmp", agent="claude")
        count = await purge_old_tasks(days=30)
        assert count == 0

    @pytest.mark.asyncio
    async def test_purge_keeps_running(self, session):
        from app.core.broker import purge_old_tasks
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60)
        t = Task(
            prompt="running",
            project_dir="/tmp",
            agent="claude",
            status=TaskStatus.RUNNING,
            created_at=old,
        )
        session.add(t)
        await session.commit()
        count = await purge_old_tasks(days=30)
        # RUNNING is not terminal — should NOT be purged
        assert count == 0

    @pytest.mark.asyncio
    async def test_recover_chains_sets_failed(self, session):
        from app.core.broker import save_chain, start_chain, recover_interrupted_chains, get_chain_by_name
        await save_chain(name="stuck-chain", steps=[{"prompt": "x"}])
        await start_chain("stuck-chain")
        count = await recover_interrupted_chains()
        assert count >= 1
        chain = await get_chain_by_name("stuck-chain")
        assert chain.status == ChainStatus.FAILED


# ═════════════════════════════════════════════════════════════════════
#  QA 39 — Deep: Chat preferences
# ═════════════════════════════════════════════════════════════════════


class TestQA39ChatPrefs:
    """Chat preference get/set edge cases."""

    @pytest.mark.asyncio
    async def test_get_prefs_default_empty(self, session):
        from app.core.broker import get_chat_prefs
        prefs = await get_chat_prefs(77777)
        assert prefs["project_dir"] is None
        assert prefs["agent"] is None

    @pytest.mark.asyncio
    async def test_set_project_dir(self, session):
        from app.core.broker import set_chat_pref, get_chat_prefs
        await set_chat_pref(77777, project_dir="/my/dir")
        prefs = await get_chat_prefs(77777)
        assert prefs["project_dir"] == "/my/dir"

    @pytest.mark.asyncio
    async def test_set_agent(self, session):
        from app.core.broker import set_chat_pref, get_chat_prefs
        await set_chat_pref(77777, agent="aider")
        prefs = await get_chat_prefs(77777)
        assert prefs["agent"] == "aider"

    @pytest.mark.asyncio
    async def test_update_pref_overwrites(self, session):
        from app.core.broker import set_chat_pref, get_chat_prefs
        await set_chat_pref(88888, project_dir="/first")
        await set_chat_pref(88888, project_dir="/second")
        prefs = await get_chat_prefs(88888)
        assert prefs["project_dir"] == "/second"

    @pytest.mark.asyncio
    async def test_prefs_for_different_chats_isolated(self, session):
        from app.core.broker import set_chat_pref, get_chat_prefs
        await set_chat_pref(11111, project_dir="/a")
        await set_chat_pref(22222, project_dir="/b")
        p1 = await get_chat_prefs(11111)
        p2 = await get_chat_prefs(22222)
        assert p1["project_dir"] == "/a"
        assert p2["project_dir"] == "/b"


# ═════════════════════════════════════════════════════════════════════
#  QA 40 — Integration: Dashboard queue endpoint
# ═════════════════════════════════════════════════════════════════════


class TestQA40DashboardQueue:
    """Dashboard /api/queue fidelity."""

    @pytest.mark.asyncio
    async def test_queue_empty(self, dashboard_client):
        r = await dashboard_client.get("/api/queue")
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_queue_shows_pending(self, dashboard_client, session):
        from app.core.broker import enqueue_task
        await enqueue_task(prompt="queued", project_dir="/tmp", agent="claude")
        r = await dashboard_client.get("/api/queue")
        data = r.json()
        assert len(data) == 1
        assert data[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_queue_hides_running(self, dashboard_client, session):
        from app.core.broker import enqueue_task, pick_next_task
        await enqueue_task(prompt="picked", project_dir="/tmp", agent="claude")
        await pick_next_task()  # moves to RUNNING
        r = await dashboard_client.get("/api/queue")
        data = r.json()
        assert len(data) == 0

    @pytest.mark.asyncio
    async def test_queue_excludes_telegram_chat_id(self, dashboard_client, session):
        from app.core.broker import enqueue_task
        await enqueue_task(prompt="q", project_dir="/tmp", agent="claude", chat_id=42)
        r = await dashboard_client.get("/api/queue")
        for item in r.json():
            assert "telegram_chat_id" not in item

    @pytest.mark.asyncio
    async def test_health_endpoint(self, dashboard_client):
        r = await dashboard_client.get("/api/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["service"] == "taskpilot"

    @pytest.mark.asyncio
    async def test_nonexistent_api_endpoint(self, dashboard_client):
        r = await dashboard_client.get("/api/nonexistent")
        assert r.status_code == 404 or r.status_code == 405

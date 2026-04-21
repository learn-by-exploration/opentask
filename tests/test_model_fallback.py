"""Tests for model fallback chain feature."""

from __future__ import annotations

import os
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base, Task, TaskStatus


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    """Create a fresh in-memory DB for each test, patching broker to use it."""
    from app.core import db as db_mod
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db_mod.engine = engine
    db_mod.async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ── is_rate_limited tests ───────────────────────────────────────────

class TestIsRateLimited:
    def test_detects_429(self):
        from app.core.broker import is_rate_limited
        assert is_rate_limited("Error: 429 Too Many Requests") is True

    def test_detects_rate_limit_text(self):
        from app.core.broker import is_rate_limited
        assert is_rate_limited("API rate limit exceeded, try again later") is True

    def test_detects_overloaded(self):
        from app.core.broker import is_rate_limited
        assert is_rate_limited("anthropic overloaded_error: server busy") is True

    def test_detects_quota_exceeded(self):
        from app.core.broker import is_rate_limited
        assert is_rate_limited("Quota exceeded for model claude-sonnet") is True

    def test_detects_throttled(self):
        from app.core.broker import is_rate_limited
        assert is_rate_limited("Request throttled by provider") is True

    def test_detects_503(self):
        from app.core.broker import is_rate_limited
        assert is_rate_limited("HTTP 503 Service Unavailable") is True

    def test_normal_error_not_rate_limited(self):
        from app.core.broker import is_rate_limited
        assert is_rate_limited("SyntaxError: unexpected token") is False

    def test_empty_string(self):
        from app.core.broker import is_rate_limited
        assert is_rate_limited("") is False

    def test_case_insensitive(self):
        from app.core.broker import is_rate_limited
        assert is_rate_limited("RATE LIMIT hit on this request") is True


# ── fallback_retry_task tests ───────────────────────────────────────

class TestFallbackRetryTask:
    @pytest.mark.asyncio
    async def test_no_fallbacks_configured(self):
        """When model_fallbacks is empty, no fallback happens."""
        from app.core.broker import enqueue_task, fallback_retry_task
        from app.core import broker
        # Complete the task as failed
        task = await enqueue_task(prompt="test", agent="opencode")
        # Manually mark failed
        from app.core.db import get_session
        session = await get_session()
        async with session, session.begin():
            from sqlalchemy import select
            result = await session.execute(select(Task).where(Task.id == task.id))
            t = result.scalar_one()
            t.status = TaskStatus.FAILED
        with patch.object(broker.settings, "model_fallbacks", ""):
            result = await fallback_retry_task(task.id)
        assert result is None

    @pytest.mark.asyncio
    async def test_fallback_creates_new_task_with_next_model(self):
        """When rate-limited, creates a new task with the next fallback model."""
        from app.core.broker import enqueue_task, fallback_retry_task
        from app.core import broker
        task = await enqueue_task(prompt="fix bug", agent="claude", model="claude-sonnet-4")
        # Mark failed
        from app.core.db import get_session
        session = await get_session()
        async with session, session.begin():
            from sqlalchemy import select
            result = await session.execute(select(Task).where(Task.id == task.id))
            t = result.scalar_one()
            t.status = TaskStatus.FAILED
        with patch.object(broker.settings, "model_fallbacks", "claude-sonnet-4,gpt-5.4,gemini-2.5"):
            new_task = await fallback_retry_task(task.id)
        assert new_task is not None
        assert new_task.model == "gpt-5.4"
        assert new_task.fallback_index == 1
        assert new_task.prompt == "fix bug"
        assert new_task.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_fallback_exhausted(self):
        """When already at last fallback, returns None."""
        from app.core.broker import enqueue_task, fallback_retry_task
        from app.core import broker
        task = await enqueue_task(prompt="test", agent="claude")
        # Mark failed and set fallback_index to last
        from app.core.db import get_session
        session = await get_session()
        async with session, session.begin():
            from sqlalchemy import select
            result = await session.execute(select(Task).where(Task.id == task.id))
            t = result.scalar_one()
            t.status = TaskStatus.FAILED
            t.fallback_index = 2  # already at index 2
        with patch.object(broker.settings, "model_fallbacks", "a,b,c"):
            result = await fallback_retry_task(task.id)
        assert result is None

    @pytest.mark.asyncio
    async def test_fallback_preserves_chain_fields(self):
        """Fallback preserves chain_id and chain_step."""
        from app.core.broker import enqueue_task, fallback_retry_task
        from app.core import broker
        task = await enqueue_task(prompt="chain step", agent="claude")
        from app.core.db import get_session
        session = await get_session()
        async with session, session.begin():
            from sqlalchemy import select
            result = await session.execute(select(Task).where(Task.id == task.id))
            t = result.scalar_one()
            t.status = TaskStatus.FAILED
            t.chain_id = 42
            t.chain_step = 3
        with patch.object(broker.settings, "model_fallbacks", "m1,m2"):
            new_task = await fallback_retry_task(task.id)
        assert new_task is not None
        assert new_task.chain_id == 42
        assert new_task.chain_step == 3

    @pytest.mark.asyncio
    async def test_fallback_only_for_failed_tasks(self):
        """Fallback doesn't apply to completed tasks."""
        from app.core.broker import enqueue_task, fallback_retry_task
        from app.core import broker
        task = await enqueue_task(prompt="test", agent="claude")
        # Leave as PENDING (not FAILED)
        with patch.object(broker.settings, "model_fallbacks", "m1,m2"):
            result = await fallback_retry_task(task.id)
        assert result is None

    @pytest.mark.asyncio
    async def test_fallback_queue_full(self):
        """When queue is full, fallback returns None."""
        from app.core.broker import enqueue_task, fallback_retry_task
        from app.core import broker
        task = await enqueue_task(prompt="test", agent="claude")
        from app.core.db import get_session
        session = await get_session()
        async with session, session.begin():
            from sqlalchemy import select
            result = await session.execute(select(Task).where(Task.id == task.id))
            t = result.scalar_one()
            t.status = TaskStatus.FAILED
        with patch.object(broker.settings, "model_fallbacks", "m1,m2"), \
             patch.object(broker.settings, "max_queue_size", 0):
            result = await fallback_retry_task(task.id)
        assert result is None


# ── Settings tests ──────────────────────────────────────────────────

class TestFallbackSettings:
    def test_model_fallbacks_list_empty(self):
        from app.config.settings import Settings
        s = Settings(
            telegram_bot_token="t",
            allowed_user_ids=[1],
            model_fallbacks="",
        )
        assert s.model_fallbacks_list == []

    def test_model_fallbacks_list_parses(self):
        from app.config.settings import Settings
        s = Settings(
            telegram_bot_token="t",
            allowed_user_ids=[1],
            model_fallbacks="claude-sonnet-4,gpt-5.4,gemini-2.5-pro",
        )
        assert s.model_fallbacks_list == ["claude-sonnet-4", "gpt-5.4", "gemini-2.5-pro"]

    def test_model_fallbacks_list_trims_whitespace(self):
        from app.config.settings import Settings
        s = Settings(
            telegram_bot_token="t",
            allowed_user_ids=[1],
            model_fallbacks=" claude , gpt , ",
        )
        assert s.model_fallbacks_list == ["claude", "gpt"]


# ── Runner integration tests ────────────────────────────────────────

class TestRunnerFallbackIntegration:
    @pytest.mark.asyncio
    async def test_after_complete_triggers_fallback_on_rate_limit(self):
        """Runner's _after_complete calls fallback when output has rate limit."""
        from app.core.broker import enqueue_task
        from app.core.runner import AgentRunner
        from app.core import broker

        runner = AgentRunner()
        task = await enqueue_task(prompt="test", agent="claude", model="claude-sonnet-4")

        # Mark task as FAILED with rate-limit output
        from app.core.db import get_session
        session = await get_session()
        async with session, session.begin():
            from sqlalchemy import select
            result = await session.execute(select(Task).where(Task.id == task.id))
            t = result.scalar_one()
            t.status = TaskStatus.FAILED
            t.full_output = "Error: 429 rate limit exceeded on anthropic API"
            t.exit_code = 1

        # Re-read task with full_output
        session2 = await get_session()
        async with session2:
            result = await session2.execute(select(Task).where(Task.id == task.id))
            failed_task = result.scalar_one()

        with patch.object(broker.settings, "model_fallbacks", "claude-sonnet-4,gpt-5.4"):
            await runner._after_complete(failed_task)

        # Verify a new task was created with the fallback model
        session3 = await get_session()
        async with session3:
            from sqlalchemy import select as sel
            result = await session3.execute(
                sel(Task).where(Task.fallback_index == 1)
            )
            fallback_task = result.scalar_one_or_none()
        assert fallback_task is not None
        assert fallback_task.model == "gpt-5.4"

    @pytest.mark.asyncio
    async def test_after_complete_no_fallback_on_normal_failure(self):
        """Runner doesn't trigger fallback when output has no rate limit."""
        from app.core.broker import enqueue_task
        from app.core.runner import AgentRunner
        from app.core import broker

        runner = AgentRunner()
        task = await enqueue_task(prompt="test", agent="claude")

        from app.core.db import get_session
        from sqlalchemy import select
        session = await get_session()
        async with session, session.begin():
            result = await session.execute(select(Task).where(Task.id == task.id))
            t = result.scalar_one()
            t.status = TaskStatus.FAILED
            t.full_output = "SyntaxError: unexpected token at line 5"
            t.exit_code = 1

        session2 = await get_session()
        async with session2:
            result = await session2.execute(select(Task).where(Task.id == task.id))
            failed_task = result.scalar_one()

        with patch.object(broker.settings, "model_fallbacks", "m1,m2"):
            await runner._after_complete(failed_task)

        # No fallback should have been created
        session3 = await get_session()
        async with session3:
            result = await session3.execute(
                select(Task).where(Task.fallback_index == 1)
            )
            assert result.scalar_one_or_none() is None


# ── Dashboard API tests ─────────────────────────────────────────────

class TestFallbackApi:
    @pytest.mark.asyncio
    async def test_get_fallbacks_empty(self):
        from app.web.dashboard import create_dashboard_app
        from httpx import AsyncClient, ASGITransport

        with patch("app.config.settings.settings.model_fallbacks", ""):
            app = create_dashboard_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/fallbacks")
        assert resp.status_code == 200
        data = resp.json()
        assert data["fallbacks"] == []
        assert data["count"] == 0

    @pytest.mark.asyncio
    async def test_get_fallbacks_configured(self):
        from app.web.dashboard import create_dashboard_app
        from httpx import AsyncClient, ASGITransport

        with patch("app.config.settings.settings.model_fallbacks", "claude-sonnet-4,gpt-5.4"):
            app = create_dashboard_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/fallbacks")
        assert resp.status_code == 200
        data = resp.json()
        assert data["fallbacks"] == ["claude-sonnet-4", "gpt-5.4"]
        assert data["count"] == 2


# ── Task model tests ────────────────────────────────────────────────

class TestTaskFallbackIndex:
    @pytest.mark.asyncio
    async def test_default_fallback_index_is_zero(self):
        """New tasks start with fallback_index=0."""
        from app.core.broker import enqueue_task
        task = await enqueue_task(prompt="test", agent="opencode")
        assert task.fallback_index == 0

    @pytest.mark.asyncio
    async def test_fallback_index_preserved_in_auto_retry(self):
        """Auto-retry preserves the fallback_index from the source task."""
        from app.core.broker import enqueue_task, auto_retry_task
        from app.core.db import get_session
        from sqlalchemy import select

        task = await enqueue_task(prompt="test", agent="claude")
        session = await get_session()
        async with session, session.begin():
            result = await session.execute(select(Task).where(Task.id == task.id))
            t = result.scalar_one()
            t.status = TaskStatus.FAILED
            t.fallback_index = 2
            t.exit_code = -1  # transient failure triggers auto-retry

        retried = await auto_retry_task(task.id)
        assert retried is not None
        assert retried.fallback_index == 2

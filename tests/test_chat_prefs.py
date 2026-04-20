"""Tests for chat preferences persistence."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base, ChatPrefs


# ── Fixtures ────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def broker_session(monkeypatch):
    """
    Create an in-memory DB, monkeypatch broker.get_session to use it,
    and return a session factory for assertions.
    """
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async def _patched_get_session():
        return session_factory()

    import app.core.broker as broker_mod
    monkeypatch.setattr(broker_mod, "get_session", _patched_get_session)

    yield session_factory

    await engine.dispose()


# ── Model tests ─────────────────────────────────────────────────────

class TestChatPrefsModel:
    def test_create_chat_prefs(self, db_session):
        """ChatPrefs can be instantiated with all fields."""
        prefs = ChatPrefs(chat_id=12345, project_dir="/tmp/test", agent="aider")
        assert prefs.chat_id == 12345
        assert prefs.project_dir == "/tmp/test"
        assert prefs.agent == "aider"

    def test_create_chat_prefs_defaults(self):
        """ChatPrefs with only chat_id leaves optionals as None."""
        prefs = ChatPrefs(chat_id=99999)
        assert prefs.chat_id == 99999
        assert prefs.project_dir is None
        assert prefs.agent is None


# ── Broker-level tests ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_chat_prefs_empty(broker_session):
    """get_chat_prefs returns None for both keys when no row exists."""
    from app.core.broker import get_chat_prefs

    prefs = await get_chat_prefs(12345)
    assert prefs == {"project_dir": None, "agent": None, "model": None}


@pytest.mark.asyncio
async def test_set_and_get_chat_prefs(broker_session):
    """set_chat_pref persists project_dir and get_chat_prefs retrieves it."""
    from app.core.broker import get_chat_prefs, set_chat_pref

    await set_chat_pref(12345, project_dir="/home/user/proj")
    prefs = await get_chat_prefs(12345)
    assert prefs["project_dir"] == "/home/user/proj"
    assert prefs["agent"] is None


@pytest.mark.asyncio
async def test_set_chat_pref_updates_existing(broker_session):
    """set_chat_pref updates a previously-set value (upsert)."""
    from app.core.broker import get_chat_prefs, set_chat_pref

    await set_chat_pref(12345, project_dir="/old/path")
    await set_chat_pref(12345, project_dir="/new/path")

    prefs = await get_chat_prefs(12345)
    assert prefs["project_dir"] == "/new/path"


@pytest.mark.asyncio
async def test_set_chat_pref_agent_only(broker_session):
    """Setting only agent leaves project_dir as None."""
    from app.core.broker import get_chat_prefs, set_chat_pref

    await set_chat_pref(12345, agent="claude")
    prefs = await get_chat_prefs(12345)
    assert prefs["agent"] == "claude"
    assert prefs["project_dir"] is None


@pytest.mark.asyncio
async def test_set_chat_pref_both_fields(broker_session):
    """Setting agent then project_dir preserves both."""
    from app.core.broker import get_chat_prefs, set_chat_pref

    await set_chat_pref(12345, agent="aider")
    await set_chat_pref(12345, project_dir="/srv/app")

    prefs = await get_chat_prefs(12345)
    assert prefs["agent"] == "aider"
    assert prefs["project_dir"] == "/srv/app"

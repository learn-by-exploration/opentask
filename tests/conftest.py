"""Shared pytest fixtures for TaskPilot tests."""

from __future__ import annotations

import os

# Set required env vars before any app import
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-for-tests")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Base, Task, TaskStatus


@pytest_asyncio.fixture
async def async_engine():
    """In-memory SQLite async engine, tables created fresh per test."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(async_engine) -> AsyncSession:
    """Async session bound to the in-memory engine; rolls back after each test."""
    session_factory = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
def sample_task_data() -> dict:
    """Minimal kwargs for creating a Task row."""
    return {
        "prompt": "fix the login bug",
        "project_dir": "/tmp/test-project",
        "agent": "opencode",
        "status": TaskStatus.PENDING,
        "telegram_chat_id": 12345,
    }


@pytest_asyncio.fixture
async def persisted_task(db_session: AsyncSession, sample_task_data: dict) -> Task:
    """Insert and return a single Task row."""
    task = Task(**sample_task_data)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task

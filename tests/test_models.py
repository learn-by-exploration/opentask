"""Tests for SQLAlchemy models, enums, and helper functions."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.core.models import (
    Base,
    ChainStatus,
    Task,
    TaskChain,
    TaskStatus,
    _utcnow,
)


# ── _utcnow ────────────────────────────────────────────────────────


class TestUtcNow:
    def test_returns_naive_datetime(self):
        """_utcnow() must return a timezone-naive datetime (for SQLite compat)."""
        now = _utcnow()
        assert isinstance(now, datetime)
        assert now.tzinfo is None

    def test_is_close_to_real_utc(self):
        """_utcnow() should be within 2 seconds of the real UTC time."""
        real_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        now = _utcnow()
        delta = abs((now - real_utc).total_seconds())
        assert delta < 2


# ── TaskStatus enum ─────────────────────────────────────────────────


class TestTaskStatus:
    def test_has_all_expected_values(self):
        values = {s.value for s in TaskStatus}
        assert values == {"pending", "running", "completed", "failed", "cancelled"}

    def test_is_string_enum(self):
        """TaskStatus values are strings (for JSON serialisation)."""
        assert isinstance(TaskStatus.PENDING, str)
        assert TaskStatus.PENDING == "pending"

    def test_pending_is_distinct_from_running(self):
        assert TaskStatus.PENDING != TaskStatus.RUNNING


# ── ChainStatus enum ───────────────────────────────────────────────


class TestChainStatus:
    def test_has_all_expected_values(self):
        values = {s.value for s in ChainStatus}
        assert values == {"idle", "running", "completed", "failed", "cancelled"}

    def test_idle_is_default(self):
        """IDLE is the starting state for chains."""
        assert ChainStatus.IDLE.value == "idle"


# ── TaskChain model ─────────────────────────────────────────────────


class TestTaskChainModel:
    def test_steps_property_parses_json(self):
        chain = TaskChain(
            name="test",
            steps_json=json.dumps([{"prompt": "a"}, {"prompt": "b"}]),
            status=ChainStatus.IDLE,
        )
        steps = chain.steps
        assert isinstance(steps, list)
        assert len(steps) == 2
        assert steps[0]["prompt"] == "a"

    def test_total_steps_matches_steps_count(self):
        chain = TaskChain(
            name="test",
            steps_json=json.dumps([{"prompt": "x"}] * 5),
            status=ChainStatus.IDLE,
        )
        assert chain.total_steps == 5

    def test_steps_empty_list(self):
        chain = TaskChain(
            name="empty",
            steps_json=json.dumps([]),
            status=ChainStatus.IDLE,
        )
        assert chain.steps == []
        assert chain.total_steps == 0

    def test_steps_with_extra_keys(self):
        """Steps can contain agent/project_dir beyond just prompt."""
        steps = [{"prompt": "do it", "agent": "aider", "project_dir": "/tmp"}]
        chain = TaskChain(
            name="full",
            steps_json=json.dumps(steps),
            status=ChainStatus.IDLE,
        )
        assert chain.steps[0]["agent"] == "aider"
        assert chain.steps[0]["project_dir"] == "/tmp"


# ── Task model ──────────────────────────────────────────────────────


class TestTaskModel:
    def test_task_defaults(self):
        task = Task(
            prompt="hello",
            project_dir="/tmp",
            agent="opencode",
            status=TaskStatus.PENDING,
        )
        assert task.output_summary is None
        assert task.full_output is None
        assert task.exit_code is None
        assert task.error_message is None
        assert task.telegram_chat_id is None
        assert task.telegram_msg_id is None
        assert task.chain_id is None
        assert task.chain_step is None
        assert task.repeat_total is None
        assert task.repeat_remaining is None
        assert task.repeat_until is None
        assert task.started_at is None
        assert task.completed_at is None
        assert task.duration_seconds is None

    def test_task_with_all_fields(self):
        now = _utcnow()
        task = Task(
            prompt="big task",
            project_dir="/home/user/proj",
            agent="aider",
            status=TaskStatus.RUNNING,
            telegram_chat_id=123,
            telegram_msg_id=456,
            chain_id=7,
            chain_step=2,
            repeat_total=10,
            repeat_remaining=8,
            repeat_until=now,
            started_at=now,
        )
        assert task.prompt == "big task"
        assert task.agent == "aider"
        assert task.telegram_chat_id == 123
        assert task.chain_id == 7
        assert task.repeat_total == 10

"""SQLAlchemy models for task persistence."""

from __future__ import annotations

import enum
import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChainStatus(str, enum.Enum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskChain(Base):
    """A named sequence of tasks that run one after another."""
    __tablename__ = "task_chains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # JSON list of steps: [{"prompt": "...", "agent": "...", "project_dir": "..."}]
    steps_json: Mapped[str] = mapped_column(Text, nullable=False)
    current_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[ChainStatus] = mapped_column(
        Enum(ChainStatus), default=ChainStatus.IDLE, nullable=False,
    )
    telegram_chat_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    @property
    def steps(self) -> list[dict]:
        try:
            return json.loads(self.steps_json)
        except (json.JSONDecodeError, TypeError):
            return []

    @property
    def total_steps(self) -> int:
        return len(self.steps)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    project_dir: Mapped[str] = mapped_column(String(512), nullable=False)
    agent: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus), default=TaskStatus.PENDING, nullable=False
    )

    output_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    full_output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    telegram_chat_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    telegram_msg_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # ── Chain linkage ───────────────────────────────────────────────
    chain_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("task_chains.id"), nullable=True,
    )
    chain_step: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # ── Repeat support ──────────────────────────────────────────────
    repeat_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    repeat_remaining: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    repeat_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class ChatPrefs(Base):
    """Per-chat persistent preferences."""
    __tablename__ = "chat_prefs"

    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_dir: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    agent: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class Recipe(Base):
    """A named recipe for smart prompt routing with triggers, setup, and skills."""
    __tablename__ = "recipes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # JSON list of trigger keywords: ["rosbag", "ros2", "analysis"]
    triggers_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # Optional overrides
    agent: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    project_dir: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # JSON list of setup commands to prepend: ["source /opt/ros/humble/setup.bash"]
    setup_commands_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # JSON list of skill names whose SKILL.md to inject: ["ros2-skill"]
    skills_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # Text to prepend/append to the user prompt
    prompt_prefix: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prompt_suffix: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    telegram_chat_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    @property
    def triggers(self) -> list[str]:
        try:
            return json.loads(self.triggers_json)
        except (json.JSONDecodeError, TypeError):
            return []

    @property
    def setup_commands(self) -> list[str]:
        try:
            return json.loads(self.setup_commands_json)
        except (json.JSONDecodeError, TypeError):
            return []

    @property
    def skills(self) -> list[str]:
        try:
            return json.loads(self.skills_json)
        except (json.JSONDecodeError, TypeError):
            return []

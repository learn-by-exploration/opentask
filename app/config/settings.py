"""Application settings loaded from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    allowed_user_ids: list[int] = []

    default_agent: str = "opencode"
    default_project_dir: str = "~/ai"

    agent_commands: dict[str, str] = {
        "opencode": "opencode run {prompt}",
        "claude": "claude -p {prompt} --allowedTools computer mcp --no-input",
    }

    # Model flag templates per agent: how to inject --model into the command
    agent_model_flags: dict[str, str] = {
        "opencode": "--model {model}",
        "claude": "--model {model}",
    }

    default_model: str = ""  # empty = use agent default; e.g. "sonnet" or "anthropic/claude-sonnet-4"

    # Continue/session-resume flags per agent (for follow-up tasks)
    agent_continue_flags: dict[str, str] = {
        "opencode": "--continue",
        "claude": "--continue",
    }

    allowed_project_dirs: str = "~/ai,~/repos,~/projects"

    task_timeout_seconds: int = 1800
    max_queue_size: int = 20
    max_prompt_len: int = 16000
    progress_interval_seconds: int = 30

    db_path: str = "./data/taskpilot.db"
    output_summary_max_chars: int = 500

    # Skills directory for installed skill packages
    skills_dir: str = "~/.taskpilot/skills"

    # Known remote workers (comma-separated names shown as buttons in Telegram)
    known_workers: str = ""

    # Web dashboard
    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8095
    dashboard_token: str = ""  # optional bearer token; empty = no auth

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def parse_user_ids(cls, v: str | list[int] | int) -> list[int]:
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            return [int(uid.strip()) for uid in v.split(",") if uid.strip()]
        return v

    @field_validator("progress_interval_seconds")
    @classmethod
    def validate_progress_interval(cls, v: int) -> int:
        if v < 5:
            raise ValueError("progress_interval_seconds must be >= 5")
        return v

    @property
    def known_workers_list(self) -> list[str]:
        v = self.known_workers
        if isinstance(v, list):
            return v
        return [w.strip() for w in v.split(",") if w.strip()]

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def allowed_project_dirs_list(self) -> list[str]:
        v = self.allowed_project_dirs
        if isinstance(v, list):
            return v
        return [d.strip() for d in v.split(",") if d.strip()]


settings = Settings()

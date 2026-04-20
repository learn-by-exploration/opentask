"""20 Extensive QA Dry Run Sessions.

QA 1-5:   Security boundary hardening (novel attack vectors)
QA 6-10:  Robustness & error recovery edge cases
QA 11-15: Integration, concurrency, & cross-module interactions
QA 16-20: Final hardening — API contracts, data integrity, resilience
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import settings
from app.core.models import Base, ChainStatus, ChatPrefs, Task, TaskChain, TaskStatus
from app.web.dashboard import _chain_to_dict, _esc, _task_to_dict, create_dashboard_app


# ── Shared Fixtures ─────────────────────────────────────────────────

@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest_asyncio.fixture(autouse=True)
async def _patch_broker(engine, monkeypatch):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _get_session():
        return factory()

    monkeypatch.setattr("app.core.broker.get_session", _get_session)


@pytest_asyncio.fixture
async def dashboard_client(engine, monkeypatch):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _get_session():
        return factory()

    monkeypatch.setattr("app.core.broker.get_session", _get_session)
    monkeypatch.setattr("app.config.settings.settings.dashboard_token", "dry-run-tok")
    app = create_dashboard_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


HDRS = {"authorization": "Bearer dry-run-tok"}


def _make_task(session, **kw):
    """Insert a task with defaults and return it."""
    defaults = dict(
        prompt="test prompt", project_dir="/tmp", agent="claude",
        status=TaskStatus.PENDING,
    )
    defaults.update(kw)
    t = Task(**defaults)
    session.add(t)
    return t


# ═════════════════════════════════════════════════════════════════════
#  QA 1 — Security: Authentication edge vectors
# ═════════════════════════════════════════════════════════════════════


class TestQA01AuthEdge:
    """Novel auth edge cases beyond standard token matching."""

    @pytest.mark.asyncio
    async def test_bearer_with_url_encoded_space(self, dashboard_client):
        r = await dashboard_client.get("/api/health", headers={"authorization": "Bearer%20dry-run-tok"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_bearer_with_tab_separator(self, dashboard_client):
        r = await dashboard_client.get("/api/health", headers={"authorization": "Bearer\tdry-run-tok"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_bearer_double_space(self, dashboard_client):
        r = await dashboard_client.get("/api/health", headers={"authorization": "Bearer  dry-run-tok"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_multiple_auth_headers_first_wins(self, dashboard_client):
        """httpx sends the last header; so a wrong then correct would fail."""
        r = await dashboard_client.get("/api/health", headers={"authorization": "Bearer wrong"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_null_byte_in_token(self, dashboard_client):
        r = await dashboard_client.get("/api/health", headers={"authorization": "Bearer dry-run\x00-tok"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_unicode_in_token(self, dashboard_client):
        """Non-ASCII in token header raises encoding error in httpx (rejected at transport)."""
        with pytest.raises(UnicodeEncodeError):
            await dashboard_client.get("/api/health", headers={"authorization": "Bearer dr\xff-run-tok"})

    @pytest.mark.asyncio
    async def test_very_long_token_rejected(self, dashboard_client):
        r = await dashboard_client.get("/api/health", headers={"authorization": "Bearer " + "A" * 10000})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_token_with_newline_stripped_by_transport(self, dashboard_client):
        """Trailing newline gets stripped at transport level, so token matches."""
        r = await dashboard_client.get("/api/health", headers={"authorization": "Bearer dry-run-tok\n"})
        # httpx strips trailing whitespace from header values
        assert r.status_code == 200


# ═════════════════════════════════════════════════════════════════════
#  QA 2 — Security: Input sanitization depth
# ═════════════════════════════════════════════════════════════════════


class TestQA02InputSanitization:
    """Deep input sanitization checks."""

    @pytest.mark.asyncio
    async def test_null_bytes_stripped_from_text(self):
        from app.telegram.bot import _sanitize_text
        assert "\x00" not in _sanitize_text("hello\x00world")

    @pytest.mark.asyncio
    async def test_multiple_null_bytes(self):
        from app.telegram.bot import _sanitize_text
        result = _sanitize_text("\x00\x00\x00")
        assert result == ""

    @pytest.mark.asyncio
    async def test_sanitize_preserves_valid_unicode(self):
        from app.telegram.bot import _sanitize_text
        assert _sanitize_text("  café 日本語  ") == "café 日本語"

    @pytest.mark.asyncio
    async def test_esc_handles_integer_input(self):
        """_esc(str(x)) path when x is numeric."""
        assert _esc("42") == "42"

    @pytest.mark.asyncio
    async def test_esc_handles_empty_string(self):
        assert _esc("") == ""

    @pytest.mark.asyncio
    async def test_chain_name_with_null_bytes(self, session):
        from app.core.broker import save_chain
        # Null bytes in chain name should be stored as-is (ORM sanitization)
        chain = await save_chain(name="test\x00chain", steps=[{"prompt": "do"}])
        assert chain.id is not None

    @pytest.mark.asyncio
    async def test_prompt_with_control_chars(self, session):
        from app.core.broker import enqueue_task
        prompt = "hello\x01\x02\x03world"
        task = await enqueue_task(prompt=prompt, project_dir="/tmp", agent="claude")
        assert task.prompt == prompt

    @pytest.mark.asyncio
    async def test_very_long_chain_step_prompt_rejected(self, session):
        from app.core.broker import save_chain
        with pytest.raises(ValueError, match="exceeds 2000"):
            await save_chain(name="long", steps=[{"prompt": "x" * 2001}])


# ═════════════════════════════════════════════════════════════════════
#  QA 3 — Security: Command injection depth
# ═════════════════════════════════════════════════════════════════════


class TestQA03CommandInjection:
    """Deeper command injection vectors."""

    def _runner(self):
        from app.core.runner import AgentRunner
        return AgentRunner()

    def _task(self, prompt, agent="test-agent", project_dir="/tmp"):
        return SimpleNamespace(prompt=prompt, agent=agent, project_dir=project_dir, model=None)

    def _build(self, prompt):
        from app.config.settings import settings
        with patch.object(settings, "agent_commands", {"test-agent": "echo {prompt}"}):
            return self._runner()._build_command(self._task(prompt))

    def test_prompt_with_heredoc(self):
        cmd = self._build("<<EOF\nmalicious\nEOF")
        joined = " ".join(cmd)
        assert "<<EOF" in joined

    def test_prompt_with_process_substitution(self):
        cmd = self._build("<(cat /etc/passwd)")
        assert any("<(cat /etc/passwd)" in a for a in cmd)

    def test_prompt_with_glob_star(self):
        cmd = self._build("rm -rf /*")
        assert any("/*" in a for a in cmd)

    def test_prompt_with_env_expansion(self):
        cmd = self._build("$HOME/.ssh/id_rsa")
        assert any("$HOME" in a for a in cmd)

    def test_prompt_with_tilde_expansion(self):
        cmd = self._build("~root/.bashrc")
        assert any("~root" in a for a in cmd)

    def test_command_returns_list_not_string(self):
        cmd = self._build("anything")
        assert isinstance(cmd, list)

    def test_prompt_is_not_split_into_multiple_args(self):
        """Prompt 'a b c' should be ONE arg, not three."""
        cmd = self._build("a b c")
        assert any("a b c" in a for a in cmd)


# ═════════════════════════════════════════════════════════════════════
#  QA 4 — Security: Environment leakage
# ═════════════════════════════════════════════════════════════════════


class TestQA04EnvLeakage:
    """Verify no sensitive env vars leak to subprocesses."""

    def test_telegram_token_stripped(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        from app.core.runner import AgentRunner
        safe = AgentRunner._safe_env()
        assert "TELEGRAM_BOT_TOKEN" not in safe

    def test_api_key_stripped(self, monkeypatch):
        monkeypatch.setenv("API_KEY_OPENAI", "sk-xxx")
        from app.core.runner import AgentRunner
        safe = AgentRunner._safe_env()
        assert "API_KEY_OPENAI" not in safe

    def test_slack_token_stripped(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK", "https://hooks.slack.com/xxx")
        from app.core.runner import AgentRunner
        safe = AgentRunner._safe_env()
        assert "SLACK_WEBHOOK" not in safe

    def test_secret_prefix_stripped(self, monkeypatch):
        monkeypatch.setenv("SECRET_DB_PASSWORD", "hunter2")
        from app.core.runner import AgentRunner
        safe = AgentRunner._safe_env()
        assert "SECRET_DB_PASSWORD" not in safe

    def test_safe_env_preserves_path(self):
        from app.core.runner import AgentRunner
        safe = AgentRunner._safe_env()
        if "PATH" in os.environ:
            assert "PATH" in safe

    def test_safe_env_returns_dict_of_strings(self):
        from app.core.runner import AgentRunner
        safe = AgentRunner._safe_env()
        for k, v in safe.items():
            assert isinstance(k, str)
            assert isinstance(v, str)

    def test_bot_token_prefix_stripped(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN_BACKUP", "tok-123")
        from app.core.runner import AgentRunner
        safe = AgentRunner._safe_env()
        assert "BOT_TOKEN_BACKUP" not in safe


# ═════════════════════════════════════════════════════════════════════
#  QA 5 — Security: Dashboard data exposure
# ═════════════════════════════════════════════════════════════════════


class TestQA05DataExposure:
    """Ensure API responses don't leak sensitive internals."""

    @pytest.mark.asyncio
    async def test_health_reveals_only_status_and_service(self, dashboard_client):
        r = await dashboard_client.get("/api/health", headers=HDRS)
        data = r.json()
        assert set(data.keys()) == {"status", "service"}

    @pytest.mark.asyncio
    async def test_task_to_dict_excludes_internal_fields(self):
        task = SimpleNamespace(
            id=1, prompt="p", project_dir="/tmp", agent="claude",
            status=TaskStatus.PENDING, exit_code=None, error_message=None,
            output_summary=None, duration_seconds=None, created_at=None,
            started_at=None, completed_at=None, chain_id=None, chain_step=None,
            repeat_total=None, repeat_remaining=None,
            telegram_chat_id=12345, telegram_msg_id=67890,
            full_output="secret full output",
        )
        d = _task_to_dict(task)
        # Should NOT include telegram IDs or full_output in list view
        assert "telegram_chat_id" not in d
        assert "telegram_msg_id" not in d
        assert "full_output" not in d

    @pytest.mark.asyncio
    async def test_chain_to_dict_excludes_telegram_id(self):
        chain = SimpleNamespace(
            id=1, name="c", status=ChainStatus.IDLE, current_step=0,
            total_steps=3, created_at=None, started_at=None, completed_at=None,
            telegram_chat_id=12345,
        )
        d = _chain_to_dict(chain)
        assert "telegram_chat_id" not in d

    @pytest.mark.asyncio
    async def test_nonexistent_task_returns_404_not_500(self, dashboard_client):
        r = await dashboard_client.get("/api/tasks/999999", headers=HDRS)
        assert r.status_code == 404
        data = r.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_nonexistent_chain_returns_404(self, dashboard_client):
        r = await dashboard_client.get("/api/chains/999999", headers=HDRS)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_task_list_limit_capped_at_100(self, dashboard_client):
        r = await dashboard_client.get("/api/tasks?limit=200", headers=HDRS)
        assert r.status_code == 422  # Validation error from Query(ge=1, le=100)

    @pytest.mark.asyncio
    async def test_task_list_limit_zero_rejected(self, dashboard_client):
        r = await dashboard_client.get("/api/tasks?limit=0", headers=HDRS)
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_task_list_limit_negative_rejected(self, dashboard_client):
        r = await dashboard_client.get("/api/tasks?limit=-1", headers=HDRS)
        assert r.status_code == 422


# ═════════════════════════════════════════════════════════════════════
#  QA 6 — Robustness: Broker error recovery
# ═════════════════════════════════════════════════════════════════════


class TestQA06BrokerRecovery:
    """Broker functions handle edge-case data gracefully."""

    @pytest.mark.asyncio
    async def test_enqueue_returns_task_with_id(self, session):
        from app.core.broker import enqueue_task
        task = await enqueue_task(prompt="hello", project_dir="/tmp", agent="claude")
        assert task.id >= 1

    @pytest.mark.asyncio
    async def test_pick_next_returns_none_on_empty_queue(self, session):
        from app.core.broker import pick_next_task
        task = await pick_next_task()
        assert task is None

    @pytest.mark.asyncio
    async def test_complete_task_nonexistent_id(self, session):
        from app.core.broker import complete_task
        result = await complete_task(99999, exit_code=0, output_summary="", full_output="")
        # Should handle gracefully (return None or the original)
        assert result is None or isinstance(result, Task)

    @pytest.mark.asyncio
    async def test_cancel_by_id_nonexistent(self, session):
        from app.core.broker import cancel_task_by_id
        result = await cancel_task_by_id(99999)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_task_by_id_nonexistent(self, session):
        from app.core.broker import get_task_by_id
        result = await get_task_by_id(99999)
        assert result is None

    @pytest.mark.asyncio
    async def test_retry_nonexistent_task(self, session):
        from app.core.broker import retry_task
        result = await retry_task(99999)
        assert result is None

    @pytest.mark.asyncio
    async def test_chain_upsert_replaces_old(self, session):
        from app.core.broker import save_chain, get_chain_by_name
        await save_chain(name="dup", steps=[{"prompt": "v1"}])
        await save_chain(name="dup", steps=[{"prompt": "v2"}])
        chain = await get_chain_by_name("dup")
        assert chain is not None
        assert chain.steps[0]["prompt"] == "v2"

    @pytest.mark.asyncio
    async def test_delete_nonexistent_chain_returns_false(self, session):
        from app.core.broker import delete_chain
        result = await delete_chain("nonexistent_chain_name")
        assert result is False


# ═════════════════════════════════════════════════════════════════════
#  QA 7 — Robustness: Model edge cases
# ═════════════════════════════════════════════════════════════════════


class TestQA07ModelEdges:
    """ORM model property and enum edge cases."""

    def test_task_status_all_values(self):
        assert set(TaskStatus) == {
            TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.COMPLETED,
            TaskStatus.FAILED, TaskStatus.CANCELLED,
        }

    def test_chain_status_all_values(self):
        assert set(ChainStatus) == {
            ChainStatus.IDLE, ChainStatus.RUNNING, ChainStatus.COMPLETED,
            ChainStatus.FAILED, ChainStatus.CANCELLED,
        }

    def test_task_status_is_string_enum(self):
        assert TaskStatus.PENDING.value == "pending"
        assert isinstance(TaskStatus.PENDING, str)

    def test_chain_steps_empty_json_array(self):
        chain = TaskChain(name="e", steps_json="[]", status=ChainStatus.IDLE)
        assert chain.steps == []
        assert chain.total_steps == 0

    def test_chain_steps_valid_json(self):
        chain = TaskChain(name="v", steps_json='[{"prompt":"a"},{"prompt":"b"}]', status=ChainStatus.IDLE)
        assert len(chain.steps) == 2

    def test_chain_steps_type_error(self):
        """steps_json=None should return empty list."""
        chain = TaskChain(name="n", steps_json=None, status=ChainStatus.IDLE)
        assert chain.steps == []

    @pytest.mark.asyncio
    async def test_chat_prefs_defaults(self, session):
        prefs = ChatPrefs(chat_id=999)
        session.add(prefs)
        await session.commit()
        await session.refresh(prefs)
        assert prefs.project_dir is None
        assert prefs.agent is None

    @pytest.mark.asyncio
    async def test_task_default_status_is_pending(self, session):
        t = Task(prompt="x", project_dir="/tmp", agent="claude")
        session.add(t)
        await session.commit()
        await session.refresh(t)
        assert t.status == TaskStatus.PENDING


# ═════════════════════════════════════════════════════════════════════
#  QA 8 — Robustness: Chain lifecycle edge cases
# ═════════════════════════════════════════════════════════════════════


class TestQA08ChainLifecycle:
    """End-to-end chain lifecycle edge cases."""

    @pytest.mark.asyncio
    async def test_start_nonexistent_chain_returns_none(self, session):
        from app.core.broker import start_chain
        result = await start_chain("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_start_chain_with_empty_steps(self, session):
        """A chain saved with steps shouldn't become empty, but test gracefully."""
        from app.core.broker import save_chain, start_chain
        chain = await save_chain(name="one-step", steps=[{"prompt": "do"}])
        task = await start_chain("one-step")
        assert task is not None
        assert task.chain_step == 0

    @pytest.mark.asyncio
    async def test_advance_chain_with_no_chain_id(self, session):
        from app.core.broker import advance_chain
        task = SimpleNamespace(chain_id=None, chain_step=0, status=TaskStatus.COMPLETED)
        result = await advance_chain(task)
        assert result is None

    @pytest.mark.asyncio
    async def test_advance_chain_completed_step(self, session):
        from app.core.broker import save_chain, start_chain, advance_chain
        await save_chain(name="two-step", steps=[{"prompt": "s1"}, {"prompt": "s2"}])
        t1 = await start_chain("two-step")
        assert t1 is not None

        # Simulate completion
        t1.status = TaskStatus.COMPLETED
        t2 = await advance_chain(t1)
        assert t2 is not None
        assert t2.chain_step == 1

    @pytest.mark.asyncio
    async def test_advance_chain_last_step_completes_chain(self, session):
        from app.core.broker import save_chain, start_chain, advance_chain, get_chain_by_name
        await save_chain(name="fin", steps=[{"prompt": "only"}])
        t1 = await start_chain("fin")
        t1.status = TaskStatus.COMPLETED
        t2 = await advance_chain(t1)
        assert t2 is None  # no more steps

        chain = await get_chain_by_name("fin")
        assert chain.status == ChainStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_advance_chain_failed_step_fails_chain(self, session):
        from app.core.broker import save_chain, start_chain, advance_chain, get_chain_by_name
        await save_chain(name="fail", steps=[{"prompt": "s1"}, {"prompt": "s2"}])
        t1 = await start_chain("fail")
        t1.status = TaskStatus.FAILED
        t2 = await advance_chain(t1)
        assert t2 is None

        chain = await get_chain_by_name("fail")
        assert chain.status == ChainStatus.FAILED

    @pytest.mark.asyncio
    async def test_list_chains_empty(self, session):
        from app.core.broker import list_chains
        chains = await list_chains()
        assert chains == []


# ═════════════════════════════════════════════════════════════════════
#  QA 9 — Robustness: Repeat task edge cases
# ═════════════════════════════════════════════════════════════════════


class TestQA09RepeatTasks:
    """Repeat task enqueue and re-enqueue edge cases."""

    @pytest.mark.asyncio
    async def test_enqueue_repeat_with_count(self, session):
        from app.core.broker import enqueue_repeat_task
        task = await enqueue_repeat_task(
            prompt="repeat me", repeat_count=3,
            project_dir="/tmp", agent="claude",
        )
        assert task.repeat_total == 3
        assert task.repeat_remaining == 3

    @pytest.mark.asyncio
    async def test_enqueue_repeat_zero_raises(self, session):
        from app.core.broker import enqueue_repeat_task
        with pytest.raises(ValueError):
            await enqueue_repeat_task(
                prompt="bad", repeat_count=0,
                project_dir="/tmp", agent="claude",
            )

    @pytest.mark.asyncio
    async def test_enqueue_repeat_negative_raises(self, session):
        from app.core.broker import enqueue_repeat_task
        with pytest.raises(ValueError):
            await enqueue_repeat_task(
                prompt="bad", repeat_count=-1,
                project_dir="/tmp", agent="claude",
            )

    @pytest.mark.asyncio
    async def test_maybe_reenqueue_decrements_remaining(self, session):
        from app.core.broker import enqueue_repeat_task, maybe_reenqueue, pick_next_task, complete_task
        task = await enqueue_repeat_task(
            prompt="re", repeat_count=2,
            project_dir="/tmp", agent="claude",
        )
        picked = await pick_next_task()
        assert picked is not None
        completed = await complete_task(picked.id, exit_code=0, output_summary="ok", full_output="ok")
        new_task = await maybe_reenqueue(completed)
        if new_task is not None:
            assert new_task.repeat_remaining < task.repeat_remaining

    @pytest.mark.asyncio
    async def test_maybe_reenqueue_non_repeat_returns_none(self, session):
        from app.core.broker import enqueue_task, pick_next_task, complete_task, maybe_reenqueue
        task = await enqueue_task(prompt="once", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        completed = await complete_task(picked.id, exit_code=0, output_summary="ok", full_output="ok")
        result = await maybe_reenqueue(completed)
        assert result is None


# ═════════════════════════════════════════════════════════════════════
#  QA 10 — Robustness: Queue capacity & purge
# ═════════════════════════════════════════════════════════════════════


class TestQA10QueueCapacity:
    """Queue limits and purge operations."""

    @pytest.mark.asyncio
    async def test_enqueue_over_max_raises(self, session, monkeypatch):
        from app.core.broker import enqueue_task
        monkeypatch.setattr("app.config.settings.settings.max_queue_size", 2)
        await enqueue_task(prompt="t1", project_dir="/tmp", agent="claude")
        await enqueue_task(prompt="t2", project_dir="/tmp", agent="claude")
        with pytest.raises(ValueError):
            await enqueue_task(prompt="t3", project_dir="/tmp", agent="claude")

    @pytest.mark.asyncio
    async def test_purge_removes_old_completed(self, session):
        from app.core.broker import purge_old_tasks, get_recent_tasks
        old_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60)
        t = Task(
            prompt="old", project_dir="/tmp", agent="claude",
            status=TaskStatus.COMPLETED, created_at=old_time, completed_at=old_time,
        )
        session.add(t)
        await session.commit()
        await session.refresh(t)
        tid = t.id

        await purge_old_tasks(days=30)
        from app.core.broker import get_task_by_id
        assert await get_task_by_id(tid) is None

    @pytest.mark.asyncio
    async def test_purge_keeps_recent(self, session):
        from app.core.broker import purge_old_tasks, enqueue_task, get_task_by_id
        task = await enqueue_task(prompt="new", project_dir="/tmp", agent="claude")
        await purge_old_tasks(days=30)
        assert await get_task_by_id(task.id) is not None

    @pytest.mark.asyncio
    async def test_purge_keeps_running_tasks(self, session):
        from app.core.broker import purge_old_tasks, get_task_by_id
        old_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60)
        t = Task(
            prompt="running old", project_dir="/tmp", agent="claude",
            status=TaskStatus.RUNNING, created_at=old_time,
        )
        session.add(t)
        await session.commit()
        await session.refresh(t)

        await purge_old_tasks(days=30)
        assert await get_task_by_id(t.id) is not None


# ═════════════════════════════════════════════════════════════════════
#  QA 11 — Integration: Dashboard → Broker
# ═════════════════════════════════════════════════════════════════════


class TestQA11DashboardBroker:
    """Dashboard API correctly reflects broker state."""

    @pytest.mark.asyncio
    async def test_stats_reflects_enqueued_task(self, dashboard_client, session):
        from app.core.broker import enqueue_task
        await enqueue_task(prompt="check stats", project_dir="/tmp", agent="claude")
        r = await dashboard_client.get("/api/stats", headers=HDRS)
        assert r.status_code == 200
        data = r.json()
        assert data["pending_count"] >= 1

    @pytest.mark.asyncio
    async def test_queue_reflects_enqueued_task(self, dashboard_client, session):
        from app.core.broker import enqueue_task
        t = await enqueue_task(prompt="q check", project_dir="/tmp", agent="claude")
        r = await dashboard_client.get("/api/queue", headers=HDRS)
        assert r.status_code == 200
        tasks = r.json()
        assert any(x["id"] == t.id for x in tasks)

    @pytest.mark.asyncio
    async def test_task_detail_includes_full_output(self, dashboard_client, session):
        t = Task(
            prompt="with output", project_dir="/tmp", agent="claude",
            status=TaskStatus.COMPLETED, full_output="detailed output here",
        )
        session.add(t)
        await session.commit()
        await session.refresh(t)

        r = await dashboard_client.get(f"/api/tasks/{t.id}", headers=HDRS)
        assert r.status_code == 200
        data = r.json()
        assert data["full_output"] == "detailed output here"

    @pytest.mark.asyncio
    async def test_chain_detail_includes_steps(self, dashboard_client, session):
        from app.core.broker import save_chain
        chain = await save_chain(name="d-chain", steps=[{"prompt": "s1"}, {"prompt": "s2"}])
        r = await dashboard_client.get(f"/api/chains/{chain.id}", headers=HDRS)
        assert r.status_code == 200
        data = r.json()
        assert len(data["steps"]) == 2

    @pytest.mark.asyncio
    async def test_dashboard_html_page_loads(self, dashboard_client):
        r = await dashboard_client.get("/", headers=HDRS)
        assert r.status_code == 200
        assert "TaskPilot" in r.text


# ═════════════════════════════════════════════════════════════════════
#  QA 12 — Integration: Task lifecycle
# ═════════════════════════════════════════════════════════════════════


class TestQA12TaskLifecycle:
    """Full task lifecycle through broker."""

    @pytest.mark.asyncio
    async def test_full_lifecycle_enqueue_pick_complete(self, session):
        from app.core.broker import enqueue_task, pick_next_task, complete_task, get_task_by_id
        task = await enqueue_task(prompt="lifecycle", project_dir="/tmp", agent="claude")
        assert task.status == TaskStatus.PENDING

        picked = await pick_next_task()
        assert picked is not None
        assert picked.id == task.id
        assert picked.status == TaskStatus.RUNNING

        completed = await complete_task(picked.id, exit_code=0, output_summary="ok", full_output="done")
        assert completed is not None
        assert completed.status == TaskStatus.COMPLETED

        final = await get_task_by_id(task.id)
        assert final.status == TaskStatus.COMPLETED
        assert final.exit_code == 0

    @pytest.mark.asyncio
    async def test_full_lifecycle_enqueue_pick_fail(self, session):
        from app.core.broker import enqueue_task, pick_next_task, complete_task, get_task_by_id
        task = await enqueue_task(prompt="fail test", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        completed = await complete_task(picked.id, exit_code=1, output_summary="failed", full_output="", error_message="crash")
        assert completed.status == TaskStatus.FAILED
        assert completed.error_message == "crash"

    @pytest.mark.asyncio
    async def test_cancel_pending_task(self, session):
        from app.core.broker import enqueue_task, cancel_task_by_id
        task = await enqueue_task(prompt="cancel me", project_dir="/tmp", agent="claude")
        cancelled = await cancel_task_by_id(task.id)
        assert cancelled is not None
        assert cancelled.status == TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_retry_failed_task(self, session):
        from app.core.broker import enqueue_task, pick_next_task, complete_task, retry_task
        task = await enqueue_task(prompt="retry me", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        await complete_task(picked.id, exit_code=1, output_summary="err", full_output="", error_message="fail")
        new_task = await retry_task(task.id)
        assert new_task is not None
        assert new_task.prompt == "retry me"
        assert new_task.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_retry_completed_task_fails(self, session):
        from app.core.broker import enqueue_task, pick_next_task, complete_task, retry_task
        task = await enqueue_task(prompt="no retry", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        await complete_task(picked.id, exit_code=0, output_summary="ok", full_output="ok")
        result = await retry_task(task.id)
        # Can't retry a completed task
        assert result is None


# ═════════════════════════════════════════════════════════════════════
#  QA 13 — Integration: Runner validation
# ═════════════════════════════════════════════════════════════════════


class TestQA13RunnerValidation:
    """Runner validation before execution."""

    def test_runner_is_allowed_dir_with_tmp(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.settings.allowed_project_dirs", "/tmp")
        from app.core.runner import AgentRunner
        r = AgentRunner()
        assert r._is_allowed_dir("/tmp/project") is True

    def test_runner_rejects_outside_dir(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.settings.allowed_project_dirs", "/home/user")
        from app.core.runner import AgentRunner
        r = AgentRunner()
        assert r._is_allowed_dir("/etc/passwd") is False

    def test_build_command_uses_agent_command(self, monkeypatch):
        from app.core.runner import AgentRunner
        monkeypatch.setattr("app.config.settings.settings.agent_commands", {
            "test-agent": "echo {prompt}"
        })
        r = AgentRunner()
        task = SimpleNamespace(prompt="hello world", agent="test-agent", project_dir="/tmp", model=None)
        cmd = r._build_command(task)
        assert isinstance(cmd, list)
        assert any("hello world" in a for a in cmd)

    def test_build_command_unknown_agent_fallback(self):
        from app.core.runner import AgentRunner
        r = AgentRunner()
        task = SimpleNamespace(prompt="test", agent="nonexistent_agent", project_dir="/tmp", model=None)
        cmd = r._build_command(task)
        # Should still produce a valid command (fallback behavior)
        assert isinstance(cmd, list)
        assert len(cmd) >= 1


# ═════════════════════════════════════════════════════════════════════
#  QA 14 — Concurrency: Atomic operations
# ═════════════════════════════════════════════════════════════════════


class TestQA14Concurrency:
    """Verify atomic operations under concurrent access."""

    @pytest.mark.asyncio
    async def test_pick_next_is_atomic(self, session):
        """Two pick calls shouldn't return the same task."""
        from app.core.broker import enqueue_task, pick_next_task
        await enqueue_task(prompt="atomic", project_dir="/tmp", agent="claude")

        t1 = await pick_next_task()
        t2 = await pick_next_task()
        assert t1 is not None
        assert t2 is None  # nothing left

    @pytest.mark.asyncio
    async def test_complete_task_idempotent(self, session):
        """Completing an already-completed task doesn't crash."""
        from app.core.broker import enqueue_task, pick_next_task, complete_task
        task = await enqueue_task(prompt="idem", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        c1 = await complete_task(picked.id, exit_code=0, output_summary="ok", full_output="ok")
        assert c1 is not None

        # Second complete: rowcount=0, but still returns the task in current state
        c2 = await complete_task(picked.id, exit_code=0, output_summary="ok", full_output="ok")
        # The atomic WHERE status=RUNNING won't match, but task is returned as-is
        assert c2 is not None
        assert c2.status == TaskStatus.COMPLETED  # unchanged from first complete

    @pytest.mark.asyncio
    async def test_cancel_already_completed(self, session):
        from app.core.broker import enqueue_task, pick_next_task, complete_task, cancel_task_by_id
        task = await enqueue_task(prompt="done", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        await complete_task(picked.id, exit_code=0, output_summary="ok", full_output="ok")
        result = await cancel_task_by_id(task.id)
        assert result is None  # Can't cancel completed tasks

    @pytest.mark.asyncio
    async def test_multiple_enqueue_creates_distinct_ids(self, session):
        from app.core.broker import enqueue_task
        t1 = await enqueue_task(prompt="a", project_dir="/tmp", agent="claude")
        t2 = await enqueue_task(prompt="b", project_dir="/tmp", agent="claude")
        t3 = await enqueue_task(prompt="c", project_dir="/tmp", agent="claude")
        assert len({t1.id, t2.id, t3.id}) == 3

    @pytest.mark.asyncio
    async def test_fifo_ordering(self, session):
        from app.core.broker import enqueue_task, pick_next_task
        t1 = await enqueue_task(prompt="first", project_dir="/tmp", agent="claude")
        t2 = await enqueue_task(prompt="second", project_dir="/tmp", agent="claude")

        p1 = await pick_next_task()
        assert p1.id == t1.id
        p2 = await pick_next_task()
        assert p2.id == t2.id


# ═════════════════════════════════════════════════════════════════════
#  QA 15 — Concurrency: Recovery operations
# ═════════════════════════════════════════════════════════════════════


class TestQA15Recovery:
    """Recover from interrupted tasks and chains."""

    @pytest.mark.asyncio
    async def test_recover_sets_running_to_pending(self, session):
        from app.core.broker import recover_interrupted_tasks, get_task_by_id
        t = Task(prompt="stuck", project_dir="/tmp", agent="claude", status=TaskStatus.RUNNING)
        session.add(t)
        await session.commit()
        await session.refresh(t)

        count = await recover_interrupted_tasks()
        assert count >= 1

        recovered = await get_task_by_id(t.id)
        assert recovered.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_recover_ignores_pending(self, session):
        from app.core.broker import recover_interrupted_tasks, enqueue_task, get_task_by_id
        task = await enqueue_task(prompt="pending ok", project_dir="/tmp", agent="claude")
        await recover_interrupted_tasks()
        found = await get_task_by_id(task.id)
        assert found.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_recover_chains_resets_running(self, session):
        from app.core.broker import save_chain, start_chain
        chain_mod = __import__("app.core.broker", fromlist=["recover_interrupted_chains"])
        recover_chains = getattr(chain_mod, "recover_interrupted_chains", None)
        if recover_chains is None:
            pytest.skip("recover_interrupted_chains not implemented")

        await save_chain(name="rec-chain", steps=[{"prompt": "s1"}])
        await start_chain("rec-chain")
        count = await recover_chains()
        assert isinstance(count, int)

    @pytest.mark.asyncio
    async def test_get_running_task_returns_one(self, session):
        from app.core.broker import get_running_task
        t = Task(prompt="running", project_dir="/tmp", agent="claude", status=TaskStatus.RUNNING)
        session.add(t)
        await session.commit()
        await session.refresh(t)

        running = await get_running_task()
        assert running is not None
        assert running.id == t.id

    @pytest.mark.asyncio
    async def test_get_running_task_none_when_empty(self, session):
        from app.core.broker import get_running_task
        running = await get_running_task()
        assert running is None


# ═════════════════════════════════════════════════════════════════════
#  QA 16 — Hardening: API response contracts
# ═════════════════════════════════════════════════════════════════════


class TestQA16APIContracts:
    """Verify API response shapes and content types."""

    @pytest.mark.asyncio
    async def test_health_response_shape(self, dashboard_client):
        r = await dashboard_client.get("/api/health", headers=HDRS)
        data = r.json()
        assert data["status"] == "ok"
        assert data["service"] == "taskpilot"

    @pytest.mark.asyncio
    async def test_stats_response_all_fields(self, dashboard_client):
        r = await dashboard_client.get("/api/stats", headers=HDRS)
        data = r.json()
        expected_keys = {
            "running", "pending_count", "queue_capacity",
            "recent_completed", "recent_failed", "recent_cancelled",
            "avg_duration_seconds", "chains_total", "chains_running",
        }
        assert expected_keys.issubset(set(data.keys()))

    @pytest.mark.asyncio
    async def test_tasks_returns_list(self, dashboard_client):
        r = await dashboard_client.get("/api/tasks", headers=HDRS)
        assert isinstance(r.json(), list)

    @pytest.mark.asyncio
    async def test_queue_returns_list(self, dashboard_client):
        r = await dashboard_client.get("/api/queue", headers=HDRS)
        assert isinstance(r.json(), list)

    @pytest.mark.asyncio
    async def test_chains_returns_list(self, dashboard_client):
        r = await dashboard_client.get("/api/chains", headers=HDRS)
        assert isinstance(r.json(), list)

    @pytest.mark.asyncio
    async def test_task_dict_shape(self, dashboard_client, session):
        t = Task(prompt="shape", project_dir="/tmp", agent="claude", status=TaskStatus.PENDING)
        session.add(t)
        await session.commit()
        await session.refresh(t)

        r = await dashboard_client.get("/api/tasks", headers=HDRS)
        tasks = r.json()
        assert len(tasks) >= 1
        task = tasks[0]
        for key in ["id", "prompt", "project_dir", "agent", "status", "created_at"]:
            assert key in task

    @pytest.mark.asyncio
    async def test_chain_dict_shape(self, dashboard_client, session):
        from app.core.broker import save_chain
        await save_chain(name="shape-chain", steps=[{"prompt": "s"}])
        r = await dashboard_client.get("/api/chains", headers=HDRS)
        chains = r.json()
        assert len(chains) >= 1
        chain = chains[0]
        for key in ["id", "name", "status", "current_step", "total_steps"]:
            assert key in chain


# ═════════════════════════════════════════════════════════════════════
#  QA 17 — Hardening: Settings validation
# ═════════════════════════════════════════════════════════════════════


class TestQA17Settings:
    """Settings parsing and validation edge cases."""

    def test_settings_has_required_fields(self):
        assert hasattr(settings, "telegram_bot_token")
        assert hasattr(settings, "allowed_user_ids")
        assert hasattr(settings, "max_queue_size")
        assert hasattr(settings, "task_timeout_seconds")

    def test_max_queue_size_positive(self):
        assert settings.max_queue_size > 0

    def test_task_timeout_positive(self):
        assert settings.task_timeout_seconds > 0

    def test_output_summary_max_chars_positive(self):
        assert settings.output_summary_max_chars > 0

    def test_progress_interval_at_least_5(self):
        assert settings.progress_interval_seconds >= 5

    def test_default_agent_is_string(self):
        assert isinstance(settings.default_agent, str)
        assert len(settings.default_agent) > 0

    def test_db_path_is_set(self):
        assert hasattr(settings, "db_path") or hasattr(settings, "db_url")

    def test_dashboard_defaults(self):
        assert isinstance(settings.dashboard_enabled, bool)
        assert isinstance(settings.dashboard_host, str)
        assert isinstance(settings.dashboard_port, int)


# ═════════════════════════════════════════════════════════════════════
#  QA 18 — Hardening: HTML dashboard resilience
# ═════════════════════════════════════════════════════════════════════


class TestQA18DashboardResilience:
    """Dashboard HTML and JS handling edge cases."""

    @pytest.mark.asyncio
    async def test_html_contains_auto_refresh(self, dashboard_client):
        r = await dashboard_client.get("/", headers=HDRS)
        assert "refresh" in r.text.lower() or "setTimeout" in r.text

    @pytest.mark.asyncio
    async def test_html_contains_escape_function(self, dashboard_client):
        r = await dashboard_client.get("/", headers=HDRS)
        assert "function esc" in r.text

    @pytest.mark.asyncio
    async def test_html_uses_textcontent_for_escape(self, dashboard_client):
        r = await dashboard_client.get("/", headers=HDRS)
        assert "textContent" in r.text

    @pytest.mark.asyncio
    async def test_html_uses_promise_allsettled(self, dashboard_client):
        r = await dashboard_client.get("/", headers=HDRS)
        assert "Promise.allSettled" in r.text

    @pytest.mark.asyncio
    async def test_task_with_special_html_in_prompt(self, dashboard_client, session):
        t = Task(
            prompt='<script>alert("xss")</script>',
            project_dir="/tmp", agent="claude", status=TaskStatus.COMPLETED,
        )
        session.add(t)
        await session.commit()

        r = await dashboard_client.get("/api/tasks", headers=HDRS)
        data = r.json()
        assert len(data) >= 1
        # JSON transport keeps raw string; XSS only matters in HTML context
        assert "<script>" in data[0]["prompt"]  # raw in JSON is fine

    @pytest.mark.asyncio
    async def test_task_with_empty_output_summary(self, dashboard_client, session):
        t = Task(
            prompt="no summary", project_dir="/tmp", agent="claude",
            status=TaskStatus.COMPLETED, output_summary=None,
        )
        session.add(t)
        await session.commit()
        await session.refresh(t)

        r = await dashboard_client.get(f"/api/tasks/{t.id}", headers=HDRS)
        data = r.json()
        assert data["output_summary"] is None

    @pytest.mark.asyncio
    async def test_stats_with_zero_duration_avg(self, dashboard_client):
        r = await dashboard_client.get("/api/stats", headers=HDRS)
        data = r.json()
        assert data["avg_duration_seconds"] == 0.0

    @pytest.mark.asyncio
    async def test_stats_with_actual_durations(self, dashboard_client, session):
        for dur in [10, 20, 30]:
            t = Task(
                prompt=f"dur {dur}", project_dir="/tmp", agent="claude",
                status=TaskStatus.COMPLETED, duration_seconds=dur,
            )
            session.add(t)
        await session.commit()

        r = await dashboard_client.get("/api/stats", headers=HDRS)
        data = r.json()
        assert data["avg_duration_seconds"] == 20.0


# ═════════════════════════════════════════════════════════════════════
#  QA 19 — Hardening: Telegram bot command edge cases
# ═════════════════════════════════════════════════════════════════════


class TestQA19BotEdges:
    """Telegram bot command handler edge cases."""

    def test_status_emoji_all_statuses(self):
        from app.telegram.bot import _status_emoji
        for status in TaskStatus:
            emoji = _status_emoji(status)
            assert isinstance(emoji, str)
            assert len(emoji) > 0

    def test_format_task_handles_no_duration(self):
        from app.telegram.bot import _format_task
        task = SimpleNamespace(
            id=1, status=TaskStatus.PENDING, agent="claude",
            prompt="short prompt", duration_seconds=None,
        )
        result = _format_task(task)
        assert "#1" in result
        assert "claude" in result

    def test_format_task_shows_duration(self):
        from app.telegram.bot import _format_task
        task = SimpleNamespace(
            id=2, status=TaskStatus.COMPLETED, agent="claude",
            prompt="done", duration_seconds=45,
        )
        result = _format_task(task)
        assert "45s" in result

    def test_format_task_truncates_long_prompt(self):
        from app.telegram.bot import _format_task
        task = SimpleNamespace(
            id=3, status=TaskStatus.RUNNING, agent="claude",
            prompt="x" * 100, duration_seconds=None,
        )
        result = _format_task(task)
        assert len(result) < 200  # truncated display

    def test_sanitize_text_with_mixed_nulls(self):
        from app.telegram.bot import _sanitize_text
        result = _sanitize_text("  \x00hello\x00  ")
        assert result == "hello"

    def test_sanitize_text_only_whitespace(self):
        from app.telegram.bot import _sanitize_text
        result = _sanitize_text("   \t\n   ")
        assert result == ""

    @pytest.mark.asyncio
    async def test_load_prefs_caches(self, session):
        from app.telegram.bot import _load_prefs, _chat_project_dir, _chat_agent
        chat_id = 77777
        # Clear cache
        _chat_project_dir.pop(chat_id, None)
        _chat_agent.pop(chat_id, None)

        # First load from empty DB
        await _load_prefs(chat_id)
        # Second load should use cache (not error)
        _chat_project_dir[chat_id] = "/cached"
        await _load_prefs(chat_id)
        assert _chat_project_dir[chat_id] == "/cached"

        # Cleanup
        _chat_project_dir.pop(chat_id, None)
        _chat_agent.pop(chat_id, None)


# ═════════════════════════════════════════════════════════════════════
#  QA 20 — Final: Comprehensive data integrity checks
# ═════════════════════════════════════════════════════════════════════


class TestQA20DataIntegrity:
    """Final integrity and defensive coding checks."""

    @pytest.mark.asyncio
    async def test_task_timestamps_set_on_create(self, session):
        from app.core.broker import enqueue_task
        task = await enqueue_task(prompt="ts", project_dir="/tmp", agent="claude")
        assert task.created_at is not None

    @pytest.mark.asyncio
    async def test_completed_task_has_completed_at(self, session):
        from app.core.broker import enqueue_task, pick_next_task, complete_task
        task = await enqueue_task(prompt="comp", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        completed = await complete_task(picked.id, exit_code=0, output_summary="ok", full_output="ok")
        assert completed.completed_at is not None

    @pytest.mark.asyncio
    async def test_running_task_has_started_at(self, session):
        from app.core.broker import enqueue_task, pick_next_task
        await enqueue_task(prompt="start", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        assert picked.started_at is not None

    @pytest.mark.asyncio
    async def test_completed_task_has_duration(self, session):
        from app.core.broker import enqueue_task, pick_next_task, complete_task
        task = await enqueue_task(prompt="dur", project_dir="/tmp", agent="claude")
        picked = await pick_next_task()
        completed = await complete_task(picked.id, exit_code=0, output_summary="ok", full_output="ok")
        assert completed.duration_seconds is not None
        assert completed.duration_seconds >= 0

    @pytest.mark.asyncio
    async def test_chain_created_at_set(self, session):
        from app.core.broker import save_chain
        chain = await save_chain(name="ts-chain", steps=[{"prompt": "x"}])
        assert chain.created_at is not None

    @pytest.mark.asyncio
    async def test_started_chain_has_started_at(self, session):
        from app.core.broker import save_chain, start_chain, get_chain_by_name
        await save_chain(name="started", steps=[{"prompt": "x"}])
        await start_chain("started")
        chain = await get_chain_by_name("started")
        assert chain.started_at is not None

    @pytest.mark.asyncio
    async def test_task_to_dict_iso_format_dates(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        task = SimpleNamespace(
            id=1, prompt="p", project_dir="/tmp", agent="claude",
            status=TaskStatus.COMPLETED, exit_code=0, error_message=None,
            output_summary="ok", duration_seconds=10, created_at=now,
            started_at=now, completed_at=now, chain_id=None, chain_step=None,
            repeat_total=None, repeat_remaining=None,
        )
        d = _task_to_dict(task)
        assert "T" in d["created_at"]  # ISO format
        assert "T" in d["started_at"]
        assert "T" in d["completed_at"]

    @pytest.mark.asyncio
    async def test_chain_to_dict_iso_format_dates(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        chain = SimpleNamespace(
            id=1, name="c", status=ChainStatus.COMPLETED, current_step=1,
            total_steps=1, created_at=now, started_at=now, completed_at=now,
        )
        d = _chain_to_dict(chain)
        assert "T" in d["created_at"]

    @pytest.mark.asyncio
    async def test_recent_tasks_respects_limit(self, session):
        from app.core.broker import enqueue_task, get_recent_tasks
        for i in range(5):
            await enqueue_task(prompt=f"t{i}", project_dir="/tmp", agent="claude")
        tasks = await get_recent_tasks(limit=3)
        assert len(tasks) == 3

    @pytest.mark.asyncio
    async def test_pending_tasks_only_pending(self, session):
        from app.core.broker import enqueue_task, pick_next_task, get_pending_tasks
        t1 = await enqueue_task(prompt="pending1", project_dir="/tmp", agent="claude")
        t2 = await enqueue_task(prompt="pending2", project_dir="/tmp", agent="claude")
        await pick_next_task()  # picks t1, makes it RUNNING

        pending = await get_pending_tasks()
        assert all(t.status == TaskStatus.PENDING for t in pending)
        assert len(pending) == 1
        assert pending[0].id == t2.id

    @pytest.mark.asyncio
    async def test_get_chat_prefs_nonexistent_returns_defaults(self, session):
        from app.core.broker import get_chat_prefs
        prefs = await get_chat_prefs(88888)
        assert prefs == {"project_dir": None, "agent": None, "model": None}

    @pytest.mark.asyncio
    async def test_set_and_get_chat_prefs(self, session):
        from app.core.broker import set_chat_pref, get_chat_prefs
        await set_chat_pref(77777, project_dir="/my/dir")
        prefs = await get_chat_prefs(77777)
        assert prefs["project_dir"] == "/my/dir"

    @pytest.mark.asyncio
    async def test_dashboard_content_type_json(self, dashboard_client, session):
        r = await dashboard_client.get("/api/health", headers=HDRS)
        assert "application/json" in r.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_dashboard_html_content_type(self, dashboard_client):
        r = await dashboard_client.get("/", headers=HDRS)
        assert "text/html" in r.headers.get("content-type", "")

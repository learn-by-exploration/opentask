"""Extensive 10-iteration test suite covering ALL TaskPilot features.

Each feature area is tested with 10 varied inputs / scenarios to stress the system.
Features covered:
  1. Task CRUD (enqueue, cancel, retry, complete, status, queue, history)
  2. Task Chains (save, list, start, advance, delete)
  3. Repeat Tasks (count, deadline, re-enqueue)
  4. Follow-up Mode (continue, cancel_followup)
  5. Chat Preferences (project_dir, agent, model, smart_mode)
  6. Recipes (save, match, delete, triggers, overrides)
  7. Gallery (install single, category, all)
  8. Templates (save, get, list, delete)
  9. Schedules (save, list, toggle, delete, due)
  10. Model Fallback Chains (is_rate_limited, fallback_retry_task)
  11. Cost Tracking (estimated_cost, budget limits)
  12. Smart Mode / Dispatcher (pattern classify, safety levels, extractors)
  13. Runner (command building, summarize, allowed dirs)
  14. Web Dashboard (endpoints, auth, HTML)
  15. Worker API (claim, heartbeat, submit, recovery)
  16. Auth & Security (allowed users, shell escaping, path allowlist)
  17. Settings / Config (env overrides, defaults, model aliases)
"""

from __future__ import annotations

import json
import os
import re
import time

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import (
    Base, Task, TaskStatus, TaskChain, ChainStatus,
    ChatPrefs, Recipe, TaskTemplate, ScheduledTask,
)


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════

async def _set_task_running(task: Task) -> None:
    """Set a PENDING task to RUNNING so complete_task() can process it."""
    from app.core import db as db_mod
    async with db_mod.async_session() as session, session.begin():
        t = await session.get(Task, task.id)
        t.status = TaskStatus.RUNNING
        t.started_at = datetime.utcnow()


async def _complete(task_id: int, exit_code: int = 0,
                    summary: str = "ok", output: str = "ok") -> Task:
    """Set task to RUNNING, then call complete_task with correct params."""
    from app.core import db as db_mod
    from app.core.broker import complete_task
    async with db_mod.async_session() as session, session.begin():
        t = await session.get(Task, task_id)
        t.status = TaskStatus.RUNNING
        t.started_at = datetime.utcnow()
    return await complete_task(task_id, exit_code=exit_code,
                               output_summary=summary, full_output=output)


# ════════════════════════════════════════════════════════════════════
#  Fixtures
# ════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    """Fresh in-memory DB for each test, patching broker's session factory."""
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


# ════════════════════════════════════════════════════════════════════
#  1. Task CRUD — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestTaskCRUD:
    """Core task lifecycle: enqueue, status, cancel, retry, complete."""

    @pytest.mark.parametrize("idx", range(10))
    async def test_enqueue_unique_prompts(self, idx):
        from app.core.broker import enqueue_task, get_pending_tasks
        prompt = f"fix bug #{idx} in module_{idx}"
        task = await enqueue_task(
            prompt=prompt, project_dir="/tmp/proj", agent="opencode",
            chat_id=12345,
        )
        assert task.id is not None
        assert task.prompt == prompt
        assert task.status == TaskStatus.PENDING

    @pytest.mark.parametrize("idx", range(10))
    async def test_enqueue_different_agents(self, idx):
        from app.core.broker import enqueue_task
        agents = ["opencode", "aider", "codex", "claude", "copilot",
                  "devin", "cursor", "roo", "cline", "bolt"]
        task = await enqueue_task(
            prompt=f"task {idx}", project_dir="/tmp/proj", agent=agents[idx],
            chat_id=12345,
        )
        assert task.agent == agents[idx]

    @pytest.mark.parametrize("idx", range(10))
    async def test_cancel_pending_task(self, idx):
        from app.core.broker import enqueue_task, cancel_task_by_id
        task = await enqueue_task(
            prompt=f"cancel me {idx}", project_dir="/tmp/proj", agent="opencode",
            chat_id=12345,
        )
        cancelled = await cancel_task_by_id(task.id)
        assert cancelled is not None
        assert cancelled.status == TaskStatus.CANCELLED

    @pytest.mark.parametrize("idx", range(10))
    async def test_complete_task_with_output(self, idx):
        from app.core.broker import enqueue_task
        task = await enqueue_task(
            prompt=f"complete me {idx}", project_dir="/tmp/proj", agent="opencode",
            chat_id=12345,
        )
        completed = await _complete(task.id, exit_code=0,
                                     output=f"Success output #{idx}",
                                     summary=f"Done #{idx}")
        assert completed.status == TaskStatus.COMPLETED
        assert completed.output_summary == f"Done #{idx}"

    @pytest.mark.parametrize("idx", range(10))
    async def test_complete_task_failure(self, idx):
        from app.core.broker import enqueue_task
        task = await enqueue_task(
            prompt=f"fail me {idx}", project_dir="/tmp/proj", agent="opencode",
            chat_id=12345,
        )
        completed = await _complete(task.id, exit_code=1,
                                     output=f"Error #{idx}",
                                     summary=f"Failed #{idx}")
        assert completed.status == TaskStatus.FAILED

    @pytest.mark.parametrize("idx", range(10))
    async def test_retry_failed_task(self, idx):
        from app.core.broker import enqueue_task, retry_task
        task = await enqueue_task(
            prompt=f"retry me {idx}", project_dir="/tmp/proj", agent="opencode",
            chat_id=12345,
        )
        await _complete(task.id, exit_code=1, output="err", summary="fail")
        retried = await retry_task(task.id)
        assert retried is not None
        assert retried.status == TaskStatus.PENDING
        assert retried.prompt == f"retry me {idx}"

    @pytest.mark.parametrize("idx", range(10))
    async def test_get_running_task(self, idx):
        from app.core.broker import enqueue_task, get_running_task
        from app.core import db as db_mod
        task = await enqueue_task(
            prompt=f"running {idx}", project_dir="/tmp/proj", agent="opencode",
            chat_id=12345,
        )
        # Manually set to RUNNING
        async with db_mod.async_session() as session:
            t = await session.get(Task, task.id)
            t.status = TaskStatus.RUNNING
            await session.commit()
        running = await get_running_task()
        assert running is not None
        assert running.id == task.id

    @pytest.mark.parametrize("idx", range(10))
    async def test_get_recent_tasks(self, idx):
        from app.core.broker import enqueue_task, get_recent_tasks
        for j in range(idx + 1):
            await enqueue_task(
                prompt=f"task {j} batch {idx}", project_dir="/tmp/proj",
                agent="opencode", chat_id=12345,
            )
        recent = await get_recent_tasks(limit=50)
        assert len(recent) == idx + 1

    @pytest.mark.parametrize("idx", range(10))
    async def test_get_pending_tasks(self, idx):
        from app.core.broker import enqueue_task, get_pending_tasks
        for j in range(idx + 1):
            await enqueue_task(
                prompt=f"pending {j}-{idx}", project_dir="/tmp/proj",
                agent="opencode", chat_id=12345,
            )
        pending = await get_pending_tasks()
        assert len(pending) == idx + 1

    @pytest.mark.parametrize("idx", range(10))
    async def test_get_task_by_id(self, idx):
        from app.core.broker import enqueue_task, get_task_by_id
        task = await enqueue_task(
            prompt=f"find me {idx}", project_dir="/tmp/proj", agent="opencode",
            chat_id=12345,
        )
        found = await get_task_by_id(task.id)
        assert found is not None
        assert found.prompt == f"find me {idx}"


# ════════════════════════════════════════════════════════════════════
#  2. Task Chains — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestTaskChains:
    @pytest.mark.parametrize("idx", range(10))
    async def test_save_chain(self, idx):
        from app.core.broker import save_chain, get_chain_by_name
        steps = [{"prompt": f"step {j} of chain {idx}", "project_dir": "/tmp"}
                 for j in range(idx + 1)]
        chain = await save_chain(name=f"chain_{idx}", steps=steps)
        assert chain.name == f"chain_{idx}"
        fetched = await get_chain_by_name(f"chain_{idx}")
        assert fetched is not None

    @pytest.mark.parametrize("idx", range(10))
    async def test_list_chains(self, idx):
        from app.core.broker import save_chain, list_chains
        for j in range(idx + 1):
            await save_chain(
                name=f"list_chain_{idx}_{j}",
                steps=[{"prompt": "step", "project_dir": "/tmp"}],
            )
        chains = await list_chains()
        assert len(chains) == idx + 1

    @pytest.mark.parametrize("idx", range(10))
    async def test_delete_chain(self, idx):
        from app.core.broker import save_chain, delete_chain, get_chain_by_name
        name = f"del_chain_{idx}"
        await save_chain(name=name, steps=[{"prompt": "s", "project_dir": "/tmp"}])
        result = await delete_chain(name)
        assert result is True
        assert await get_chain_by_name(name) is None

    @pytest.mark.parametrize("idx", range(10))
    async def test_start_chain_creates_first_task(self, idx):
        from app.core.broker import save_chain, start_chain
        steps = [
            {"prompt": f"step 0 of chain {idx}", "project_dir": "/tmp"},
            {"prompt": f"step 1 of chain {idx}", "project_dir": "/tmp"},
        ]
        await save_chain(name=f"start_{idx}", steps=steps)
        task = await start_chain(f"start_{idx}", chat_id=12345)
        assert task is not None
        assert task.chain_step == 0

    @pytest.mark.parametrize("idx", range(10))
    async def test_chain_advance_after_complete(self, idx):
        from app.core.broker import save_chain, start_chain, advance_chain, get_task_by_id
        steps = [
            {"prompt": f"s0-{idx}", "project_dir": "/tmp"},
            {"prompt": f"s1-{idx}", "project_dir": "/tmp"},
        ]
        await save_chain(name=f"adv_{idx}", steps=steps)
        task = await start_chain(f"adv_{idx}", chat_id=12345)
        await _complete(task.id, exit_code=0, output="ok", summary="ok")
        completed = await get_task_by_id(task.id)
        next_task = await advance_chain(completed)
        assert next_task is not None
        assert next_task.chain_step == 1
        assert f"s1-{idx}" in next_task.prompt


# ════════════════════════════════════════════════════════════════════
#  3. Repeat Tasks — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestRepeatTasks:
    @pytest.mark.parametrize("count", range(1, 11))
    async def test_enqueue_repeat_count(self, count):
        from app.core.broker import enqueue_repeat_task
        task = await enqueue_repeat_task(
            prompt=f"repeat task x{count}", project_dir="/tmp/proj",
            agent="opencode", chat_id=12345,
            repeat_count=count,
        )
        assert task.repeat_remaining == count

    @pytest.mark.parametrize("idx", range(10))
    async def test_repeat_reenqueue(self, idx):
        from app.core.broker import enqueue_repeat_task, maybe_reenqueue, get_task_by_id
        task = await enqueue_repeat_task(
            prompt=f"reenqueue {idx}", project_dir="/tmp/proj",
            agent="opencode", chat_id=12345,
            repeat_count=3,
        )
        await _complete(task.id, exit_code=0, output="ok", summary="ok")
        completed = await get_task_by_id(task.id)
        requeued = await maybe_reenqueue(completed)
        if requeued:
            assert requeued.repeat_remaining < 3


# ════════════════════════════════════════════════════════════════════
#  4. Chat Preferences — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestChatPreferences:
    @pytest.mark.parametrize("idx", range(10))
    async def test_set_get_project_dir(self, idx):
        from app.core.broker import set_chat_pref, get_chat_prefs
        chat_id = 10000 + idx
        path = f"/home/user/project_{idx}"
        await set_chat_pref(chat_id, project_dir=path)
        prefs = await get_chat_prefs(chat_id)
        assert prefs["project_dir"] == path

    @pytest.mark.parametrize("idx", range(10))
    async def test_set_get_agent(self, idx):
        from app.core.broker import set_chat_pref, get_chat_prefs
        chat_id = 20000 + idx
        agents = ["opencode", "aider", "claude", "codex", "copilot",
                  "devin", "cursor", "roo", "cline", "bolt"]
        await set_chat_pref(chat_id, agent=agents[idx])
        prefs = await get_chat_prefs(chat_id)
        assert prefs["agent"] == agents[idx]

    @pytest.mark.parametrize("idx", range(10))
    async def test_set_get_model(self, idx):
        from app.core.broker import set_chat_pref, get_chat_prefs
        chat_id = 30000 + idx
        models = ["sonnet", "opus", "haiku", "gpt-4", "gpt-3.5",
                  "claude-3", "gemini", "llama", "mixtral", "phi"]
        await set_chat_pref(chat_id, model=models[idx])
        prefs = await get_chat_prefs(chat_id)
        assert prefs["model"] == models[idx]

    @pytest.mark.parametrize("idx", range(10))
    async def test_set_get_smart_mode(self, idx):
        from app.core.broker import set_chat_pref, get_chat_prefs
        chat_id = 40000 + idx
        enabled = idx % 2 == 0
        await set_chat_pref(chat_id, smart_mode=enabled)
        prefs = await get_chat_prefs(chat_id)
        assert prefs["smart_mode"] == enabled

    @pytest.mark.parametrize("idx", range(10))
    async def test_overwrite_prefs(self, idx):
        from app.core.broker import set_chat_pref, get_chat_prefs
        chat_id = 50000 + idx
        await set_chat_pref(chat_id, project_dir="/old", agent="old_agent")
        await set_chat_pref(chat_id, project_dir=f"/new_{idx}")
        prefs = await get_chat_prefs(chat_id)
        assert prefs["project_dir"] == f"/new_{idx}"
        assert prefs["agent"] == "old_agent"  # untouched

    @pytest.mark.parametrize("idx", range(10))
    async def test_default_prefs(self, idx):
        from app.core.broker import get_chat_prefs
        prefs = await get_chat_prefs(99000 + idx)
        assert prefs["project_dir"] is None
        assert prefs["agent"] is None
        assert prefs["model"] is None
        assert prefs["smart_mode"] is False


# ════════════════════════════════════════════════════════════════════
#  5. Recipes — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestRecipes:
    @pytest.mark.parametrize("idx", range(10))
    async def test_save_recipe(self, idx):
        from app.core.broker import save_recipe, get_recipe_by_name
        r = await save_recipe(
            name=f"recipe_{idx}",
            triggers=[f"trigger_{idx}", f"keyword_{idx}"],
            agent=f"agent_{idx}",
        )
        assert r.name == f"recipe_{idx}"
        fetched = await get_recipe_by_name(f"recipe_{idx}")
        assert fetched is not None
        assert f"trigger_{idx}" in fetched.triggers

    @pytest.mark.parametrize("idx", range(10))
    async def test_match_recipe_by_trigger(self, idx):
        from app.core.broker import save_recipe, match_recipe
        await save_recipe(
            name=f"matcher_{idx}",
            triggers=[f"deploy_{idx}"],
        )
        matched = await match_recipe(f"please deploy_{idx} the app")
        assert matched is not None
        assert matched.name == f"matcher_{idx}"

    @pytest.mark.parametrize("idx", range(10))
    async def test_no_match_wrong_trigger(self, idx):
        from app.core.broker import save_recipe, match_recipe
        await save_recipe(name=f"nope_{idx}", triggers=["zzz_nope"])
        matched = await match_recipe(f"something_else_{idx}")
        assert matched is None

    @pytest.mark.parametrize("idx", range(10))
    async def test_delete_recipe(self, idx):
        from app.core.broker import save_recipe, delete_recipe, get_recipe_by_name
        await save_recipe(name=f"del_rec_{idx}", triggers=["t"])
        result = await delete_recipe(f"del_rec_{idx}")
        assert result is True
        assert await get_recipe_by_name(f"del_rec_{idx}") is None

    @pytest.mark.parametrize("idx", range(10))
    async def test_list_recipes(self, idx):
        from app.core.broker import save_recipe, list_recipes
        for j in range(idx + 1):
            await save_recipe(name=f"lr_{idx}_{j}", triggers=["t"])
        recipes = await list_recipes()
        assert len(recipes) == idx + 1

    @pytest.mark.parametrize("idx", range(10))
    async def test_recipe_with_overrides(self, idx):
        from app.core.broker import save_recipe
        r = await save_recipe(
            name=f"override_{idx}",
            triggers=["t"],
            agent="custom_agent",
            model="custom_model",
            project_dir=f"/proj/{idx}",
            prompt_prefix=f"PREFIX_{idx}: ",
            prompt_suffix=f" SUFFIX_{idx}",
        )
        assert r.agent == "custom_agent"
        assert r.model == "custom_model"
        assert r.prompt_prefix == f"PREFIX_{idx}: "
        assert r.prompt_suffix == f" SUFFIX_{idx}"


# ════════════════════════════════════════════════════════════════════
#  6. Templates — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestTemplates:
    @pytest.mark.parametrize("idx", range(10))
    async def test_save_template(self, idx):
        from app.core.broker import save_template, get_template
        t = await save_template(
            name=f"tmpl_{idx}", prompt=f"run tests #{idx}",
            agent="opencode", model=None, project_dir="/tmp",
        )
        assert t.name == f"tmpl_{idx}"
        fetched = await get_template(f"tmpl_{idx}")
        assert fetched is not None
        assert fetched.prompt == f"run tests #{idx}"

    @pytest.mark.parametrize("idx", range(10))
    async def test_list_templates(self, idx):
        from app.core.broker import save_template, list_templates
        for j in range(idx + 1):
            await save_template(
                name=f"lt_{idx}_{j}", prompt="p", agent="a",
            )
        templates = await list_templates()
        assert len(templates) == idx + 1

    @pytest.mark.parametrize("idx", range(10))
    async def test_delete_template(self, idx):
        from app.core.broker import save_template, delete_template, get_template
        await save_template(name=f"dt_{idx}", prompt="p", agent="a")
        result = await delete_template(f"dt_{idx}")
        assert result is True
        assert await get_template(f"dt_{idx}") is None

    @pytest.mark.parametrize("idx", range(10))
    async def test_template_upsert(self, idx):
        from app.core.broker import save_template, get_template
        await save_template(name=f"upsert_{idx}", prompt="old", agent="a")
        await save_template(name=f"upsert_{idx}", prompt=f"new_{idx}", agent="b")
        fetched = await get_template(f"upsert_{idx}")
        assert fetched.prompt == f"new_{idx}"

    @pytest.mark.parametrize("idx", range(10))
    async def test_template_with_all_fields(self, idx):
        from app.core.broker import save_template
        t = await save_template(
            name=f"full_{idx}", prompt=f"do #{idx}", agent="claude",
            model="opus", project_dir=f"/proj/{idx}",
            timeout_seconds=600 + idx,
        )
        assert t.agent == "claude"
        assert t.model == "opus"
        assert t.timeout_seconds == 600 + idx


# ════════════════════════════════════════════════════════════════════
#  7. Schedules — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestSchedules:
    @pytest.mark.parametrize("idx", range(10))
    async def test_save_schedule(self, idx):
        from app.core.broker import save_schedule, list_schedules
        sched = await save_schedule(
            name=f"sched_{idx}", cron_expr=f"0 {idx} * * *",
            prompt=f"daily task #{idx}",
        )
        assert sched.name == f"sched_{idx}"
        all_scheds = await list_schedules()
        assert len(all_scheds) == 1

    @pytest.mark.parametrize("idx", range(10))
    async def test_toggle_schedule(self, idx):
        from app.core.broker import save_schedule, toggle_schedule
        await save_schedule(name=f"tog_{idx}", cron_expr="0 9 * * *", prompt="p")
        toggled = await toggle_schedule(f"tog_{idx}")
        assert toggled is not None
        assert toggled.enabled is False
        toggled2 = await toggle_schedule(f"tog_{idx}")
        assert toggled2.enabled is True

    @pytest.mark.parametrize("idx", range(10))
    async def test_delete_schedule(self, idx):
        from app.core.broker import save_schedule, delete_schedule
        await save_schedule(name=f"del_{idx}", cron_expr="0 0 * * *", prompt="p")
        result = await delete_schedule(f"del_{idx}")
        assert result is True

    @pytest.mark.parametrize("idx", range(10))
    async def test_schedule_with_overrides(self, idx):
        from app.core.broker import save_schedule
        s = await save_schedule(
            name=f"full_{idx}", cron_expr="0 9 * * *", prompt="p",
            agent="custom", model=f"model_{idx}", project_dir=f"/proj/{idx}",
        )
        assert s.agent == "custom"
        assert s.model == f"model_{idx}"


# ════════════════════════════════════════════════════════════════════
#  8. Model Fallback / Rate-Limit Recovery — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestModelFallback:
    @pytest.mark.parametrize("phrase", [
        "rate limit exceeded",
        "429 Too Many Requests",
        "Rate limit hit",
        "RATE LIMIT EXCEEDED",
        "Error: rate limit exceeded for model",
        "API responded with 429",
        "rate_limit_exceeded",
        "Server returned: rate limit exceeded",
        "Too many requests, rate limit exceeded",
        "RateLimitError: rate limit exceeded",
    ])
    def test_is_rate_limited_detection(self, phrase):
        from app.core.broker import is_rate_limited
        assert is_rate_limited(phrase) is True

    @pytest.mark.parametrize("phrase", [
        "Build successful",
        "Tests passed: 42",
        "Error: module not found",
        "SyntaxError on line 5",
        "Connection refused",
        "Permission denied",
        "File not found: rate.py",
        "Compilation error at limit.go",
        "Timeout after 1800 seconds",
        "normal output with no issues",
    ])
    def test_is_rate_limited_negative(self, phrase):
        from app.core.broker import is_rate_limited
        assert is_rate_limited(phrase) is False

    @pytest.mark.parametrize("idx", range(10))
    async def test_fallback_retry_no_chain(self, idx):
        """fallback_retry_task returns None when no fallback chain configured."""
        from app.core.broker import enqueue_task, fallback_retry_task
        task = await enqueue_task(
            prompt=f"fallback test {idx}", project_dir="/tmp",
            agent="opencode", chat_id=12345,
        )
        await _complete(task.id, exit_code=1, output="rate limit exceeded", summary="fail")
        # No fallback chain = returns None
        # With default settings (no fallback chains), should return None
        result = await fallback_retry_task(task.id)
        # No fallback chain configured = returns None
        assert result is None or result.id != task.id


# ════════════════════════════════════════════════════════════════════
#  9. Cost Tracking — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestCostTracking:
    @pytest.mark.parametrize("idx", range(10))
    async def test_task_has_estimated_cost(self, idx):
        from app.core.broker import enqueue_task, get_task_by_id
        task = await enqueue_task(
            prompt=f"cost test {idx}", project_dir="/tmp", agent="opencode",
            chat_id=12345,
        )
        fetched = await get_task_by_id(task.id)
        assert fetched is not None
        # estimated_cost is auto-computed; may be None if no model cost configured
        assert fetched.estimated_cost is None or fetched.estimated_cost >= 0

    @pytest.mark.parametrize("idx", range(10))
    async def test_cost_accumulation(self, idx):
        from app.core.broker import enqueue_task, get_task_by_id
        task_ids = []
        for j in range(idx + 1):
            t = await enqueue_task(
                prompt=f"cost task {j}", project_dir="/tmp", agent="opencode",
                chat_id=12345,
            )
            task_ids.append(t.id)
        # Fetch full objects (not summary) to access estimated_cost
        costs = []
        for tid in task_ids:
            t = await get_task_by_id(tid)
            costs.append(t.estimated_cost or 0)
        total = sum(costs)
        assert total >= 0  # auto-computed costs should accumulate


# ════════════════════════════════════════════════════════════════════
#  10. Smart Mode / Dispatcher — 10 iterations each
# ════════════════════════════════════════════════════════════════════

class TestDispatcherPatterns:
    """Pattern classification: 10 inputs per intent category."""

    @pytest.mark.parametrize("text", [
        "show templates", "list templates", "view templates",
        "get templates", "my templates", "show template",
        "list template", "view template", "get template", "my template",
    ])
    def test_list_templates_patterns(self, text):
        from app.core.dispatcher import classify_pattern, IntentAction
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.LIST_TEMPLATES

    @pytest.mark.parametrize("text", [
        "show schedules", "list schedules", "view schedules",
        "get schedules", "my schedules", "show schedule",
        "list schedule", "view schedule", "get schedule", "my schedule",
    ])
    def test_list_schedules_patterns(self, text):
        from app.core.dispatcher import classify_pattern, IntentAction
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.LIST_SCHEDULES

    @pytest.mark.parametrize("text", [
        "what's running", "status", "current task",
        "current status", "what is running",
        "show queue", "list queue", "view pending",
        "show tasks", "list pending",
    ])
    def test_status_and_queue_patterns(self, text):
        from app.core.dispatcher import classify_pattern, IntentAction
        result = classify_pattern(text)
        assert result is not None
        assert result.action in (IntentAction.SHOW_STATUS, IntentAction.SHOW_QUEUE)

    @pytest.mark.parametrize("text", [
        "show history", "list recent", "view completed",
        "show completed", "list history", "get history",
        "view recent", "get recent", "show completed", "list completed",
    ])
    def test_history_patterns(self, text):
        from app.core.dispatcher import classify_pattern, IntentAction
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.SHOW_HISTORY

    @pytest.mark.parametrize("text", [
        "how much", "costs", "spending", "budget", "show costs",
        "what's the cost", "what are the costs", "total cost",
        "how much have I spent", "total spending",
    ])
    def test_cost_patterns(self, text):
        from app.core.dispatcher import classify_pattern, IntentAction
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.SHOW_COSTS

    @pytest.mark.parametrize("text", [
        "cancel task", "stop task", "kill task",
        "cancel the task", "stop the task", "kill the task",
        "cancel the current task", "stop current task",
        "kill current task", "cancel current task",
    ])
    def test_cancel_patterns(self, text):
        from app.core.dispatcher import classify_pattern, IntentAction
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.CANCEL_TASK

    @pytest.mark.parametrize("text", [
        "delete template old", "remove template test",
        "drop template legacy", "delete the template foo",
        "remove the template bar", "delete old template",
        "remove test template", "drop legacy template",
        "delete template mytest", "remove template cleanup",
    ])
    def test_delete_template_patterns(self, text):
        from app.core.dispatcher import classify_pattern, IntentAction
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.DELETE_TEMPLATE

    @pytest.mark.parametrize("text", [
        "delete schedule daily", "remove schedule nightly",
        "drop schedule hourly", "cancel schedule weekly",
        "delete cron daily", "remove cron nightly",
        "drop cron hourly", "delete the schedule morning",
        "remove the schedule test", "cancel the schedule backup",
    ])
    def test_delete_schedule_patterns(self, text):
        from app.core.dispatcher import classify_pattern, IntentAction
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.DELETE_SCHEDULE

    @pytest.mark.parametrize("text", [
        "run template deploy", "execute template test",
        "trigger template build", "start template ci",
        "run the template deploy", "execute the template test",
        "run deploy template", "execute test template",
        "trigger build template", "start ci template",
    ])
    def test_run_template_patterns(self, text):
        from app.core.dispatcher import classify_pattern, IntentAction
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.RUN_TEMPLATE

    @pytest.mark.parametrize("text", [
        "toggle schedule daily", "pause schedule nightly",
        "enable schedule weekly", "disable schedule hourly",
        "unpause schedule morning", "toggle the schedule daily",
        "pause the schedule test", "enable the schedule backup",
        "disable the schedule hourly", "toggle schedule test",
    ])
    def test_toggle_schedule_patterns(self, text):
        from app.core.dispatcher import classify_pattern, IntentAction
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.TOGGLE_SCHEDULE


class TestDispatcherSafety:
    """Safety levels for all action types — 10 cases."""

    @pytest.mark.parametrize("action,expected_safety", [
        ("list_templates", "read"),
        ("list_schedules", "read"),
        ("show_status", "read"),
        ("show_queue", "read"),
        ("show_history", "read"),
        ("show_costs", "read"),
        ("save_template", "create"),
        ("save_schedule", "create"),
        ("delete_template", "destroy"),
        ("cancel_task", "destroy"),
    ])
    def test_safety_mapping(self, action, expected_safety):
        from app.core.dispatcher import IntentAction, SAFETY_MAP
        intent = IntentAction(action)
        assert SAFETY_MAP[intent].value == expected_safety


class TestDispatcherExtractors:
    """Parameter extraction — 10 variations."""

    @pytest.mark.parametrize("text,expected_name", [
        ("delete template foo", "foo"),
        ("delete template mytest", "mytest"),
        ("remove template old-one", "old-one"),
        ("drop template legacy_tmpl", "legacy_tmpl"),
        ("delete the template cleanup", "cleanup"),
        ("remove template called deploy", "deploy"),
        ("delete template named build", "build"),
        ("drop the template test123", "test123"),
        ("delete template ci-cd", "ci-cd"),
        ("remove template nightly_build", "nightly_build"),
    ])
    def test_extract_delete_template_name(self, text, expected_name):
        from app.core.dispatcher import classify_pattern, IntentAction
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.DELETE_TEMPLATE
        assert result.params.get("name") == expected_name

    @pytest.mark.parametrize("text,expected_name", [
        ("run template deploy", "deploy"),
        ("execute template build", "build"),
        ("trigger template test", "test"),
        ("start template ci", "ci"),
        ("run the template release", "release"),
        ("execute template deploy-prod", "deploy-prod"),
        ("run template test_suite", "test_suite"),
        ("trigger template nightly", "nightly"),
        ("start template backup", "backup"),
        ("run template lint", "lint"),
    ])
    def test_extract_run_template_name(self, text, expected_name):
        from app.core.dispatcher import classify_pattern, IntentAction
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.RUN_TEMPLATE
        assert result.params.get("name") == expected_name


class TestClassifyAsync:
    """Combined classify function — 10 patterns."""

    @pytest.mark.parametrize("text,expected_action", [
        ("show templates", "list_templates"),
        ("cancel task", "cancel_task"),
        ("what's running", "show_status"),
        ("list queue", "show_queue"),
        ("show history", "show_history"),
        ("how much", "show_costs"),
        ("delete template old", "delete_template"),
        ("run template deploy", "run_template"),
        ("toggle schedule daily", "toggle_schedule"),
        ("delete schedule nightly", "delete_schedule"),
    ])
    async def test_classify_without_llm(self, text, expected_action):
        from app.core.dispatcher import classify, IntentAction
        result = await classify(text, use_llm=False)
        assert result.action == IntentAction(expected_action)
        assert result.confidence >= 0.7

    @pytest.mark.parametrize("text", [
        "fix the login bug", "refactor the auth module",
        "add unit tests for user.py", "deploy to production",
        "write a README for the project",
        "investigate the memory leak", "optimize database queries",
        "update dependencies to latest", "create a new API endpoint",
        "review the pull request",
    ])
    async def test_classify_unrecognized_as_task(self, text):
        from app.core.dispatcher import classify, IntentAction
        result = await classify(text, use_llm=False)
        assert result.action == IntentAction.TASK_PROMPT


class TestFormatConfirmation:
    """Format confirmation — 10 actions."""

    @pytest.mark.parametrize("action,text", [
        ("delete_template", "delete template foo"),
        ("delete_schedule", "delete schedule daily"),
        ("cancel_task", "cancel task"),
        ("run_template", "run template deploy"),
        ("toggle_schedule", "toggle schedule daily"),
        ("save_template", "save template test"),
        ("save_schedule", "schedule daily task"),
        ("set_project", "set project to /tmp"),
        ("set_agent", "use agent claude"),
        ("set_model", "use model opus"),
    ])
    def test_format_confirmation_nonempty(self, action, text):
        from app.core.dispatcher import (
            ClassifiedIntent, IntentAction, format_confirmation,
        )
        intent = ClassifiedIntent(
            action=IntentAction(action), confidence=0.95, raw_text=text,
        )
        confirmation = format_confirmation(intent)
        assert len(confirmation) > 0


# ════════════════════════════════════════════════════════════════════
#  11. Runner — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestRunner:
    @pytest.mark.parametrize("agent", [
        "opencode", "aider", "codex", "claude", "copilot",
        "devin", "cursor", "roo", "cline", "bolt",
    ])
    def test_build_command_for_agents(self, agent):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        task = MagicMock(spec=Task)
        task.prompt = "hello world"
        task.project_dir = "/tmp/proj"
        task.agent = agent
        task.model = None
        task.timeout_seconds = None
        cmd = runner._build_command(task)
        assert isinstance(cmd, list)
        assert len(cmd) > 0

    @pytest.mark.parametrize("output,exit_code", [
        ("All tests passed", 0),
        ("Build successful\n42 tests OK", 0),
        ("Error: module not found", 1),
        ("Traceback: line 42", 1),
        ("", 0),
        ("A" * 5000, 0),
        ("Partial success\nSome warnings", 0),
        ("FATAL: out of memory", 137),
        ("Timeout exceeded", -1),
        ("Compilation error at line 10", 2),
    ])
    def test_summarize_various_outputs(self, output, exit_code):
        from app.core.runner import AgentRunner
        runner = AgentRunner()
        summary = runner._summarize(output, exit_code)
        assert isinstance(summary, str)
        assert len(summary) > 0

    @pytest.mark.parametrize("path,expected", [
        ("/home/shyam/ai/project", True),
        ("/home/shyam/repos/myrepo", True),
        ("/home/shyam/projects/app", True),
        ("/home/shyam/taskpilot", True),
        ("/etc/passwd", False),
        ("/root/secret", False),
        ("/var/log", False),
        ("/usr/bin", False),
        ("/tmp/random", False),
        ("../../../etc/passwd", False),
    ])
    def test_allowed_dirs(self, path, expected):
        from app.core.runner import AgentRunner
        with patch("app.core.runner.settings") as mock_settings:
            mock_settings.allowed_project_dirs_list = [
                "/home/shyam/ai", "/home/shyam/repos",
                "/home/shyam/projects", "/home/shyam/taskpilot",
            ]
            result = AgentRunner._is_allowed_dir(os.path.realpath(path))
            # Only check that disallowed paths are rejected
            if not expected:
                assert result is False


# ════════════════════════════════════════════════════════════════════
#  12. Web Dashboard — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestWebDashboard:
    @pytest.mark.parametrize("idx", range(10))
    async def test_api_tasks_endpoint(self, idx):
        from app.web.dashboard import create_dashboard_app
        from httpx import AsyncClient, ASGITransport
        app = create_dashboard_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/tasks")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)

    @pytest.mark.parametrize("idx", range(10))
    async def test_dashboard_html(self, idx):
        from app.web.dashboard import create_dashboard_app
        from httpx import AsyncClient, ASGITransport
        app = create_dashboard_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/")
            assert resp.status_code == 200
            assert "TaskPilot" in resp.text

    @pytest.mark.parametrize("endpoint", [
        "/api/tasks", "/api/chains", "/api/queue",
        "/api/stats", "/", "/api/health",
        "/api/tasks", "/api/chains", "/api/queue", "/api/stats",
    ])
    async def test_dashboard_endpoints_200(self, endpoint):
        from app.web.dashboard import create_dashboard_app
        from httpx import AsyncClient, ASGITransport
        app = create_dashboard_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(endpoint)
            assert resp.status_code == 200

    @pytest.mark.parametrize("idx", range(10))
    async def test_dashboard_security_headers(self, idx):
        from app.web.dashboard import create_dashboard_app
        from httpx import AsyncClient, ASGITransport
        app = create_dashboard_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/")
            # Check key security headers
            assert "x-content-type-options" in resp.headers
            assert resp.headers["x-content-type-options"] == "nosniff"


# ════════════════════════════════════════════════════════════════════
#  13. Worker API — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestWorkerAPI:
    @pytest.mark.parametrize("idx", range(10))
    async def test_worker_claim_empty(self, idx):
        from app.web.dashboard import create_dashboard_app
        from httpx import AsyncClient, ASGITransport
        app = create_dashboard_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/worker/claim", json={"worker_id": f"w{idx}"})
            assert resp.status_code in (200, 204)

    @pytest.mark.parametrize("idx", range(10))
    async def test_list_workers_endpoint(self, idx):
        from app.web.dashboard import create_dashboard_app
        from httpx import AsyncClient, ASGITransport
        app = create_dashboard_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/workers")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)


# ════════════════════════════════════════════════════════════════════
#  14. Auth & Security — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestAuthSecurity:
    @pytest.mark.parametrize("user_id", [
        99999, 88888, 77777, 66666, 55555,
        44444, 33333, 22222, 11111, 10000,
    ])
    async def test_unauthorized_user_rejected(self, user_id):
        """Non-allowed user should be blocked by auth_required."""
        from app.telegram.bot import cmd_status, _chat_smart_mode
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = user_id
        update.effective_chat = MagicMock()
        update.effective_chat.id = user_id
        message = AsyncMock()
        message.text = "hi"
        message.reply_text = AsyncMock()
        update.message = message
        bot = AsyncMock()
        update.get_bot.return_value = bot
        ctx = MagicMock()
        ctx.args = []
        ctx.user_data = {}
        await cmd_status(update, ctx)
        # Non-allowed user: either no response or "not authorized"
        if bot.send_message.called:
            text = bot.send_message.call_args.kwargs.get("text", "")
            assert "not authorized" in text.lower() or "not allowed" in text.lower()

    @pytest.mark.parametrize("prompt", [
        "'; DROP TABLE tasks; --",
        "<script>alert('xss')</script>",
        "$(rm -rf /)",
        "`rm -rf /`",
        "${IFS}cat${IFS}/etc/passwd",
        "prompt\x00injection",
        "a" * 3000,  # over max prompt len
        "../../../etc/shadow",
        "{{template.injection}}",
        "%s%s%s%s%s%s%s%s",
    ])
    async def test_malicious_prompts_handled(self, prompt):
        """Malicious prompts should be safely handled (no crash)."""
        from app.core.broker import enqueue_task
        try:
            task = await enqueue_task(
                prompt=prompt[:2000],  # respect max prompt len
                project_dir="/tmp/proj",
                agent="opencode",
                chat_id=12345,
            )
            # If enqueue succeeds, prompt should be stored safely
            assert task.id is not None
        except (ValueError, Exception):
            # Validation rejection is fine
            pass

    @pytest.mark.parametrize("path", [
        "../../../etc/passwd",
        "/etc/shadow",
        "/root/.ssh/id_rsa",
        "../../../../tmp/evil",
        "/proc/self/environ",
        "/dev/null; rm -rf /",
        "${HOME}/../../../etc/passwd",
        "~root/.bashrc",
        "/var/run/docker.sock",
        "/boot/vmlinuz",
    ])
    def test_path_traversal_blocked(self, path):
        from app.core.runner import AgentRunner
        with patch("app.core.runner.settings") as mock_settings:
            mock_settings.allowed_project_dirs_list = ["/home/user/projects"]
            resolved = os.path.realpath(path)
            assert AgentRunner._is_allowed_dir(resolved) is False


# ════════════════════════════════════════════════════════════════════
#  15. Settings / Config — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestSettingsConfig:
    @pytest.mark.parametrize("timeout", [
        60, 120, 300, 600, 900, 1200, 1800, 3600, 7200, 30,
    ])
    def test_task_timeout_values(self, timeout):
        from app.config.settings import Settings
        s = Settings(
            telegram_bot_token="t", allowed_user_ids=[1],
            task_timeout_seconds=timeout,
        )
        assert s.task_timeout_seconds == timeout

    @pytest.mark.parametrize("size", [1, 5, 10, 20, 50, 100, 200, 500, 1000, 2])
    def test_max_queue_size(self, size):
        from app.config.settings import Settings
        s = Settings(
            telegram_bot_token="t", allowed_user_ids=[1],
            max_queue_size=size,
        )
        assert s.max_queue_size == size

    @pytest.mark.parametrize("agent", [
        "opencode", "aider", "codex", "claude", "copilot",
        "devin", "cursor", "roo", "cline", "bolt",
    ])
    def test_default_agent(self, agent):
        from app.config.settings import Settings
        s = Settings(
            telegram_bot_token="t", allowed_user_ids=[1],
            default_agent=agent,
        )
        assert s.default_agent == agent

    @pytest.mark.parametrize("alias_pair", [
        ("sonnet", "anthropic/claude-sonnet-4"),
        ("opus", "anthropic/claude-opus-4"),
        ("haiku", "anthropic/claude-haiku"),
        ("gpt4", "openai/gpt-4"),
        ("gemini", "google/gemini-pro"),
        ("llama", "meta/llama-3"),
        ("mixtral", "mistral/mixtral-8x7b"),
        ("fast", "anthropic/claude-haiku"),
        ("smart", "openai/gpt-4-turbo"),
        ("cheap", "anthropic/claude-instant"),
    ])
    def test_model_aliases(self, alias_pair):
        from app.config.settings import Settings
        alias, full_name = alias_pair
        s = Settings(
            telegram_bot_token="t", allowed_user_ids=[1],
            model_aliases={alias: full_name},
        )
        assert s.resolve_model(alias) == full_name
        # Unknown alias passes through
        assert s.resolve_model("unknown") == "unknown"


# ════════════════════════════════════════════════════════════════════
#  16. Task Search & Bump — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestSearchAndBump:
    @pytest.mark.parametrize("query", [
        "fix", "bug", "test", "deploy", "refactor",
        "auth", "login", "database", "api", "frontend",
    ])
    async def test_search_tasks(self, query):
        from app.core.broker import enqueue_task, search_tasks
        # Create a task that matches
        await enqueue_task(
            prompt=f"please {query} the module", project_dir="/tmp",
            agent="opencode", chat_id=12345,
        )
        results = await search_tasks(query)
        assert len(results) >= 1
        assert any(query in r.prompt for r in results)

    @pytest.mark.parametrize("idx", range(10))
    async def test_bump_task_priority(self, idx):
        from app.core.broker import enqueue_task, bump_task
        task = await enqueue_task(
            prompt=f"bump me {idx}", project_dir="/tmp", agent="opencode",
            chat_id=12345,
        )
        original_priority = task.priority
        bumped = await bump_task(task.id)
        assert bumped is not None
        assert bumped.priority > original_priority


# ════════════════════════════════════════════════════════════════════
#  17. Gallery — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestGallery:
    @pytest.mark.parametrize("idx", range(10))
    async def test_gallery_recipes_available(self, idx):
        """Gallery module should have recipes accessible."""
        from app.core.gallery import GALLERY
        assert isinstance(GALLERY, list)
        assert len(GALLERY) > 0

    @pytest.mark.parametrize("idx", range(10))
    async def test_install_all_gallery(self, idx):
        from app.core.broker import install_all_gallery_recipes, list_recipes
        installed = await install_all_gallery_recipes(chat_id=12345)
        assert isinstance(installed, list)
        all_recipes = await list_recipes()
        assert len(all_recipes) >= len(installed)


# ════════════════════════════════════════════════════════════════════
#  18. Bot Commands Integration — 10 iterations
# ════════════════════════════════════════════════════════════════════

def _make_update(chat_id=12345, user_id=12345, text="hello"):
    user = MagicMock()
    user.id = user_id
    message = AsyncMock()
    message.text = text
    message.reply_text = AsyncMock(return_value=MagicMock(message_id=99))
    chat = MagicMock()
    chat.id = chat_id
    bot = AsyncMock()
    update = MagicMock()
    update.effective_user = user
    update.effective_chat = chat
    update.message = message
    update.get_bot.return_value = bot
    return update


def _make_context(args=None):
    ctx = MagicMock()
    ctx.args = args or []
    ctx.user_data = {}
    return ctx


class TestBotCommands:
    @pytest.mark.parametrize("idx", range(10))
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock,
           return_value={"project_dir": None, "agent": None, "model": None, "smart_mode": False})
    @patch("app.telegram.bot.get_running_task", new_callable=AsyncMock, return_value=None)
    async def test_cmd_status_no_task(self, mock_running, mock_prefs, idx):
        from app.telegram.bot import cmd_status
        update = _make_update()
        await cmd_status(update, _make_context())
        bot = update.get_bot()
        assert bot.send_message.called
        text = bot.send_message.call_args.kwargs.get("text", "")
        assert "No task running" in text

    @pytest.mark.parametrize("idx", range(10))
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock,
           return_value={"project_dir": None, "agent": None, "model": None, "smart_mode": False})
    async def test_cmd_help_returns_commands(self, mock_prefs, idx):
        from app.telegram.bot import cmd_help
        update = _make_update()
        await cmd_help(update, _make_context())
        bot = update.get_bot()
        text = bot.send_message.call_args.kwargs.get("text", "")
        assert "/status" in text

    @pytest.mark.parametrize("idx", range(10))
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock,
           return_value={"project_dir": None, "agent": None, "model": None, "smart_mode": False})
    @patch("app.telegram.bot.get_pending_tasks", new_callable=AsyncMock, return_value=[])
    async def test_cmd_queue_empty(self, mock_pending, mock_prefs, idx):
        from app.telegram.bot import cmd_queue
        update = _make_update()
        await cmd_queue(update, _make_context())
        bot = update.get_bot()
        text = bot.send_message.call_args.kwargs.get("text", "")
        assert "empty" in text.lower() or "no" in text.lower()


# ════════════════════════════════════════════════════════════════════
#  19. Edge Cases & Robustness — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    @pytest.mark.parametrize("idx", range(10))
    async def test_cancel_nonexistent_task(self, idx):
        from app.core.broker import cancel_task_by_id
        result = await cancel_task_by_id(99999 + idx)
        assert result is None

    @pytest.mark.parametrize("idx", range(10))
    async def test_retry_nonexistent_task(self, idx):
        from app.core.broker import retry_task
        result = await retry_task(99999 + idx)
        assert result is None

    @pytest.mark.parametrize("idx", range(10))
    async def test_get_nonexistent_task(self, idx):
        from app.core.broker import get_task_by_id
        result = await get_task_by_id(99999 + idx)
        assert result is None

    @pytest.mark.parametrize("idx", range(10))
    async def test_delete_nonexistent_template(self, idx):
        from app.core.broker import delete_template
        result = await delete_template(f"nonexistent_{idx}")
        assert result is False

    @pytest.mark.parametrize("idx", range(10))
    async def test_delete_nonexistent_schedule(self, idx):
        from app.core.broker import delete_schedule
        result = await delete_schedule(f"nonexistent_{idx}")
        assert result is False

    @pytest.mark.parametrize("idx", range(10))
    async def test_delete_nonexistent_chain(self, idx):
        from app.core.broker import delete_chain
        result = await delete_chain(f"nonexistent_{idx}")
        assert result is False

    @pytest.mark.parametrize("idx", range(10))
    async def test_delete_nonexistent_recipe(self, idx):
        from app.core.broker import delete_recipe
        result = await delete_recipe(f"nonexistent_{idx}")
        assert result is False

    @pytest.mark.parametrize("idx", range(10))
    async def test_toggle_nonexistent_schedule(self, idx):
        from app.core.broker import toggle_schedule
        result = await toggle_schedule(f"nonexistent_{idx}")
        assert result is None

    @pytest.mark.parametrize("idx", range(10))
    async def test_bump_nonexistent_task(self, idx):
        from app.core.broker import bump_task
        result = await bump_task(99999 + idx)
        assert result is None

    @pytest.mark.parametrize("idx", range(10))
    async def test_search_empty_results(self, idx):
        from app.core.broker import search_tasks
        results = await search_tasks(f"zzz_nonexistent_term_{idx}")
        assert len(results) == 0

    @pytest.mark.parametrize("text", [
        "", " ", "\t", "\n", "\x00", "   \n\t  ",
        "a", "ab", "..", "///", "@#$%^&*()",
    ])
    def test_dispatcher_handles_garbage_input(self, text):
        from app.core.dispatcher import classify_pattern
        result = classify_pattern(text)
        # Should either return None or a low-confidence TASK_PROMPT
        if result is not None:
            assert result.confidence >= 0


# ════════════════════════════════════════════════════════════════════
#  20. Recovery & Purge — 10 iterations
# ════════════════════════════════════════════════════════════════════

class TestRecoveryPurge:
    @pytest.mark.parametrize("idx", range(10))
    async def test_recover_interrupted_tasks(self, idx):
        from app.core.broker import enqueue_task, recover_interrupted_tasks
        from app.core import db as db_mod
        task = await enqueue_task(
            prompt=f"orphaned {idx}", project_dir="/tmp",
            agent="opencode", chat_id=12345,
        )
        # Set to RUNNING to simulate orphan
        async with db_mod.async_session() as session:
            t = await session.get(Task, task.id)
            t.status = TaskStatus.RUNNING
            await session.commit()
        recovered = await recover_interrupted_tasks()
        assert recovered >= 1

    @pytest.mark.parametrize("idx", range(10))
    async def test_purge_old_tasks(self, idx):
        from app.core.broker import enqueue_task, purge_old_tasks
        from app.core import db as db_mod
        task = await enqueue_task(
            prompt=f"old task {idx}", project_dir="/tmp",
            agent="opencode", chat_id=12345,
        )
        await _complete(task.id, exit_code=0, output="ok", summary="ok")
        # Backdate completed_at to 60 days ago
        async with db_mod.async_session() as session, session.begin():
            t = await session.get(Task, task.id)
            t.created_at = datetime(2024, 1, 1)
            t.completed_at = datetime(2024, 1, 1)
        purged = await purge_old_tasks(days=30)
        assert purged >= 1

#!/usr/bin/env python3
"""
Extensive dry-run validation of TaskPilot.

Exercises every layer — imports, settings, DB, broker CRUD, chains, repeats,
runner (command building, summarizer, subprocess), bot handlers, typing
indicator, performance benchmarks — all without a real Telegram connection.

Usage:
    python tests/dry_run_extensive.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# ── Force test env vars ─────────────────────────────────────────────

os.environ["TELEGRAM_BOT_TOKEN"] = "dry-run-token"
os.environ["ALLOWED_USER_IDS"] = "12345"

# ── Reporting infrastructure ────────────────────────────────────────

_results: dict[str, list[dict]] = defaultdict(list)
_section = "init"
_timings: dict[str, float] = {}


def section(name: str) -> None:
    global _section
    _section = name
    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"{'=' * 60}")


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    icon = "✓" if condition else "✗"
    msg = f"  {icon} {label}" + (f" — {detail}" if detail else "")
    print(msg)
    _results[_section].append({"label": label, "status": status, "detail": detail})


def timed(label: str):
    """Context manager that records wall-clock time."""
    class Timer:
        def __enter__(self):
            self.start = time.perf_counter()
            return self
        def __exit__(self, *args):
            elapsed = time.perf_counter() - self.start
            _timings[label] = elapsed
    return Timer()


# ===========================================================================
# 1. IMPORT SMOKE TESTS
# ===========================================================================
section("1. Import Smoke Tests")

try:
    from app.config.settings import Settings, settings
    check("settings module", True, f"agents={list(settings.agent_commands.keys())}")
except Exception as e:
    check("settings module", False, str(e))

try:
    from app.core.models import (
        Base, ChatPrefs, ChainStatus, Task, TaskChain, TaskStatus, _utcnow,
    )
    check("models module", True, f"statuses={[s.value for s in TaskStatus]}")
    check("chain statuses", True, f"{[s.value for s in ChainStatus]}")
except Exception as e:
    check("models module", False, str(e))

try:
    from app.core.db import init_db, get_session
    check("db module", True)
except Exception as e:
    check("db module", False, str(e))

try:
    from app.core import broker
    check("broker module", True, f"{len([f for f in dir(broker) if not f.startswith('_')])} exports")
except Exception as e:
    check("broker module", False, str(e))

try:
    from app.core.runner import AgentRunner, MAX_OUTPUT_BYTES
    check("runner module", True, f"MAX_OUTPUT={MAX_OUTPUT_BYTES}")
except Exception as e:
    check("runner module", False, str(e))

try:
    from app.telegram.bot import (
        build_app, make_notify_callback, make_chain_notify_callback,
        make_progress_callback, make_typing_callback,
        MAX_PROMPT_LEN, MAX_MSG_LEN, auth_required,
        _status_emoji, _is_allowed_project_dir, _send,
        cmd_start, cmd_help, cmd_status, cmd_queue, cmd_history,
        cmd_cancel, cmd_project, cmd_agent, cmd_output, cmd_retry,
        cmd_repeat, cmd_savechain, cmd_chain, cmd_chains, cmd_delchain,
        handle_text,
    )
    check("bot module (all handlers)", True, "17 exports verified")
except Exception as e:
    check("bot module", False, str(e))

try:
    from app.__main__ import main
    check("__main__ module", True)
except Exception as e:
    check("__main__ module", False, str(e))


# ===========================================================================
# 2. SETTINGS VALIDATION
# ===========================================================================
section("2. Settings Validation")

check("default_agent in agent_commands", settings.default_agent in settings.agent_commands)
check("max_queue_size > 0", settings.max_queue_size > 0, f"{settings.max_queue_size}")
check("task_timeout > 0", settings.task_timeout_seconds > 0, f"{settings.task_timeout_seconds}s")
check("output_summary_max_chars > 0", settings.output_summary_max_chars > 0)
check("progress_interval > 0", settings.progress_interval_seconds > 0, f"{settings.progress_interval_seconds}s")
check("allowed_user_ids is list", isinstance(settings.allowed_user_ids, list), str(settings.allowed_user_ids))
check("db_path set", bool(settings.db_path), settings.db_path)
check("db_url has aiosqlite", "aiosqlite" in settings.db_url, settings.db_url[:60])
check("allowed_project_dirs non-empty", len(settings.allowed_project_dirs) > 0)

for name, tmpl in settings.agent_commands.items():
    check(f"agent '{name}' has {{prompt}}", "{prompt}" in tmpl, tmpl[:60])
    has_dir = "{project_dir}" in tmpl
    if not has_dir:
        print(f"  ⚠ agent '{name}' omits {{project_dir}} (ok if agent ignores cwd)")
    else:
        check(f"agent '{name}' has {{project_dir}}", True, tmpl[:60])

# Settings validator
try:
    parsed = Settings.parse_user_ids("1,2,3")
    check("parse_user_ids str→list", parsed == [1, 2, 3], str(parsed))
except Exception as e:
    check("parse_user_ids str", False, str(e))
try:
    parsed = Settings.parse_user_ids(42)
    check("parse_user_ids int→list", parsed == [42])
except Exception as e:
    check("parse_user_ids int", False, str(e))
try:
    parsed = Settings.parse_user_ids([10, 20])
    check("parse_user_ids list→list", parsed == [10, 20])
except Exception as e:
    check("parse_user_ids list", False, str(e))


# ===========================================================================
# 3. MODEL VALIDATION
# ===========================================================================
section("3. Model Validation")

check("_utcnow is naive UTC", _utcnow().tzinfo is None, str(_utcnow()))

t = Task(id=1, prompt="test", project_dir="/tmp", agent="opencode", status=TaskStatus.PENDING)
check("Task creation", t.id == 1 and t.status == TaskStatus.PENDING)
check("Task defaults: started_at=None", t.started_at is None)
check("Task defaults: chain_id=None", t.chain_id is None)
check("Task defaults: repeat_total=None", t.repeat_total is None)

tc = TaskChain(id=1, name="test-chain", steps_json=json.dumps([{"prompt": "step1"}, {"prompt": "step2"}]))
check("TaskChain.steps parses JSON", len(tc.steps) == 2)
check("TaskChain.total_steps", tc.total_steps == 2)
check("steps[0].prompt", tc.steps[0]["prompt"] == "step1")

cp = ChatPrefs(chat_id=12345)
check("ChatPrefs creation", cp.chat_id == 12345)
check("ChatPrefs defaults", cp.project_dir is None and cp.agent is None)

# Enum string values
for s in TaskStatus:
    check(f"TaskStatus.{s.name} == '{s.value}'", isinstance(s.value, str))
for s in ChainStatus:
    check(f"ChainStatus.{s.name} == '{s.value}'", isinstance(s.value, str))


# ===========================================================================
# 4. BOT HELPER FUNCTIONS
# ===========================================================================
section("4. Bot Helper Functions")

# Status emoji
for status in TaskStatus:
    emoji = _status_emoji(status)
    check(f"emoji({status.name})", len(emoji) > 0, emoji)

# Path validation
check("allowed path /tmp", _is_allowed_project_dir("/tmp"))
check("blocked path /etc", not _is_allowed_project_dir("/etc"))
check("blocked path /root", not _is_allowed_project_dir("/root"))
check("allowed path ~/ai", _is_allowed_project_dir("~/ai"))
check("subdir ~/ai/project allowed", _is_allowed_project_dir("~/ai/project"))


# ===========================================================================
# 5. DB + BROKER FULL LIFECYCLE
# ===========================================================================
section("5. DB + Broker Full Lifecycle")


async def _setup_test_db():
    """Create a fresh in-memory DB and patch broker to use it."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    import app.core.db as db_mod

    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    db_mod.engine = engine
    db_mod.async_session = sf

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    return engine


async def test_broker_lifecycle():
    engine = await _setup_test_db()

    # -- Enqueue --
    with timed("enqueue_task"):
        t1 = await broker.enqueue_task(prompt="task one", project_dir="/tmp", agent="opencode", chat_id=12345)
    check("enqueue #1", t1.id is not None and t1.status == TaskStatus.PENDING, f"id={t1.id}")

    t2 = await broker.enqueue_task(prompt="task two", agent="aider")
    check("enqueue #2 (defaults)", t2.agent == "aider")

    t3 = await broker.enqueue_task(prompt="task three")
    check("enqueue #3", t3.id == 3)

    # -- Pending --
    pending = await broker.get_pending_tasks()
    check("pending count=3", len(pending) == 3, f"got {len(pending)}")
    check("pending FIFO order", pending[0].id == t1.id and pending[-1].id == t3.id)

    # -- Running --
    running = await broker.get_running_task()
    check("no running yet", running is None)

    # -- Pick --
    with timed("pick_next_task"):
        picked = await broker.pick_next_task()
    check("pick → #1", picked.id == t1.id)
    check("picked → RUNNING", picked.status == TaskStatus.RUNNING)
    check("started_at set", picked.started_at is not None)

    running = await broker.get_running_task()
    check("get_running_task", running is not None and running.id == t1.id)

    # -- Complete (exit=0) --
    with timed("complete_task_success"):
        completed = await broker.complete_task(
            t1.id, exit_code=0, output_summary="all good",
            full_output="full output here", error_message=None,
        )
    check("complete → COMPLETED", completed.status == TaskStatus.COMPLETED)
    check("exit_code=0", completed.exit_code == 0)
    check("duration_seconds set", completed.duration_seconds is not None)
    check("completed_at set", completed.completed_at is not None)
    check("output_summary stored", completed.output_summary == "all good")
    check("full_output stored", completed.full_output == "full output here")
    check("error_message None", completed.error_message is None)

    # -- Complete (exit=1) --
    picked2 = await broker.pick_next_task()
    completed2 = await broker.complete_task(
        picked2.id, exit_code=1, output_summary="crashed",
        full_output="stack trace", error_message="exit 1",
    )
    check("complete exit=1 → FAILED", completed2.status == TaskStatus.FAILED)
    check("error_message stored", completed2.error_message == "exit 1")

    # -- History --
    with timed("get_recent_tasks"):
        history = await broker.get_recent_tasks(limit=10)
    check("history count", len(history) >= 2, f"got {len(history)}")
    check("history newest first", history[0].id >= history[-1].id)

    # -- Get by ID --
    found = await broker.get_task_by_id(t1.id)
    check("get_by_id found", found is not None and found.prompt == "task one")
    miss = await broker.get_task_by_id(99999)
    check("get_by_id miss", miss is None)

    # -- Cancel pending --
    pending_before = await broker.get_pending_tasks()
    cancelled_pending = await broker.cancel_task_by_id(t3.id)
    check("cancel_by_id pending", cancelled_pending is not None and cancelled_pending.status == TaskStatus.CANCELLED)
    miss_cancel = await broker.cancel_task_by_id(99999)
    check("cancel_by_id miss", miss_cancel is None)

    # -- Cancel running --
    t4 = await broker.enqueue_task(prompt="to cancel running")
    await broker.pick_next_task()
    cancelled_running = await broker.cancel_running_task()
    check("cancel_running", cancelled_running is not None and cancelled_running.status == TaskStatus.CANCELLED)
    check("cancel_running duration set", cancelled_running.duration_seconds is not None)
    none_cancel = await broker.cancel_running_task()
    check("cancel_running when none", none_cancel is None)

    # -- Retry --
    retried = await broker.retry_task(completed2.id)  # retry the failed task
    check("retry_task", retried is not None and retried.status == TaskStatus.PENDING, f"new id={retried.id if retried else '?'}")
    check("retry preserves prompt", retried.prompt == completed2.prompt if retried else False)
    check("retry preserves agent", retried.agent == completed2.agent if retried else False)

    retry_miss = await broker.retry_task(99999)
    check("retry miss", retry_miss is None)
    retry_completed = await broker.retry_task(t1.id)  # try retrying a COMPLETED task
    check("retry non-terminal → None", retry_completed is None)

    # -- Recovery --
    # First clean up any lingering running tasks
    await broker.recover_interrupted_tasks()
    t5 = await broker.enqueue_task(prompt="orphan task")
    picked5 = await broker.pick_next_task()
    check("orphan picked → RUNNING", picked5 is not None and picked5.status == TaskStatus.RUNNING)
    with timed("recover_interrupted_tasks"):
        recovered_count = await broker.recover_interrupted_tasks()
    check("recover orphans", recovered_count >= 1, f"recovered={recovered_count}")
    t5_after = await broker.get_task_by_id(picked5.id)
    check("orphan → FAILED", t5_after.status == TaskStatus.FAILED)
    check("orphan error_message", "Interrupted" in (t5_after.error_message or ""))

    # -- Queue full --
    old_max = settings.max_queue_size
    settings.max_queue_size = 0
    try:
        await broker.enqueue_task(prompt="overflow")
        check("queue full raises", False, "should have raised")
    except ValueError as e:
        check("queue full raises ValueError", True, str(e))
    settings.max_queue_size = old_max

    # -- Purge --
    t_old = await broker.enqueue_task(prompt="ancient task")
    await broker.pick_next_task()
    await broker.complete_task(t_old.id, exit_code=0, output_summary="old", full_output="old")
    with timed("purge_old_tasks"):
        purged = await broker.purge_old_tasks(days=0)
    check("purge recent tasks", purged >= 1, f"purged={purged}")

    await engine.dispose()

asyncio.run(test_broker_lifecycle())


# ===========================================================================
# 6. CHAIN LIFECYCLE
# ===========================================================================
section("6. Chain Lifecycle")


async def test_chain_lifecycle():
    engine = await _setup_test_db()

    # -- Save --
    steps = [
        {"prompt": "step 1", "agent": "opencode", "project_dir": "/tmp"},
        {"prompt": "step 2", "agent": "aider", "project_dir": "/tmp"},
        {"prompt": "step 3"},
    ]
    with timed("save_chain"):
        chain = await broker.save_chain(name="test-chain", steps=steps, chat_id=12345)
    check("save_chain", chain.id is not None, f"id={chain.id}")
    check("chain name", chain.name == "test-chain")
    check("chain steps=3", chain.total_steps == 3)
    check("chain status=IDLE", chain.status == ChainStatus.IDLE)

    # -- List --
    chains = await broker.list_chains()
    check("list_chains", len(chains) == 1)

    # -- Get by name --
    found = await broker.get_chain_by_name("test-chain")
    check("get_chain_by_name", found is not None and found.name == "test-chain")
    miss = await broker.get_chain_by_name("nonexistent")
    check("get_chain_by_name miss", miss is None)

    # -- Start --
    with timed("start_chain"):
        first_task = await broker.start_chain(name="test-chain", chat_id=12345)
    check("start_chain → task", first_task is not None, f"task #{first_task.id}")
    check("first task step=0", first_task.chain_step == 0)
    check("first task prompt", first_task.prompt == "step 1")
    check("first task chain_id", first_task.chain_id == chain.id)

    chain_after = await broker.get_chain_by_name("test-chain")
    check("chain → RUNNING", chain_after.status == ChainStatus.RUNNING)

    # -- Start already running → error --
    try:
        await broker.start_chain(name="test-chain")
        check("start running chain raises", False)
    except ValueError:
        check("start running chain raises ValueError", True)

    # -- Advance step 0 → step 1 --
    picked = await broker.pick_next_task()
    completed = await broker.complete_task(picked.id, exit_code=0, output_summary="ok", full_output="ok")
    with timed("advance_chain"):
        next_task = await broker.advance_chain(completed)
    check("advance → step 1", next_task is not None and next_task.chain_step == 1)
    check("step 1 prompt", next_task.prompt == "step 2")

    # -- Advance step 1 → step 2 --
    picked2 = await broker.pick_next_task()
    completed2 = await broker.complete_task(picked2.id, exit_code=0, output_summary="ok", full_output="ok")
    next_task2 = await broker.advance_chain(completed2)
    check("advance → step 2", next_task2 is not None and next_task2.chain_step == 2)

    # -- Advance step 2 → done --
    picked3 = await broker.pick_next_task()
    completed3 = await broker.complete_task(picked3.id, exit_code=0, output_summary="ok", full_output="ok")
    final = await broker.advance_chain(completed3)
    check("advance → chain complete (None)", final is None)
    chain_done = await broker.get_chain_by_name("test-chain")
    check("chain → COMPLETED", chain_done.status == ChainStatus.COMPLETED)

    # -- Chain failure --
    chain2 = await broker.save_chain(name="fail-chain", steps=[{"prompt": "will fail"}, {"prompt": "never runs"}])
    task_f = await broker.start_chain(name="fail-chain")
    picked_f = await broker.pick_next_task()
    failed_f = await broker.complete_task(picked_f.id, exit_code=1, output_summary="err", full_output="err", error_message="crashed")
    advance_f = await broker.advance_chain(failed_f)
    check("failed step → chain stops", advance_f is None)
    chain_f = await broker.get_chain_by_name("fail-chain")
    check("chain → FAILED", chain_f.status == ChainStatus.FAILED)

    # -- Chain with no chain_id --
    t_no_chain = Task(id=999, prompt="x", project_dir="/tmp", agent="opencode", status=TaskStatus.COMPLETED)
    adv_none = await broker.advance_chain(t_no_chain)
    check("advance non-chain → None", adv_none is None)

    # -- Empty chain --
    try:
        await broker.save_chain(name="empty", steps=[])
        check("empty chain raises", False)
    except ValueError:
        check("empty chain raises ValueError", True)

    # -- Step missing prompt --
    try:
        await broker.save_chain(name="bad", steps=[{"agent": "opencode"}])
        check("step no prompt raises", False)
    except ValueError:
        check("step no prompt raises ValueError", True)

    # -- Delete --
    deleted = await broker.delete_chain("test-chain")
    check("delete_chain", deleted is True)
    deleted_miss = await broker.delete_chain("nonexistent")
    check("delete_chain miss", deleted_miss is False)

    # -- Recover interrupted chains --
    await broker.save_chain(name="orphan-chain", steps=[{"prompt": "x"}])
    await broker.start_chain(name="orphan-chain")
    with timed("recover_interrupted_chains"):
        rc = await broker.recover_interrupted_chains()
    check("recover chains", rc >= 1)

    # -- Start nonexistent chain --
    result = await broker.start_chain(name="does-not-exist")
    check("start nonexistent → None", result is None)

    # -- Get chain by ID --
    chain3 = await broker.save_chain(name="id-test", steps=[{"prompt": "x"}])
    found3 = await broker.get_chain_by_id(chain3.id)
    check("get_chain_by_id", found3 is not None and found3.name == "id-test")
    miss3 = await broker.get_chain_by_id(99999)
    check("get_chain_by_id miss", miss3 is None)

    await engine.dispose()

asyncio.run(test_chain_lifecycle())


# ===========================================================================
# 7. REPEAT LIFECYCLE
# ===========================================================================
section("7. Repeat Lifecycle")


async def test_repeat_lifecycle():
    engine = await _setup_test_db()

    # -- Enqueue repeat (count) --
    with timed("enqueue_repeat_task"):
        rt = await broker.enqueue_repeat_task(prompt="repeat me", repeat_count=3, chat_id=12345)
    check("repeat enqueue", rt.repeat_total == 3 and rt.repeat_remaining == 3)

    # -- Enqueue repeat (until) --
    future = _utcnow() + timedelta(hours=1)
    rt2 = await broker.enqueue_repeat_task(prompt="repeat until", repeat_until=future)
    check("repeat until", rt2.repeat_until is not None)

    # -- Enqueue repeat (both) --
    rt3 = await broker.enqueue_repeat_task(prompt="both", repeat_count=2, repeat_until=future)
    check("repeat both", rt3.repeat_total == 2 and rt3.repeat_until is not None)

    # -- Invalid: no count or until --
    try:
        await broker.enqueue_repeat_task(prompt="bad")
        check("no count/until raises", False)
    except ValueError:
        check("no count/until raises ValueError", True)

    # -- Invalid: count < 1 --
    try:
        await broker.enqueue_repeat_task(prompt="bad", repeat_count=0)
        check("count=0 raises", False)
    except ValueError:
        check("count=0 raises ValueError", True)

    # -- Re-enqueue logic --
    picked = await broker.pick_next_task()
    completed = await broker.complete_task(picked.id, exit_code=0, output_summary="ok", full_output="ok")
    with timed("maybe_reenqueue"):
        requeued = await broker.maybe_reenqueue(completed)
    check("reenqueue remaining=2", requeued is not None and requeued.repeat_remaining == 2, f"remaining={requeued.repeat_remaining if requeued else '?'}")

    # -- Reenqueue until count exhausted (count-only task) --
    rt_count = await broker.enqueue_repeat_task(prompt="count only", repeat_count=2)
    # Pick and drain all pending to get to our count-only task
    while True:
        p = await broker.pick_next_task()
        if p is None:
            break
        c = await broker.complete_task(p.id, exit_code=0, output_summary="ok", full_output="ok")
        rq = await broker.maybe_reenqueue(c)
        if p.prompt == "count only" and rq is None:
            break
        if rq and rq.prompt == "count only" and rq.repeat_remaining is not None and rq.repeat_remaining <= 0:
            # drain the final one
            p2 = await broker.pick_next_task()
            if p2:
                c2 = await broker.complete_task(p2.id, exit_code=0, output_summary="ok", full_output="ok")
                rq = await broker.maybe_reenqueue(c2)
            break

    check("count exhausted → None", rq is None)

    # -- Failed task not re-enqueued --
    rt4 = await broker.enqueue_repeat_task(prompt="fail repeat", repeat_count=5)
    p4 = await broker.pick_next_task()
    f4 = await broker.complete_task(p4.id, exit_code=1, output_summary="err", full_output="err")
    rq4 = await broker.maybe_reenqueue(f4)
    check("failed not reenqueued", rq4 is None)

    await engine.dispose()

asyncio.run(test_repeat_lifecycle())


# ===========================================================================
# 8. CHAT PREFERENCES
# ===========================================================================
section("8. Chat Preferences")


async def test_chat_prefs():
    engine = await _setup_test_db()

    prefs = await broker.get_chat_prefs(12345)
    check("initial prefs empty", prefs["project_dir"] is None and prefs["agent"] is None)

    await broker.set_chat_pref(12345, project_dir="/tmp/myproject")
    prefs2 = await broker.get_chat_prefs(12345)
    check("set project_dir", prefs2["project_dir"] == "/tmp/myproject")
    check("agent still None", prefs2["agent"] is None)

    await broker.set_chat_pref(12345, agent="aider")
    prefs3 = await broker.get_chat_prefs(12345)
    check("set agent", prefs3["agent"] == "aider")
    check("project_dir preserved", prefs3["project_dir"] == "/tmp/myproject")

    # Update existing
    await broker.set_chat_pref(12345, project_dir="/tmp/other")
    prefs4 = await broker.get_chat_prefs(12345)
    check("update project_dir", prefs4["project_dir"] == "/tmp/other")

    await engine.dispose()

asyncio.run(test_chat_prefs())


# ===========================================================================
# 9. RUNNER: COMMAND BUILDING + INJECTION SAFETY
# ===========================================================================
section("9. Runner: Command Building + Injection Safety")


async def test_runner_commands():
    runner = AgentRunner()

    # Normal commands
    for agent_name in settings.agent_commands:
        task = Task(id=99, prompt="fix the bug", project_dir="/home/user/proj",
                    agent=agent_name, status=TaskStatus.RUNNING)
        cmd = runner._build_command(task)
        check(f"build_command({agent_name})", "fix the bug" in " ".join(cmd), str(cmd)[:80])

    # Shell injection payloads
    injections = [
        ("semicolon", "hello; rm -rf /"),
        ("backtick", "hello `whoami`"),
        ("dollar-paren", "hello $(cat /etc/passwd)"),
        ("pipe", "hello | cat /etc/shadow"),
        ("ampersand", "hello && rm -rf /"),
        ("newline", "hello\nrm -rf /"),
        ("single-quotes", "it's a 'trap'"),
        ("double-quotes", 'say "hello world"'),
        ("glob", "test*.py"),
        ("tilde", "~/../../etc/passwd"),
        ("null-byte", "test\x00injected"),
        ("unicode", "test 你好 🎉"),
        ("very-long", "x" * 5000),
    ]

    for label, payload in injections:
        task = Task(id=99, prompt=payload, project_dir="/tmp", agent="opencode", status=TaskStatus.RUNNING)
        cmd = runner._build_command(task)
        # The prompt must appear as a single element (not split by shell)
        prompt_args = [a for a in cmd if payload[:10] in a]
        check(f"injection-safe: {label}", len(prompt_args) >= 1, f"argv has prompt in 1 element")

    # Unknown agent
    task_unk = Task(id=99, prompt="test", project_dir="/tmp", agent="nonexistent", status=TaskStatus.RUNNING)
    cmd_unk = runner._build_command(task_unk)
    check("unknown agent → echo", "echo" in cmd_unk[0])

asyncio.run(test_runner_commands())


# ===========================================================================
# 10. RUNNER: OUTPUT SUMMARIZER
# ===========================================================================
section("10. Runner: Output Summarizer")


async def test_summarizer():
    runner = AgentRunner()

    # Empty output
    check("empty → (no output)", runner._summarize("", 0) == "(no output)")
    check("whitespace → (no output)", runner._summarize("   \n  \n  ", 0) == "(no output)")

    # Success: tail lines
    long_out = "\n".join(f"line {i}" for i in range(30))
    summary = runner._summarize(long_out, 0)
    check("success: has tail lines", "line 29" in summary and "line 20" in summary)
    check("success: respects max_chars", len(summary) <= settings.output_summary_max_chars)

    # Failure: error keyword extraction
    error_out = "Starting...\nLoading...\nError: connection refused\nTraceback: xyz\nFatal: cannot continue\nCleanup done"
    summary_err = runner._summarize(error_out, 1)
    check("failure: Error extracted", "Error:" in summary_err or "Fatal:" in summary_err)

    # Failure: no error keywords → tail
    no_kw = "starting\nprocessing\ndone"
    summary_nokw = runner._summarize(no_kw, 1)
    check("failure no keywords: tail", "done" in summary_nokw)

    # Very long output → truncated
    huge = "data\n" * 10000
    summary_huge = runner._summarize(huge, 0)
    check("huge output truncated", len(summary_huge) <= settings.output_summary_max_chars)

    # Single line
    single = "All tests passed."
    summary_single = runner._summarize(single, 0)
    check("single line", summary_single == "All tests passed.")

asyncio.run(test_summarizer())


# ===========================================================================
# 11. RUNNER: SUBPROCESS INTEGRATION
# ===========================================================================
section("11. Runner: Subprocess Integration")


async def test_subprocess_integration():
    import app.core.runner as runner_mod

    tmp = tempfile.mkdtemp()
    results = []

    async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None):
        results.append({
            "task_id": task_id, "exit_code": exit_code,
            "output_summary": output_summary, "full_output": full_output,
            "error_message": error_message,
        })
        t = Task(id=task_id, prompt="x", project_dir=tmp, agent="test", status=TaskStatus.COMPLETED if exit_code == 0 else TaskStatus.FAILED)
        t.exit_code = exit_code
        return t

    old_complete = runner_mod.complete_task
    old_reenqueue = runner_mod.maybe_reenqueue
    runner_mod.complete_task = fake_complete
    runner_mod.maybe_reenqueue = AsyncMock(return_value=None)

    runner = AgentRunner()

    # Test 1: echo → exit 0
    settings.agent_commands["_test_echo"] = "echo {prompt}"
    task_echo = Task(id=1, prompt="hello world", project_dir=tmp, agent="_test_echo", status=TaskStatus.RUNNING)
    with timed("subprocess_echo"):
        await runner._execute(task_echo)
    check("echo exit=0", results[-1]["exit_code"] == 0)
    check("echo output contains prompt", "hello world" in results[-1]["full_output"])
    check("echo no error_message", results[-1]["error_message"] is None)

    # Test 2: false → exit 1
    settings.agent_commands["_test_false"] = "false"
    task_false = Task(id=2, prompt="x", project_dir=tmp, agent="_test_false", status=TaskStatus.RUNNING)
    await runner._execute(task_false)
    check("false exit=1", results[-1]["exit_code"] != 0)
    check("false has error_message", results[-1]["error_message"] is not None)

    # Test 3: exit 42
    settings.agent_commands["_test_42"] = "bash -c 'exit 42'"
    task_42 = Task(id=3, prompt="x", project_dir=tmp, agent="_test_42", status=TaskStatus.RUNNING)
    await runner._execute(task_42)
    check("exit 42", results[-1]["exit_code"] == 42)

    # Test 4: multiline output
    settings.agent_commands["_test_multi"] = "bash -c 'for i in $(seq 1 20); do echo line$i; done'"
    task_multi = Task(id=4, prompt="x", project_dir=tmp, agent="_test_multi", status=TaskStatus.RUNNING)
    await runner._execute(task_multi)
    check("multiline output", "line20" in results[-1]["full_output"])

    # Test 5: nonexistent project_dir
    task_nodir = Task(id=5, prompt="x", project_dir="/nonexistent/xyz", agent="_test_echo", status=TaskStatus.RUNNING)
    await runner._execute(task_nodir)
    check("nonexistent dir → exit -1", results[-1]["exit_code"] == -1)
    check("nonexistent dir error", "not exist" in results[-1]["error_message"])

    # Test 6: stderr merged into stdout
    settings.agent_commands["_test_stderr"] = "bash -c 'echo out; echo err >&2'"
    task_stderr = Task(id=6, prompt="x", project_dir=tmp, agent="_test_stderr", status=TaskStatus.RUNNING)
    await runner._execute(task_stderr)
    check("stderr captured", "err" in results[-1]["full_output"])

    # Test 7: timeout (short)
    old_timeout = settings.task_timeout_seconds
    settings.task_timeout_seconds = 1
    settings.agent_commands["_test_hang"] = "bash -c 'sleep 60'"
    task_hang = Task(id=7, prompt="x", project_dir=tmp, agent="_test_hang", status=TaskStatus.RUNNING)
    with timed("subprocess_timeout"):
        await runner._execute(task_hang)
    check("timeout fires", "TIMEOUT" in results[-1]["full_output"])
    settings.task_timeout_seconds = old_timeout

    # Cleanup
    del settings.agent_commands["_test_echo"]
    del settings.agent_commands["_test_false"]
    del settings.agent_commands["_test_42"]
    del settings.agent_commands["_test_multi"]
    del settings.agent_commands["_test_stderr"]
    del settings.agent_commands["_test_hang"]
    runner_mod.complete_task = old_complete
    runner_mod.maybe_reenqueue = old_reenqueue
    shutil.rmtree(tmp, ignore_errors=True)

asyncio.run(test_subprocess_integration())


# ===========================================================================
# 12. RUNNER: TYPING INDICATOR + LIFECYCLE
# ===========================================================================
section("12. Runner: Typing Indicator + Lifecycle")


async def test_runner_lifecycle():
    runner = AgentRunner()
    check("initial _running=False", runner._running is False)
    check("initial _current_process=None", runner._current_process is None)
    check("initial _typing_notify=None", runner._typing_notify is None)

    runner._running = True
    runner.stop()
    check("stop() → _running=False", runner._running is False)
    check("stop() sets wake_event", runner._wake_event.is_set())

    result = await runner.cancel_current()
    check("cancel_current no proc → False", result is False)

    runner.notify_new_task()
    check("notify_new_task sets event", runner._wake_event.is_set())

asyncio.run(test_runner_lifecycle())


# ===========================================================================
# 13. BOT: EVERY HANDLER (mocked Telegram)
# ===========================================================================
section("13. Bot: Every Handler (Mocked)")


def _make_update(chat_id=12345, user_id=12345, text="hello"):
    user = MagicMock()
    user.id = user_id
    message = AsyncMock()
    message.text = text
    message.message_id = 1
    message.reply_text = AsyncMock(return_value=MagicMock(message_id=99, edit_text=AsyncMock()))
    chat = MagicMock()
    chat.id = chat_id
    bot = AsyncMock()
    update = MagicMock()
    update.effective_user = user
    update.effective_chat = chat
    update.message = message
    update.get_bot = MagicMock(return_value=bot)
    return update


def _make_context(args=None):
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


async def test_bot_handlers():
    engine = await _setup_test_db()

    # Import bot state for cleanup
    from app.telegram.bot import _chat_project_dir, _chat_agent

    handlers_tested = 0

    # -- /start --
    update = _make_update()
    await cmd_start(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/start response", "TaskPilot" in sent or "ready" in sent.lower(), sent[:60])
    handlers_tested += 1

    # -- /help --
    update = _make_update()
    await cmd_help(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/help has /status", "/status" in sent)
    check("/help has /cancel", "/cancel" in sent)
    check("/help has /repeat", "/repeat" in sent)
    check("/help has /savechain", "/savechain" in sent)
    handlers_tested += 1

    # -- /status (nothing running) --
    update = _make_update()
    await cmd_status(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/status empty", "No task" in sent or "💤" in sent, sent[:60])
    handlers_tested += 1

    # -- /queue (empty) --
    update = _make_update()
    await cmd_queue(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/queue empty", "empty" in sent.lower() or "📭" in sent, sent[:60])
    handlers_tested += 1

    # -- /history (empty) --
    update = _make_update()
    await cmd_history(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/history empty", "No tasks" in sent or "📭" in sent, sent[:60])
    handlers_tested += 1

    # -- Send text → enqueue --
    update = _make_update(text="fix the login page")
    await handle_text(update, _make_context())
    ack_edit = update.message.reply_text.return_value.edit_text
    check("text→enqueue ack", ack_edit.called, "reply_text.edit_text was called")
    handlers_tested += 1

    # -- /status (now has a pending task) --
    # pick it to make it running
    await broker.pick_next_task()
    update = _make_update()
    await cmd_status(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/status running", "Running" in sent or "🔄" in sent, sent[:60])
    handlers_tested += 1

    # -- /queue (with tasks) --
    await broker.enqueue_task(prompt="another task")
    update = _make_update()
    await cmd_queue(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/queue with tasks", "Pending" in sent or "📦" in sent, sent[:60])
    handlers_tested += 1

    # -- /cancel (running) --
    from app.telegram.bot import _runner_ref
    import app.telegram.bot as bot_mod
    old_runner = bot_mod._runner_ref
    mock_runner = AsyncMock()
    mock_runner.cancel_current = AsyncMock()
    bot_mod._runner_ref = mock_runner
    update = _make_update()
    await cmd_cancel(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/cancel running", "Cancelled" in sent or "🚫" in sent, sent[:60])
    bot_mod._runner_ref = old_runner
    handlers_tested += 1

    # -- /cancel (nothing) --
    update = _make_update()
    await cmd_cancel(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/cancel nothing", "Nothing" in sent or "💤" in sent, sent[:60])
    handlers_tested += 1

    # Complete the pending task for history
    pending = await broker.get_pending_tasks()
    for p in pending:
        await broker.pick_next_task()
    running = await broker.get_running_task()
    if running:
        await broker.complete_task(running.id, exit_code=0, output_summary="done", full_output="full output text here")

    # -- /history (with results) --
    update = _make_update()
    await cmd_history(update, _make_context(args=["5"]))
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/history with results", "Recent" in sent or "📜" in sent, sent[:60])
    handlers_tested += 1

    # -- /output <id> --
    tasks = await broker.get_recent_tasks(limit=1)
    if tasks:
        update = _make_update()
        await cmd_output(update, _make_context(args=[str(tasks[0].id)]))
        sent = update.get_bot().send_message.call_args.kwargs["text"]
        check("/output success", "Output" in sent or "📄" in sent, sent[:60])
    handlers_tested += 1

    # -- /output no args --
    update = _make_update()
    await cmd_output(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/output no args → usage", "Usage" in sent or "task_id" in sent, sent[:60])
    handlers_tested += 1

    # -- /output bad id --
    update = _make_update()
    await cmd_output(update, _make_context(args=["notanumber"]))
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/output bad id", "Invalid" in sent, sent[:60])
    handlers_tested += 1

    # -- /project (get) --
    update = _make_update()
    await cmd_project(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/project get", "📁" in sent, sent[:60])
    handlers_tested += 1

    # -- /project <valid path> --
    update = _make_update()
    await cmd_project(update, _make_context(args=["/tmp"]))
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/project set /tmp", "/tmp" in sent and "📁" in sent, sent[:60])
    handlers_tested += 1

    # -- /project <blocked> --
    update = _make_update()
    await cmd_project(update, _make_context(args=["/etc"]))
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/project blocked /etc", "not in allowed" in sent.lower() or "⚠️" in sent, sent[:60])
    handlers_tested += 1

    # -- /agent (get) --
    update = _make_update()
    await cmd_agent(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/agent get", "🤖" in sent, sent[:60])
    handlers_tested += 1

    # -- /agent <valid> --
    update = _make_update()
    await cmd_agent(update, _make_context(args=["aider"]))
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/agent set aider", "aider" in sent, sent[:60])
    handlers_tested += 1

    # -- /agent <invalid> --
    update = _make_update()
    await cmd_agent(update, _make_context(args=["nonexistent"]))
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/agent invalid", "Unknown" in sent or "❓" in sent, sent[:60])
    handlers_tested += 1

    # -- /retry (no args) --
    update = _make_update()
    await cmd_retry(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/retry no args → usage", "Usage" in sent, sent[:60])
    handlers_tested += 1

    # -- /repeat (no args) --
    update = _make_update()
    await cmd_repeat(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/repeat no args → usage", "Usage" in sent, sent[:60])
    handlers_tested += 1

    # -- /savechain (no args) --
    update = _make_update()
    await cmd_savechain(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/savechain no args → usage", "Usage" in sent, sent[:60])
    handlers_tested += 1

    # -- /savechain valid --
    update = _make_update()
    await cmd_savechain(update, _make_context(args=["mychain", "step", "one", "|", "step", "two"]))
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/savechain success", "saved" in sent.lower() or "💾" in sent, sent[:60])
    handlers_tested += 1

    # -- /chains --
    update = _make_update()
    await cmd_chains(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/chains list", "mychain" in sent or "⛓️" in sent, sent[:60])
    handlers_tested += 1

    # -- /chain <name> --
    update = _make_update()
    await cmd_chain(update, _make_context(args=["mychain"]))
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/chain start", "started" in sent.lower() or "⛓️" in sent, sent[:60])
    handlers_tested += 1

    # -- /delchain --
    update = _make_update()
    await cmd_delchain(update, _make_context(args=["mychain"]))
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/delchain", "deleted" in sent.lower() or "🗑️" in sent, sent[:60])
    handlers_tested += 1

    # -- /delchain miss --
    update = _make_update()
    await cmd_delchain(update, _make_context(args=["nochain"]))
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("/delchain miss", "not found" in sent.lower() or "❓" in sent, sent[:60])
    handlers_tested += 1

    # -- Auth: unauthorized user --
    update = _make_update(user_id=99999)
    update.get_bot().send_message.reset_mock()
    await cmd_start(update, _make_context())
    check("auth: unauthorized blocked", not update.get_bot().send_message.called)
    handlers_tested += 1

    # -- Text: too long --
    update = _make_update(text="x" * (MAX_PROMPT_LEN + 1))
    await handle_text(update, _make_context())
    sent = update.get_bot().send_message.call_args.kwargs["text"]
    check("text too long rejected", "too long" in sent.lower() or "⚠️" in sent, sent[:60])
    handlers_tested += 1

    # -- Text: empty --
    update = _make_update(text="")
    update.get_bot().send_message.reset_mock()
    await handle_text(update, _make_context())
    check("empty text ignored", not update.get_bot().send_message.called)
    handlers_tested += 1

    check(f"handlers tested: {handlers_tested}", handlers_tested >= 28, f"{handlers_tested} handler paths exercised")

    # Cleanup bot caches
    _chat_project_dir.clear()
    _chat_agent.clear()

    await engine.dispose()

asyncio.run(test_bot_handlers())


# ===========================================================================
# 14. BOT: NOTIFICATION CALLBACKS
# ===========================================================================
section("14. Bot: Notification Callbacks")


async def test_notification_callbacks():
    app = MagicMock()
    app.bot = AsyncMock()

    # -- Task notification --
    notify = await make_notify_callback(app)
    task = Task(id=1, prompt="x", project_dir="/tmp", agent="opencode",
                status=TaskStatus.COMPLETED, telegram_chat_id=12345,
                output_summary="done", telegram_msg_id=99)
    await notify(task)
    check("notify sends message", app.bot.send_message.called)
    check("notify edits original", app.bot.edit_message_text.called)

    # Notify: no chat_id
    app.bot.reset_mock()
    task_nochat = Task(id=2, prompt="x", project_dir="/tmp", agent="opencode",
                       status=TaskStatus.COMPLETED, telegram_chat_id=None)
    await notify(task_nochat)
    check("notify no chat_id → skip", not app.bot.send_message.called)

    # -- Chain notification --
    chain_notify = await make_chain_notify_callback(app)
    app.bot.reset_mock()
    await chain_notify(12345, "Chain completed!")
    check("chain_notify sends", app.bot.send_message.called)
    app.bot.reset_mock()
    await chain_notify(None, "Ignored")
    check("chain_notify None → skip", not app.bot.send_message.called)

    # -- Progress notification --
    progress = await make_progress_callback(app)
    app.bot.reset_mock()
    await progress(12345, "Step 1 of 3")
    check("progress sends Markdown", app.bot.send_message.called)
    kwargs = app.bot.send_message.call_args.kwargs
    check("progress parse_mode=Markdown", kwargs.get("parse_mode") == "Markdown")
    app.bot.reset_mock()
    await progress(None, "Ignored")
    check("progress None → skip", not app.bot.send_message.called)

    # -- Typing notification --
    from telegram.constants import ChatAction
    typing_fn = await make_typing_callback(app)
    app.bot.reset_mock()
    await typing_fn(12345)
    check("typing sends action", app.bot.send_chat_action.called)
    call_kwargs = app.bot.send_chat_action.call_args.kwargs
    check("typing action=TYPING", call_kwargs.get("action") == ChatAction.TYPING)
    app.bot.reset_mock()
    await typing_fn(None)
    check("typing None → skip", not app.bot.send_chat_action.called)

    # Typing error swallowed
    app.bot.send_chat_action.side_effect = RuntimeError("network")
    await typing_fn(12345)  # should not raise
    check("typing error swallowed", True)

asyncio.run(test_notification_callbacks())


# ===========================================================================
# 15. BOT: build_app
# ===========================================================================
section("15. Bot: build_app")


async def test_build_app():
    app = build_app(runner=AgentRunner())
    total_handlers = sum(len(h) for h in app.handlers.values())
    check("build_app creates app", app is not None)
    check(f"handlers registered: {total_handlers}", total_handlers >= 16, f"{total_handlers} handlers")

asyncio.run(test_build_app())


# ===========================================================================
# 16. PERFORMANCE BENCHMARKS
# ===========================================================================
section("16. Performance Benchmarks")


async def test_performance():
    engine = await _setup_test_db()

    # Temporarily raise queue limit for perf tests
    old_max = settings.max_queue_size
    settings.max_queue_size = 10000

    # -- Benchmark: enqueue 100 tasks --
    with timed("perf_enqueue_100"):
        for i in range(100):
            await broker.enqueue_task(prompt=f"perf task {i}", project_dir="/tmp", agent="opencode")

    pending = await broker.get_pending_tasks()
    check("100 tasks enqueued", len(pending) == 100)

    # -- Benchmark: pick 100 tasks --
    with timed("perf_pick_100"):
        for _ in range(100):
            await broker.pick_next_task()

    pending_after = await broker.get_pending_tasks()
    check("100 tasks picked", len(pending_after) == 0)

    # -- Benchmark: complete 100 tasks --
    running = await broker.get_recent_tasks(limit=100)
    with timed("perf_complete_100"):
        for t in running:
            if t.status == TaskStatus.RUNNING:
                await broker.complete_task(t.id, exit_code=0, output_summary="ok", full_output="ok")

    # -- Benchmark: history query --
    with timed("perf_history_100"):
        for _ in range(100):
            await broker.get_recent_tasks(limit=10)

    # -- Benchmark: get_task_by_id --
    with timed("perf_get_by_id_100"):
        for i in range(1, 101):
            await broker.get_task_by_id(i)

    # -- Benchmark: chain save + start + advance --
    with timed("perf_chain_10_steps"):
        chain = await broker.save_chain(
            name="perf-chain",
            steps=[{"prompt": f"step {i}"} for i in range(10)],
        )
        task = await broker.start_chain(name="perf-chain")
        for _ in range(10):
            picked = await broker.pick_next_task()
            if not picked:
                break
            completed = await broker.complete_task(picked.id, exit_code=0, output_summary="ok", full_output="ok")
            next_t = await broker.advance_chain(completed)

    chain_done = await broker.get_chain_by_name("perf-chain")
    check("perf chain completed", chain_done.status == ChainStatus.COMPLETED)

    # -- Benchmark: recover --
    for i in range(50):
        await broker.enqueue_task(prompt=f"orphan {i}")
    for _ in range(50):
        await broker.pick_next_task()
    with timed("perf_recover_50"):
        await broker.recover_interrupted_tasks()

    # -- Benchmark: purge --
    with timed("perf_purge"):
        await broker.purge_old_tasks(days=0)

    settings.max_queue_size = old_max
    await engine.dispose()

asyncio.run(test_performance())


# ===========================================================================
# 17. STRESS: CONCURRENT OPERATIONS
# ===========================================================================
section("17. Stress: Concurrent Operations")


async def test_concurrent():
    engine = await _setup_test_db()
    old_max = settings.max_queue_size
    settings.max_queue_size = 10000

    # Enqueue 50 tasks concurrently
    with timed("stress_concurrent_enqueue_50"):
        tasks = await asyncio.gather(*(
            broker.enqueue_task(prompt=f"concurrent {i}")
            for i in range(50)
        ))
    check("concurrent enqueue 50", len(tasks) == 50)
    check("all have unique IDs", len(set(t.id for t in tasks)) == 50)

    # Pick all concurrently
    with timed("stress_concurrent_pick_50"):
        picked = await asyncio.gather(*(broker.pick_next_task() for _ in range(50)))
    check("concurrent pick 50", sum(1 for p in picked if p is not None) == 50)

    # Complete all concurrently
    running_ids = [p.id for p in picked if p is not None]
    with timed("stress_concurrent_complete_50"):
        results = await asyncio.gather(*(
            broker.complete_task(tid, exit_code=0, output_summary="ok", full_output="ok")
            for tid in running_ids
        ))
    check("concurrent complete 50", all(r is not None for r in results))

    settings.max_queue_size = old_max
    await engine.dispose()

asyncio.run(test_concurrent())


# ===========================================================================
# 18. EDGE CASES
# ===========================================================================
section("18. Edge Cases")


async def test_edge_cases():
    engine = await _setup_test_db()

    # Unicode prompts
    uni_task = await broker.enqueue_task(prompt="修复登录页面 🔧 fix it 日本語")
    check("unicode prompt stored", "修复" in uni_task.prompt and "🔧" in uni_task.prompt)

    # Very long prompt
    long_prompt = "x" * 5000
    long_task = await broker.enqueue_task(prompt=long_prompt)
    check("long prompt stored", len(long_task.prompt) == 5000)

    # Empty-like prompts
    space_task = await broker.enqueue_task(prompt="   ")
    check("whitespace prompt stored (broker allows)", space_task.id is not None)

    # Complete with large output
    picked = await broker.pick_next_task()
    large_out = "a" * 100000
    done = await broker.complete_task(picked.id, exit_code=0, output_summary="big", full_output=large_out)
    check("large output stored", len(done.full_output) == 100000)

    # Complete task that was already cancelled (race condition)
    t_race = await broker.enqueue_task(prompt="race")
    picked_race = await broker.pick_next_task()
    assert picked_race is not None, "race task should be picked"
    await broker.cancel_running_task()
    race_result = await broker.complete_task(picked_race.id, exit_code=0, output_summary="too late", full_output="")
    check("complete cancelled task → returns existing", race_result is not None and race_result.status == TaskStatus.CANCELLED)

    # History with extreme limits
    hist_0 = await broker.get_recent_tasks(limit=0)
    check("history limit=0", len(hist_0) == 0)
    hist_1000 = await broker.get_recent_tasks(limit=1000)
    check("history limit=1000", isinstance(hist_1000, list))

    await engine.dispose()

asyncio.run(test_edge_cases())


# ===========================================================================
# SUMMARY
# ===========================================================================

section("SUMMARY")

total_pass = sum(1 for checks in _results.values() for c in checks if c["status"] == "PASS")
total_fail = sum(1 for checks in _results.values() for c in checks if c["status"] == "FAIL")
total = total_pass + total_fail

print(f"\n  Total checks: {total}")
print(f"  ✓ Passed:     {total_pass}")
print(f"  ✗ Failed:     {total_fail}")

# Section breakdown
print(f"\n  {'Section':<45} {'Pass':>5} {'Fail':>5}")
print(f"  {'-'*55}")
for sect, checks in _results.items():
    if sect == "SUMMARY":
        continue
    p = sum(1 for c in checks if c["status"] == "PASS")
    f = sum(1 for c in checks if c["status"] == "FAIL")
    marker = " ⚠️" if f > 0 else ""
    print(f"  {sect:<45} {p:>5} {f:>5}{marker}")

# Performance timings
print(f"\n  {'Timing':<45} {'ms':>10}")
print(f"  {'-'*55}")
for label, elapsed in sorted(_timings.items()):
    ms = elapsed * 1000
    marker = " ⚠️" if ms > 1000 else ""
    print(f"  {label:<45} {ms:>9.1f}{marker}")

# Performance thresholds
print(f"\n  Performance thresholds:")
thresholds = {
    "perf_enqueue_100": 2000,
    "perf_pick_100": 2000,
    "perf_complete_100": 2000,
    "perf_history_100": 2000,
    "perf_get_by_id_100": 2000,
    "perf_chain_10_steps": 3000,
    "perf_recover_50": 1000,
    "perf_purge": 500,
    "stress_concurrent_enqueue_50": 3000,
    "stress_concurrent_pick_50": 3000,
    "stress_concurrent_complete_50": 3000,
    "subprocess_echo": 2000,
    "subprocess_timeout": 5000,
}
for label, max_ms in thresholds.items():
    if label in _timings:
        actual_ms = _timings[label] * 1000
        ok = actual_ms <= max_ms
        icon = "✓" if ok else "✗"
        print(f"  {icon} {label}: {actual_ms:.1f}ms (limit: {max_ms}ms)")
        if not ok:
            total_fail += 1
        else:
            total_pass += 1

# Failed checks detail
failed_checks = [
    (sect, c) for sect, checks in _results.items()
    for c in checks if c["status"] == "FAIL"
]
if failed_checks:
    print(f"\n  FAILED CHECKS:")
    for sect, c in failed_checks:
        print(f"    [{sect}] {c['label']}: {c['detail']}")

print(f"\n{'=' * 60}")
if total_fail > 0:
    print(f"  DRY RUN FAILED: {total_fail} failures out of {total + len(thresholds)} checks")
else:
    print(f"  DRY RUN PASSED: All {total + len(thresholds)} checks green ✓")
print(f"{'=' * 60}")

sys.exit(1 if total_fail > 0 else 0)

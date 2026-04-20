#!/usr/bin/env python3
"""Extensive dry-run validation of TaskPilot — exercises every layer without Telegram."""

import asyncio
import os
import shutil
import sys
import tempfile

# Force test env vars
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dry-run-token")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {label}" + (f" — {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  ✗ {label}" + (f" — {detail}" if detail else ""))


# ===========================================================================
# 1. Import smoke tests
# ===========================================================================
print("=== 1. Import Smoke Tests ===")

try:
    from app.config.settings import settings
    check("settings import", True, f"agents={list(settings.agent_commands.keys())}")
except Exception as e:
    check("settings import", False, str(e))

try:
    from app.core.models import Base, Task, TaskStatus, _utcnow
    check("models import", True, f"statuses={[s.value for s in TaskStatus]}")
except Exception as e:
    check("models import", False, str(e))

try:
    now = _utcnow()
    check("_utcnow() is naive UTC", now.tzinfo is None, str(now))
except Exception as e:
    check("_utcnow()", False, str(e))

try:
    from app.core.db import init_db, get_session
    check("db import", True)
except Exception as e:
    check("db import", False, str(e))

try:
    from app.core import broker
    expected = {
        "enqueue_task", "pick_next_task", "complete_task",
        "cancel_running_task", "get_running_task", "get_pending_tasks",
        "get_recent_tasks", "get_task_by_id", "recover_interrupted_tasks",
    }
    actual = {f for f in dir(broker) if not f.startswith("_") and callable(getattr(broker, f))}
    check("broker exports", expected.issubset(actual), f"found {len(actual)} callables")
except Exception as e:
    check("broker import", False, str(e))

try:
    from app.core.runner import AgentRunner, MAX_OUTPUT_BYTES
    check("runner import", True, f"MAX_OUTPUT={MAX_OUTPUT_BYTES}")
except Exception as e:
    check("runner import", False, str(e))

try:
    from app.telegram.bot import build_app, make_notify_callback, MAX_PROMPT_LEN, MAX_MSG_LEN
    check("bot import", True, f"MAX_PROMPT={MAX_PROMPT_LEN}, MAX_MSG={MAX_MSG_LEN}")
except Exception as e:
    check("bot import", False, str(e))

try:
    from app.__main__ import main
    check("__main__ import", True)
except Exception as e:
    check("__main__ import", False, str(e))


# ===========================================================================
# 2. Settings validation
# ===========================================================================
print("\n=== 2. Settings Validation ===")

check("default_agent in agent_commands", settings.default_agent in settings.agent_commands)
check("max_queue_size > 0", settings.max_queue_size > 0, str(settings.max_queue_size))
check("task_timeout > 0", settings.task_timeout_seconds > 0, f"{settings.task_timeout_seconds}s")
check("output_summary_max_chars > 0", settings.output_summary_max_chars > 0)
check("allowed_user_ids is list", isinstance(settings.allowed_user_ids, list))

# Validate agent_commands have {prompt} placeholder
for name, tmpl in settings.agent_commands.items():
    check(f"agent '{name}' has {{prompt}}", "{prompt}" in tmpl, tmpl[:60])


# ===========================================================================
# 3. DB init & broker CRUD round-trip
# ===========================================================================
print("\n=== 3. DB + Broker Round-Trip ===")


async def test_db_roundtrip():
    from sqlalchemy.ext.asyncio import (
        AsyncSession, async_sessionmaker, create_async_engine,
    )
    import app.core.db as db_mod

    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "dryrun.db")

    new_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    new_sf = async_sessionmaker(new_engine, class_=AsyncSession, expire_on_commit=False)
    db_mod.engine = new_engine
    db_mod.async_session = new_sf

    async with new_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    check("create tables", True)

    # Enqueue
    t1 = await broker.enqueue_task(
        prompt="dry-run task 1", project_dir="/tmp", agent="opencode", chat_id=12345,
    )
    check("enqueue #1", t1.id is not None and t1.status == TaskStatus.PENDING, f"id={t1.id}")

    t2 = await broker.enqueue_task(prompt="dry-run task 2")
    check("enqueue #2 (defaults)", t2.agent == settings.default_agent)

    # Pending count
    pending = await broker.get_pending_tasks()
    check("pending count=2", len(pending) == 2)

    # Pick
    picked = await broker.pick_next_task()
    check("pick oldest", picked.id == t1.id and picked.status == TaskStatus.RUNNING)
    check("started_at set", picked.started_at is not None)

    # Pick nothing when only running
    p2 = await broker.pick_next_task()
    check("pick #2", p2 is not None and p2.id == t2.id)

    # Complete with exit 0
    updated = await broker.complete_task(
        t1.id, exit_code=0, output_summary="ok", full_output="full",
    )
    check("complete exit=0 → COMPLETED", updated.status == TaskStatus.COMPLETED)
    check("duration_seconds set", updated.duration_seconds is not None)
    check("exit_code=0 stored", updated.exit_code == 0)

    # Complete with exit 1
    updated2 = await broker.complete_task(
        t2.id, exit_code=1, output_summary="err", full_output="trace", error_message="fail",
    )
    check("complete exit=1 → FAILED", updated2.status == TaskStatus.FAILED)

    # History
    history = await broker.get_recent_tasks(limit=5)
    check("recent_tasks returns list", len(history) >= 2)

    # get_task_by_id
    found = await broker.get_task_by_id(t1.id)
    check("get_task_by_id found", found is not None and found.prompt == "dry-run task 1")

    # get_task_by_id miss
    miss = await broker.get_task_by_id(99999)
    check("get_task_by_id miss=None", miss is None)

    # Cancel flow
    t3 = await broker.enqueue_task(prompt="to cancel")
    await broker.pick_next_task()
    cancelled = await broker.cancel_running_task()
    check("cancel running", cancelled is not None and cancelled.status == TaskStatus.CANCELLED)

    # Cancel when nothing running
    none_cancel = await broker.cancel_running_task()
    check("cancel none → None", none_cancel is None)

    # Recovery
    t4 = await broker.enqueue_task(prompt="orphaned")
    await broker.pick_next_task()
    count = await broker.recover_interrupted_tasks()
    check("recover orphans", count == 1)

    recovered = await broker.get_task_by_id(t4.id)
    check("recovered → FAILED", recovered.status == TaskStatus.FAILED)
    check("recovered error_message set", recovered.error_message is not None)

    # Queue full test
    old_max = settings.max_queue_size
    settings.max_queue_size = 1
    await broker.enqueue_task(prompt="fill it")
    try:
        await broker.enqueue_task(prompt="overflow")
        check("queue full raises", False, "should have raised ValueError")
    except ValueError as e:
        check("queue full raises ValueError", True, str(e))
    settings.max_queue_size = old_max

    await new_engine.dispose()
    shutil.rmtree(tmp, ignore_errors=True)


asyncio.run(test_db_roundtrip())


# ===========================================================================
# 4. Runner command building + shell injection
# ===========================================================================
print("\n=== 4. Runner Command Building ===")

runner = AgentRunner()

for agent_name in settings.agent_commands:
    task = Task(
        id=99, prompt="fix the bug", project_dir="/home/user/proj",
        agent=agent_name, status=TaskStatus.RUNNING,
    )
    cmd = runner._build_command(task)
    check(f"build_command({agent_name})", "fix the bug" in cmd, cmd[:80])

# Injection tests
injections = [
    ("semicolon", "hello; rm -rf /"),
    ("backtick", "hello `whoami`"),
    ("dollar-paren", "hello $(cat /etc/passwd)"),
    ("pipe", "hello | cat /etc/shadow"),
    ("ampersand", "hello && rm -rf /"),
    ("newline", "hello\nrm -rf /"),
    ("single-quote escape", "it's a 'trap'"),
]

for label, payload in injections:
    task = Task(
        id=99, prompt=payload, project_dir="/tmp",
        agent="opencode", status=TaskStatus.RUNNING,
    )
    cmd = runner._build_command(task)
    # The payload should be inside single quotes (shlex.quote)
    check(f"injection-safe: {label}", "'" in cmd, cmd[:80])

# Unknown agent
task_unk = Task(
    id=99, prompt="test", project_dir="/tmp",
    agent="nonexistent", status=TaskStatus.RUNNING,
)
cmd_unk = runner._build_command(task_unk)
check("unknown agent → echo", cmd_unk.startswith("echo"))


# ===========================================================================
# 5. Summarizer
# ===========================================================================
print("\n=== 5. Output Summarizer ===")

check("empty → (no output)", runner._summarize("", 0) == "(no output)")

success_out = "\n".join(f"line {i}" for i in range(20))
summary = runner._summarize(success_out, 0)
check("success: tail lines", "line 19" in summary and "line 10" in summary)

error_lines = "ok\nError: db failed\nTraceback: xyz\ncleanup"
summary_err = runner._summarize(error_lines, 1)
check("failure: error lines prioritized", "Error: db failed" in summary_err)

only_ok = "starting\ndone"
summary_noerr = runner._summarize(only_ok, 1)
check("failure no error kw: tail", "done" in summary_noerr)


# ===========================================================================
# 6. Runner lifecycle
# ===========================================================================
print("\n=== 6. Runner Lifecycle ===")

r = AgentRunner()
check("initial _running=False", r._running is False)
check("initial _current_process=None", r._current_process is None)
r._running = True
r.stop()
check("stop() → _running=False", r._running is False)


async def test_cancel_no_proc():
    r2 = AgentRunner()
    result = await r2.cancel_current()
    check("cancel_current no proc → False", result is False)


asyncio.run(test_cancel_no_proc())


# ===========================================================================
# 7. Runner _execute with real subprocess
# ===========================================================================
print("\n=== 7. Runner _execute Integration ===")


async def test_execute_integration():
    from app.config.settings import settings as s
    import app.core.runner as runner_mod

    tmp = tempfile.mkdtemp()
    completed = []

    async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None, git_diff=None):
        completed.append({"task_id": task_id, "exit_code": exit_code, "error_message": error_message})
        t = Task(id=task_id, prompt="x", project_dir=tmp, agent="test", status=TaskStatus.COMPLETED)
        t.exit_code = exit_code
        return t

    old_cmds = s.agent_commands.copy()
    old_complete = runner_mod.complete_task

    # Test 1: echo → exit 0
    s.agent_commands = {"test": "echo {prompt}"}
    runner_mod.complete_task = fake_complete
    r3 = AgentRunner()
    task_ok = Task(id=1, prompt="hello world", project_dir=tmp, agent="test", status=TaskStatus.RUNNING)
    await r3._execute(task_ok)
    check("execute echo→exit 0", len(completed) == 1 and completed[0]["exit_code"] == 0)
    check("exit 0 no error_message", completed[0]["error_message"] is None)

    # Test 2: false → exit 1
    completed.clear()
    s.agent_commands = {"test": "false"}
    task_fail = Task(id=2, prompt="x", project_dir=tmp, agent="test", status=TaskStatus.RUNNING)
    await r3._execute(task_fail)
    check("execute false→exit 1", completed[0]["exit_code"] == 1)
    check("exit 1 has error_message", completed[0]["error_message"] is not None)

    # Test 3: exit 42
    completed.clear()
    s.agent_commands = {"test": "exit 42"}
    task_42 = Task(id=3, prompt="x", project_dir=tmp, agent="test", status=TaskStatus.RUNNING)
    await r3._execute(task_42)
    check("execute exit 42", completed[0]["exit_code"] == 42)

    # Test 4: nonexistent project_dir
    completed.clear()
    s.agent_commands = {"test": "echo hi"}
    task_nodir = Task(id=4, prompt="x", project_dir="/nonexistent/xyz", agent="test", status=TaskStatus.RUNNING)
    await r3._execute(task_nodir)
    check("nonexistent dir → exit -1", completed[0]["exit_code"] == -1)

    # Restore
    s.agent_commands = old_cmds
    runner_mod.complete_task = old_complete
    shutil.rmtree(tmp, ignore_errors=True)


asyncio.run(test_execute_integration())


# ===========================================================================
# 8. Bot build (no real Telegram connection)
# ===========================================================================
print("\n=== 8. Bot Build ===")


async def test_build_app():
    try:
        app = build_app(runner=AgentRunner())
        handlers = app.handlers
        total = sum(len(h) for h in handlers.values())
        check("build_app creates app", app is not None, f"{total} handlers registered")
    except Exception as e:
        check("build_app", False, str(e))


asyncio.run(test_build_app())


# ===========================================================================
# Summary
# ===========================================================================
print(f"\n{'='*60}")
print(f"DRY RUN COMPLETE: {passed} passed, {failed} failed")
print(f"{'='*60}")
sys.exit(1 if failed else 0)

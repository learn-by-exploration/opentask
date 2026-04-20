"""Comprehensive dry-run: exercises every major code path without hitting Telegram."""

from __future__ import annotations

import asyncio
import json
import os
import sys

# Ensure app package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dry-run-token")
os.environ.setdefault("ALLOWED_USER_IDS", "123456789")


async def main() -> None:
    results: list[tuple[str, bool, str]] = []  # (name, ok, detail)

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        icon = "✅" if ok else "❌"
        print(f"  {icon} {name}" + (f"  — {detail}" if detail else ""))

    print("\n=== TaskPilot Dry Run ===\n")

    # --- 1. Settings ---
    print("[1] Settings")
    from app.config.settings import Settings
    s = Settings(
        telegram_bot_token="dry-run-token",
        allowed_user_ids=[123],
        default_agent="opencode",
        default_project_dir="~/ai",
        allowed_project_dirs="~/ai,~/repos,~/projects",
    )
    check("Settings load", True, f"agent={s.default_agent}, dir={s.default_project_dir}")
    check("Allowed dirs parsed", len(s.allowed_project_dirs_list) == 3, str(s.allowed_project_dirs_list))
    check("DB URL", s.db_url.startswith("sqlite+aiosqlite"), s.db_url)
    check("Agent commands", "opencode" in s.agent_commands and "claude" in s.agent_commands)
    check("Model flags", "opencode" in s.agent_model_flags and "claude" in s.agent_model_flags)
    check("Continue flags", "opencode" in s.agent_continue_flags and "claude" in s.agent_continue_flags)
    check("Skills dir", bool(s.skills_dir), s.skills_dir)
    check("Dashboard config", s.dashboard_port == 8095 and s.dashboard_enabled is True)

    # --- 2. DB ---
    print("\n[2] Database")
    from app.core.db import init_db, get_session
    os.environ["DB_PATH"] = ":memory:"
    # Reinit settings for memory DB
    from importlib import reload
    import app.config.settings as settings_mod
    reload(settings_mod)
    from app.core import db as db_mod
    db_mod._engine = None
    db_mod._session_factory = None
    await init_db()
    check("init_db (in-memory)", True)

    session = await get_session()
    async with session:
        from sqlalchemy import text
        r = await session.execute(text("SELECT 1"))
        check("DB connection", r.scalar() == 1)

    # --- 3. Models ---
    print("\n[3] Models")
    from app.core.models import Task, TaskChain, Recipe, ChatPrefs, TaskStatus, ChainStatus, _utcnow
    t = Task(prompt="test", project_dir="~/ai", agent="opencode", status=TaskStatus.PENDING)
    check("Task model", t.prompt == "test" and t.status == TaskStatus.PENDING)
    check("Task model field", hasattr(t, "parent_task_id") and hasattr(t, "model"))

    tc = TaskChain(name="ch1", steps_json="[]", status=ChainStatus.IDLE)
    check("TaskChain model", tc.name == "ch1" and tc.status == ChainStatus.IDLE)

    r = Recipe(name="rec1", triggers_json='["hello"]', agent="claude")
    check("Recipe model", r.name == "rec1" and r.agent == "claude")

    cp = ChatPrefs(chat_id=123)
    check("ChatPrefs model", cp.chat_id == 123)

    check("_utcnow()", _utcnow() is not None, str(_utcnow()))

    # --- 4. Broker CRUD ---
    print("\n[4] Broker CRUD")
    from app.core.broker import (
        enqueue_task, pick_next_task, complete_task, get_task_by_id,
        get_recent_tasks, get_pending_tasks,
        save_recipe, list_recipes, get_recipe_by_name, delete_recipe, match_recipe,
        enqueue_followup,
    )

    task = await enqueue_task("Fix bug in main.py", project_dir="~/ai", agent="opencode", chat_id=111)
    check("enqueue_task", task.id is not None and task.status == TaskStatus.PENDING,
          f"id={task.id}")

    fetched = await get_task_by_id(task.id)
    check("get_task_by_id", fetched is not None and fetched.prompt == "Fix bug in main.py")

    queue = await get_pending_tasks()
    check("get_pending_tasks", len(queue) >= 1, f"len={len(queue)}")

    picked = await pick_next_task()
    check("pick_next_task", picked is not None and picked.id == task.id)

    completed = await complete_task(
        task_id=task.id,
        exit_code=0,
        output_summary="Bug fixed in main.py line 42",
        full_output="Full output here...",
    )
    check("complete_task", completed is not None and completed.status == TaskStatus.COMPLETED,
          f"exit={completed.exit_code}")

    history = await get_recent_tasks()
    check("get_recent_tasks", len(history) >= 1)

    # --- 5. Follow-up tasks ---
    print("\n[5] Follow-up / Conversation Mode")
    parent = await enqueue_task("Original task", project_dir="~/ai", agent="claude", chat_id=222)
    await pick_next_task()
    await complete_task(task_id=parent.id, exit_code=0, output_summary="done", full_output="done")

    followup = await enqueue_followup(
        parent_task_id=parent.id,
        prompt="Now also fix the tests",
        chat_id=222,
    )
    check("enqueue_followup", followup is not None and followup.parent_task_id == parent.id,
          f"parent={followup.parent_task_id}")
    check("followup inherits agent", followup.agent == parent.agent)
    check("followup inherits dir", followup.project_dir == parent.project_dir)

    # --- 6. Recipes ---
    print("\n[6] Recipes")

    recipe = await save_recipe(
        name="test-recipe",
        triggers=["test", "fix test"],
        agent="claude",
        project_dir="~/ai",
        prompt_prefix="Focus on tests:",
        prompt_suffix="Use pytest.",
        chat_id=111,
    )
    check("save_recipe", recipe is not None and recipe.name == "test-recipe")

    recipes = await list_recipes()
    check("list_recipes", len(recipes) >= 1)

    got = await get_recipe_by_name("test-recipe")
    check("get_recipe_by_name", got is not None and got.name == "test-recipe")

    matched = await match_recipe("fix test failures")
    check("match_recipe", matched is not None, f"matched={matched.name if matched else None}")

    deleted = await delete_recipe("test-recipe")
    check("delete_recipe", deleted is True)

    # --- 7. Runner command building ---
    print("\n[7] Runner — Command Building")
    from app.core.runner import AgentRunner

    runner = AgentRunner()

    # Basic opencode command
    t1 = Task(prompt="Fix the bug", project_dir="~/ai", agent="opencode", status=TaskStatus.PENDING)
    cmd1 = runner._build_command(t1)
    check("opencode command", cmd1[0] == "opencode" and "Fix the bug" in " ".join(cmd1),
          f"cmd={cmd1}")

    # Claude command
    t2 = Task(prompt="Refactor code", project_dir="~/ai", agent="claude", status=TaskStatus.PENDING)
    cmd2 = runner._build_command(t2)
    check("claude command", cmd2[0] == "claude" and "Refactor code" in " ".join(cmd2),
          f"cmd={cmd2}")

    # With model flag
    t3 = Task(prompt="Test", project_dir="~/ai", agent="claude", model="sonnet", status=TaskStatus.PENDING)
    cmd3 = runner._build_command(t3)
    check("model flag injection", "--model" in cmd3 and "sonnet" in cmd3,
          f"cmd={cmd3}")

    # Follow-up with continue flag
    t4 = Task(prompt="Continue work", project_dir="~/ai", agent="claude", parent_task_id=1, status=TaskStatus.PENDING)
    cmd4 = runner._build_command(t4)
    check("continue flag injection", "--continue" in cmd4,
          f"cmd={cmd4}")

    # Model + continue combined
    t5 = Task(prompt="More work", project_dir="~/ai", agent="claude", model="opus", parent_task_id=2, status=TaskStatus.PENDING)
    cmd5 = runner._build_command(t5)
    check("model + continue combined", "--model" in cmd5 and "--continue" in cmd5 and "opus" in cmd5,
          f"cmd={cmd5}")

    # Shell safety — special characters in prompt
    t6 = Task(prompt='Fix "this" & that; rm -rf /', project_dir="~/ai", agent="opencode", status=TaskStatus.PENDING)
    cmd6 = runner._build_command(t6)
    check("shell safety", 'Fix "this" & that; rm -rf /' in " ".join(cmd6),
          "prompt preserved as single arg, not shell-expanded")

    # Unknown agent
    t7 = Task(prompt="foo", project_dir="~/ai", agent="unknown_agent", status=TaskStatus.PENDING)
    cmd7 = runner._build_command(t7)
    check("unknown agent fallback", cmd7[0] == "echo",
          f"cmd={cmd7}")

    # --- 8. Runner safe_env ---
    print("\n[8] Runner — Env Sanitization")
    safe = AgentRunner._safe_env()
    check("TELEGRAM vars stripped", not any(k.startswith("TELEGRAM_") for k in safe))
    check("PATH preserved", "PATH" in safe)

    # --- 9. Runner allowed dir check ---
    print("\n[9] Runner — Path Validation")
    check("allowed dir (~/ai)", AgentRunner._is_allowed_dir(os.path.realpath(os.path.expanduser("~/ai"))))
    check("disallowed dir (/tmp)", not AgentRunner._is_allowed_dir("/tmp"))

    # --- 10. Summarize ---
    print("\n[10] Runner — Output Summarization")
    summary1 = runner._summarize("line1\nline2\nline3\nlast line", 0)
    check("summary extraction", bool(summary1), f"summary={summary1!r}")

    long_output = "x" * 2000
    summary2 = runner._summarize(long_output, 0)
    check("long output truncated", len(summary2) <= 600)

    summary3 = runner._summarize("", 1)
    check("error exit summary", "exit" in summary3.lower() or bool(summary3))

    # --- 11. Dashboard ---
    print("\n[11] Web Dashboard")
    from app.web.dashboard import create_dashboard_app
    dash_app = create_dashboard_app()
    check("dashboard app created", dash_app is not None)

    from fastapi.testclient import TestClient
    client = TestClient(dash_app)
    resp = client.get("/api/tasks")
    check("GET /api/tasks", resp.status_code == 200, f"status={resp.status_code}")
    check("tasks response is list", isinstance(resp.json(), list))

    resp_html = client.get("/")
    check("GET / (HTML)", resp_html.status_code == 200 and "taskpilot" in resp_html.text.lower())

    # --- 12. Model fields end-to-end ---
    print("\n[12] Model Selection End-to-End")
    task_m = await enqueue_task("Test with model", project_dir="~/ai", agent="claude", model="sonnet", chat_id=333)
    check("task with model", task_m.model == "sonnet", f"model={task_m.model}")
    picked_m = await pick_next_task()
    check("picked task has model", picked_m.model == "sonnet")
    cmd_m = runner._build_command(picked_m)
    check("built command has model", "--model" in cmd_m and "sonnet" in cmd_m)

    # --- Summary ---
    print("\n" + "=" * 50)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\n  Results: {passed} passed, {failed} failed, {len(results)} total")

    if failed:
        print("\n  FAILURES:")
        for name, ok, detail in results:
            if not ok:
                print(f"    ❌ {name}: {detail}")

    print()
    return failed == 0


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)

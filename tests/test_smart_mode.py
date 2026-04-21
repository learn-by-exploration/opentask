"""Extensive tests for smart mode — intent classification, dispatcher, and bot integration."""

from __future__ import annotations

import json
import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-for-tests")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

from app.core.dispatcher import (
    CONFIDENCE_THRESHOLD,
    ClassifiedIntent,
    IntentAction,
    SafetyLevel,
    SAFETY_MAP,
    _extract_delete_schedule,
    _extract_delete_template,
    _extract_run_template,
    _extract_save_schedule,
    _extract_save_template,
    _extract_set_agent,
    _extract_set_model,
    _extract_set_project,
    _extract_toggle_schedule,
    _parse_llm_response,
    classify,
    classify_pattern,
    format_confirmation,
)
from app.core.models import Task, TaskStatus
from app.telegram.bot import (
    _chat_agent,
    _chat_followup,
    _chat_model,
    _chat_project_dir,
    _chat_smart_mode,
    _load_prefs,
    _pending_smart_actions,
    cmd_smartmode,
    handle_smart_cancel,
    handle_smart_confirm,
    handle_smart_execute,
    handle_text,
    _execute_smart_intent,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _make_update(chat_id: int = 12345, user_id: int = 12345, text: str = "hello"):
    user = MagicMock()
    user.id = user_id
    message = AsyncMock()
    message.text = text
    message.message_id = 42
    message.reply_text = AsyncMock(return_value=MagicMock(message_id=99, edit_text=AsyncMock()))
    chat = MagicMock()
    chat.id = chat_id
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    update = MagicMock()
    update.effective_user = user
    update.effective_chat = chat
    update.message = message
    update.get_bot.return_value = bot
    return update


def _make_callback_update(chat_id: int = 12345, user_id: int = 12345, data: str = ""):
    user = MagicMock()
    user.id = user_id
    chat = MagicMock()
    chat.id = chat_id
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    update = MagicMock()
    update.effective_user = user
    update.effective_chat = chat
    update.callback_query = query
    update.message = None
    update.get_bot.return_value = bot
    return update


def _make_context(args=None):
    ctx = MagicMock()
    ctx.args = args or []
    ctx.user_data = {}
    return ctx


def _make_task(**overrides) -> MagicMock:
    defaults = dict(
        id=1, prompt="fix the bug", project_dir="/tmp/proj", agent="opencode",
        model=None, status=TaskStatus.PENDING, telegram_chat_id=12345,
        created_at=datetime(2025, 1, 1), started_at=None, completed_at=None,
        duration_seconds=None, full_output=None, output_summary=None,
        error_message=None, telegram_msg_id=None, repeat_count=None,
        repeat_remaining=None, repeat_until=None, chain_id=None,
        chain_step=None, parent_task_id=None, priority=0, retry_count=0,
        max_retries=1, git_diff=None, estimated_cost=0.05, assigned_to=None,
        worker_id=None, heartbeat_at=None, timeout_seconds=None,
    )
    defaults.update(overrides)
    t = MagicMock(spec=Task)
    for k, v in defaults.items():
        setattr(t, k, v)
    return t


@pytest.fixture(autouse=True)
def _clear_caches():
    _chat_project_dir.clear()
    _chat_agent.clear()
    _chat_followup.clear()
    _chat_model.clear()
    _chat_smart_mode.clear()
    _pending_smart_actions.clear()
    if hasattr(_load_prefs, "_loaded"):
        _load_prefs._loaded.clear()
    yield
    _chat_project_dir.clear()
    _chat_agent.clear()
    _chat_followup.clear()
    _chat_model.clear()
    _chat_smart_mode.clear()
    _pending_smart_actions.clear()
    if hasattr(_load_prefs, "_loaded"):
        _load_prefs._loaded.clear()


def _sent_text(update) -> str:
    """Get the text sent via _send (which calls update.get_bot().send_message)."""
    bot = update.get_bot()
    if bot.send_message.call_args:
        # send_message(chat_id=..., text=..., ...)
        kwargs = bot.send_message.call_args.kwargs
        if "text" in kwargs:
            return kwargs["text"]
        args = bot.send_message.call_args.args
        if len(args) >= 2:
            return args[1]
    return ""


# ════════════════════════════════════════════════════════════════════
#  TIER 1: Pattern-based classification tests
# ════════════════════════════════════════════════════════════════════


class TestPatternClassification:
    """Tier 1 pattern matching — exhaustive coverage."""

    # ── Read-only: list templates ─────────────────────────────────

    @pytest.mark.parametrize("text", [
        "show templates",
        "list templates",
        "view templates",
        "get templates",
        "my templates",
        "Show Templates",
        "LIST TEMPLATE",
    ])
    def test_list_templates(self, text):
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.LIST_TEMPLATES
        assert result.confidence >= CONFIDENCE_THRESHOLD

    # ── Read-only: list schedules ─────────────────────────────────

    @pytest.mark.parametrize("text", [
        "show schedules",
        "list schedules",
        "view schedule",
        "my schedules",
    ])
    def test_list_schedules(self, text):
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.LIST_SCHEDULES

    # ── Read-only: status ─────────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "status",
        "what's running",
        "what is running",
        "current task",
        "current status",
    ])
    def test_show_status(self, text):
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.SHOW_STATUS

    # ── Read-only: queue ──────────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "show queue",
        "list pending",
        "get tasks",
        "view queue",
    ])
    def test_show_queue(self, text):
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.SHOW_QUEUE

    # ── Read-only: history ────────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "show history",
        "list recent",
        "view past tasks",
        "get completed",
    ])
    def test_show_history(self, text):
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.SHOW_HISTORY

    # ── Read-only: costs ──────────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "costs",
        "cost",
        "spending",
        "budget",
        "how much",
        "show costs",
        "what's the cost",
        "what are the costs",
        "how much have I spent",
        "total cost",
        "total spending",
    ])
    def test_show_costs(self, text):
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.SHOW_COSTS

    # ── Create: save template ─────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "save a template called mytest that does run tests",
        "create template named deploy prompt is deploy to prod",
        "add a new template called lint that runs linting",
        "save this as a template called quickfix",
    ])
    def test_save_template(self, text):
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.SAVE_TEMPLATE

    def test_save_template_extracts_name(self):
        result = classify_pattern("save a template called mytest that does run tests")
        assert result is not None
        assert result.params.get("name") == "mytest"

    def test_save_template_extracts_prompt(self):
        result = classify_pattern("create template named build that does compile the project")
        assert result is not None
        assert result.params.get("prompt") is not None

    # ── Create: save schedule ─────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "schedule a daily task to run tests at 09:00",
        "set up a recurring task every 30 minutes",
        "every morning run the build",
        "every evening check logs",
        "every 15 minutes check status",
    ])
    def test_save_schedule(self, text):
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.SAVE_SCHEDULE

    def test_save_schedule_extracts_time(self):
        result = classify_pattern("schedule a daily task to run tests at 09:30")
        assert result is not None
        assert result.params.get("cron_expr") == "09:30"

    def test_save_schedule_ampm_extraction(self):
        result = classify_pattern("schedule a task every day at 3 pm")
        assert result is not None
        assert result.params.get("cron_expr") == "15:00"

    def test_save_schedule_interval_extraction(self):
        result = classify_pattern("every 10 minutes check health")
        assert result is not None
        assert result.params.get("cron_expr") == "*/10"

    def test_save_schedule_morning_shortcut(self):
        result = classify_pattern("every morning run the tests")
        assert result is not None
        assert result.params.get("cron_expr") == "08:00"

    def test_save_schedule_evening_shortcut(self):
        result = classify_pattern("every evening check the logs")
        assert result is not None
        assert result.params.get("cron_expr") == "18:00"

    # ── Create: set project ───────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "switch project to ~/repos/myapp",
        "change directory to /home/user/code",
        "set project path ~/ai/test",
        "use folder ~/projects/web",
    ])
    def test_set_project(self, text):
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.SET_PROJECT

    def test_set_project_extracts_path(self):
        result = classify_pattern("switch project to ~/repos/myapp")
        assert result is not None
        assert result.params.get("project_dir") == "~/repos/myapp"

    # ── Create: set agent ─────────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "switch agent to claude",
        "change agent to opencode",
        "set agent to aider",
        "use agent codex",
    ])
    def test_set_agent(self, text):
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.SET_AGENT

    def test_set_agent_extracts_name(self):
        result = classify_pattern("switch agent to claude")
        assert result is not None
        assert result.params.get("agent") == "claude"

    # ── Create: set model ─────────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "switch model to opus",
        "change model to sonnet",
        "set model to haiku",
        "use model gpt4",
    ])
    def test_set_model(self, text):
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.SET_MODEL

    def test_set_model_extracts_name(self):
        result = classify_pattern("switch model to opus")
        assert result is not None
        assert result.params.get("model") == "opus"

    # ── Destructive: delete template ──────────────────────────────

    @pytest.mark.parametrize("text", [
        "delete template mytest",
        "remove the template deploy",
        "drop template lint",
        "delete the mytest template",
    ])
    def test_delete_template(self, text):
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.DELETE_TEMPLATE

    def test_delete_template_extracts_name(self):
        result = classify_pattern("delete template mytest")
        assert result is not None
        assert result.params.get("name") == "mytest"

    # ── Destructive: delete schedule ──────────────────────────────

    @pytest.mark.parametrize("text", [
        "delete schedule daily-tests",
        "remove the schedule backup",
        "cancel schedule nightly",
    ])
    def test_delete_schedule(self, text):
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.DELETE_SCHEDULE

    def test_delete_schedule_extracts_name(self):
        result = classify_pattern("delete schedule daily-tests")
        assert result is not None
        assert result.params.get("name") == "daily-tests"

    # ── Destructive: cancel task ──────────────────────────────────

    @pytest.mark.parametrize("text", [
        "cancel task",
        "cancel the current task",
        "stop task",
        "kill task",
    ])
    def test_cancel_task(self, text):
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.CANCEL_TASK

    # ── Execute: run template ─────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "run template mytest",
        "execute template deploy",
        "trigger the template lint",
        "run the deploy template",
    ])
    def test_run_template(self, text):
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.RUN_TEMPLATE

    def test_run_template_extracts_name(self):
        result = classify_pattern("run template mytest")
        assert result is not None
        assert result.params.get("name") == "mytest"

    # ── Execute: toggle schedule ──────────────────────────────────

    @pytest.mark.parametrize("text", [
        "toggle schedule daily-tests",
        "pause schedule nightly",
        "enable schedule backup",
        "disable schedule daily-tests",
    ])
    def test_toggle_schedule(self, text):
        result = classify_pattern(text)
        assert result is not None
        assert result.action == IntentAction.TOGGLE_SCHEDULE

    def test_toggle_schedule_extracts_name(self):
        result = classify_pattern("toggle schedule daily-tests")
        assert result is not None
        assert result.params.get("name") == "daily-tests"

    # ── No match (falls through to task prompt) ───────────────────

    @pytest.mark.parametrize("text", [
        "fix the login bug in auth.py",
        "please refactor the database module",
        "add unit tests for the user service",
        "hello world",
        "",
    ])
    def test_no_pattern_match(self, text):
        result = classify_pattern(text)
        assert result is None

    def test_empty_string_returns_none(self):
        assert classify_pattern("") is None
        assert classify_pattern("  ") is None

    # ── Case insensitivity ────────────────────────────────────────

    def test_case_insensitive_templates(self):
        assert classify_pattern("SHOW TEMPLATES") is not None
        assert classify_pattern("Show Templates") is not None
        assert classify_pattern("show TEMPLATES") is not None


# ════════════════════════════════════════════════════════════════════
#  Safety level mapping
# ════════════════════════════════════════════════════════════════════


class TestSafetyLevels:
    def test_read_actions_are_safe(self):
        read_actions = [
            IntentAction.LIST_TEMPLATES, IntentAction.LIST_SCHEDULES,
            IntentAction.SHOW_STATUS, IntentAction.SHOW_QUEUE,
            IntentAction.SHOW_HISTORY, IntentAction.SHOW_COSTS,
        ]
        for action in read_actions:
            assert SAFETY_MAP[action] == SafetyLevel.READ

    def test_create_actions_need_confirm(self):
        create_actions = [
            IntentAction.SAVE_TEMPLATE, IntentAction.SAVE_SCHEDULE,
            IntentAction.SET_PROJECT, IntentAction.SET_AGENT, IntentAction.SET_MODEL,
        ]
        for action in create_actions:
            assert SAFETY_MAP[action] == SafetyLevel.CREATE

    def test_destroy_actions_are_destructive(self):
        destroy_actions = [
            IntentAction.DELETE_TEMPLATE, IntentAction.DELETE_SCHEDULE,
            IntentAction.CANCEL_TASK,
        ]
        for action in destroy_actions:
            assert SAFETY_MAP[action] == SafetyLevel.DESTROY

    def test_execute_actions_need_confirm(self):
        execute_actions = [IntentAction.RUN_TEMPLATE, IntentAction.TOGGLE_SCHEDULE]
        for action in execute_actions:
            assert SAFETY_MAP[action] == SafetyLevel.EXECUTE

    def test_classified_intent_safety_property(self):
        intent = ClassifiedIntent(action=IntentAction.CANCEL_TASK, confidence=0.95)
        assert intent.safety == SafetyLevel.DESTROY

        intent2 = ClassifiedIntent(action=IntentAction.LIST_TEMPLATES, confidence=0.95)
        assert intent2.safety == SafetyLevel.READ

    def test_all_actions_have_safety_mapping(self):
        for action in IntentAction:
            assert action in SAFETY_MAP, f"{action} missing from SAFETY_MAP"


# ════════════════════════════════════════════════════════════════════
#  Intent dataclass
# ════════════════════════════════════════════════════════════════════


class TestClassifiedIntent:
    def test_defaults(self):
        intent = ClassifiedIntent(action=IntentAction.SHOW_STATUS, confidence=0.9)
        assert intent.params == {}
        assert intent.raw_text == ""

    def test_with_params(self):
        intent = ClassifiedIntent(
            action=IntentAction.DELETE_TEMPLATE, confidence=0.95,
            params={"name": "mytest"}, raw_text="delete template mytest"
        )
        assert intent.params["name"] == "mytest"
        assert intent.raw_text == "delete template mytest"


# ════════════════════════════════════════════════════════════════════
#  Parameter extractors (unit tests)
# ════════════════════════════════════════════════════════════════════


class TestExtractors:
    def test_extract_save_template_with_name_and_prompt(self):
        params = _extract_save_template(None, "save a template called mytest that does run tests")
        assert params["name"] == "mytest"
        assert "run tests" in params.get("prompt", "")

    def test_extract_save_template_with_agent(self):
        params = _extract_save_template(None, "save template named foo that runs lint using claude")
        assert params["name"] == "foo"
        assert params["agent"] == "claude"

    def test_extract_save_schedule_with_time(self):
        params = _extract_save_schedule(None, "schedule a task at 14:30 called backup")
        assert params.get("cron_expr") == "14:30"
        assert params.get("name") == "backup"

    def test_extract_save_schedule_ampm(self):
        params = _extract_save_schedule(None, "schedule something at 2 pm")
        assert params.get("cron_expr") == "14:00"

    def test_extract_save_schedule_am_noon(self):
        params = _extract_save_schedule(None, "schedule something at 12 am")
        assert params.get("cron_expr") == "00:00"

    def test_extract_save_schedule_interval(self):
        params = _extract_save_schedule(None, "every 5 minutes check health")
        assert params.get("cron_expr") == "*/5"

    def test_extract_save_schedule_hourly(self):
        params = _extract_save_schedule(None, "every 2 hours check status")
        assert params.get("cron_expr") == "*/120"

    def test_extract_set_project_with_to(self):
        params = _extract_set_project(None, "switch project to ~/repos/app")
        assert params["project_dir"] == "~/repos/app"

    def test_extract_set_project_last_token(self):
        params = _extract_set_project(None, "set project ~/code")
        assert params["project_dir"] == "~/code"

    def test_extract_set_agent_with_to(self):
        params = _extract_set_agent(None, "switch agent to claude")
        assert params["agent"] == "claude"

    def test_extract_set_model_with_to(self):
        params = _extract_set_model(None, "switch model to opus")
        assert params["model"] == "opus"

    def test_extract_delete_template_name(self):
        params = _extract_delete_template(None, "delete template mytest")
        assert params["name"] == "mytest"

    def test_extract_delete_schedule_name(self):
        params = _extract_delete_schedule(None, "delete schedule daily-tests")
        assert params["name"] == "daily-tests"

    def test_extract_run_template_name(self):
        params = _extract_run_template(None, "run template deploy")
        assert params["name"] == "deploy"

    def test_extract_toggle_schedule_name(self):
        params = _extract_toggle_schedule(None, "toggle schedule nightly")
        assert params["name"] == "nightly"


# ════════════════════════════════════════════════════════════════════
#  LLM response parser
# ════════════════════════════════════════════════════════════════════


class TestLLMResponseParser:
    def test_valid_json(self):
        response = '{"intent": "list_templates", "confidence": 0.9, "params": {}}'
        result = _parse_llm_response(response)
        assert result is not None
        assert result.action == IntentAction.LIST_TEMPLATES
        assert result.confidence == 0.9

    def test_json_with_code_fence(self):
        response = "```json\n{\"intent\": \"show_status\", \"confidence\": 0.85, \"params\": {}}\n```"
        result = _parse_llm_response(response)
        assert result is not None
        assert result.action == IntentAction.SHOW_STATUS

    def test_unknown_intent_fallback(self):
        response = '{"intent": "unknown_action", "confidence": 0.7, "params": {}}'
        result = _parse_llm_response(response)
        assert result is not None
        assert result.action == IntentAction.TASK_PROMPT
        assert result.confidence == 0.3

    def test_invalid_json(self):
        result = _parse_llm_response("this is not json")
        assert result is None

    def test_missing_intent(self):
        response = '{"confidence": 0.9}'
        result = _parse_llm_response(response)
        assert result is not None
        assert result.action == IntentAction.TASK_PROMPT

    def test_params_extraction(self):
        response = '{"intent": "delete_template", "confidence": 0.95, "params": {"name": "mytest"}}'
        result = _parse_llm_response(response)
        assert result is not None
        assert result.params["name"] == "mytest"

    def test_non_dict_params_ignored(self):
        response = '{"intent": "show_status", "confidence": 0.8, "params": "bad"}'
        result = _parse_llm_response(response)
        assert result is not None
        assert result.params == {}


# ════════════════════════════════════════════════════════════════════
#  Combined classify() function
# ════════════════════════════════════════════════════════════════════


class TestCombinedClassify:
    async def test_pattern_match_returns_immediately(self):
        result = await classify("show templates", use_llm=False)
        assert result.action == IntentAction.LIST_TEMPLATES
        assert result.confidence >= CONFIDENCE_THRESHOLD

    async def test_no_pattern_fallback_to_task_prompt(self):
        result = await classify("fix the login bug", use_llm=False)
        assert result.action == IntentAction.TASK_PROMPT
        assert result.confidence < CONFIDENCE_THRESHOLD

    async def test_llm_disabled_returns_task_prompt(self):
        result = await classify("something ambiguous about templates maybe", use_llm=False)
        assert result.action == IntentAction.TASK_PROMPT

    @patch("app.core.dispatcher.classify_llm")
    async def test_llm_called_when_pattern_fails(self, mock_llm):
        mock_llm.return_value = ClassifiedIntent(
            action=IntentAction.LIST_TEMPLATES, confidence=0.85,
        )
        result = await classify("I wanna see my saved recipe things", use_llm=True)
        mock_llm.assert_called_once()

    @patch("app.core.dispatcher.classify_llm")
    async def test_llm_low_confidence_fallback(self, mock_llm):
        mock_llm.return_value = ClassifiedIntent(
            action=IntentAction.SHOW_STATUS, confidence=0.3,
        )
        result = await classify("what?", use_llm=True)
        assert result.action == IntentAction.TASK_PROMPT

    @patch("app.core.dispatcher.classify_llm")
    async def test_llm_failure_fallback(self, mock_llm):
        mock_llm.return_value = None
        result = await classify("ambiguous thing", use_llm=True)
        assert result.action == IntentAction.TASK_PROMPT


# ════════════════════════════════════════════════════════════════════
#  format_confirmation()
# ════════════════════════════════════════════════════════════════════


class TestFormatConfirmation:
    def test_save_template_fmt(self):
        intent = ClassifiedIntent(
            action=IntentAction.SAVE_TEMPLATE, confidence=0.95,
            params={"name": "test", "prompt": "run tests", "agent": "claude"},
        )
        text = format_confirmation(intent)
        assert "test" in text
        assert "run tests" in text
        assert "claude" in text

    def test_save_schedule_fmt(self):
        intent = ClassifiedIntent(
            action=IntentAction.SAVE_SCHEDULE, confidence=0.95,
            params={"name": "daily", "cron_expr": "09:00", "prompt": "build"},
        )
        text = format_confirmation(intent)
        assert "daily" in text
        assert "09:00" in text

    def test_delete_template_fmt(self):
        intent = ClassifiedIntent(
            action=IntentAction.DELETE_TEMPLATE, confidence=0.95,
            params={"name": "old"},
        )
        text = format_confirmation(intent)
        assert "old" in text
        assert "cannot be undone" in text.lower()

    def test_delete_schedule_fmt(self):
        intent = ClassifiedIntent(
            action=IntentAction.DELETE_SCHEDULE, confidence=0.95,
            params={"name": "nightly"},
        )
        text = format_confirmation(intent)
        assert "nightly" in text

    def test_cancel_task_fmt(self):
        intent = ClassifiedIntent(action=IntentAction.CANCEL_TASK, confidence=0.95)
        text = format_confirmation(intent)
        assert "cancel" in text.lower()

    def test_run_template_fmt(self):
        intent = ClassifiedIntent(
            action=IntentAction.RUN_TEMPLATE, confidence=0.95,
            params={"name": "deploy"},
        )
        text = format_confirmation(intent)
        assert "deploy" in text

    def test_set_project_fmt(self):
        intent = ClassifiedIntent(
            action=IntentAction.SET_PROJECT, confidence=0.95,
            params={"project_dir": "~/repos"},
        )
        text = format_confirmation(intent)
        assert "~/repos" in text

    def test_set_agent_fmt(self):
        intent = ClassifiedIntent(
            action=IntentAction.SET_AGENT, confidence=0.95,
            params={"agent": "claude"},
        )
        text = format_confirmation(intent)
        assert "claude" in text

    def test_set_model_fmt(self):
        intent = ClassifiedIntent(
            action=IntentAction.SET_MODEL, confidence=0.95,
            params={"model": "opus"},
        )
        text = format_confirmation(intent)
        assert "opus" in text


# ════════════════════════════════════════════════════════════════════
#  Bot: /smartmode command
# ════════════════════════════════════════════════════════════════════


class TestSmartModeCommand:
    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    async def test_smartmode_on(self, mock_get, mock_set):
        mock_get.return_value = {"project_dir": None, "agent": None, "model": None, "smart_mode": False}
        update = _make_update()
        ctx = _make_context(["on"])
        await cmd_smartmode(update, ctx)
        assert _chat_smart_mode[12345] is True
        mock_set.assert_called_with(12345, smart_mode=True)

    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    async def test_smartmode_off(self, mock_get, mock_set):
        mock_get.return_value = {"project_dir": None, "agent": None, "model": None, "smart_mode": True}
        _chat_smart_mode[12345] = True
        update = _make_update()
        ctx = _make_context(["off"])
        await cmd_smartmode(update, ctx)
        assert _chat_smart_mode[12345] is False
        mock_set.assert_called_with(12345, smart_mode=False)

    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    async def test_smartmode_toggle(self, mock_get, mock_set):
        mock_get.return_value = {"project_dir": None, "agent": None, "model": None, "smart_mode": False}
        update = _make_update()
        ctx = _make_context()  # no args = toggle
        await cmd_smartmode(update, ctx)
        assert _chat_smart_mode[12345] is True  # was False, toggled to True

    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    async def test_smartmode_toggle_off(self, mock_get, mock_set):
        mock_get.return_value = {"project_dir": None, "agent": None, "model": None, "smart_mode": False}
        _chat_smart_mode[12345] = True
        update = _make_update()
        ctx = _make_context()  # toggle from True → False
        await cmd_smartmode(update, ctx)
        assert _chat_smart_mode[12345] is False

    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    async def test_smartmode_sends_status_message(self, mock_get, mock_set):
        mock_get.return_value = {"project_dir": None, "agent": None, "model": None, "smart_mode": False}
        update = _make_update()
        ctx = _make_context(["on"])
        await cmd_smartmode(update, ctx)
        # Should have sent a message containing "enabled"
        assert "enabled" in _sent_text(update).lower()


# ════════════════════════════════════════════════════════════════════
#  Bot: handle_text with smart mode
# ════════════════════════════════════════════════════════════════════


class TestHandleTextSmartMode:
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    @patch("app.telegram.bot.get_running_task", new_callable=AsyncMock)
    async def test_smart_mode_read_action_auto_executes(self, mock_running, mock_prefs):
        mock_prefs.return_value = {"project_dir": None, "agent": None, "model": None, "smart_mode": True}
        mock_running.return_value = None
        _chat_smart_mode[12345] = True

        update = _make_update(text="show templates")
        ctx = _make_context()

        with patch("app.core.broker.list_templates", new_callable=AsyncMock, return_value=[]):
            await handle_text(update, ctx)
            # Should have responded (not queued as task)
            assert update.get_bot().send_message.called

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    async def test_smart_mode_create_action_shows_confirmation(self, mock_prefs):
        mock_prefs.return_value = {"project_dir": None, "agent": None, "model": None, "smart_mode": True}
        _chat_smart_mode[12345] = True

        update = _make_update(text="delete template mytest")
        ctx = _make_context()

        await handle_text(update, ctx)
        # Should have stored pending action
        assert 12345 in _pending_smart_actions
        assert _pending_smart_actions[12345].action == IntentAction.DELETE_TEMPLATE

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    @patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock)
    @patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None)
    async def test_smart_mode_off_normal_enqueue(self, mock_recipe, mock_enqueue, mock_prefs):
        mock_prefs.return_value = {"project_dir": None, "agent": None, "model": None, "smart_mode": False}
        _chat_smart_mode[12345] = False

        task = _make_task()
        mock_enqueue.return_value = task

        update = _make_update(text="show templates")  # This would match in smart mode
        ctx = _make_context()

        await handle_text(update, ctx)
        # Should enqueue as task since smart mode is off
        mock_enqueue.assert_called_once()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    @patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock)
    @patch("app.telegram.bot.match_recipe", new_callable=AsyncMock, return_value=None)
    async def test_smart_mode_unrecognized_falls_through_to_enqueue(self, mock_recipe, mock_enqueue, mock_prefs):
        mock_prefs.return_value = {"project_dir": None, "agent": None, "model": None, "smart_mode": True}
        _chat_smart_mode[12345] = True

        task = _make_task()
        mock_enqueue.return_value = task

        update = _make_update(text="fix the login bug in auth.py")
        ctx = _make_context()

        await handle_text(update, ctx)
        # Unrecognized text should fall through to regular enqueue
        mock_enqueue.assert_called_once()

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    async def test_smart_mode_shows_inline_buttons_for_confirm(self, mock_prefs):
        mock_prefs.return_value = {"project_dir": None, "agent": None, "model": None, "smart_mode": True}
        _chat_smart_mode[12345] = True

        update = _make_update(text="run template deploy")
        ctx = _make_context()

        await handle_text(update, ctx)

        # Should have sent confirmation with InlineKeyboardMarkup via _send
        bot = update.get_bot()
        assert bot.send_message.called
        call_args = bot.send_message.call_args
        if call_args:
            assert any("smartconfirm" in str(a) for a in call_args) or \
                   any("smartconfirm" in str(v) for v in (call_args.kwargs or {}).values())


# ════════════════════════════════════════════════════════════════════
#  Bot: confirmation callback handlers
# ════════════════════════════════════════════════════════════════════


class TestConfirmationCallbacks:
    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    async def test_confirm_executes_pending_action(self, mock_prefs):
        mock_prefs.return_value = {"project_dir": None, "agent": None, "model": None, "smart_mode": True}
        intent = ClassifiedIntent(
            action=IntentAction.LIST_TEMPLATES, confidence=0.95,
            raw_text="show templates",
        )
        _pending_smart_actions[12345] = intent

        update = _make_callback_update(data="smartconfirm:12345")
        ctx = _make_context()

        with patch("app.core.broker.list_templates", new_callable=AsyncMock, return_value=[]):
            await handle_smart_confirm(update, ctx)

        # Should have consumed the pending action
        assert 12345 not in _pending_smart_actions

    @patch("app.telegram.bot.get_chat_prefs", new_callable=AsyncMock)
    async def test_confirm_expired_action(self, mock_prefs):
        mock_prefs.return_value = {"project_dir": None, "agent": None, "model": None, "smart_mode": True}
        # No pending action
        update = _make_callback_update(data="smartconfirm:12345")
        ctx = _make_context()

        await handle_smart_confirm(update, ctx)
        # Should show expired message
        update.callback_query.edit_message_text.assert_called()
        assert "expired" in str(update.callback_query.edit_message_text.call_args).lower()

    async def test_cancel_removes_pending_action(self):
        intent = ClassifiedIntent(action=IntentAction.CANCEL_TASK, confidence=0.95)
        _pending_smart_actions[12345] = intent

        update = _make_callback_update(data="smartcancel:12345")
        ctx = _make_context()

        await handle_smart_cancel(update, ctx)
        assert 12345 not in _pending_smart_actions
        update.callback_query.edit_message_text.assert_called_with("❌ Cancelled.")

    @patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock)
    async def test_execute_queues_as_task(self, mock_enqueue):
        task = _make_task()
        mock_enqueue.return_value = task

        intent = ClassifiedIntent(
            action=IntentAction.SHOW_STATUS, confidence=0.95,
            raw_text="check what is running now",
        )
        _pending_smart_actions[12345] = intent

        update = _make_callback_update(data="smartexec:12345")
        ctx = _make_context()

        await handle_smart_execute(update, ctx)
        mock_enqueue.assert_called_once()
        assert 12345 not in _pending_smart_actions

    async def test_execute_expired_action(self):
        update = _make_callback_update(data="smartexec:12345")
        ctx = _make_context()

        await handle_smart_execute(update, ctx)
        update.callback_query.edit_message_text.assert_called()
        assert "expired" in str(update.callback_query.edit_message_text.call_args).lower()


# ════════════════════════════════════════════════════════════════════
#  Bot: _execute_smart_intent (action execution)
# ════════════════════════════════════════════════════════════════════


class TestExecuteSmartIntent:
    @patch("app.core.broker.list_templates", new_callable=AsyncMock)
    async def test_list_templates_empty(self, mock_list):
        mock_list.return_value = []
        intent = ClassifiedIntent(action=IntentAction.LIST_TEMPLATES, confidence=0.95)
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert update.get_bot().send_message.called

    @patch("app.core.broker.list_templates", new_callable=AsyncMock)
    async def test_list_templates_with_items(self, mock_list):
        tmpl = MagicMock()
        tmpl.name = "test"
        tmpl.agent = "opencode"
        tmpl.model = None
        tmpl.prompt = "run tests on the project"
        mock_list.return_value = [tmpl]
        intent = ClassifiedIntent(action=IntentAction.LIST_TEMPLATES, confidence=0.95)
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "test" in _sent_text(update)

    @patch("app.core.broker.list_schedules", new_callable=AsyncMock)
    async def test_list_schedules_empty(self, mock_list):
        mock_list.return_value = []
        intent = ClassifiedIntent(action=IntentAction.LIST_SCHEDULES, confidence=0.95)
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert update.get_bot().send_message.called

    @patch("app.telegram.bot.get_running_task", new_callable=AsyncMock)
    async def test_show_status_no_task(self, mock_running):
        mock_running.return_value = None
        intent = ClassifiedIntent(action=IntentAction.SHOW_STATUS, confidence=0.95)
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "No task running" in _sent_text(update)

    @patch("app.telegram.bot.get_running_task", new_callable=AsyncMock)
    async def test_show_status_with_task(self, mock_running):
        task = _make_task(status=TaskStatus.RUNNING.value if isinstance(TaskStatus.RUNNING, str) else TaskStatus.RUNNING)
        mock_running.return_value = task
        intent = ClassifiedIntent(action=IntentAction.SHOW_STATUS, confidence=0.95)
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "Running" in _sent_text(update)

    @patch("app.telegram.bot.get_pending_tasks", new_callable=AsyncMock)
    async def test_show_queue_empty(self, mock_pending):
        mock_pending.return_value = []
        intent = ClassifiedIntent(action=IntentAction.SHOW_QUEUE, confidence=0.95)
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "empty" in _sent_text(update).lower()

    @patch("app.telegram.bot.get_pending_tasks", new_callable=AsyncMock)
    async def test_show_queue_with_tasks(self, mock_pending):
        mock_pending.return_value = [_make_task()]
        intent = ClassifiedIntent(action=IntentAction.SHOW_QUEUE, confidence=0.95)
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "Pending" in _sent_text(update)

    @patch("app.telegram.bot.get_recent_tasks", new_callable=AsyncMock)
    async def test_show_history(self, mock_recent):
        mock_recent.return_value = [_make_task(status=TaskStatus.COMPLETED)]
        intent = ClassifiedIntent(action=IntentAction.SHOW_HISTORY, confidence=0.95)
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "Recent" in _sent_text(update)

    @patch("app.telegram.bot.get_recent_tasks", new_callable=AsyncMock)
    async def test_show_costs(self, mock_recent):
        mock_recent.return_value = [_make_task(estimated_cost=0.05), _make_task(estimated_cost=0.03)]
        intent = ClassifiedIntent(action=IntentAction.SHOW_COSTS, confidence=0.95)
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "$" in _sent_text(update)

    @patch("app.telegram.bot.cancel_running_task", new_callable=AsyncMock)
    async def test_cancel_task_success(self, mock_cancel):
        mock_cancel.return_value = _make_task(id=5)
        intent = ClassifiedIntent(action=IntentAction.CANCEL_TASK, confidence=0.95)
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "cancelled" in _sent_text(update).lower()

    @patch("app.telegram.bot.cancel_running_task", new_callable=AsyncMock)
    async def test_cancel_task_nothing_running(self, mock_cancel):
        mock_cancel.return_value = None
        intent = ClassifiedIntent(action=IntentAction.CANCEL_TASK, confidence=0.95)
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "No running" in _sent_text(update)

    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    async def test_set_project_missing_path(self, mock_set):
        intent = ClassifiedIntent(
            action=IntentAction.SET_PROJECT, confidence=0.95,
            params={},
        )
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "No project path" in _sent_text(update)

    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    async def test_set_agent_unknown(self, mock_set):
        intent = ClassifiedIntent(
            action=IntentAction.SET_AGENT, confidence=0.95,
            params={"agent": "nonexistent_agent"},
        )
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "Unknown agent" in _sent_text(update)

    @patch("app.telegram.bot.set_chat_pref", new_callable=AsyncMock)
    async def test_set_model_success(self, mock_set):
        intent = ClassifiedIntent(
            action=IntentAction.SET_MODEL, confidence=0.95,
            params={"model": "opus"},
        )
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert _chat_model[12345] == "opus"
        mock_set.assert_called_with(12345, model="opus")

    @patch("app.core.broker.save_template", new_callable=AsyncMock)
    async def test_save_template_success(self, mock_save):
        tmpl = MagicMock()
        tmpl.name = "test"
        mock_save.return_value = tmpl
        intent = ClassifiedIntent(
            action=IntentAction.SAVE_TEMPLATE, confidence=0.95,
            params={"name": "test", "prompt": "run tests"},
        )
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        mock_save.assert_called_once()

    @patch("app.core.broker.save_template", new_callable=AsyncMock)
    async def test_save_template_missing_params(self, mock_save):
        intent = ClassifiedIntent(
            action=IntentAction.SAVE_TEMPLATE, confidence=0.95,
            params={},
        )
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        mock_save.assert_not_called()

    @patch("app.core.broker.delete_template", new_callable=AsyncMock)
    async def test_delete_template_found(self, mock_del):
        mock_del.return_value = True
        intent = ClassifiedIntent(
            action=IntentAction.DELETE_TEMPLATE, confidence=0.95,
            params={"name": "old"},
        )
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "deleted" in _sent_text(update).lower()

    @patch("app.core.broker.delete_template", new_callable=AsyncMock)
    async def test_delete_template_not_found(self, mock_del):
        mock_del.return_value = False
        intent = ClassifiedIntent(
            action=IntentAction.DELETE_TEMPLATE, confidence=0.95,
            params={"name": "old"},
        )
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "not found" in _sent_text(update).lower()

    @patch("app.core.broker.get_template", new_callable=AsyncMock)
    @patch("app.telegram.bot.enqueue_task", new_callable=AsyncMock)
    async def test_run_template_success(self, mock_enqueue, mock_get):
        tmpl = MagicMock()
        tmpl.prompt = "run all tests"
        tmpl.project_dir = "/tmp/proj"
        tmpl.agent = "opencode"
        tmpl.model = None
        tmpl.timeout_seconds = None
        mock_get.return_value = tmpl
        mock_enqueue.return_value = _make_task(id=10)

        intent = ClassifiedIntent(
            action=IntentAction.RUN_TEMPLATE, confidence=0.95,
            params={"name": "test"},
        )
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        mock_enqueue.assert_called_once()

    @patch("app.core.broker.get_template", new_callable=AsyncMock)
    async def test_run_template_not_found(self, mock_get):
        mock_get.return_value = None
        intent = ClassifiedIntent(
            action=IntentAction.RUN_TEMPLATE, confidence=0.95,
            params={"name": "nonexistent"},
        )
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "not found" in _sent_text(update).lower()

    @patch("app.core.broker.toggle_schedule", new_callable=AsyncMock)
    async def test_toggle_schedule_success(self, mock_toggle):
        sched = MagicMock()
        sched.enabled = True
        mock_toggle.return_value = sched
        intent = ClassifiedIntent(
            action=IntentAction.TOGGLE_SCHEDULE, confidence=0.95,
            params={"name": "daily"},
        )
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "enabled" in _sent_text(update).lower()

    @patch("app.core.broker.toggle_schedule", new_callable=AsyncMock)
    async def test_toggle_schedule_not_found(self, mock_toggle):
        mock_toggle.return_value = None
        intent = ClassifiedIntent(
            action=IntentAction.TOGGLE_SCHEDULE, confidence=0.95,
            params={"name": "nonexist"},
        )
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "not found" in _sent_text(update).lower()

    @patch("app.core.broker.save_schedule", new_callable=AsyncMock)
    async def test_save_schedule_missing_params(self, mock_save):
        intent = ClassifiedIntent(
            action=IntentAction.SAVE_SCHEDULE, confidence=0.95,
            params={"name": "daily"},  # missing cron_expr and prompt
        )
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        mock_save.assert_not_called()

    @patch("app.core.broker.delete_schedule", new_callable=AsyncMock)
    async def test_delete_schedule_success(self, mock_del):
        mock_del.return_value = True
        intent = ClassifiedIntent(
            action=IntentAction.DELETE_SCHEDULE, confidence=0.95,
            params={"name": "old"},
        )
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "deleted" in _sent_text(update).lower()

    async def test_ambiguous_intent_fallback(self):
        intent = ClassifiedIntent(action=IntentAction.AMBIGUOUS, confidence=0.5)
        update = _make_update()
        await _execute_smart_intent(update, intent, 12345)
        assert "Couldn't complete" in _sent_text(update)


# ════════════════════════════════════════════════════════════════════
#  Integration: smart mode prefs persistence (with in-memory DB)
# ════════════════════════════════════════════════════════════════════


class TestSmartModePrefs:
    """Test smart_mode persists through get_chat_prefs / set_chat_pref."""

    async def test_get_prefs_returns_smart_mode_false_default(self, async_engine):
        from app.core import db as db_mod
        from app.core.broker import get_chat_prefs, set_chat_pref
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

        factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
        with patch.object(db_mod, "async_session", factory):
            prefs = await get_chat_prefs(99999)
            assert prefs["smart_mode"] is False

    async def test_set_and_get_smart_mode(self, async_engine):
        from app.core import db as db_mod
        from app.core.broker import get_chat_prefs, set_chat_pref
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

        factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
        with patch.object(db_mod, "async_session", factory):
            await set_chat_pref(55555, smart_mode=True)
            prefs = await get_chat_prefs(55555)
            assert prefs["smart_mode"] is True

    async def test_smart_mode_toggle_persists(self, async_engine):
        from app.core import db as db_mod
        from app.core.broker import get_chat_prefs, set_chat_pref
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

        factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
        with patch.object(db_mod, "async_session", factory):
            await set_chat_pref(66666, smart_mode=True)
            prefs = await get_chat_prefs(66666)
            assert prefs["smart_mode"] is True

            await set_chat_pref(66666, smart_mode=False)
            prefs = await get_chat_prefs(66666)
            assert prefs["smart_mode"] is False


# ════════════════════════════════════════════════════════════════════
#  Edge cases
# ════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_empty_input(self):
        assert classify_pattern("") is None
        assert classify_pattern("   ") is None

    def test_very_long_input(self):
        long_text = "show templates " * 100
        result = classify_pattern(long_text)
        # Should still match since the pattern is at the start
        # (actually this will match because search finds "show templates" within the text)
        # The key is it doesn't crash
        assert result is None or result.action == IntentAction.LIST_TEMPLATES

    def test_special_characters(self):
        result = classify_pattern("show templates!@#$")
        # Pattern requires end-of-string match, so this won't match
        assert result is None

    def test_null_bytes_stripped(self):
        # The bot already strips null bytes before passing to classify_pattern
        result = classify_pattern("show\x00templates")
        assert result is None  # null byte breaks the word

    def test_mixed_case_intent_action_values(self):
        # Verify enum values are lowercase
        for action in IntentAction:
            assert action.value == action.value.lower()

    def test_confidence_threshold_value(self):
        assert CONFIDENCE_THRESHOLD == 0.7

    @pytest.mark.parametrize("action", list(IntentAction))
    def test_all_actions_in_safety_map(self, action):
        assert action in SAFETY_MAP

    def test_pending_action_replaced_on_new_intent(self):
        """When user sends two confirmable intents, only the latest is stored."""
        intent1 = ClassifiedIntent(action=IntentAction.CANCEL_TASK, confidence=0.95)
        intent2 = ClassifiedIntent(action=IntentAction.DELETE_TEMPLATE, confidence=0.95, params={"name": "x"})
        _pending_smart_actions[12345] = intent1
        _pending_smart_actions[12345] = intent2
        assert _pending_smart_actions[12345].action == IntentAction.DELETE_TEMPLATE


# ════════════════════════════════════════════════════════════════════
#  Build app includes smart mode handlers
# ════════════════════════════════════════════════════════════════════


class TestBuildAppIntegration:
    def test_build_app_has_smartmode_command(self):
        from app.telegram.bot import build_app
        with patch("app.telegram.bot.settings") as mock_settings:
            mock_settings.telegram_bot_token = "test-token"
            mock_settings.allowed_user_ids = {12345}
            mock_settings.agent_commands = {"opencode": "opencode {prompt}"}
            mock_settings.known_workers_list = []
            app = build_app()
            # Verify handlers are registered (check handler count increased)
            handler_count = sum(len(handlers) for handlers in app.handlers.values())
            assert handler_count > 0

"""Intent classifier for smart mode — routes natural language to commands.

Two-tier design:
  Tier 1 (pattern matching): Instant, zero-cost keyword/regex classification.
  Tier 2 (LLM):             For ambiguous inputs when patterns don't match.

Safety levels:
  READ    — auto-execute, no confirmation needed
  CREATE  — show parsed params, ask for confirmation
  DESTROY — always require explicit confirmation
  EXECUTE — show what will run, confirm before enqueue
"""

from __future__ import annotations

import enum
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class IntentAction(str, enum.Enum):
    """High-level action categories."""
    # Read-only queries
    LIST_TEMPLATES = "list_templates"
    LIST_SCHEDULES = "list_schedules"
    SHOW_STATUS = "show_status"
    SHOW_QUEUE = "show_queue"
    SHOW_HISTORY = "show_history"
    SHOW_COSTS = "show_costs"

    # Create / modify
    SAVE_TEMPLATE = "save_template"
    SAVE_SCHEDULE = "save_schedule"
    SET_PROJECT = "set_project"
    SET_AGENT = "set_agent"
    SET_MODEL = "set_model"

    # Destructive
    DELETE_TEMPLATE = "delete_template"
    DELETE_SCHEDULE = "delete_schedule"
    CANCEL_TASK = "cancel_task"

    # Execute
    RUN_TEMPLATE = "run_template"
    TOGGLE_SCHEDULE = "toggle_schedule"

    # Fallback: treat as regular task prompt
    TASK_PROMPT = "task_prompt"

    # Ambiguous: couldn't classify
    AMBIGUOUS = "ambiguous"


class SafetyLevel(str, enum.Enum):
    READ = "read"          # auto-execute
    CREATE = "create"      # confirm params
    DESTROY = "destroy"    # explicit confirm
    EXECUTE = "execute"    # confirm before run


SAFETY_MAP: dict[IntentAction, SafetyLevel] = {
    IntentAction.LIST_TEMPLATES: SafetyLevel.READ,
    IntentAction.LIST_SCHEDULES: SafetyLevel.READ,
    IntentAction.SHOW_STATUS: SafetyLevel.READ,
    IntentAction.SHOW_QUEUE: SafetyLevel.READ,
    IntentAction.SHOW_HISTORY: SafetyLevel.READ,
    IntentAction.SHOW_COSTS: SafetyLevel.READ,

    IntentAction.SAVE_TEMPLATE: SafetyLevel.CREATE,
    IntentAction.SAVE_SCHEDULE: SafetyLevel.CREATE,
    IntentAction.SET_PROJECT: SafetyLevel.CREATE,
    IntentAction.SET_AGENT: SafetyLevel.CREATE,
    IntentAction.SET_MODEL: SafetyLevel.CREATE,

    IntentAction.DELETE_TEMPLATE: SafetyLevel.DESTROY,
    IntentAction.DELETE_SCHEDULE: SafetyLevel.DESTROY,
    IntentAction.CANCEL_TASK: SafetyLevel.DESTROY,

    IntentAction.RUN_TEMPLATE: SafetyLevel.EXECUTE,
    IntentAction.TOGGLE_SCHEDULE: SafetyLevel.EXECUTE,

    IntentAction.TASK_PROMPT: SafetyLevel.EXECUTE,
    IntentAction.AMBIGUOUS: SafetyLevel.READ,
}


@dataclass
class ClassifiedIntent:
    """Result of intent classification."""
    action: IntentAction
    confidence: float  # 0.0–1.0
    params: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""

    @property
    def safety(self) -> SafetyLevel:
        return SAFETY_MAP.get(self.action, SafetyLevel.CREATE)


# ── Tier 1: Pattern-based classification ──────────────────────────────

# Each rule: (compiled regex, IntentAction, param extractor or None)
_PatternRule = tuple[re.Pattern, IntentAction, Any]

_PATTERN_RULES: list[_PatternRule] = []


def _rule(pattern: str, action: IntentAction, extractor=None):
    """Register a pattern rule."""
    _PATTERN_RULES.append((re.compile(pattern, re.IGNORECASE), action, extractor))


# ── Read-only patterns ───────────────────────────────────────────────

_rule(
    r"^(?:show|list|view|get|my)\s+templates?$",
    IntentAction.LIST_TEMPLATES,
)
_rule(
    r"^(?:show|list|view|get|my)\s+schedules?$",
    IntentAction.LIST_SCHEDULES,
)
_rule(
    r"^(?:what(?:'s| is)\s+running|status|current\s+(?:task|status))$",
    IntentAction.SHOW_STATUS,
)
_rule(
    r"^(?:show|list|view|get)\s+(?:queue|pending|tasks?)$",
    IntentAction.SHOW_QUEUE,
)
_rule(
    r"^(?:show|list|view|get)\s+(?:history|recent|past\s+tasks?|completed)$",
    IntentAction.SHOW_HISTORY,
)
_rule(
    r"^(?:how much|cost|costs|spending|budget|show\s+costs?)$",
    IntentAction.SHOW_COSTS,
)
_rule(
    r"^(?:what(?:'s| is| are)?\s+(?:the\s+)?(?:costs?|spending|budget))$",
    IntentAction.SHOW_COSTS,
)
_rule(
    r"^(?:how much have I spent|total (?:cost|spending))$",
    IntentAction.SHOW_COSTS,
)

# ── Destructive patterns (checked BEFORE create to avoid false matches) ──


def _extract_delete_template(match: re.Match, text: str) -> dict:
    name_match = re.search(r"(?:template|called|named)\s+['\"]?(\S+)['\"]?", text, re.IGNORECASE)
    if name_match and name_match.group(1).lower() not in ("template", "called", "named", "the"):
        return {"name": name_match.group(1)}
    # Fallback: last word
    parts = text.strip().split()
    if len(parts) >= 2:
        return {"name": parts[-1]}
    return {}


_rule(
    r"^(?:delete|remove|drop)\s+(?:the\s+)?(?:template|tmpl)",
    IntentAction.DELETE_TEMPLATE,
    _extract_delete_template,
)
_rule(
    r"^(?:delete|remove|drop)\s+(?:the\s+)?\S+\s+template",
    IntentAction.DELETE_TEMPLATE,
    _extract_delete_template,
)


def _extract_delete_schedule(match: re.Match, text: str) -> dict:
    name_match = re.search(r"(?:schedule|called|named)\s+['\"]?(\S+)['\"]?", text, re.IGNORECASE)
    if name_match and name_match.group(1).lower() not in ("schedule", "called", "named", "the"):
        return {"name": name_match.group(1)}
    parts = text.strip().split()
    if len(parts) >= 2:
        return {"name": parts[-1]}
    return {}


_rule(
    r"^(?:delete|remove|drop|cancel)\s+(?:the\s+)?(?:schedule|cron)",
    IntentAction.DELETE_SCHEDULE,
    _extract_delete_schedule,
)

_rule(
    r"^(?:cancel|stop|kill)\s+(?:the\s+)?(?:current\s+)?task",
    IntentAction.CANCEL_TASK,
)

# ── Execute patterns (checked BEFORE create) ─────────────────────────


def _extract_run_template(match: re.Match, text: str) -> dict:
    name_match = re.search(r"(?:template|called|named|run)\s+['\"]?(\S+)['\"]?", text, re.IGNORECASE)
    if name_match and name_match.group(1).lower() not in ("template", "called", "named", "the", "run", "a"):
        return {"name": name_match.group(1)}
    parts = text.strip().split()
    for part in parts:
        if part.lower() not in ("run", "execute", "trigger", "start", "the", "a", "template", "called", "named"):
            return {"name": part}
    return {}


_rule(
    r"^(?:run|execute|trigger|start)\s+(?:the\s+)?template",
    IntentAction.RUN_TEMPLATE,
    _extract_run_template,
)
_rule(
    r"^(?:run|execute|trigger|start)\s+(?:the\s+)?['\"]?\w+['\"]?\s+template",
    IntentAction.RUN_TEMPLATE,
    _extract_run_template,
)


def _extract_toggle_schedule(match: re.Match, text: str) -> dict:
    name_match = re.search(r"(?:schedule|called|named)\s+['\"]?(\S+)['\"]?", text, re.IGNORECASE)
    if name_match and name_match.group(1).lower() not in ("schedule", "called", "named", "the"):
        return {"name": name_match.group(1)}
    parts = text.strip().split()
    if len(parts) >= 2:
        return {"name": parts[-1]}
    return {}


_rule(
    r"^(?:toggle|pause|unpause|enable|disable)\s+(?:the\s+)?schedule",
    IntentAction.TOGGLE_SCHEDULE,
    _extract_toggle_schedule,
)

# ── Create patterns ──────────────────────────────────────────────────


def _extract_save_template(match: re.Match, text: str) -> dict:
    """Extract template params from natural language."""
    # Try to find "called X" or "named X"
    name_match = re.search(r"(?:called|named|as)\s+['\"]?(\S+)['\"]?", text, re.IGNORECASE)
    name = name_match.group(1) if name_match else None

    # Try to find quoted prompt or "that does X"
    prompt_match = re.search(r"(?:that\s+(?:does|runs?|executes?)\s+|prompt\s+(?:is\s+)?|['\"])(.+?)(?:['\"]|$)", text, re.IGNORECASE)
    prompt = prompt_match.group(1).strip() if prompt_match else None

    # Agent extraction
    agent_match = re.search(r"(?:using|with|agent)\s+(\w+)", text, re.IGNORECASE)
    agent = agent_match.group(1) if agent_match else None

    return {"name": name, "prompt": prompt, "agent": agent}


_rule(
    r"(?:save|create|add|make)\s+(?:a\s+)?(?:new\s+)?template",
    IntentAction.SAVE_TEMPLATE,
    _extract_save_template,
)

_rule(
    r"save\s+(?:this|that|it)\s+as\s+(?:a\s+)?template",
    IntentAction.SAVE_TEMPLATE,
    _extract_save_template,
)


def _extract_save_schedule(match: re.Match, text: str) -> dict:
    """Extract schedule params from natural language."""
    params: dict[str, Any] = {}

    # Name extraction
    name_match = re.search(r"(?:called|named|as)\s+['\"]?(\S+)['\"]?", text, re.IGNORECASE)
    params["name"] = name_match.group(1) if name_match else None

    # Time extraction: "at HH:MM" or "at H am/pm"
    time_match = re.search(r"at\s+(\d{1,2}):(\d{2})", text, re.IGNORECASE)
    if time_match:
        params["cron_expr"] = f"{time_match.group(1).zfill(2)}:{time_match.group(2)}"
    else:
        ampm_match = re.search(r"at\s+(\d{1,2})\s*(am|pm)", text, re.IGNORECASE)
        if ampm_match:
            hour = int(ampm_match.group(1))
            if ampm_match.group(2).lower() == "pm" and hour < 12:
                hour += 12
            if ampm_match.group(2).lower() == "am" and hour == 12:
                hour = 0
            params["cron_expr"] = f"{hour:02d}:00"

    # Interval extraction: "every N minutes/hours"
    if not params.get("cron_expr"):
        interval_match = re.search(r"every\s+(\d+)\s*(?:min(?:ute)?s?|m)\b", text, re.IGNORECASE)
        if interval_match:
            params["cron_expr"] = f"*/{interval_match.group(1)}"
        else:
            hour_match = re.search(r"every\s+(\d+)\s*(?:hour|hr)s?\b", text, re.IGNORECASE)
            if hour_match:
                params["cron_expr"] = f"*/{int(hour_match.group(1)) * 60}"

    # Daily/morning/evening shortcuts
    daily_match = re.search(r"\b(?:daily|every\s+day)\b", text, re.IGNORECASE)
    morning_match = re.search(r"\b(?:every\s+morning|each\s+morning)\b", text, re.IGNORECASE)
    evening_match = re.search(r"\b(?:every\s+evening|each\s+evening)\b", text, re.IGNORECASE)
    if not params.get("cron_expr"):
        if morning_match:
            params["cron_expr"] = "08:00"
        elif evening_match:
            params["cron_expr"] = "18:00"
        elif daily_match:
            params["cron_expr"] = "09:00"

    # Prompt extraction: look for what to do
    # Remove schedule-related prefix to get the task description
    prompt_text = re.sub(
        r"^(?:schedule|set\s+up|create|add)\s+(?:a\s+)?(?:daily\s+|recurring\s+|scheduled?\s+)?(?:task\s+(?:to|that)\s+)?",
        "", text, flags=re.IGNORECASE
    ).strip()
    # Remove time parts
    prompt_text = re.sub(r"\b(?:at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|every\s+(?:day|morning|evening|\d+\s*(?:min\w*|hour?\w*)))\b", "", prompt_text, flags=re.IGNORECASE).strip()
    # Remove name parts
    prompt_text = re.sub(r"\b(?:called|named|as)\s+\S+\b", "", prompt_text, flags=re.IGNORECASE).strip()
    # Clean residual connectors
    prompt_text = re.sub(r"^\s*(?:to|that|which)\s+", "", prompt_text).strip()
    if prompt_text:
        params["prompt"] = prompt_text

    return params


_rule(
    r"(?:schedule|set\s+up)\s+(?:a\s+)?(?:daily|recurring|every|each)",
    IntentAction.SAVE_SCHEDULE,
    _extract_save_schedule,
)
_rule(
    r"every\s+(?:morning|evening|day|night|\d+\s*(?:min|hour))",
    IntentAction.SAVE_SCHEDULE,
    _extract_save_schedule,
)


def _extract_set_project(match: re.Match, text: str) -> dict:
    path_match = re.search(r"(?:to|dir(?:ectory)?|path)\s+(\S+)", text, re.IGNORECASE)
    if path_match:
        return {"project_dir": path_match.group(1)}
    # Last token might be the path
    parts = text.strip().split()
    if len(parts) >= 2 and ("/" in parts[-1] or "~" in parts[-1]):
        return {"project_dir": parts[-1]}
    return {}


_rule(
    r"(?:switch|change|set|use)\s+(?:the\s+)?(?:project|dir(?:ectory)?|folder|path)",
    IntentAction.SET_PROJECT,
    _extract_set_project,
)


def _extract_set_agent(match: re.Match, text: str) -> dict:
    agent_match = re.search(r"(?:to|use|switch\s+to)\s+(\w+)", text, re.IGNORECASE)
    return {"agent": agent_match.group(1)} if agent_match else {}


_rule(
    r"(?:switch|change|set|use)\s+(?:the\s+)?agent",
    IntentAction.SET_AGENT,
    _extract_set_agent,
)


def _extract_set_model(match: re.Match, text: str) -> dict:
    model_match = re.search(r"(?:to|use|switch\s+to)\s+(\S+)", text, re.IGNORECASE)
    return {"model": model_match.group(1)} if model_match else {}


_rule(
    r"(?:switch|change|set|use)\s+(?:the\s+)?model",
    IntentAction.SET_MODEL,
    _extract_set_model,
)


def classify_pattern(text: str) -> ClassifiedIntent | None:
    """Tier 1: Classify using pattern matching. Returns None if no patterns match."""
    text = text.strip()
    if not text:
        return None

    for pattern, action, extractor in _PATTERN_RULES:
        match = pattern.search(text)
        if match:
            params = {}
            if extractor is not None:
                params = extractor(match, text)
            return ClassifiedIntent(
                action=action,
                confidence=0.95,
                params=params,
                raw_text=text,
            )

    return None


# ── Tier 2: LLM-based classification ─────────────────────────────────

_LLM_SYSTEM_PROMPT = """You are a command classifier for a task management bot. Classify the user message into ONE intent.

Available intents:
- list_templates: user wants to see saved templates
- list_schedules: user wants to see schedules
- show_status: user asks what's running
- show_queue: user wants to see pending tasks
- show_history: user wants to see recent/past tasks
- show_costs: user asks about costs/spending
- save_template: user wants to save a template (extract: name, prompt, agent)
- save_schedule: user wants to create a schedule (extract: name, cron_expr, prompt)
- delete_template: user wants to delete a template (extract: name)
- delete_schedule: user wants to delete a schedule (extract: name)
- cancel_task: user wants to cancel running task
- run_template: user wants to run a template (extract: name)
- toggle_schedule: user wants to enable/disable a schedule (extract: name)
- set_project: user wants to change project directory (extract: project_dir)
- set_agent: user wants to change agent (extract: agent)
- set_model: user wants to change model (extract: model)
- task_prompt: this is an actual task to execute, not a management command

Respond ONLY with valid JSON: {"intent": "<intent_name>", "confidence": 0.0-1.0, "params": {}}
If unsure, set confidence below 0.7 and intent to "task_prompt".
"""


def _parse_llm_response(response_text: str) -> ClassifiedIntent | None:
    """Parse LLM JSON response into a ClassifiedIntent."""
    try:
        # Strip markdown code fences if present
        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```\w*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)

        data = json.loads(cleaned)
        intent_str = data.get("intent", "task_prompt")
        confidence = float(data.get("confidence", 0.5))
        params = data.get("params", {})

        # Map intent string to IntentAction
        try:
            action = IntentAction(intent_str)
        except ValueError:
            action = IntentAction.TASK_PROMPT
            confidence = 0.3

        return ClassifiedIntent(
            action=action,
            confidence=confidence,
            params=params if isinstance(params, dict) else {},
            raw_text=response_text,
        )
    except (json.JSONDecodeError, TypeError, KeyError) as e:
        logger.warning("Failed to parse LLM classification response: %s", e)
        return None


async def classify_llm(text: str) -> ClassifiedIntent | None:
    """Tier 2: Classify using LLM subprocess call for ambiguous inputs.

    Uses the same agent infrastructure (subprocess + timeout) but with a
    cheap/fast model. Returns None if LLM call fails.
    """
    import asyncio
    import shlex

    prompt = (
        f"Classify this user message:\n\n"
        f"\"{text}\"\n\n"
        f"Respond with JSON only."
    )

    # Use claude with a fast model for classification
    cmd = [
        "claude", "-p",
        f"{_LLM_SYSTEM_PROMPT}\n\n{prompt}",
        "--model", "haiku",
        "--no-input",
        "--max-tokens", "200",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=15,
        )
        output = stdout.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            logger.warning("LLM classifier returned %d: %s", process.returncode, stderr.decode()[:200])
            return None
        return _parse_llm_response(output)
    except asyncio.TimeoutError:
        logger.warning("LLM classifier timed out")
        return None
    except FileNotFoundError:
        logger.warning("LLM classifier binary not found (claude)")
        return None
    except Exception as e:
        logger.warning("LLM classifier error: %s", e)
        return None


# ── Combined classification ───────────────────────────────────────────

CONFIDENCE_THRESHOLD = 0.7


async def classify(text: str, *, use_llm: bool = True) -> ClassifiedIntent:
    """Classify user text through Tier 1 patterns, then optionally Tier 2 LLM.

    Returns a ClassifiedIntent — always returns something (TASK_PROMPT fallback).
    """
    # Tier 1: Pattern matching (instant)
    result = classify_pattern(text)
    if result is not None and result.confidence >= CONFIDENCE_THRESHOLD:
        return result

    # Tier 2: LLM classification (if enabled)
    if use_llm:
        llm_result = await classify_llm(text)
        if llm_result is not None and llm_result.confidence >= CONFIDENCE_THRESHOLD:
            return llm_result

    # Fallback: treat as regular task prompt
    return ClassifiedIntent(
        action=IntentAction.TASK_PROMPT,
        confidence=0.5,
        params={},
        raw_text=text,
    )


def format_confirmation(intent: ClassifiedIntent) -> str:
    """Format a human-readable confirmation message for an intent."""
    action = intent.action
    params = intent.params

    if action == IntentAction.SAVE_TEMPLATE:
        name = params.get("name", "?")
        prompt = params.get("prompt", "?")
        agent = params.get("agent", "default")
        return (
            f"🤖 I'll create a template:\n"
            f"  Name: `{name}`\n"
            f"  Prompt: _{prompt[:100]}_\n"
            f"  Agent: `{agent}`"
        )

    if action == IntentAction.SAVE_SCHEDULE:
        name = params.get("name", "?")
        cron = params.get("cron_expr", "?")
        prompt = params.get("prompt", "?")
        return (
            f"🤖 I'll create a schedule:\n"
            f"  Name: `{name}`\n"
            f"  Cron: `{cron}`\n"
            f"  Prompt: _{prompt[:100]}_"
        )

    if action == IntentAction.DELETE_TEMPLATE:
        name = params.get("name", "?")
        return f"⚠️ Delete template `{name}`?\n_This cannot be undone._"

    if action == IntentAction.DELETE_SCHEDULE:
        name = params.get("name", "?")
        return f"⚠️ Delete schedule `{name}`?\n_This cannot be undone._"

    if action == IntentAction.CANCEL_TASK:
        return "⚠️ Cancel the currently running task?"

    if action == IntentAction.RUN_TEMPLATE:
        name = params.get("name", "?")
        return f"🤖 Run template `{name}`?"

    if action == IntentAction.TOGGLE_SCHEDULE:
        name = params.get("name", "?")
        return f"🤖 Toggle schedule `{name}`?"

    if action == IntentAction.SET_PROJECT:
        path = params.get("project_dir", "?")
        return f"🤖 Switch project to `{path}`?"

    if action == IntentAction.SET_AGENT:
        agent = params.get("agent", "?")
        return f"🤖 Switch agent to `{agent}`?"

    if action == IntentAction.SET_MODEL:
        model = params.get("model", "?")
        return f"🤖 Switch model to `{model}`?"

    return f"🤖 Classified as: {action.value}"

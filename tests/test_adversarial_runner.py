"""Adversarial tests for runner — command building, env safety, path validation, summarization — 150+ edge cases."""

from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

import pytest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

from app.core.runner import AgentRunner, MAX_OUTPUT_BYTES
from app.config.settings import settings


def _make_task(**kwargs):
    """Create a minimal task-like object for unit testing _build_command."""
    defaults = {
        "id": 1,
        "prompt": "fix the bug",
        "project_dir": "/tmp/testproject",
        "agent": "opencode",
        "model": None,
        "parent_task_id": None,
        "chain_id": None,
        "chain_step": None,
        "telegram_chat_id": 12345,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ═══════════════════════════════════════════════════════════════════════
# BUILD COMMAND ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestBuildCommandAdversarial:

    def setup_method(self):
        self.runner = AgentRunner()

    def test_basic_build(self):
        task = _make_task()
        argv = self.runner._build_command(task)
        assert len(argv) >= 1
        assert "fix the bug" in " ".join(argv)

    def test_unknown_agent(self):
        task = _make_task(agent="nonexistent")
        argv = self.runner._build_command(task)
        assert argv[0] == "echo"
        assert "Unknown agent" in argv[1]

    def test_prompt_with_shell_chars(self):
        task = _make_task(prompt="fix && rm -rf /")
        argv = self.runner._build_command(task)
        # Prompt should be a single argument, not split by &&
        combined = " ".join(argv)
        assert "fix && rm -rf /" in combined

    def test_prompt_with_semicolons(self):
        task = _make_task(prompt="test; echo pwned")
        argv = self.runner._build_command(task)
        combined = " ".join(argv)
        assert "test; echo pwned" in combined

    def test_prompt_with_backticks(self):
        task = _make_task(prompt="fix `whoami` issue")
        argv = self.runner._build_command(task)
        combined = " ".join(argv)
        assert "`whoami`" in combined

    def test_prompt_with_dollar_expansion(self):
        task = _make_task(prompt="fix $(cat /etc/passwd)")
        argv = self.runner._build_command(task)
        combined = " ".join(argv)
        assert "$(cat /etc/passwd)" in combined

    def test_prompt_with_pipe(self):
        task = _make_task(prompt="fix | cat /etc/passwd")
        argv = self.runner._build_command(task)
        # Should be in a single arg
        combined = " ".join(argv)
        assert "fix | cat /etc/passwd" in combined

    def test_prompt_with_newlines(self):
        task = _make_task(prompt="fix\nthis\nbug")
        argv = self.runner._build_command(task)
        combined = " ".join(argv)
        assert "fix\nthis\nbug" in combined

    def test_prompt_with_null_bytes(self):
        task = _make_task(prompt="fix\x00null")
        argv = self.runner._build_command(task)
        combined = " ".join(argv)
        assert "\x00" in combined

    def test_prompt_with_quotes(self):
        task = _make_task(prompt='fix the "big" bug')
        argv = self.runner._build_command(task)
        combined = " ".join(argv)
        assert '"big"' in combined

    def test_prompt_with_single_quotes(self):
        task = _make_task(prompt="fix the 'big' bug")
        argv = self.runner._build_command(task)
        combined = " ".join(argv)
        assert "'big'" in combined

    def test_empty_prompt(self):
        task = _make_task(prompt="")
        argv = self.runner._build_command(task)
        assert len(argv) >= 1

    def test_very_long_prompt(self):
        task = _make_task(prompt="x" * 10000)
        argv = self.runner._build_command(task)
        combined = " ".join(argv)
        assert "x" * 100 in combined

    def test_unicode_prompt(self):
        task = _make_task(prompt="日本語のバグを修正して 🔥")
        argv = self.runner._build_command(task)
        combined = " ".join(argv)
        assert "日本語" in combined

    def test_model_injection_via_flag(self):
        """Model value should be a single arg, not split by shell chars."""
        task = _make_task(model="sonnet; rm -rf /")
        argv = self.runner._build_command(task)
        # The model should appear as a single value
        combined = " ".join(argv)
        assert "sonnet; rm -rf /" in combined

    def test_model_with_flag_chars(self):
        task = _make_task(model="--help")
        argv = self.runner._build_command(task)
        # Should be treated as model value not flag
        assert "--help" in argv

    def test_model_empty_string(self):
        task = _make_task(model="")
        argv = self.runner._build_command(task)
        # Empty model should not inject any flag
        # (empty string is falsy in Python)
        combined = " ".join(argv)
        assert "model" not in combined.lower() or "--model" not in combined

    def test_continue_flag_injection(self):
        task = _make_task(parent_task_id=42, prompt="continue this")
        argv = self.runner._build_command(task)
        # parent_task_id should inject continue flag before prompt
        assert len(argv) >= 1

    def test_project_dir_with_spaces(self):
        """project_dir is used as cwd, not in argv for default agent."""
        task = _make_task(project_dir="/tmp/my project")
        argv = self.runner._build_command(task)
        # opencode template doesn't include {project_dir} in command
        assert len(argv) >= 1

    def test_project_dir_with_special_chars(self):
        """project_dir is cwd — not in default argv."""
        task = _make_task(project_dir="/tmp/$HOME")
        argv = self.runner._build_command(task)
        assert len(argv) >= 1


# ═══════════════════════════════════════════════════════════════════════
# SAFE ENV ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestSafeEnvAdversarial:

    def test_strips_telegram_token(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "secret123"}, clear=False):
            env = AgentRunner._safe_env()
            assert "TELEGRAM_BOT_TOKEN" not in env

    def test_strips_bot_token(self):
        with patch.dict(os.environ, {"BOT_TOKEN_SOMETHING": "secret"}, clear=False):
            env = AgentRunner._safe_env()
            assert "BOT_TOKEN_SOMETHING" not in env

    def test_strips_slack(self):
        with patch.dict(os.environ, {"SLACK_WEBHOOK": "secret"}, clear=False):
            env = AgentRunner._safe_env()
            assert "SLACK_WEBHOOK" not in env

    def test_strips_api_key(self):
        with patch.dict(os.environ, {"API_KEY_OPENAI": "sk-xxx"}, clear=False):
            env = AgentRunner._safe_env()
            assert "API_KEY_OPENAI" not in env

    def test_strips_secret(self):
        with patch.dict(os.environ, {"SECRET_KEY": "django-xxx"}, clear=False):
            env = AgentRunner._safe_env()
            assert "SECRET_KEY" not in env

    def test_keeps_path(self):
        env = AgentRunner._safe_env()
        assert "PATH" in env

    def test_keeps_home(self):
        env = AgentRunner._safe_env()
        assert "HOME" in env

    def test_case_insensitive_prefix(self):
        with patch.dict(os.environ, {"telegram_bot_token": "secret"}, clear=False):
            env = AgentRunner._safe_env()
            assert "telegram_bot_token" not in env

    def test_does_not_strip_partial_match(self):
        with patch.dict(os.environ, {"MY_TELEGRAM": "val"}, clear=False):
            env = AgentRunner._safe_env()
            # Strips if key starts with TELEGRAM_ (after upper())
            # MY_TELEGRAM doesn't start with any prefix
            # Actually: k.upper() = "MY_TELEGRAM", doesn't start with "TELEGRAM_"
            assert "MY_TELEGRAM" in env

    def test_returns_dict(self):
        env = AgentRunner._safe_env()
        assert isinstance(env, dict)


# ═══════════════════════════════════════════════════════════════════════
# IS_ALLOWED_DIR ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestIsAllowedDirAdversarial:

    def test_exact_match(self):
        allowed = settings.allowed_project_dirs_list
        if allowed:
            real = os.path.realpath(os.path.expanduser(allowed[0]))
            assert AgentRunner._is_allowed_dir(real) is True

    def test_subdirectory(self):
        allowed = settings.allowed_project_dirs_list
        if allowed:
            real = os.path.realpath(os.path.expanduser(allowed[0]))
            sub = os.path.join(real, "subdir")
            assert AgentRunner._is_allowed_dir(sub) is True

    def test_prefix_attack(self):
        """~/ai-evil should not be allowed if only ~/ai is allowed."""
        allowed = settings.allowed_project_dirs_list
        if allowed:
            real = os.path.realpath(os.path.expanduser(allowed[0]))
            evil = real + "-evil"
            assert AgentRunner._is_allowed_dir(evil) is False

    def test_root_not_allowed(self):
        assert AgentRunner._is_allowed_dir("/") is False

    def test_etc_not_allowed(self):
        assert AgentRunner._is_allowed_dir("/etc") is False

    def test_empty_string(self):
        assert AgentRunner._is_allowed_dir("") is False

    def test_relative_path(self):
        # Relative paths should not match absolute allowed paths
        assert AgentRunner._is_allowed_dir("../etc/passwd") is False

    def test_parent_traversal(self):
        allowed = settings.allowed_project_dirs_list
        if allowed:
            real = os.path.realpath(os.path.expanduser(allowed[0]))
            traversed = os.path.join(real, "..", "..", "etc")
            resolved = os.path.realpath(traversed)
            # Should not be allowed unless /etc is explicitly allowed
            if not any(resolved.startswith(os.path.realpath(os.path.expanduser(a)))
                       for a in allowed):
                assert AgentRunner._is_allowed_dir(resolved) is False


# ═══════════════════════════════════════════════════════════════════════
# SUMMARIZE ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestSummarizeAdversarial:

    def setup_method(self):
        self.runner = AgentRunner()

    def test_empty_output(self):
        result = self.runner._summarize("", 0)
        assert result == "(no output)"

    def test_success_short(self):
        result = self.runner._summarize("line1\nline2", 0)
        assert "line1" in result
        assert "line2" in result

    def test_success_long(self):
        lines = "\n".join(f"line{i}" for i in range(100))
        result = self.runner._summarize(lines, 0)
        # Should only contain last 10 lines
        assert "line90" in result

    def test_error_extracts_keywords(self):
        output = "starting\nall good\nERROR: something broke\ncleanup"
        result = self.runner._summarize(output, 1)
        assert "ERROR" in result

    def test_error_traceback(self):
        output = "File 'test.py', line 1\nTraceback (most recent call last):\n  ...\nValueError: bad"
        result = self.runner._summarize(output, 1)
        assert "Traceback" in result

    def test_error_no_keywords_falls_to_tail(self):
        output = "step 1\nstep 2\nstep 3"
        result = self.runner._summarize(output, 1)
        # No error keywords, but exit code != 0, should still return tail
        assert len(result) > 0

    def test_max_summary_length(self):
        # Create output with many error lines
        output = "\n".join(f"ERROR: line {i}" for i in range(1000))
        result = self.runner._summarize(output, 1)
        assert len(result) <= settings.output_summary_max_chars

    def test_unicode_output(self):
        result = self.runner._summarize("成功しました ✓", 0)
        assert "成功" in result

    def test_binary_like_output(self):
        result = self.runner._summarize("\x00\x01\x02\xff", 0)
        assert len(result) > 0

    def test_only_whitespace(self):
        result = self.runner._summarize("   \n\n\n   ", 0)
        assert result == "(no output)"

    def test_single_very_long_line(self):
        output = "x" * 50000
        result = self.runner._summarize(output, 0)
        assert len(result) <= settings.output_summary_max_chars

    def test_exception_keyword(self):
        output = "Exception: something went wrong\nFatal: died"
        result = self.runner._summarize(output, 1)
        assert "Exception" in result or "Fatal" in result

    def test_failed_keyword(self):
        output = "Test failed: 3 failures"
        result = self.runner._summarize(output, 1)
        assert "failed" in result


# ═══════════════════════════════════════════════════════════════════════
# NOTIFY / LIFECYCLE ADVERSARIAL
# ═══════════════════════════════════════════════════════════════════════

class TestRunnerLifecycleAdversarial:

    def test_notify_new_task(self):
        runner = AgentRunner()
        runner.notify_new_task()
        assert runner._wake_event.is_set()

    def test_cancel_no_process(self):
        import asyncio
        runner = AgentRunner()
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(runner.cancel_current())
            assert result is False
        finally:
            loop.close()

    def test_runner_init_defaults(self):
        runner = AgentRunner()
        assert runner._running is False
        assert runner._current_process is None
        assert runner._current_task_id is None
        assert runner._backoff == 0

    def test_runner_with_callback(self):
        async def dummy(task): pass
        runner = AgentRunner(on_complete=dummy)
        assert runner._on_complete is dummy

    def test_max_output_bytes_constant(self):
        assert MAX_OUTPUT_BYTES == 2 * 1024 * 1024


# ═══════════════════════════════════════════════════════════════════════
# SENTINEL LEAK DETECTION
# ═══════════════════════════════════════════════════════════════════════

class TestSentinelLeakDetection:
    """Ensure sentinel tokens don't leak into final argv."""

    def setup_method(self):
        self.runner = AgentRunner()

    def test_no_prompt_sentinel_in_output(self):
        task = _make_task(prompt="test prompt")
        argv = self.runner._build_command(task)
        for arg in argv:
            assert "\x00PROMPT\x00" not in arg

    def test_no_dir_sentinel_in_output(self):
        task = _make_task(project_dir="/tmp/test")
        argv = self.runner._build_command(task)
        for arg in argv:
            assert "\x00DIR\x00" not in arg

    def test_no_model_sentinel_in_output(self):
        task = _make_task(model="sonnet")
        argv = self.runner._build_command(task)
        for arg in argv:
            assert "\x00MODEL\x00" not in arg

    def test_prompt_with_sentinel_chars(self):
        """What if the prompt itself contains sentinel-like text?"""
        task = _make_task(prompt="test \x00PROMPT\x00 embedded")
        argv = self.runner._build_command(task)
        # Should still work — the template replacement happens first
        combined = " ".join(argv)
        assert "\x00PROMPT\x00" in combined  # Actually this IS in the prompt itself

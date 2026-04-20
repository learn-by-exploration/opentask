"""Tests for the agent runner — command building, summarization, and execution."""

from __future__ import annotations

import asyncio

import pytest

from app.core.models import Task, TaskStatus


def _make_task(**overrides) -> Task:
    """Build a Task instance without persisting to DB."""
    defaults = {
        "id": 1,
        "prompt": "fix the login bug",
        "project_dir": "/home/user/project",
        "agent": "opencode",
        "status": TaskStatus.RUNNING,
        "model": None,
        "priority": 0,
        "retry_count": 0,
        "max_retries": 1,
        "git_diff": None,
    }
    defaults.update(overrides)
    return Task(**defaults)


# ── _build_command ──────────────────────────────────────────────────


class TestBuildCommand:
    """Test command building returns an argv list."""

    def setup_method(self):
        from app.core.runner import AgentRunner

        self.runner = AgentRunner()

    def test_basic_command(self):
        task = _make_task(prompt="fix the bug", agent="opencode")
        cmd = self.runner._build_command(task)
        assert isinstance(cmd, list)
        assert "opencode" in cmd
        assert "fix the bug" in cmd

    def test_prompt_with_shell_special_chars(self):
        task = _make_task(prompt="fix bug; rm -rf /")
        cmd = self.runner._build_command(task)
        # With exec, the prompt is a single element — no shell interpretation
        assert "fix bug; rm -rf /" in cmd

    def test_prompt_with_single_quotes(self):
        task = _make_task(prompt="fix the user's profile")
        cmd = self.runner._build_command(task)
        assert "fix the user's profile" in cmd

    def test_project_dir_with_spaces(self):
        # project_dir is passed as cwd to subprocess, not in argv
        task = _make_task(project_dir="/home/user/my project")
        cmd = self.runner._build_command(task)
        assert isinstance(cmd, list)
        assert "opencode" in cmd

    def test_unknown_agent_returns_echo(self):
        task = _make_task(agent="nonexistent")
        cmd = self.runner._build_command(task)
        assert cmd[0] == "echo"
        assert "Unknown agent" in cmd[1]

    def test_claude_agent(self):
        task = _make_task(agent="claude")
        cmd = self.runner._build_command(task)
        assert "claude" in cmd
        assert "-p" in cmd


# ── _summarize ──────────────────────────────────────────────────────


class TestSummarize:
    """Test output summarization."""

    def setup_method(self):
        from app.core.runner import AgentRunner

        self.runner = AgentRunner()

    def test_success_returns_tail(self):
        output = "\n".join(f"line {i}" for i in range(20))
        summary = self.runner._summarize(output, exit_code=0)
        assert "line 19" in summary
        assert "line 10" in summary

    def test_failure_prioritizes_error_lines(self):
        lines = [
            "starting up",
            "loading config",
            "Error: database connection failed",
            "Traceback (most recent call last):",
            "  File 'main.py'",
            "done cleanup",
        ]
        output = "\n".join(lines)
        summary = self.runner._summarize(output, exit_code=1)
        assert "Error: database connection failed" in summary
        assert "Traceback" in summary

    def test_failure_without_error_lines_returns_tail(self):
        output = "some normal output\nall fine"
        summary = self.runner._summarize(output, exit_code=1)
        assert "all fine" in summary

    def test_empty_output(self):
        summary = self.runner._summarize("", exit_code=0)
        assert summary == "(no output)"

    def test_summary_truncated_to_max_chars(self, monkeypatch):
        from app.config.settings import settings

        monkeypatch.setattr(settings, "output_summary_max_chars", 20)
        output = "a" * 100
        summary = self.runner._summarize(output, exit_code=0)
        assert len(summary) <= 20


# ── cancel_current / stop ───────────────────────────────────────────


class TestRunnerLifecycle:
    def test_stop_sets_running_false(self):
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        runner._running = True
        runner.stop()
        assert runner._running is False

    @pytest.mark.asyncio
    async def test_cancel_current_returns_false_when_no_process(self):
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        result = await runner.cancel_current()
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_current_kills_running_process(self):
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        # Start a long-running process
        proc = await asyncio.create_subprocess_shell(
            "sleep 60",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        runner._current_process = proc
        result = await runner.cancel_current()
        assert result is True
        # Process should be terminated
        assert proc.returncode is not None


# ── _execute with real subprocess ───────────────────────────────────


class TestExecuteIntegration:
    """Integration tests that run real subprocesses."""

    @pytest.mark.asyncio
    async def test_successful_exit_code_zero(self, monkeypatch, tmp_path):
        """Verify exit code 0 is correctly reported as COMPLETED, not FAILED."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        # Use echo as the agent — always exits 0
        monkeypatch.setattr(
            settings,
            "agent_commands",
            {"test_agent": "echo {prompt}"},
        )
        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])

        completed_tasks = []

        async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None, git_diff=None):
            completed_tasks.append({
                "task_id": task_id,
                "exit_code": exit_code,
                "output_summary": output_summary,
                "error_message": error_message,
            })
            task = _make_task(id=task_id, status=TaskStatus.COMPLETED)
            task.exit_code = exit_code
            task.output_summary = output_summary
            return task

        import app.core.runner as runner_mod
        monkeypatch.setattr(runner_mod, "complete_task", fake_complete)

        runner = AgentRunner()
        task = _make_task(agent="test_agent", project_dir=str(tmp_path))
        await runner._execute(task)

        assert len(completed_tasks) == 1
        assert completed_tasks[0]["exit_code"] == 0
        assert completed_tasks[0]["error_message"] is None

    @pytest.mark.asyncio
    async def test_failing_command_exit_code_nonzero(self, monkeypatch, tmp_path):
        """Verify a failing command reports the correct exit code."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        monkeypatch.setattr(
            settings,
            "agent_commands",
            {"test_agent": "bash -c {prompt}"},
        )
        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])

        completed_tasks = []

        async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None, git_diff=None):
            completed_tasks.append({"exit_code": exit_code, "error_message": error_message})
            task = _make_task(id=task_id, status=TaskStatus.FAILED)
            task.exit_code = exit_code
            return task

        import app.core.runner as runner_mod
        monkeypatch.setattr(runner_mod, "complete_task", fake_complete)

        runner = AgentRunner()
        task = _make_task(prompt="exit 42", agent="test_agent", project_dir=str(tmp_path))
        await runner._execute(task)

        assert len(completed_tasks) == 1
        assert completed_tasks[0]["exit_code"] == 42
        assert completed_tasks[0]["error_message"] is not None

    @pytest.mark.asyncio
    async def test_nonexistent_project_dir(self, monkeypatch):
        """Verify missing project_dir is caught before subprocess spawn."""
        from app.core.runner import AgentRunner

        completed_tasks = []

        async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None, git_diff=None):
            completed_tasks.append({"exit_code": exit_code, "error_message": error_message})
            task = _make_task(id=task_id, status=TaskStatus.FAILED)
            return task

        import app.core.runner as runner_mod
        monkeypatch.setattr(runner_mod, "complete_task", fake_complete)

        runner = AgentRunner()
        task = _make_task(project_dir="/nonexistent/path/xyz")
        await runner._execute(task)

        assert len(completed_tasks) == 1
        assert completed_tasks[0]["exit_code"] == -1
        assert "does not exist" in completed_tasks[0]["error_message"]


# ── _after_complete resilience ──────────────────────────────────────


class TestAfterCompleteResilience:
    """Tests for error handling in _after_complete."""

    @pytest.mark.asyncio
    async def test_notify_failure_does_not_crash_repeat(self, monkeypatch):
        """If _on_complete throws, maybe_reenqueue should still run."""
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        reenqueue_called = False
        original_maybe = runner_mod.maybe_reenqueue

        async def tracking_reenqueue(task):
            nonlocal reenqueue_called
            reenqueue_called = True
            return None  # no actual re-enqueue

        monkeypatch.setattr(runner_mod, "maybe_reenqueue", tracking_reenqueue)

        async def exploding_notify(task):
            raise RuntimeError("Telegram API down!")

        runner = AgentRunner(on_complete=exploding_notify)
        task = _make_task(status=TaskStatus.COMPLETED)
        task.chain_id = None
        task.repeat_remaining = None
        task.repeat_until = None

        # Should NOT raise despite the callback failing
        await runner._after_complete(task)
        assert reenqueue_called

    @pytest.mark.asyncio
    async def test_notify_failure_does_not_crash_chain(self, monkeypatch):
        """If _on_complete throws, advance_chain should still run."""
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        advance_called = False

        async def noop_reenqueue(task):
            return None

        async def tracking_advance(task):
            nonlocal advance_called
            advance_called = True
            return None

        monkeypatch.setattr(runner_mod, "maybe_reenqueue", noop_reenqueue)
        monkeypatch.setattr(runner_mod, "advance_chain", tracking_advance)

        async def exploding_notify(task):
            raise ConnectionError("Network error!")

        runner = AgentRunner(on_complete=exploding_notify)
        task = _make_task(status=TaskStatus.COMPLETED)
        task.chain_id = 42
        task.chain_step = 0
        task.repeat_remaining = None
        task.repeat_until = None

        await runner._after_complete(task)
        assert advance_called


# ── Crash guard & event-based wake-up ───────────────────────────────


class TestRunnerCrashGuard:
    """Runner poll loop should survive exceptions from pick_next_task."""

    @pytest.mark.asyncio
    async def test_continues_after_pick_next_failure(self, monkeypatch):
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        call_count = 0

        async def mock_pick():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("DB locked")
            return None

        monkeypatch.setattr(runner_mod, "pick_next_task", mock_pick)

        runner = AgentRunner()
        runner._wake_event.set()  # pre-set so waits return immediately

        async def stop_soon():
            await asyncio.sleep(0.3)
            runner.stop()

        await asyncio.gather(runner.start(), stop_soon())
        assert call_count >= 2  # survived the crash

    @pytest.mark.asyncio
    async def test_backoff_resets_on_success(self, monkeypatch):
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        call_count = 0

        async def mock_pick():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient")
            return None

        monkeypatch.setattr(runner_mod, "pick_next_task", mock_pick)

        runner = AgentRunner()
        runner._wake_event.set()

        async def stop_soon():
            await asyncio.sleep(0.3)
            runner.stop()

        await asyncio.gather(runner.start(), stop_soon())
        assert runner._backoff == 0  # reset after successful poll


class TestNotifyNewTask:
    """Tests for the event-based wake-up mechanism."""

    def test_event_set_on_notify(self):
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        assert not runner._wake_event.is_set()
        runner.notify_new_task()
        assert runner._wake_event.is_set()

    def test_stop_sets_wake_event(self):
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        runner._running = True
        runner.stop()
        assert runner._wake_event.is_set()
        assert not runner._running

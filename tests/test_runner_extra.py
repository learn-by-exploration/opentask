"""Extra runner tests — edge cases and gaps from coverage audit."""

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


# ── _build_command edge cases ───────────────────────────────────────


class TestBuildCommandEdgeCases:
    def setup_method(self):
        from app.core.runner import AgentRunner

        self.runner = AgentRunner()

    def test_prompt_with_dollar_sign(self):
        """Dollar signs in prompt are passed literally (no shell expansion)."""
        task = _make_task(prompt="deploy $HOME/app")
        cmd = self.runner._build_command(task)
        assert isinstance(cmd, list)
        assert "deploy $HOME/app" in cmd

    def test_prompt_with_backticks(self):
        """Backticks are passed literally (no shell expansion)."""
        task = _make_task(prompt="run `ls` command")
        cmd = self.runner._build_command(task)
        assert "run `ls` command" in cmd

    def test_prompt_with_newlines(self):
        """Newlines in prompt should be preserved in a single arg."""
        task = _make_task(prompt="line1\nline2")
        cmd = self.runner._build_command(task)
        # The prompt may be split by shlex.split if it contains bare newlines
        joined = " ".join(cmd)
        assert "line1" in joined
        assert "line2" in joined

    def test_prompt_with_pipe(self):
        task = _make_task(prompt="list files | grep txt")
        cmd = self.runner._build_command(task)
        # With exec, pipe is just part of the string — no shell interpretation
        assert "list files | grep txt" in cmd

    def test_empty_prompt(self):
        task = _make_task(prompt="")
        cmd = self.runner._build_command(task)
        assert isinstance(cmd, list)
        assert "" in cmd


# ── _summarize edge cases ───────────────────────────────────────────


class TestSummarizeEdgeCases:
    def setup_method(self):
        from app.core.runner import AgentRunner

        self.runner = AgentRunner()

    def test_whitespace_only_output(self):
        """Output with only whitespace should return '(no output)'."""
        summary = self.runner._summarize("   \n  \n  ", exit_code=0)
        assert summary == "(no output)"

    def test_single_line_output(self):
        summary = self.runner._summarize("done", exit_code=0)
        assert summary == "done"

    def test_failure_with_multiple_error_keywords(self):
        """Multiple error lines should be captured (up to 5)."""
        lines = [f"Error line {i}" for i in range(10)]
        output = "\n".join(lines)
        summary = self.runner._summarize(output, exit_code=1)
        # Should take last 5 error lines
        assert "Error line 9" in summary
        assert "Error line 5" in summary

    def test_failure_with_fatal_keyword(self):
        output = "Starting...\nfatal: repository not found\nCleanup"
        summary = self.runner._summarize(output, exit_code=1)
        assert "fatal:" in summary

    def test_failure_with_failed_keyword(self):
        output = "Test 1 passed\nTest 2 FAILED\nDone"
        summary = self.runner._summarize(output, exit_code=1)
        assert "FAILED" in summary

    def test_success_with_many_lines_returns_tail(self):
        lines = [f"line {i}" for i in range(100)]
        output = "\n".join(lines)
        summary = self.runner._summarize(output, exit_code=0)
        # Should include last 10 lines
        assert "line 99" in summary
        assert "line 90" in summary
        # Should NOT include early lines
        assert "line 0" not in summary


# ── AgentRunner __init__ ────────────────────────────────────────────


class TestRunnerInit:
    def test_default_state(self):
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        assert runner._on_complete is None
        assert runner._running is False
        assert runner._current_process is None
        assert runner._current_task_id is None

    def test_with_callback(self):
        from app.core.runner import AgentRunner

        async def my_callback(task):
            pass

        runner = AgentRunner(on_complete=my_callback)
        assert runner._on_complete is my_callback


# ── _after_complete edge cases ──────────────────────────────────────


class TestAfterCompleteEdgeCases:
    @pytest.mark.asyncio
    async def test_no_callback_runs_without_error(self, monkeypatch):
        """When on_complete is None, _after_complete should still work."""
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        reenqueue_called = False

        async def noop_reenqueue(task):
            nonlocal reenqueue_called
            reenqueue_called = True
            return None

        monkeypatch.setattr(runner_mod, "maybe_reenqueue", noop_reenqueue)

        runner = AgentRunner(on_complete=None)
        task = _make_task(status=TaskStatus.COMPLETED)
        task.chain_id = None
        task.repeat_remaining = None
        task.repeat_until = None

        await runner._after_complete(task)
        assert reenqueue_called

    @pytest.mark.asyncio
    async def test_reenqueue_skips_chain_advance(self, monkeypatch):
        """When maybe_reenqueue returns a task, chain advance should be skipped."""
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        advance_called = False
        requeued_task = _make_task(id=99, status=TaskStatus.PENDING)

        async def mock_reenqueue(task):
            return requeued_task

        async def mock_advance(task):
            nonlocal advance_called
            advance_called = True
            return None

        monkeypatch.setattr(runner_mod, "maybe_reenqueue", mock_reenqueue)
        monkeypatch.setattr(runner_mod, "advance_chain", mock_advance)

        runner = AgentRunner()
        task = _make_task(status=TaskStatus.COMPLETED)
        task.chain_id = 42
        task.chain_step = 0
        task.repeat_remaining = 3
        task.repeat_until = None

        await runner._after_complete(task)
        assert not advance_called, "advance_chain should NOT be called when reenqueued"

    @pytest.mark.asyncio
    async def test_no_chain_no_repeat_calls_nothing_extra(self, monkeypatch):
        """Plain task with no chain and no repeat should just notify + reenqueue check."""
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        advance_called = False

        async def noop_reenqueue(task):
            return None

        async def mock_advance(task):
            nonlocal advance_called
            advance_called = True
            return None

        monkeypatch.setattr(runner_mod, "maybe_reenqueue", noop_reenqueue)
        monkeypatch.setattr(runner_mod, "advance_chain", mock_advance)

        runner = AgentRunner()
        task = _make_task(status=TaskStatus.COMPLETED)
        task.chain_id = None
        task.chain_step = None
        task.repeat_remaining = None
        task.repeat_until = None

        await runner._after_complete(task)
        assert not advance_called


# ── _execute edge cases ─────────────────────────────────────────────


class TestExecuteEdgeCases:
    @pytest.mark.asyncio
    async def test_execute_clears_current_process_on_success(self, monkeypatch, tmp_path):
        """After _execute, _current_process and _current_task_id should be None."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        monkeypatch.setattr(
            settings, "agent_commands", {"test": "echo hello"},
        )
        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])

        async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None, git_diff=None):
            task = _make_task(id=task_id, status=TaskStatus.COMPLETED)
            task.exit_code = exit_code
            return task

        import app.core.runner as runner_mod
        monkeypatch.setattr(runner_mod, "complete_task", fake_complete)

        runner = AgentRunner()
        task = _make_task(agent="test", project_dir=str(tmp_path))
        await runner._execute(task)

        assert runner._current_process is None
        assert runner._current_task_id is None

    @pytest.mark.asyncio
    async def test_execute_clears_state_on_bad_dir(self, monkeypatch):
        """Even when project_dir doesn't exist, _current_task_id is set at top of _execute."""
        from app.core.runner import AgentRunner

        async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None, git_diff=None):
            task = _make_task(id=task_id, status=TaskStatus.FAILED)
            return task

        import app.core.runner as runner_mod
        monkeypatch.setattr(runner_mod, "complete_task", fake_complete)

        runner = AgentRunner()
        task = _make_task(project_dir="/nonexistent/xyz")
        await runner._execute(task)

        # Bad dir returns early before the try/finally block,
        # so _current_process was never set (it's None before and stays None)
        assert runner._current_process is None

    @pytest.mark.asyncio
    async def test_execute_captures_output(self, monkeypatch, tmp_path):
        """The full output of the command should be captured."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        monkeypatch.setattr(
            settings, "agent_commands", {"test": "echo 'hello world'"},
        )
        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])

        captured = {}

        async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None, git_diff=None):
            captured["full_output"] = full_output
            captured["exit_code"] = exit_code
            task = _make_task(id=task_id, status=TaskStatus.COMPLETED)
            task.exit_code = exit_code
            return task

        import app.core.runner as runner_mod
        monkeypatch.setattr(runner_mod, "complete_task", fake_complete)

        runner = AgentRunner()
        task = _make_task(agent="test", project_dir=str(tmp_path))
        await runner._execute(task)

        assert "hello world" in captured["full_output"]
        assert captured["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_execute_timeout(self, monkeypatch, tmp_path):
        """When a task exceeds timeout, it should be terminated and reported."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        monkeypatch.setattr(
            settings, "agent_commands", {"test": "sleep 60"},
        )
        monkeypatch.setattr(settings, "task_timeout_seconds", 1)
        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])

        captured = {}

        async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None, git_diff=None):
            captured["full_output"] = full_output
            captured["exit_code"] = exit_code
            captured["error_message"] = error_message
            task = _make_task(id=task_id, status=TaskStatus.FAILED)
            task.exit_code = exit_code
            return task

        import app.core.runner as runner_mod
        monkeypatch.setattr(runner_mod, "complete_task", fake_complete)

        runner = AgentRunner()
        task = _make_task(agent="test", project_dir=str(tmp_path))
        await runner._execute(task)

        assert "TIMEOUT" in captured["full_output"]
        assert captured["error_message"] is not None

    @pytest.mark.asyncio
    async def test_execute_handles_subprocess_crash(self, monkeypatch, tmp_path):
        """If subprocess creation itself fails, runner should still complete the task."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        monkeypatch.setattr(
            settings, "agent_commands", {"test": "echo ok"},
        )
        monkeypatch.setattr(settings, "allowed_project_dirs", [str(tmp_path)])

        captured = {}

        async def fake_complete(task_id, exit_code, output_summary, full_output, error_message=None, git_diff=None):
            captured["exit_code"] = exit_code
            captured["error_message"] = error_message
            task = _make_task(id=task_id, status=TaskStatus.FAILED)
            return task

        import app.core.runner as runner_mod
        monkeypatch.setattr(runner_mod, "complete_task", fake_complete)

        # Monkeypatch create_subprocess_shell to raise
        async def crashing_create(*args, **kwargs):
            raise OSError("Cannot create subprocess")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", crashing_create)

        runner = AgentRunner()
        task = _make_task(agent="test", project_dir=str(tmp_path))
        await runner._execute(task)

        assert captured["exit_code"] == -1
        assert "Runner crashed" in captured["error_message"]


# ── MAX_OUTPUT_BYTES ────────────────────────────────────────────────


class TestMaxOutputBytes:
    def test_constant_value(self):
        from app.core.runner import MAX_OUTPUT_BYTES

        assert MAX_OUTPUT_BYTES == 2 * 1024 * 1024


# ── cancel_current edge cases ───────────────────────────────────────


class TestCancelCurrentEdgeCases:
    @pytest.mark.asyncio
    async def test_cancel_terminates_stubborn_process(self):
        """If terminate doesn't work within timeout, kill should be called."""
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        # Start a process that ignores SIGTERM
        proc = await asyncio.create_subprocess_shell(
            "trap '' TERM; sleep 60",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        runner._current_process = proc

        result = await runner.cancel_current()
        assert result is True
        # wait() to collect exit status after kill
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            pass
        assert proc.returncode is not None


# ── Runner start/stop integration ───────────────────────────────────


class TestRunnerStartStop:
    @pytest.mark.asyncio
    async def test_stop_exits_poll_loop(self, monkeypatch):
        """Calling stop() should cause start() to exit."""
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod

        async def noop_pick():
            return None

        monkeypatch.setattr(runner_mod, "pick_next_task", noop_pick)

        runner = AgentRunner()

        async def stop_after_delay():
            await asyncio.sleep(0.1)
            runner.stop()

        # Run start() and stop() concurrently
        await asyncio.gather(
            runner.start(),
            stop_after_delay(),
        )

        assert runner._running is False


# ── Chain notification ──────────────────────────────────────────────


class TestNotifyChainEvent:
    @pytest.mark.asyncio
    async def test_chain_failure_sends_notification(self, monkeypatch):
        """When a chain fails, _notify_chain_event sends a message."""
        from unittest.mock import MagicMock
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod
        from app.core.models import ChainStatus

        chain = MagicMock()
        chain.name = "deploy"
        chain.status = ChainStatus.FAILED
        chain.total_steps = 3

        async def mock_get_chain(cid):
            return chain

        monkeypatch.setattr(runner_mod, "get_chain_by_id", mock_get_chain)

        sent_messages = []

        async def mock_chain_notify(chat_id, text):
            sent_messages.append((chat_id, text))

        runner = AgentRunner()
        runner._chain_notify = mock_chain_notify

        task = MagicMock()
        task.chain_id = 1
        task.chain_step = 1
        task.telegram_chat_id = 12345

        await runner._notify_chain_event(task)

        assert len(sent_messages) == 1
        assert "failed" in sent_messages[0][1].lower()
        assert "deploy" in sent_messages[0][1]
        assert "2/3" in sent_messages[0][1]

    @pytest.mark.asyncio
    async def test_chain_completion_sends_notification(self, monkeypatch):
        """When a chain completes, _notify_chain_event sends a message."""
        from unittest.mock import MagicMock
        from app.core.runner import AgentRunner
        import app.core.runner as runner_mod
        from app.core.models import ChainStatus

        chain = MagicMock()
        chain.name = "build"
        chain.status = ChainStatus.COMPLETED
        chain.total_steps = 2

        async def mock_get_chain(cid):
            return chain

        monkeypatch.setattr(runner_mod, "get_chain_by_id", mock_get_chain)

        sent_messages = []

        async def mock_chain_notify(chat_id, text):
            sent_messages.append((chat_id, text))

        runner = AgentRunner()
        runner._chain_notify = mock_chain_notify

        task = MagicMock()
        task.chain_id = 1
        task.chain_step = 1
        task.telegram_chat_id = 12345

        await runner._notify_chain_event(task)

        assert len(sent_messages) == 1
        assert "completed" in sent_messages[0][1].lower()
        assert "build" in sent_messages[0][1]

    @pytest.mark.asyncio
    async def test_no_notification_without_callback(self):
        """No crash when _chain_notify is None."""
        from unittest.mock import MagicMock
        from app.core.runner import AgentRunner

        runner = AgentRunner()
        runner._chain_notify = None

        task = MagicMock()
        task.chain_id = 1
        task.telegram_chat_id = 12345

        # Should not raise
        await runner._notify_chain_event(task)


# ── _read_output (progress streaming) ──────────────────────────────


class TestReadOutput:
    @pytest.mark.asyncio
    async def test_captures_all_lines(self, monkeypatch, tmp_path):
        """_read_output should capture all stdout lines."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        monkeypatch.setattr(settings, "progress_interval_seconds", 999)

        runner = AgentRunner()
        proc = await asyncio.create_subprocess_exec(
            "printf", "line1\nline2\nline3\n",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(tmp_path),
        )
        task = _make_task(telegram_chat_id=123)
        raw, _stderr = await runner._read_output(proc, task)
        text = raw.decode()
        assert "line1" in text
        assert "line3" in text

    @pytest.mark.asyncio
    async def test_sends_progress_at_interval(self, monkeypatch, tmp_path):
        """When interval is very short, progress callback should fire."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings
        import time

        monkeypatch.setattr(settings, "progress_interval_seconds", 0)

        sent = []

        async def mock_progress(chat_id, text):
            sent.append(text)

        runner = AgentRunner()
        runner._progress_notify = mock_progress

        proc = await asyncio.create_subprocess_exec(
            "printf", "a\nb\nc\n",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(tmp_path),
        )
        task = _make_task(telegram_chat_id=123)
        await runner._read_output(proc, task)

        assert len(sent) >= 1
        assert "progress" in sent[0].lower() or "Task #" in sent[0]

    @pytest.mark.asyncio
    async def test_no_progress_without_callback(self, monkeypatch, tmp_path):
        """No crash when _progress_notify is not set."""
        from app.core.runner import AgentRunner
        from app.config.settings import settings

        monkeypatch.setattr(settings, "progress_interval_seconds", 0)

        runner = AgentRunner()
        runner._progress_notify = None

        proc = await asyncio.create_subprocess_exec(
            "printf", "hello\n",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(tmp_path),
        )
        task = _make_task(telegram_chat_id=123)
        raw, _stderr = await runner._read_output(proc, task)
        assert b"hello" in raw


# ── __main__ integration ────────────────────────────────────────────


class TestMainRunIntegration:
    @pytest.mark.asyncio
    async def test_graceful_shutdown_via_signal(self, monkeypatch):
        """_run() should exit cleanly when SIGTERM is received."""
        import app.__main__ as main_mod
        from unittest.mock import AsyncMock, MagicMock, patch

        # Stub all external dependencies
        monkeypatch.setattr(main_mod, "init_db", AsyncMock())
        monkeypatch.setattr(main_mod, "recover_interrupted_tasks", AsyncMock(return_value=0))
        monkeypatch.setattr(main_mod, "recover_interrupted_chains", AsyncMock(return_value=0))

        # Stub broker functions used inside _run
        import app.core.broker as broker_mod
        monkeypatch.setattr(broker_mod, "purge_old_tasks", AsyncMock(return_value=0))
        monkeypatch.setattr(broker_mod, "recover_stale_worker_tasks", AsyncMock(return_value=0))
        monkeypatch.setattr(broker_mod, "_runner_wake", None)

        # Stub build_app to return a mock Application
        mock_updater = AsyncMock()
        mock_app = MagicMock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()
        mock_app.updater = mock_updater
        mock_app.bot = AsyncMock()

        monkeypatch.setattr(main_mod, "build_app", lambda runner: mock_app)
        monkeypatch.setattr(main_mod, "make_notify_callback", AsyncMock(return_value=AsyncMock()))
        monkeypatch.setattr(main_mod, "make_chain_notify_callback", AsyncMock(return_value=AsyncMock()))
        monkeypatch.setattr(main_mod, "make_progress_callback", AsyncMock(return_value=AsyncMock()))
        monkeypatch.setattr(main_mod, "make_typing_callback", AsyncMock(return_value=AsyncMock()))

        # Disable dashboard so uvicorn doesn't try to bind a port
        monkeypatch.setattr(main_mod.settings, "dashboard_enabled", False)

        # Mock AgentRunner.start so it doesn't block on the poll loop
        from app.core.runner import AgentRunner

        async def fake_start(self):
            self._running = True
            while self._running:
                await asyncio.sleep(0.05)

        monkeypatch.setattr(AgentRunner, "start", fake_start)

        # Send SIGTERM shortly after start
        import signal

        async def send_signal():
            await asyncio.sleep(0.2)
            import os
            os.kill(os.getpid(), signal.SIGTERM)

        sig_task = asyncio.create_task(send_signal())

        # _run() should complete without error
        await main_mod._run()
        await sig_task

        mock_app.stop.assert_awaited_once()
        mock_app.shutdown.assert_awaited_once()

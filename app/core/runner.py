"""Agent runner — executes tasks via subprocess with timeout and cancellation."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import time
from collections.abc import Callable, Coroutine
from typing import Any

MAX_OUTPUT_BYTES = 2 * 1024 * 1024  # 2 MB cap on stored output

from app.config.settings import settings
from app.core.broker import advance_chain, auto_retry_task, complete_task, fallback_retry_task, get_chain_by_id, is_rate_limited, maybe_reenqueue, pick_next_task
from app.core.models import ChainStatus, Task, TaskStatus
from app.core.webhooks import send_webhook_notifications

logger = logging.getLogger(__name__)

NotifyCallback = Callable[[Task], Coroutine[Any, Any, None]]


class AgentRunner:
    """Polls for pending tasks and executes them one at a time."""

    def __init__(self, on_complete: NotifyCallback | None = None) -> None:
        self._on_complete = on_complete
        self._chain_notify: Callable | None = None
        self._progress_notify: Callable | None = None
        self._typing_notify: Callable | None = None
        self._running = False
        self._current_process: asyncio.subprocess.Process | None = None
        self._current_task_id: int | None = None
        self._wake_event = asyncio.Event()
        self._backoff = 0

    def notify_new_task(self) -> None:
        """Signal the poll loop that a new task is available."""
        self._wake_event.set()

    @staticmethod
    def _safe_env() -> dict[str, str]:
        """Return a copy of env with sensitive variables stripped."""
        sensitive_prefixes = ("TELEGRAM_", "BOT_TOKEN", "SLACK_", "API_KEY", "SECRET")
        return {
            k: v
            for k, v in os.environ.items()
            if not any(k.upper().startswith(p) for p in sensitive_prefixes)
        }

    @staticmethod
    def _is_allowed_dir(real_path: str) -> bool:
        """Check if a resolved path is under an allowed project directory."""
        for allowed in settings.allowed_project_dirs_list:
            allowed_real = os.path.realpath(os.path.expanduser(allowed))
            if real_path == allowed_real or real_path.startswith(allowed_real + os.sep):
                return True
        return False

    def _kill_process(self, proc: asyncio.subprocess.Process) -> None:
        """Send SIGTERM to the entire process group if isolated, else just the process."""
        try:
            if proc.pid:
                pgid = os.getpgid(proc.pid)
                # Only kill the group if the subprocess is a group leader
                # (i.e. started with start_new_session=True)
                if pgid == proc.pid:
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    proc.terminate()
            else:
                proc.terminate()
        except (ProcessLookupError, PermissionError):
            proc.terminate()

    async def start(self) -> None:
        """Run the poll loop until stopped."""
        self._running = True
        logger.info("AgentRunner started")
        while self._running:
            try:
                task = await pick_next_task()
            except Exception:
                logger.exception("Error polling for tasks")
                self._backoff = min(max(self._backoff * 2, 2), 60)
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=self._backoff)
                except asyncio.TimeoutError:
                    pass
                self._wake_event.clear()
                continue

            if task is None:
                self._backoff = 0
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                continue  # pragma: no cover  -- CPython 3.9 bytecode optimization makes this untraceable

            self._backoff = 0
            self._wake_event.clear()
            safe_prompt = task.prompt.replace("\n", "\\n").replace("\r", "\\r")
            logger.info("Executing task #%d: %.80s", task.id, safe_prompt)
            # Send typing indicator when task starts
            if self._typing_notify:
                try:
                    await self._typing_notify(task.telegram_chat_id)
                except Exception:
                    pass
            await self._execute(task)

    async def _execute(self, task: Task) -> None:
        """Run the agent command as a subprocess."""
        self._current_task_id = task.id

        # Validate project_dir exists
        cwd = os.path.expanduser(task.project_dir)
        if not os.path.isdir(cwd):
            logger.error("Task #%d: project_dir does not exist: %s", task.id, cwd)
            updated = await complete_task(
                task_id=task.id,
                exit_code=-1,
                output_summary=f"Directory not found: {task.project_dir}",
                full_output="",
                error_message=f"project_dir does not exist: {task.project_dir}",
            )
            if updated:
                await self._after_complete(updated)
            return

        # Runtime validation: ensure project_dir is still in allowed paths
        real_cwd = os.path.realpath(cwd)
        if not self._is_allowed_dir(real_cwd):
            logger.error("Task #%d: project_dir not in allowed paths at runtime: %s", task.id, cwd)
            updated = await complete_task(
                task_id=task.id,
                exit_code=-1,
                output_summary="Project directory not in allowed paths",
                full_output="",
                error_message="project_dir validation failed at runtime",
            )
            if updated:
                await self._after_complete(updated)
            return

        try:
            argv = self._build_command(task)
            safe_prompt = task.prompt.replace("\n", "\\n").replace("\r", "\\r")
            logger.info("Task #%d command: %s (cwd=%s)", task.id, argv, cwd)

            # Pre-flight: verify the agent binary is on PATH
            import shutil
            if not shutil.which(argv[0], path=self._safe_env().get("PATH")):
                logger.error("Task #%d: agent binary not found: %s", task.id, argv[0])
                updated = await complete_task(
                    task_id=task.id,
                    exit_code=-1,
                    output_summary=f"Agent binary not found: {argv[0]}",
                    full_output="",
                    error_message=f"Agent binary '{argv[0]}' not found in PATH",
                )
                if updated:
                    await self._after_complete(updated)
                return

            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=self._safe_env(),
                start_new_session=True,
            )
            self._current_process = process

            try:
                effective_timeout = task.timeout_seconds or settings.task_timeout_seconds
                raw_output, raw_stderr = await asyncio.wait_for(
                    self._read_output(process, task),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("Task #%d timed out, terminating", task.id)
                partial = b""
                if process.stdout:
                    try:
                        partial = await asyncio.wait_for(
                            process.stdout.read(MAX_OUTPUT_BYTES), timeout=2,
                        )
                    except (asyncio.TimeoutError, Exception):
                        pass
                self._kill_process(process)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                raw_output = partial + b"\n\nTIMEOUT: task exceeded time limit"
                raw_stderr = b""

            # Combine stdout + stderr for the full picture
            combined = raw_output
            if raw_stderr:
                combined = raw_output + b"\n" + raw_stderr
            output = combined[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
            exit_code = process.returncode if process.returncode is not None else -1
            summary = self._summarize(output, exit_code)

            # Capture git diff summary for completed tasks
            git_diff = await self._git_diff_summary(cwd) if exit_code == 0 else ""

            updated = await complete_task(
                task_id=task.id,
                exit_code=exit_code,
                output_summary=summary,
                full_output=output,
                error_message=f"Exit code {exit_code}" if exit_code != 0 else None,
                git_diff=git_diff,
            )
            if updated:
                await self._after_complete(updated)

        except Exception:
            logger.exception("Unexpected error executing task #%d", task.id)
            updated = await complete_task(
                task_id=task.id,
                exit_code=-1,
                output_summary="Internal runner error",
                full_output="",
                error_message="Runner crashed — see server logs",
            )
            if updated:
                await self._after_complete(updated)
        finally:
            self._current_process = None
            self._current_task_id = None

    async def _read_output(
        self, process: asyncio.subprocess.Process, task: Task,
    ) -> tuple[bytes, bytes]:
        """Read stdout line-by-line (with progress updates) and drain stderr.

        Returns (stdout_bytes, stderr_bytes).
        """
        chunks: list[bytes] = []
        total = 0
        last_progress = time.monotonic()
        recent_lines: list[str] = []
        interval = settings.progress_interval_seconds

        async def _drain_stderr() -> bytes:
            """Read all stderr in the background."""
            if process.stderr is None:
                return b""
            data = await process.stderr.read(MAX_OUTPUT_BYTES)
            return data or b""

        stderr_task = asyncio.create_task(_drain_stderr())

        while True:
            line = await process.stdout.readline()
            if not line:
                break
            if total < MAX_OUTPUT_BYTES:
                chunks.append(line)
                total += len(line)

            decoded = line.decode("utf-8", errors="replace").rstrip()
            recent_lines.append(decoded)
            if len(recent_lines) > 5:
                recent_lines = recent_lines[-5:]

            now = time.monotonic()
            if self._progress_notify and (now - last_progress) >= interval:
                last_progress = now
                snippet = "\n".join(recent_lines)
                try:
                    await self._progress_notify(
                        task.telegram_chat_id,
                        f"⏳ Task #{task.id} progress:\n```\n{snippet}\n```",
                    )
                except Exception:
                    logger.debug("Progress notify failed for task #%d", task.id)
                if self._typing_notify:
                    try:
                        await self._typing_notify(task.telegram_chat_id)
                    except Exception:
                        pass

        await process.wait()
        stderr_data = await stderr_task
        return b"".join(chunks)[:MAX_OUTPUT_BYTES], stderr_data[:MAX_OUTPUT_BYTES]

    def _build_command(self, task: Task) -> list[str]:
        """Build the command argv list from agent_commands template.

        Uses sentinel tokens so that `shlex.split` treats each placeholder
        value as a single argument regardless of spaces or special chars.

        Flags (--model, --continue) are inserted immediately *before* the
        prompt argument, not after the executable name.  This is critical for
        CLI tools that have subcommands (e.g. ``opencode run {prompt}``):
        ``opencode run --continue "msg"`` works; ``opencode --continue run "msg"``
        does not.
        """
        template = settings.agent_commands.get(task.agent)
        if template is None:
            return ["echo", f"Unknown agent: {task.agent}"]

        # Use sentinel tokens that won't appear in real templates
        _PROMPT_SENTINEL = "\x00PROMPT\x00"
        _DIR_SENTINEL = "\x00DIR\x00"

        tokenized = template.replace("{prompt}", _PROMPT_SENTINEL).replace(
            "{project_dir}", _DIR_SENTINEL
        )
        argv = shlex.split(tokenized)
        # Replace sentinels with actual values (each stays as one arg element)
        argv = [
            arg.replace(_PROMPT_SENTINEL, task.prompt).replace(
                _DIR_SENTINEL, task.project_dir
            )
            for arg in argv
        ]

        # Find the prompt position: the index where the prompt placeholder
        # ended up (the first arg containing the task prompt).  Extra flags
        # are inserted *before* this index so they sit between the subcommand
        # and the prompt — where CLI flags belong.
        prompt_idx = len(argv)  # default: append at end
        for i, arg in enumerate(argv):
            if task.prompt and task.prompt in arg:
                prompt_idx = i
                break

        # Inject model flag if the task has a model set
        if task.model:
            model_flag_template = settings.agent_model_flags.get(task.agent)
            if model_flag_template:
                _MODEL_SENTINEL = "\x00MODEL\x00"
                flag_tokenized = model_flag_template.replace("{model}", _MODEL_SENTINEL)
                flag_parts = shlex.split(flag_tokenized)
                flag_parts = [
                    part.replace(_MODEL_SENTINEL, task.model)
                    for part in flag_parts
                ]
                argv[prompt_idx:prompt_idx] = flag_parts
                prompt_idx += len(flag_parts)

        # Inject continue flag for follow-up tasks
        if task.parent_task_id:
            continue_flag = settings.agent_continue_flags.get(task.agent)
            if continue_flag:
                continue_parts = shlex.split(continue_flag)
                argv[prompt_idx:prompt_idx] = continue_parts

        return argv

    def _summarize(self, output: str, exit_code: int) -> str:
        """Extract a short summary from command output."""
        max_chars = settings.output_summary_max_chars
        lines = output.strip().splitlines()

        if exit_code != 0:
            error_keywords = ("error", "traceback", "exception", "fatal", "failed")
            error_lines = [
                ln for ln in lines if any(kw in ln.lower() for kw in error_keywords)
            ]
            if error_lines:
                return "\n".join(error_lines[-5:])[:max_chars]

        tail = "\n".join(lines[-10:])
        return tail[:max_chars] if tail else "(no output)"

    @staticmethod
    async def _git_diff_summary(cwd: str) -> str:
        """Run `git diff --stat` in the task's working directory.

        Returns a short summary of changed files, or empty string on error.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "--stat", "HEAD~1",
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            diff_text = stdout.decode("utf-8", errors="replace").strip()
            # Cap at 500 chars to keep notifications readable
            return diff_text[:500] if diff_text else ""
        except Exception:
            return ""

    async def _after_complete(self, task: Task) -> None:
        """Handle post-completion: model fallback, auto-retry, notify, repeat, chain advance."""
        # Model fallback: if task failed with rate-limit indicators, try next model
        if task.status == TaskStatus.FAILED and task.full_output and is_rate_limited(task.full_output):
            fallback = await fallback_retry_task(task.id)
            if fallback:
                logger.info(
                    "Model fallback: task #%d (model=%s) rate-limited → #%d (model=%s, fallback %d)",
                    task.id, task.model or "default", fallback.id,
                    fallback.model, fallback.fallback_index,
                )
                if self._on_complete:
                    try:
                        await self._on_complete(task)
                    except Exception:
                        logger.exception("Notification callback failed for task #%d", task.id)
                return

        # Auto-retry transient failures (exit_code < 0: crash, signal, timeout)
        if task.status == TaskStatus.FAILED:
            retried = await auto_retry_task(task.id)
            if retried:
                logger.info("Auto-retry: task #%d → re-enqueued as #%d (attempt %d/%d)",
                            task.id, retried.id, retried.retry_count, retried.max_retries)
                # Still notify the user about the retry
                if self._on_complete:
                    try:
                        await self._on_complete(task)
                    except Exception:
                        logger.exception("Notification callback failed for task #%d", task.id)
                return

        if self._on_complete:
            try:
                await self._on_complete(task)
            except Exception:
                logger.exception("Notification callback failed for task #%d", task.id)

        # Multi-channel webhook notifications
        try:
            status_emoji = "✅" if task.status == TaskStatus.COMPLETED else "❌"
            wh_msg = (
                f"{status_emoji} Task #{task.id} {task.status.value}\n"
                f"Agent: {task.agent} | Project: {task.project_dir}\n"
                f"Summary: {(task.output_summary or '(none)')[:200]}"
            )
            await send_webhook_notifications(wh_msg)
        except Exception:
            logger.debug("Webhook notification failed for task #%d", task.id)

        # Repeat logic: re-enqueue if conditions met
        requeued = await maybe_reenqueue(task)
        if requeued:
            logger.info("Repeat: task #%d → re-enqueued as #%d", task.id, requeued.id)
            return  # don't advance chain while repeating

        # Chain logic: advance to next step
        if task.chain_id is not None:
            try:
                next_task = await advance_chain(task)
            except Exception:
                logger.exception("advance_chain failed for task #%d — marking chain as failed", task.id)
                next_task = None
            if next_task:
                logger.info(
                    "Chain: task #%d done → next step #%d (step %d)",
                    task.id, next_task.id, next_task.chain_step,
                )
            else:
                # Chain ended (completed or failed) — send chain-level notification
                await self._notify_chain_event(task)

    async def _notify_chain_event(self, task: Task) -> None:
        """Send a chain-level notification when a chain completes or fails."""
        if not self._chain_notify or task.chain_id is None:
            return
        try:
            chain = await get_chain_by_id(task.chain_id)
            if chain is None:
                return
            if chain.status == ChainStatus.FAILED:
                msg = f"⛓️ Chain '{chain.name}' failed at step {(task.chain_step or 0) + 1}/{chain.total_steps}"
            elif chain.status == ChainStatus.COMPLETED:
                msg = f"⛓️ Chain '{chain.name}' completed all {chain.total_steps} steps"
            else:
                return  # still running or idle — no notification needed
            # Send directly via the notification callback's bot
            if self._chain_notify:
                await self._chain_notify(task.telegram_chat_id, msg)
        except Exception:
            logger.exception("Chain notification failed for task #%d", task.id)

    async def cancel_current(self) -> bool:
        """Cancel the currently running subprocess. Returns True if cancelled."""
        if self._current_process is None:
            return False
        self._kill_process(self._current_process)
        try:
            await asyncio.wait_for(self._current_process.wait(), timeout=5)
        except asyncio.TimeoutError:
            self._current_process.kill()
        return True

    def stop(self) -> None:
        """Signal the poll loop to exit after the current task."""
        self._running = False
        self._wake_event.set()
        logger.info("AgentRunner stopping")

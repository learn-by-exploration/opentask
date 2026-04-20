#!/usr/bin/env python3
"""TaskPilot Remote Worker — polls the brain server for tasks, executes locally.

Usage:
    python worker.py --brain http://brain-host:8095 --id server2

Environment variables (override CLI flags):
    TASKPILOT_BRAIN_URL   — Brain server URL (e.g. http://192.168.1.10:8095)
    TASKPILOT_WORKER_ID   — Unique worker identifier
    TASKPILOT_TOKEN       — Bearer token for dashboard auth (optional)
    TASKPILOT_POLL_INTERVAL — Seconds between polls (default: 5)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] worker: %(message)s",
)
logger = logging.getLogger("worker")

MAX_OUTPUT_BYTES = 2 * 1024 * 1024  # 2 MB cap (matches server)

# Agent command templates — mirrors server defaults.  Override via env or
# place a worker.json config beside this script.
DEFAULT_AGENT_COMMANDS = {
    "opencode": "opencode run {prompt}",
    "claude": "claude -p {prompt} --allowedTools computer mcp",
}


def _load_config() -> dict[str, str]:
    """Load agent_commands from worker.json next to this script, if it exists."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.json")
    if os.path.isfile(config_path):
        with open(config_path) as f:
            data = json.load(f)
        return data.get("agent_commands", DEFAULT_AGENT_COMMANDS)
    return DEFAULT_AGENT_COMMANDS


def _api(brain_url: str, method: str, path: str, body: dict | None, token: str) -> tuple[int, dict | None]:
    """Make an HTTP request to the brain API. Returns (status_code, json_body)."""
    url = f"{brain_url.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        resp = urlopen(req, timeout=30)
        status = resp.status
        raw = resp.read()
        return status, json.loads(raw) if raw else None
    except HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except Exception:
            return e.code, None
    except URLError as e:
        logger.error("Connection error: %s", e.reason)
        return 0, None


def _build_command(task: dict, agent_commands: dict[str, str]) -> list[str]:
    """Build the command argv from agent_commands template, same logic as server runner."""
    agent = task["agent"]
    template = agent_commands.get(agent)
    if template is None:
        return ["echo", f"Unknown agent: {agent}"]

    _PROMPT_SENTINEL = "\x00PROMPT\x00"
    _DIR_SENTINEL = "\x00DIR\x00"

    tokenized = template.replace("{prompt}", _PROMPT_SENTINEL).replace(
        "{project_dir}", _DIR_SENTINEL
    )
    argv = shlex.split(tokenized)
    argv = [
        arg.replace(_PROMPT_SENTINEL, task["prompt"]).replace(
            _DIR_SENTINEL, task["project_dir"]
        )
        for arg in argv
    ]
    return argv


def _execute_task(task: dict, brain_url: str, token: str, agent_commands: dict[str, str]) -> None:
    """Execute a task locally and submit the result."""
    task_id = task["id"]
    worker_id = task["worker_id"]
    cwd = os.path.expanduser(task["project_dir"])

    if not os.path.isdir(cwd):
        logger.error("Task #%d: directory not found: %s", task_id, cwd)
        _api(brain_url, "POST", f"/api/worker/{task_id}/result", {
            "worker_id": worker_id,
            "exit_code": -1,
            "output_summary": f"Directory not found: {task['project_dir']}",
            "full_output": "",
            "error_message": f"project_dir does not exist on worker: {task['project_dir']}",
        }, token)
        return

    argv = _build_command(task, agent_commands)
    logger.info("Task #%d: running %s in %s", task_id, argv[0], cwd)

    # Start heartbeat thread
    stop_heartbeat = threading.Event()

    def heartbeat_loop() -> None:
        while not stop_heartbeat.wait(timeout=30):
            _api(brain_url, "POST", f"/api/worker/{task_id}/heartbeat", {
                "worker_id": worker_id,
            }, token)

    hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    hb_thread.start()

    try:
        # Strip sensitive env vars
        env = {
            k: v for k, v in os.environ.items()
            if not any(k.upper().startswith(p) for p in ("TELEGRAM_", "BOT_TOKEN", "SLACK_", "API_KEY", "SECRET"))
        }

        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(timeout=1800)

        output = (stdout or b"")[:MAX_OUTPUT_BYTES]
        if stderr:
            output = output + b"\n" + stderr[:MAX_OUTPUT_BYTES]
        output_str = output.decode("utf-8", errors="replace")
        exit_code = proc.returncode if proc.returncode is not None else -1

        # Simple summary
        lines = output_str.strip().splitlines()
        summary = "\n".join(lines[-10:])[:500]

    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        output_str = "TIMEOUT: task exceeded time limit"
        summary = output_str
        exit_code = -1
    except Exception as e:
        output_str = f"Worker execution error: {e}"
        summary = output_str
        exit_code = -1
    finally:
        stop_heartbeat.set()
        hb_thread.join(timeout=5)

    # Submit result
    status, resp = _api(brain_url, "POST", f"/api/worker/{task_id}/result", {
        "worker_id": worker_id,
        "exit_code": exit_code,
        "output_summary": summary,
        "full_output": output_str,
        "error_message": f"Exit code {exit_code}" if exit_code != 0 else None,
    }, token)

    if status == 200:
        logger.info("Task #%d: completed (exit=%d)", task_id, exit_code)
    else:
        logger.error("Task #%d: failed to submit result (HTTP %d)", task_id, status)


def main() -> None:
    parser = argparse.ArgumentParser(description="TaskPilot Remote Worker")
    parser.add_argument("--brain", default=os.environ.get("TASKPILOT_BRAIN_URL", "http://127.0.0.1:8095"),
                        help="Brain server URL")
    parser.add_argument("--id", default=os.environ.get("TASKPILOT_WORKER_ID", ""),
                        help="Unique worker identifier")
    parser.add_argument("--token", default=os.environ.get("TASKPILOT_TOKEN", ""),
                        help="Bearer token for dashboard auth")
    parser.add_argument("--poll-interval", type=int,
                        default=int(os.environ.get("TASKPILOT_POLL_INTERVAL", "5")),
                        help="Seconds between poll attempts")
    args = parser.parse_args()

    if not args.id:
        import socket
        args.id = socket.gethostname()
        logger.info("No --id given, using hostname: %s", args.id)

    agent_commands = _load_config()
    logger.info("Worker '%s' starting — brain=%s, poll=%ds", args.id, args.brain, args.poll_interval)
    logger.info("Agent commands: %s", list(agent_commands.keys()))

    running = True

    def _shutdown(sig: int, frame: object) -> None:
        nonlocal running
        logger.info("Received signal %d, shutting down...", sig)
        running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while running:
        status, task = _api(args.brain, "POST", "/api/worker/claim", {
            "worker_id": args.id,
        }, args.token)

        if status == 200 and task:
            _execute_task(task, args.brain, args.token, agent_commands)
            continue  # immediately check for more work

        if status == 0:
            logger.warning("Brain unreachable, retrying in %ds...", args.poll_interval * 2)
            time.sleep(args.poll_interval * 2)
            continue

        # 204 = no tasks, or other status = wait
        time.sleep(args.poll_interval)

    logger.info("Worker shut down cleanly.")


if __name__ == "__main__":
    main()

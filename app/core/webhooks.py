"""Multi-channel webhook notifications for task events."""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from typing import Any

from app.config.settings import settings

logger = logging.getLogger(__name__)


def _format_discord(task_msg: str) -> dict[str, Any]:
    """Format a task notification for Discord webhooks."""
    return {"content": task_msg}


def _format_slack(task_msg: str) -> dict[str, Any]:
    """Format a task notification for Slack webhooks."""
    return {"text": task_msg}


def _format_generic(task_msg: str) -> dict[str, Any]:
    """Format a task notification for generic webhooks."""
    return {"message": task_msg}


_FORMATTERS = {
    "discord": _format_discord,
    "slack": _format_slack,
    "generic": _format_generic,
}


async def send_webhook_notifications(task_msg: str) -> list[dict[str, Any]]:
    """Send a task notification to all configured webhooks.

    Uses urllib (no external deps) and runs in a thread to avoid blocking.
    Returns a list of result dicts with webhook name and success status.
    """
    import asyncio

    configs = settings.webhook_configs
    if not configs:
        return []

    results = []
    for config in configs:
        url = config["url"]
        wh_type = config.get("type", "generic")
        name = config.get("name", url[:40])

        formatter = _FORMATTERS.get(wh_type, _format_generic)
        payload = formatter(task_msg)

        result = await asyncio.get_event_loop().run_in_executor(
            None, _post_webhook, url, payload, name,
        )
        results.append(result)

    return results


def _post_webhook(url: str, payload: dict, name: str) -> dict[str, Any]:
    """Synchronous webhook POST (runs in executor)."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"name": name, "ok": True, "status": resp.status}
    except urllib.error.HTTPError as e:
        logger.warning("Webhook '%s' HTTP error: %s", name, e.code)
        return {"name": name, "ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        logger.warning("Webhook '%s' failed: %s", name, e)
        return {"name": name, "ok": False, "error": str(e)}

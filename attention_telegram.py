"""Internal Telegram delivery for Attention items.

This is deliberately not a model tool. It reuses the configured Hermes bot
and home channel while exposing only the fixed Virgil deep-link button.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from hermes_attention import (
    AttentionError,
    record_notification,
    validate_public_url,
)


_ITEM_PATH_RE = re.compile(r"^/item/[0-9a-f]{32}$")


def send_attention_notification(
    message: str,
    button_url: str,
    *,
    message_id: str | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(message, str)
        or not 1 <= len(message) <= 600
        or any(ord(char) < 32 and char not in "\n\t" for char in message)
        or "<" in message
        or ">" in message
    ):
        raise AttentionError("invalid_notification_message")
    parsed = urlparse(button_url)
    validate_public_url(f"{parsed.scheme}://{parsed.netloc}")
    if not _ITEM_PATH_RE.fullmatch(parsed.path) or parsed.query or parsed.fragment:
        raise AttentionError("invalid_attention_deep_link")
    if message_id is not None and not str(message_id).isdigit():
        raise AttentionError("invalid_telegram_message_id")

    from gateway.config import Platform, load_gateway_config
    from model_tools import _run_async
    from tools.send_message_tool import _send_telegram

    config = load_gateway_config()
    pconfig = config.platforms.get(Platform.TELEGRAM)
    home = config.get_home_channel(Platform.TELEGRAM)
    if not pconfig or not pconfig.enabled or not pconfig.token or not home:
        raise AttentionError("telegram_not_configured", 503)
    return _run_async(
        _send_telegram(
            pconfig.token,
            home.chat_id,
            message,
            thread_id=home.thread_id,
            disable_link_previews=True,
            button_url=button_url,
            edit_message_id=message_id,
        )
    )


def deliver_attention_result(result: dict[str, Any]) -> str:
    """Execute one deterministic queue notification plan."""

    plan = result["notification"]
    if plan["action"] == "none":
        return "queued"
    item = result["item"]
    if plan["action"] == "blocked":
        record_notification(item["item_id"], success=False)
        return "failed"
    project = str(item["project"]).replace("_", " ").title()
    heading = {
        "needs_cal": f"Needs You — {project}",
        "prepared": f"Prepared — {project}",
        "safety_hold": f"Safety Hold — {project}",
    }.get(item["status"], f"Virgil — {project}")
    message = f"{heading}\n{item['safe_summary']}\n{item['recommended_action']}"
    try:
        delivered = send_attention_notification(
            message,
            plan["deep_link"],
            message_id=plan.get("message_id"),
        )
        success = bool(delivered.get("success"))
        record_notification(
            item["item_id"],
            success=success,
            message_id=str(delivered.get("message_id") or "") or None,
        )
        return "delivered" if success else "failed"
    except Exception:
        try:
            record_notification(item["item_id"], success=False)
        except Exception:
            pass
        return "failed"

"""Internal Telegram delivery for Attention items.

This is deliberately not a model tool. It reuses the configured Hermes bot
and home channel while exposing only the fixed Virgil deep-link button.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from hermes_attention import AttentionError, validate_public_url


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

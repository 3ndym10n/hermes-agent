"""Cogitator X-link batch-intake bridge helper (``/x_batch``).

Builds a draft-only ``x_link_batch_intake`` bridge request from a list of X/
Twitter URLs (one per line, optional caller-supplied post text), POSTs it to
Cogitator's HTTP bridge (``{base_url}/api/cogitator_bridge``) with a bearer
token sourced **only** from the environment, validates the response fail-closed,
and renders a compact one-screen summary for chat.

Capture happens on Cogitator's side, gated there by the default-off
``ENABLE_X_BATCH_INTAKE`` flag; this helper only forwards the bounded URL list
and an optional ``dry_run`` preview flag. Deliberately out of scope:
  * no approval / promotion — Cogitator reports ``promotion_performed: false``
    and this helper *verifies* it before anything reaches chat
  * no provider/model call here; no arbitrary URL crawling
  * no secret/token printed or logged; ``.env`` is never touched
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Optional

# No logging in this module: the bearer token must never reach a log sink.

BRIDGE_PATH = "/api/cogitator_bridge"
TOKEN_ENV = "COGITATOR_BRIDGE_TOKEN"

_REQUESTED_ACTION = "x_link_batch_intake"
_USER_INTENT = "Capture a batch of X/Twitter links through verified intake (Virgil /x_batch)."
_REQUEST_TIMEOUT_SECONDS = 45  # bounded provider lookups for up to 25 posts
MAX_BATCH_LINKS = 25

# Response-level keys that would indicate promotion/approval execution. Any
# present (or promotion_performed not exactly False) → reject, fail closed.
_FORBIDDEN_RESPONSE_FIELDS: tuple[str, ...] = (
    "promoted",
    "approved",
    "approval_executed",
    "executed",
)


class XBatchBridgeError(Exception):
    """Helper error carrying a stable, sanitized reason code (safe to surface).

    ``detail`` is for logs only and must never contain the token or secrets.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def parse_x_batch_command(args: str) -> tuple[str, bool]:
    """Split the raw ``/x_batch`` argument text into (urls, dry_run).

    A leading ``dry_run`` (or ``dry-run``) token on the first line turns on
    preview mode; everything else is the newline-separated URL list. Keeps the
    "one URL per line" body intact.
    """
    text = str(args or "")
    first, sep, rest = text.partition("\n")
    head = first.strip().lower()
    if head in {"dry_run", "dry-run", "dryrun"}:
        return rest.strip(), True
    # also allow "dry_run" as the very first whitespace token on a single line
    parts = first.strip().split(None, 1)
    if parts and parts[0].lower() in {"dry_run", "dry-run", "dryrun"}:
        remainder = (parts[1] if len(parts) > 1 else "")
        return ((remainder + sep + rest).strip()), True
    return text.strip(), False


def count_candidate_links(urls: str) -> int:
    """Count non-empty lines — the number of links the user pasted."""
    return sum(1 for line in str(urls or "").splitlines() if line.strip())


def build_x_batch_request(*, urls: str, dry_run: bool = False) -> dict[str, Any]:
    """Build the draft-only ``x_link_batch_intake`` bridge packet."""
    return {
        "source_agent": "hermes",
        "requested_action": _REQUESTED_ACTION,
        "user_intent": _USER_INTENT,
        "content": "",
        "approval_status": "draft_only",
        "risk_level": "low",
        "context": {"urls": str(urls or ""), "dry_run": bool(dry_run)},
    }


def _post_bridge(
    packet: Mapping[str, Any],
    *,
    base_url: str,
    token: str,
    urlopen: Optional[Callable[..., Any]] = None,
) -> Any:
    """POST the packet with a bearer token. Fail-closed; token never logged."""
    url = base_url.rstrip("/") + BRIDGE_PATH
    body = json.dumps(packet).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    request.add_header("Authorization", f"Bearer {token}")

    opener = urlopen or urllib.request.urlopen
    try:
        with opener(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise XBatchBridgeError("BRIDGE_HTTP_ERROR", f"status={exc.code}")
    except urllib.error.URLError as exc:
        raise XBatchBridgeError("BRIDGE_UNREACHABLE", type(exc).__name__)
    except Exception as exc:  # defensive: any transport failure fails closed
        raise XBatchBridgeError("BRIDGE_UNREACHABLE", type(exc).__name__)

    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return json.loads(text)
    except Exception as exc:
        raise XBatchBridgeError("BRIDGE_RESPONSE_INVALID", type(exc).__name__)


def validate_x_batch_response(response: Any) -> dict[str, Any]:
    """Validate an ``x_link_batch_intake`` response. Fails closed.

    Enforces: ``status`` ok|disabled, matching action, ``promotion_performed``
    exactly False, and no approval/promotion-execution fields.
    """
    if not isinstance(response, Mapping):
        raise XBatchBridgeError("BRIDGE_RESPONSE_INVALID", "response is not an object")
    status = response.get("status")
    if status not in {"ok", "disabled"}:
        raise XBatchBridgeError("BRIDGE_STATUS_NOT_OK", f"status={status!r}")
    if response.get("requested_action") != _REQUESTED_ACTION:
        raise XBatchBridgeError("BRIDGE_ACTION_MISMATCH", f"action={response.get('requested_action')!r}")
    if response.get("promotion_performed") not in (False, None):
        raise XBatchBridgeError("BRIDGE_PROMOTION_REPORTED", f"promotion_performed={response.get('promotion_performed')!r}")
    stateful = [f for f in _FORBIDDEN_RESPONSE_FIELDS if f in response]
    if stateful:
        raise XBatchBridgeError("BRIDGE_STATEFUL_RESPONSE", f"fields={stateful}")
    return dict(response)


def request_x_batch_intake(
    *,
    base_url: str,
    token: str,
    urls: str,
    dry_run: bool = False,
    urlopen: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Build the request, POST it, and validate the reply. Fails closed."""
    if not str(base_url or "").strip():
        raise XBatchBridgeError("BRIDGE_NOT_CONFIGURED", "base_url missing")
    if not str(token or "").strip():
        raise XBatchBridgeError("BRIDGE_TOKEN_MISSING", "token missing")
    if not str(urls or "").strip():
        raise XBatchBridgeError("NO_LINKS", "no urls supplied")
    if count_candidate_links(urls) > MAX_BATCH_LINKS:
        raise XBatchBridgeError("TOO_MANY_LINKS", f"max={MAX_BATCH_LINKS}")
    packet = build_x_batch_request(urls=urls, dry_run=dry_run)
    response = _post_bridge(packet, base_url=base_url.strip(), token=token.strip(), urlopen=urlopen)
    return validate_x_batch_response(response)


def x_batch_help_text() -> str:
    """Compact help shown when ``/x_batch`` is called with no links."""
    return (
        "📥 /x_batch — capture a batch of X/Twitter links.\n"
        "Paste the command and up to 25 post URLs, one per line:\n"
        "\n"
        "/x_batch\n"
        "https://x.com/example/status/111\n"
        "https://twitter.com/example/status/222\n"
        "\n"
        "Optional: add an exact post body with `||| post text: ...` after a URL, "
        "or start with `/x_batch dry_run` to preview without capturing."
    )


def render_x_batch_message(response: Mapping[str, Any]) -> str:
    """Render a validated batch response into a compact chat summary.

    Never dumps raw JSON or per-item detail walls into chat — just the counts
    plus a short per-status sample and a pointer to the decision batch.
    """
    if response.get("status") == "disabled":
        return (
            "X batch intake is disabled on Cogitator.\n"
            "Enable it by setting ENABLE_X_BATCH_INTAKE=true and redeploying Cogitator."
        )
    if response.get("refused"):
        return f"X batch refused: {response.get('refuse_reason') or 'batch limit exceeded'}."

    dry = bool(response.get("dry_run"))
    header = "X batch preview (dry run — nothing captured):" if dry else "X batch complete:"
    lines = [
        header,
        f"- verified and captured: {response.get('verified_captured_count', 0)}",
        f"- caller-text captured: {response.get('manual_text_count', 0)}",
        f"- bookmark-export captured: {response.get('bookmark_export_count', 0)}",
        f"- duplicates skipped: {response.get('duplicate_in_batch_count', 0) + response.get('already_captured_count', 0)}",
        f"- source needed: {response.get('source_needed_count', 0)}",
        f"- failed: {response.get('failed_count', 0)}",
        f"- research candidates: {response.get('research_candidate_count', 0)}",
    ]
    if response.get("research_candidate_count") or response.get("source_needed_count"):
        lines.append("")
        lines.append("Research/review items are in the decision batch — see /decision_batch.")
    return "\n".join(lines)


__all__ = [
    "XBatchBridgeError",
    "TOKEN_ENV",
    "BRIDGE_PATH",
    "MAX_BATCH_LINKS",
    "parse_x_batch_command",
    "count_candidate_links",
    "build_x_batch_request",
    "request_x_batch_intake",
    "validate_x_batch_response",
    "render_x_batch_message",
    "x_batch_help_text",
]

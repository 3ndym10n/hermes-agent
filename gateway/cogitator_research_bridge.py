"""Cogitator Decision Inbox research-action bridge helper.

The *action* half of Cogitator's decision cockpit. Inside an open Decision Inbox
(after ``/decision_batch``), Cal replies ``research <n>``; this helper builds a
draft-only ``research_decision_item`` bridge request from the item's display
number and the snapshot it was shown under, POSTs it to Cogitator's HTTP bridge
(``{base_url}/api/cogitator_bridge``) with a bearer token sourced **only** from
the environment, validates the response fail-closed, and renders a compact result.

The bounded research runs on Cogitator's side, gated there by the default-off
``ENABLE_RESEARCH_ACTION`` flag. Deliberately out of scope here:
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

_REQUESTED_ACTION = "research_decision_item"
_USER_INTENT = "Research a Decision Inbox item by number (Virgil cockpit reply)."
_REQUEST_TIMEOUT_SECONDS = 45  # bounded research run (≤3 sources)

# Response-level keys that would indicate promotion/approval execution. Any
# present (or promotion_performed not exactly False) → reject, fail closed.
_FORBIDDEN_RESPONSE_FIELDS: tuple[str, ...] = (
    "promoted",
    "approved",
    "approval_executed",
    "executed",
)


class ResearchBridgeError(Exception):
    """Helper error carrying a stable, sanitized reason code (safe to surface).

    ``detail`` is for logs only and must never contain the token or secrets.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def build_research_request(*, item_id: str, expected_snapshot_id: str = "") -> dict[str, Any]:
    """Build the draft-only ``research_decision_item`` bridge packet.

    ``item_id`` is the Decision Inbox display number (or a stable candidate id);
    ``expected_snapshot_id`` pins the batch it was shown under so a stale number
    is rejected on Cogitator's side. ``confirm`` is always True — Cal replying
    ``research <n>`` inside the cockpit is the explicit confirmation."""
    context: dict[str, Any] = {"item_id": str(item_id), "confirm": True}
    if str(expected_snapshot_id or "").strip():
        context["expected_snapshot_id"] = str(expected_snapshot_id).strip()
    return {
        "source_agent": "hermes",
        "requested_action": _REQUESTED_ACTION,
        "user_intent": _USER_INTENT,
        "content": "",
        "approval_status": "draft_only",
        "risk_level": "low",
        "context": context,
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
        raise ResearchBridgeError("BRIDGE_HTTP_ERROR", f"status={exc.code}")
    except urllib.error.URLError as exc:
        raise ResearchBridgeError("BRIDGE_UNREACHABLE", type(exc).__name__)
    except Exception as exc:  # defensive: any transport failure fails closed
        raise ResearchBridgeError("BRIDGE_UNREACHABLE", type(exc).__name__)

    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return json.loads(text)
    except Exception as exc:
        raise ResearchBridgeError("BRIDGE_RESPONSE_INVALID", type(exc).__name__)


def validate_research_response(response: Any) -> dict[str, Any]:
    """Validate a ``research_decision_item`` response. Fails closed.

    Accepts ``status`` ok|rejected|disabled (rejected/disabled are valid,
    user-facing outcomes — a stale number or a disabled flag, not a transport
    error). Enforces matching action, ``promotion_performed`` exactly False, and
    no approval/promotion-execution fields.
    """
    if not isinstance(response, Mapping):
        raise ResearchBridgeError("BRIDGE_RESPONSE_INVALID", "response is not an object")
    status = response.get("status")
    if status not in {"ok", "rejected", "disabled"}:
        raise ResearchBridgeError("BRIDGE_STATUS_NOT_OK", f"status={status!r}")
    if response.get("requested_action") != _REQUESTED_ACTION:
        raise ResearchBridgeError("BRIDGE_ACTION_MISMATCH", f"action={response.get('requested_action')!r}")
    if response.get("promotion_performed") not in (False, None):
        raise ResearchBridgeError("BRIDGE_PROMOTION_REPORTED", f"promotion_performed={response.get('promotion_performed')!r}")
    stateful = [f for f in _FORBIDDEN_RESPONSE_FIELDS if f in response]
    if stateful:
        raise ResearchBridgeError("BRIDGE_STATEFUL_RESPONSE", f"fields={stateful}")
    return dict(response)


def request_research_decision_item(
    *,
    base_url: str,
    token: str,
    item_id: str,
    expected_snapshot_id: str = "",
    urlopen: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Build the request, POST it, and validate the reply. Fails closed."""
    if not str(base_url or "").strip():
        raise ResearchBridgeError("BRIDGE_NOT_CONFIGURED", "base_url missing")
    if not str(token or "").strip():
        raise ResearchBridgeError("BRIDGE_TOKEN_MISSING", "token missing")
    if not str(item_id or "").strip():
        raise ResearchBridgeError("NO_ITEM", "no item_id supplied")
    packet = build_research_request(item_id=item_id, expected_snapshot_id=expected_snapshot_id)
    response = _post_bridge(packet, base_url=base_url.strip(), token=token.strip(), urlopen=urlopen)
    return validate_research_response(response)


def _bullets(label: str, values) -> list[str]:
    items = [str(v).strip() for v in (values or []) if str(v).strip()]
    if not items:
        return [f"- {label}: (none)"]
    if len(items) == 1:
        return [f"- {label}: {items[0]}"]
    return [f"- {label}:"] + [f"  - {v}" for v in items]


def render_research_message(response: Mapping[str, Any]) -> str:
    """Render a validated research response into a compact cockpit reply.

    Mirrors the target UX: "Research started for: <title>" then a compact result
    block (recommendation, evidence quality, for/against, contradictions, missing
    evidence, risk, sources). Rejections (stale number, ineligible) and clean
    failures (provider unavailable) get a short, clear line. Never dumps raw JSON.
    """
    status = response.get("status")
    if status == "disabled":
        return (
            "Research is disabled on Cogitator.\n"
            "Enable it by setting ENABLE_RESEARCH_ACTION=true and redeploying Cogitator."
        )
    if status == "rejected":
        code = str(response.get("reason_code") or "")
        msg = str(response.get("message") or "That item can't be researched right now.")
        if code in ("stale_snapshot", "item_not_found", "snapshot_required"):
            return f"{msg}\nReply refresh (or run /decision_batch) and try again."
        return msg

    title = str(response.get("title") or "the selected item").strip()
    header = f"Research started for:\n{title}"

    if response.get("research_status") == "failed":
        reason = str(response.get("failure_reason") or "research could not complete")
        return (
            f"{header}\n\n"
            f"Research could not complete: {reason}.\n"
            "The item stays in Needs research."
        )

    lines = [
        header,
        "",
        "Research result:",
        f"- recommendation: {response.get('recommendation') or response.get('current_disposition') or 'unknown'}",
        f"- evidence quality: {response.get('confidence') or response.get('evidence_quality') or 'unknown'}",
    ]
    lines += _bullets("evidence for", response.get("evidence_for"))
    lines += _bullets("evidence against", response.get("evidence_against"))
    lines += _bullets("contradictions", response.get("contradictions"))
    lines += _bullets("missing evidence", response.get("missing_evidence"))
    lines.append(f"- risk if wrong: {response.get('risk_if_wrong') or '(unspecified)'}")
    lines += _bullets("sources checked", response.get("sources_checked"))
    lines += ["", "Reply refresh to see the updated inbox. No promotion happens automatically."]
    return "\n".join(lines)


__all__ = [
    "ResearchBridgeError",
    "TOKEN_ENV",
    "BRIDGE_PATH",
    "build_research_request",
    "request_research_decision_item",
    "validate_research_response",
    "render_research_message",
]

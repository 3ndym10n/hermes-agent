"""Cogitator Decision Inbox promotion-approval bridge helper (Scope D).

The approval half of the decision cockpit. Inside an open Decision Inbox (after
``/decision_batch``), Cal replies ``approve-candidate <n> preview`` or
``approve-candidate <n> confirm``; this helper builds a draft-only
``approve_promotion_candidate`` bridge request from the item's display number and
the snapshot it was shown under, POSTs it to Cogitator's HTTP bridge with a bearer
token sourced **only** from the environment, validates the response fail-closed,
and renders a compact result.

All validation, idempotency, and the *only* possible ``storage/promoted/`` write
live on Cogitator's side (Scope C, gated by the default-off ``ENABLE_PROMOTION_APPROVAL``
flag). This helper never writes storage, never approves on its own, and never
duplicates approval logic — it only maps a cockpit reply to the bridge action and
renders the reply. Unlike the research helper, a confirmed approval legitimately
reports ``mutation_performed: true`` (Cogitator wrote the approved record), so
this helper does NOT reject a mutation on the ``approved`` status — but it DOES
require ``preview``/``blocked``/``already_approved`` to report no write.

No secret/token is printed or logged; ``.env`` is never touched.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Optional

# No logging in this module: the bearer token must never reach a log sink.

BRIDGE_PATH = "/api/cogitator_bridge"
TOKEN_ENV = "COGITATOR_BRIDGE_TOKEN"

_REQUESTED_ACTION = "approve_promotion_candidate"
_USER_INTENT = "Approve or preview a Cogitator promotion candidate from the Decision Inbox."
_REQUEST_TIMEOUT_SECONDS = 45

_ACCEPTED_STATUSES = frozenset({"preview", "blocked", "approved", "already_approved"})
# Statuses that must NOT report any write. ``approved`` is intentionally excluded:
# Scope C writes the approved retrieval record on a confirmed approval.
_NO_WRITE_STATUSES = frozenset({"preview", "blocked", "already_approved"})


class ApprovalBridgeError(Exception):
    """Helper error carrying a stable, sanitized reason code (safe to surface).

    ``detail`` is for logs only and must never contain the token or secrets."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def build_approval_request(
    *, item_id: str, mode: str, confirm: bool, expected_snapshot_id: str = ""
) -> dict[str, Any]:
    """Build the draft-only ``approve_promotion_candidate`` bridge packet.

    ``mode`` is ``preview`` or ``confirm``; ``confirm`` must be True only when
    ``mode == "confirm"`` (the caller is responsible for that mapping, and we
    re-assert it here defensively). ``expected_snapshot_id`` pins the batch the
    number was shown under so a stale number is rejected on Cogitator's side."""
    mode = "confirm" if str(mode).strip().lower() == "confirm" else "preview"
    confirm = bool(confirm) and mode == "confirm"  # never an implicit confirm
    context: dict[str, Any] = {"item_id": str(item_id), "mode": mode, "confirm": confirm}
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
    packet: Mapping[str, Any], *, base_url: str, token: str,
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
        raise ApprovalBridgeError("BRIDGE_HTTP_ERROR", f"status={exc.code}")
    except urllib.error.URLError as exc:
        raise ApprovalBridgeError("BRIDGE_UNREACHABLE", type(exc).__name__)
    except Exception as exc:  # defensive: any transport failure fails closed
        raise ApprovalBridgeError("BRIDGE_UNREACHABLE", type(exc).__name__)

    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return json.loads(text)
    except Exception as exc:
        raise ApprovalBridgeError("BRIDGE_RESPONSE_INVALID", type(exc).__name__)


def validate_approval_response(response: Any) -> dict[str, Any]:
    """Validate an ``approve_promotion_candidate`` response. Fails closed.

    Accepts ``status`` in {preview, blocked, approved, already_approved}. Enforces
    the matching action. For the no-write statuses (preview/blocked/already_approved)
    it requires ``mutation_performed`` to be falsey — a write reported there is a
    contract violation. ``approved`` is allowed to report a mutation (Scope C wrote
    the record)."""
    if not isinstance(response, Mapping):
        raise ApprovalBridgeError("BRIDGE_RESPONSE_INVALID", "response is not an object")
    status = response.get("status")
    if status not in _ACCEPTED_STATUSES:
        raise ApprovalBridgeError("BRIDGE_STATUS_NOT_OK", f"status={status!r}")
    if response.get("requested_action") != _REQUESTED_ACTION:
        raise ApprovalBridgeError("BRIDGE_ACTION_MISMATCH", f"action={response.get('requested_action')!r}")
    if status in _NO_WRITE_STATUSES and response.get("mutation_performed"):
        raise ApprovalBridgeError("BRIDGE_UNEXPECTED_WRITE", f"status={status} reported a write")
    return dict(response)


def request_promotion_approval(
    *, base_url: str, token: str, item_id: str, mode: str, confirm: bool,
    expected_snapshot_id: str = "", urlopen: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Build the request, POST it, and validate the reply. Fails closed."""
    if not str(base_url or "").strip():
        raise ApprovalBridgeError("BRIDGE_NOT_CONFIGURED", "base_url missing")
    if not str(token or "").strip():
        raise ApprovalBridgeError("BRIDGE_TOKEN_MISSING", "token missing")
    if not str(item_id or "").strip():
        raise ApprovalBridgeError("NO_ITEM", "no item_id supplied")
    packet = build_approval_request(
        item_id=item_id, mode=mode, confirm=confirm, expected_snapshot_id=expected_snapshot_id)
    response = _post_bridge(packet, base_url=base_url.strip(), token=token.strip(), urlopen=urlopen)
    return validate_approval_response(response)


def _bullets(label: str, values) -> list[str]:
    items = [str(v).strip() for v in (values or []) if str(v).strip()]
    if not items:
        return [f"- {label}: (none)"]
    if len(items) == 1:
        return [f"- {label}: {items[0]}"]
    return [f"- {label}:"] + [f"  - {v}" for v in items]


def render_approval_message(response: Mapping[str, Any], *, number: int | None = None) -> str:
    """Render a validated approval response into a compact cockpit reply. Never
    dumps raw JSON; never prints secrets."""
    status = response.get("status")
    n = f"#{number}" if number is not None else "the selected item"

    if status == "blocked":
        msg = str(response.get("message") or response.get("reason") or "Approval blocked.")
        return f"Blocked: {msg}\nNo record written."

    if status == "already_approved":
        path = str(response.get("approved_retrieval_record_path") or "(unknown path)")
        return f"Already approved:\n{path}\n\nNo duplicate written."

    if status == "approved":
        path = str(response.get("approved_retrieval_record_path") or "(unknown path)")
        return (
            f"Approved. Wrote:\n{path}\n\n"
            "Cal-approved. Nothing else mutated.\n"
            "Reply refresh to see the updated inbox."
        )

    # preview
    rec = response.get("would_be_record") or {}
    target = str(response.get("target_path") or "(unknown path)")
    title = str(rec.get("title") or "the selected item").strip()
    lines = [
        f"Approval preview for {n} — {title}",
        "",
        "Would write:",
        f"- record_type: {rec.get('record_type') or 'virgil_retrieval_record'}",
        f"- target: {target}",
    ]
    lines += _bullets("trigger phrases", rec.get("trigger_phrases"))
    lines += _bullets("distinctive tokens", rec.get("distinctive_tokens"))
    lines += [
        f"- why relevant: {rec.get('why_relevant') or '(none)'}",
        f"- plan delta: {rec.get('plan_delta') or '(none)'}",
        f"- verification obligation: {rec.get('verification_obligation') or '(none)'}",
        "",
        "No write performed.",
    ]
    if not response.get("approval_enabled"):
        lines += [
            "",
            "Approval execution is disabled. Preview only. Confirm will not write "
            "until ENABLE_PROMOTION_APPROVAL=true.",
        ]
    num = number if number is not None else "<n>"
    lines += ["", f"Reply:\napprove-candidate {num} confirm"]
    return "\n".join(lines)


__all__ = [
    "ApprovalBridgeError",
    "TOKEN_ENV",
    "BRIDGE_PATH",
    "build_approval_request",
    "request_promotion_approval",
    "validate_approval_response",
    "render_approval_message",
]

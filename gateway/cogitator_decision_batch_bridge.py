"""Manual, read-only Cogitator decision-batch bridge helper.

The display half of Cogitator's "approval cockpit". It builds a draft-only
``render_decision_batch`` bridge request, POSTs it to Cogitator's read-only HTTP
bridge endpoint (``{base_url}/api/cogitator_bridge``) with a bearer token sourced
**only** from the environment, validates the response fail-closed, and hands the
rendered batch back for display in the current chat.

Read-only by construction. Deliberately out of scope:
  * no ``approve #id`` / approval execution — display only
  * no promotion, no storage write, no DB/schema change
  * no provider/model call
  * no secret/token printed or logged; ``.env`` is never touched

The Cogitator action is itself read-only (``mutated: false``,
``execution_authorized: false``). This helper additionally *verifies* that
contract on the response before anything reaches the chat.

Real-feed hook (not wired yet): Hermes currently sends no ``items``, so Cogitator
returns a representative **sample** batch. A real batch will appear once Hermes
gathers candidate/triage/research evidence and passes it as ``context.items`` —
that feed is the one remaining piece. ``build_decision_batch_request`` already
accepts ``items`` so wiring it later is a single call-site change.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Mapping, Optional

# No logging in this module: the bearer token must never reach a log sink.
# Callers surface only the sanitized DecisionBatchBridgeError.code.

BRIDGE_PATH = "/api/cogitator_bridge"
TOKEN_ENV = "COGITATOR_BRIDGE_TOKEN"

_REQUESTED_ACTION = "render_decision_batch"
_USER_INTENT = "Show the current Cogitator decision batch for review (read-only)."
_REQUEST_TIMEOUT_SECONDS = 20

# The only context fields ever forwarded. Mirrors Cogitator's
# COGITATOR_BRIDGE_DECISION_BATCH_CONTEXT_FIELDS.
DECISION_BATCH_CONTEXT_FIELDS: tuple[str, ...] = ("items", "detail_id")

# Response-level keys that would indicate the bridge did something stateful or
# executed an approval. Any present → reject (exact key match, like the
# checkpoint helper).
_FORBIDDEN_RESPONSE_FIELDS: tuple[str, ...] = (
    "storage_path",
    "stored",
    "persisted",
    "persistence",
    "promoted",
    "promotion_performed",
    "approved",
    "approval_executed",
    "executed",
)


class DecisionBatchBridgeError(Exception):
    """Internal helper error carrying a stable, sanitized reason code.

    ``code`` is safe to surface to the user; ``detail`` is for logs only and must
    never contain the token, request headers, or response secrets.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def build_decision_batch_request(
    *,
    items: Optional[Iterable[Mapping[str, Any]]] = None,
    detail_id: str = "",
) -> dict[str, Any]:
    """Build the draft-only, low-risk ``render_decision_batch`` bridge packet.

    ``items`` (the real candidate/evidence feed) is omitted today, so Cogitator
    renders a sample batch. ``detail_id`` requests one item's detail view.
    """
    context: dict[str, Any] = {}
    if items:
        context["items"] = [dict(item) for item in items]
    if str(detail_id or "").strip():
        context["detail_id"] = str(detail_id).strip()
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
    """POST the packet to the Cogitator bridge with a bearer token. Fail-closed.

    The token is placed only in the ``Authorization`` header and never logged.
    Error details are limited to status codes / exception type names.
    ponytail: same transport shape as the checkpoint helper, kept self-contained
    so this read-only path carries its own sanitized error codes.
    """
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
        raise DecisionBatchBridgeError("BRIDGE_HTTP_ERROR", f"status={exc.code}")
    except urllib.error.URLError as exc:
        raise DecisionBatchBridgeError("BRIDGE_UNREACHABLE", type(exc).__name__)
    except Exception as exc:  # defensive: any transport failure fails closed
        raise DecisionBatchBridgeError("BRIDGE_UNREACHABLE", type(exc).__name__)

    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return json.loads(text)
    except Exception as exc:
        raise DecisionBatchBridgeError("BRIDGE_RESPONSE_INVALID", type(exc).__name__)


def validate_decision_batch_response(response: Any) -> dict[str, Any]:
    """Validate a ``render_decision_batch`` bridge response. Fails closed.

    Enforces the read-only / no-execution contract before anything reaches chat:
      * ``status == "ok"``
      * ``requested_action == "render_decision_batch"``
      * ``mutated`` is exactly ``False``
      * ``execution_authorized`` is exactly ``False``
      * no response-level storage/persistence/promotion/approval-execution fields
      * ``rendered_batch`` is a non-empty string
    """
    if not isinstance(response, Mapping):
        raise DecisionBatchBridgeError("BRIDGE_RESPONSE_INVALID", "response is not an object")
    if response.get("status") != "ok":
        raise DecisionBatchBridgeError("BRIDGE_STATUS_NOT_OK", f"status={response.get('status')!r}")
    if response.get("requested_action") != _REQUESTED_ACTION:
        raise DecisionBatchBridgeError(
            "BRIDGE_ACTION_MISMATCH", f"requested_action={response.get('requested_action')!r}"
        )
    if response.get("mutated") is not False:
        raise DecisionBatchBridgeError("BRIDGE_MUTATION_REPORTED", f"mutated={response.get('mutated')!r}")
    if response.get("execution_authorized") is not False:
        raise DecisionBatchBridgeError(
            "BRIDGE_EXECUTION_REPORTED", f"execution_authorized={response.get('execution_authorized')!r}"
        )
    stateful = [field for field in _FORBIDDEN_RESPONSE_FIELDS if field in response]
    if stateful:
        raise DecisionBatchBridgeError("BRIDGE_STATEFUL_RESPONSE", f"fields={stateful}")
    rendered = response.get("rendered_batch")
    if not isinstance(rendered, str) or not rendered.strip():
        raise DecisionBatchBridgeError("BRIDGE_BATCH_MISSING", "rendered_batch missing or empty")
    return dict(response)


def request_decision_batch(
    *,
    base_url: str,
    token: str,
    items: Optional[Iterable[Mapping[str, Any]]] = None,
    detail_id: str = "",
    urlopen: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Build the request, POST it to the bridge, and validate the reply.

    Fails closed (``DecisionBatchBridgeError``) on missing config, transport
    failure, or any read-only contract violation. ``urlopen`` is injectable for
    tests; production uses ``urllib.request.urlopen``.
    """
    if not str(base_url or "").strip():
        raise DecisionBatchBridgeError("BRIDGE_NOT_CONFIGURED", "base_url missing")
    if not str(token or "").strip():
        raise DecisionBatchBridgeError("BRIDGE_TOKEN_MISSING", "token missing")
    packet = build_decision_batch_request(items=items, detail_id=detail_id)
    response = _post_bridge(packet, base_url=base_url.strip(), token=token.strip(), urlopen=urlopen)
    return validate_decision_batch_response(response)


def render_decision_batch_message(response: Mapping[str, Any]) -> str:
    """Render a validated decision-batch response back to chat.

    Cogitator's ``rendered_batch`` is already the Cal-facing "Decision Inbox" —
    plain-English sections, simple numbers, and its own read-only Status line. We
    pass it through verbatim rather than wrapping it in internal bridge/action
    text; we add only the optional item detail and a short sample-data note. No
    ``approve #id`` footer — approval execution is not implemented and the inbox's
    own Status line already says so.
    """
    rendered = str(response.get("rendered_batch") or "").strip()
    detail = str(response.get("rendered_item") or "").strip()
    is_sample = bool(response.get("sample"))

    parts: list[str] = []
    if is_sample:
        parts.append("⚠️ Sample data — no live feed wired yet.")
    parts.append(rendered or "Decision Inbox\n\nNothing needs your attention right now.")
    if detail:
        parts.append(detail)
    return "\n\n".join(parts)


__all__ = [
    "DecisionBatchBridgeError",
    "DECISION_BATCH_CONTEXT_FIELDS",
    "TOKEN_ENV",
    "BRIDGE_PATH",
    "build_decision_batch_request",
    "request_decision_batch",
    "validate_decision_batch_response",
    "render_decision_batch_message",
]

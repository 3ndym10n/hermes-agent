"""Manual, read-only Cogitator context-checkpoint bridge helper (Context Rotation V0-D).

This is the *manual command* slice of Context Rotation. It builds a draft-only
``build_context_checkpoint`` bridge request, POSTs it to Cogitator's read-only
HTTP bridge endpoint (``{base_url}/api/cogitator_bridge``) using a bearer token
sourced **only** from the environment, validates the response fail-closed, and
hands it back for rendering to the current chat.

Deliberately out of scope (V0-D does NOT do any of this):
  * no automatic rotation / detection
  * no ``/new``, ``/branch`` or ``/compress`` integration / automation
  * no fresh-thread / session creation, no checkpoint injection into context
  * no persistence, storage writes, DB or schema changes
  * no provider/model call, no retry/context-limit wiring
  * no secret/token printed or logged; ``.env`` is never touched

The Cogitator action is itself read-only: it returns a checkpoint proposal only
(``mutated: false``) and performs no side effects. This helper additionally
*verifies* that contract on the response before anything reaches the chat.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Optional

# This module deliberately performs no logging: the bearer token must never
# reach a log sink. Callers surface sanitized CheckpointBridgeError.code values.

BRIDGE_PATH = "/api/cogitator_bridge"
TOKEN_ENV = "COGITATOR_BRIDGE_TOKEN"

_REQUESTED_ACTION = "build_context_checkpoint"
_USER_INTENT = (
    "Build a read-only checkpoint for the current Hermes conversation; "
    "do not rotate or inject."
)
_REQUEST_TIMEOUT_SECONDS = 20
_AUTO_DEFAULT_THRESHOLD = 0.80
_AUTO_DEFAULT_HARD_MESSAGE_LIMIT = 400
_AUTO_CONTINUATION_ACTION = (
    "Start a clean continuation with /new, then paste this checkpoint/handoff packet as the first message."
)

# The only context fields ever forwarded to the bridge. Mirrors Cogitator's
# COGITATOR_BRIDGE_CONTEXT_CHECKPOINT_CONTEXT_FIELDS.
_TEXT_FIELDS: tuple[str, ...] = ("purpose", "current_state", "next_recommended_action")
_LIST_FIELDS: tuple[str, ...] = (
    "active_constraints",
    "decisions_made",
    "open_questions",
    "artifact_paths",
    "verification",
)
CHECKPOINT_CONTEXT_FIELDS: tuple[str, ...] = _TEXT_FIELDS + _LIST_FIELDS

# Human-ordered sections for rendering the checkpoint back to chat.
_SECTION_LABELS: tuple[tuple[str, str], ...] = (
    ("purpose", "Purpose"),
    ("current_state", "Current state"),
    ("active_constraints", "Active constraints"),
    ("decisions_made", "Decisions made"),
    ("open_questions", "Open questions"),
    ("artifact_paths", "Artifact paths"),
    ("verification", "Verification"),
    ("next_recommended_action", "Next recommended action"),
)

# Response-level keys that would indicate the bridge did something stateful.
# Any present → reject: the action must be read-only (no storage, persistence,
# rotation, injection, session/conversation creation).
_FORBIDDEN_RESPONSE_FIELDS: tuple[str, ...] = (
    "storage_path",
    "stored",
    "persisted",
    "persistence",
    "rotation",
    "rotated",
    "injected",
    "injection",
    "session_id",
    "session",
    "conversation_id",
    "conversation",
    "thread_id",
    "thread",
)


class CheckpointBridgeError(Exception):
    """Internal helper error carrying a stable, sanitized reason code.

    ``code`` is safe to surface to the user; ``detail`` is for logs only and must
    never contain the token, request headers, or response secrets.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _build_context(source: Mapping[str, Any]) -> dict[str, Any]:
    """Project caller-supplied input onto exactly the eight checkpoint fields."""
    context: dict[str, Any] = {}
    for field in _TEXT_FIELDS:
        value = source.get(field, "")
        context[field] = "" if value is None else str(value)
    for field in _LIST_FIELDS:
        value = source.get(field, [])
        if value in (None, ""):
            context[field] = []
        elif isinstance(value, (list, tuple)):
            context[field] = [str(item) for item in value]
        else:
            context[field] = [str(value)]
    return context


def build_checkpoint_request(context_fields: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Build the draft-only, low-risk ``build_context_checkpoint`` bridge packet."""
    return {
        "source_agent": "hermes",
        "requested_action": _REQUESTED_ACTION,
        "user_intent": _USER_INTENT,
        "content": "",
        "approval_status": "draft_only",
        "risk_level": "low",
        "context": _build_context(context_fields or {}),
    }


def _is_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _safe_threshold(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return _AUTO_DEFAULT_THRESHOLD
    if parsed <= 0:
        return _AUTO_DEFAULT_THRESHOLD
    if parsed > 1:
        parsed = parsed / 100.0
    return parsed if 0 < parsed <= 1 else _AUTO_DEFAULT_THRESHOLD


def evaluate_auto_checkpoint_trigger(
    context_checkpoint_config: Mapping[str, Any] | None,
    *,
    prompt_tokens: Any,
    context_length: Any,
    message_count: Any,
) -> dict[str, Any]:
    """Return a deterministic V0-E automatic checkpoint decision.

    Detection only: no bridge call, transcript write, injection, rotation,
    reset, or persistence. Non-secret settings live under
    ``context_checkpoint.auto_trigger`` in config.yaml and default off.
    """
    cfg = context_checkpoint_config if isinstance(context_checkpoint_config, Mapping) else {}
    auto_cfg = cfg.get("auto_trigger", {})
    if not isinstance(auto_cfg, Mapping):
        auto_cfg = {}

    enabled = _is_enabled(auto_cfg.get("enabled", False))
    threshold = _safe_threshold(auto_cfg.get("threshold", _AUTO_DEFAULT_THRESHOLD))
    hard_message_limit = _safe_int(
        auto_cfg.get("hard_message_limit", _AUTO_DEFAULT_HARD_MESSAGE_LIMIT),
        _AUTO_DEFAULT_HARD_MESSAGE_LIMIT,
    )
    prompt = _safe_int(prompt_tokens)
    ctx_len = _safe_int(context_length)
    msg_count = _safe_int(message_count)
    threshold_tokens = int(ctx_len * threshold) if ctx_len > 0 else 0

    decision = {
        "enabled": enabled,
        "should_trigger": False,
        "reason": "disabled" if not enabled else "insufficient_usage_data",
        "prompt_tokens": prompt,
        "context_length": ctx_len,
        "threshold": threshold,
        "threshold_tokens": threshold_tokens,
        "message_count": msg_count,
        "hard_message_limit": hard_message_limit,
    }
    if not enabled:
        return decision

    if threshold_tokens > 0:
        if prompt >= threshold_tokens:
            decision["should_trigger"] = True
            decision["reason"] = "token_threshold"
        else:
            decision["reason"] = "below_threshold"
        return decision

    if hard_message_limit > 0 and msg_count >= hard_message_limit:
        decision["should_trigger"] = True
        decision["reason"] = "message_count_threshold"
    return decision


def _clip(text: Any, limit: int = 2000) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def build_auto_checkpoint_context(
    *,
    current_user_message: Any,
    final_response: Any,
    trigger_decision: Mapping[str, Any],
    session_id: str,
    session_key: str,
) -> dict[str, Any]:
    """Build the context fields sent to Cogitator for V0-E handoff."""
    reason = str(trigger_decision.get("reason") or "threshold")
    prompt = _safe_int(trigger_decision.get("prompt_tokens"))
    ctx_len = _safe_int(trigger_decision.get("context_length"))
    threshold_tokens = _safe_int(trigger_decision.get("threshold_tokens"))
    msg_count = _safe_int(trigger_decision.get("message_count"))

    usage = []
    if prompt and ctx_len:
        usage.append(f"Context usage reached ~{prompt:,}/{ctx_len:,} tokens.")
    if threshold_tokens:
        usage.append(f"Configured auto-checkpoint threshold is {threshold_tokens:,} tokens.")
    if msg_count:
        usage.append(f"Loaded conversation history contains {msg_count} messages.")

    state_parts = [
        "Hermes detected this conversation is approaching unsafe context size.",
        f"Trigger reason: {reason}.",
    ]
    state_parts.extend(usage)
    if current_user_message:
        state_parts.append(f"Latest user message: {_clip(current_user_message)}")
    if final_response:
        state_parts.append(f"Latest assistant response: {_clip(final_response)}")

    return {
        "purpose": "Protect a Hermes conversation approaching unsafe context size by producing a read-only clean-continuation handoff.",
        "current_state": "\n".join(state_parts),
        "active_constraints": [
            "No storage/db mutation",
            "No storage/db mutation for this checkpoint trigger.",
            "No automatic injection into a new context.",
            "No automatic rotation, /new, /reset, /compress, or destructive session action.",
            "Keep manual /context_checkpoint working.",
        ],
        "decisions_made": [
            "V0-E only returns a checkpoint/handoff packet to the current chat.",
            "The operator starts the clean continuation manually.",
        ],
        "open_questions": [
            "V0-F must define and validate a safe automatic injection/continuation mechanism before full rotation is enabled.",
        ],
        "artifact_paths": [
            f"Hermes session_id: {session_id or '(unknown)'}",
            f"Hermes session_key: {session_key or '(unknown)'}",
        ],
        "verification": [
            "Trigger decision was computed from local prompt-token/context-window or message-count metadata.",
            "Cogitator bridge response must report mutated=false and checkpoint.safety.mutation_performed=false before rendering.",
        ],
        "next_recommended_action": _AUTO_CONTINUATION_ACTION,
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
        raise CheckpointBridgeError("BRIDGE_HTTP_ERROR", f"status={exc.code}")
    except urllib.error.URLError as exc:
        raise CheckpointBridgeError("BRIDGE_UNREACHABLE", type(exc).__name__)
    except Exception as exc:  # defensive: any transport failure fails closed
        raise CheckpointBridgeError("BRIDGE_UNREACHABLE", type(exc).__name__)

    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return json.loads(text)
    except Exception as exc:
        raise CheckpointBridgeError("BRIDGE_RESPONSE_INVALID", type(exc).__name__)


def validate_checkpoint_response(response: Any) -> dict[str, Any]:
    """Validate a ``build_context_checkpoint`` bridge response. Fails closed.

    Enforces the read-only contract before anything reaches the chat:
      * ``status == "ok"``
      * ``requested_action == "build_context_checkpoint"``
      * ``mutated`` is exactly ``False``
      * no response-level storage/persistence/rotation/injection/session fields
      * ``checkpoint`` is an object
      * ``checkpoint.safety.mutation_performed`` is exactly ``False``
    """
    if not isinstance(response, Mapping):
        raise CheckpointBridgeError("BRIDGE_RESPONSE_INVALID", "response is not an object")
    if response.get("status") != "ok":
        raise CheckpointBridgeError("BRIDGE_STATUS_NOT_OK", f"status={response.get('status')!r}")
    if response.get("requested_action") != _REQUESTED_ACTION:
        raise CheckpointBridgeError(
            "BRIDGE_ACTION_MISMATCH", f"requested_action={response.get('requested_action')!r}"
        )
    if response.get("mutated") is not False:
        raise CheckpointBridgeError("BRIDGE_MUTATION_REPORTED", f"mutated={response.get('mutated')!r}")
    stateful = [field for field in _FORBIDDEN_RESPONSE_FIELDS if field in response]
    if stateful:
        raise CheckpointBridgeError("BRIDGE_STATEFUL_RESPONSE", f"fields={stateful}")
    checkpoint = response.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        raise CheckpointBridgeError("BRIDGE_CHECKPOINT_MISSING", "checkpoint is not an object")
    safety = checkpoint.get("safety")
    if not isinstance(safety, Mapping):
        raise CheckpointBridgeError("BRIDGE_CHECKPOINT_UNSAFE", "checkpoint.safety missing")
    if safety.get("mutation_performed") is not False:
        raise CheckpointBridgeError("BRIDGE_CHECKPOINT_UNSAFE", "checkpoint safety reports mutation")
    return dict(response)


def request_context_checkpoint(
    context_fields: Optional[Mapping[str, Any]],
    *,
    base_url: str,
    token: str,
    urlopen: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Build the request, POST it to the bridge, and validate the reply.

    Fails closed (``CheckpointBridgeError``) on missing config, transport
    failure, or any read-only contract violation. ``urlopen`` is injectable for
    tests; production uses ``urllib.request.urlopen``.
    """
    if not str(base_url or "").strip():
        raise CheckpointBridgeError("BRIDGE_NOT_CONFIGURED", "base_url missing")
    if not str(token or "").strip():
        raise CheckpointBridgeError("BRIDGE_TOKEN_MISSING", "token missing")
    packet = build_checkpoint_request(context_fields)
    response = _post_bridge(packet, base_url=base_url.strip(), token=token.strip(), urlopen=urlopen)
    return validate_checkpoint_response(response)


def render_checkpoint_message(response: Mapping[str, Any]) -> str:
    """Render a validated checkpoint response back to chat as labeled sections."""
    checkpoint = response.get("checkpoint", {}) if isinstance(response, Mapping) else {}
    lines = [
        "🧭 Context Checkpoint (read-only, draft only)",
        "Source: Cogitator build_context_checkpoint bridge action.",
        "No mutation, no persistence, no rotation, no injection — proposal only.",
        "",
    ]
    for key, label in _SECTION_LABELS:
        value = checkpoint.get(key, "")
        if isinstance(value, (list, tuple)):
            if value:
                lines.append(f"{label}:")
                lines.extend(f"  - {item}" for item in value)
            else:
                lines.append(f"{label}: (none)")
        else:
            text = str(value).strip()
            lines.append(f"{label}: {text if text else '(none)'}")
    return "\n".join(lines)


def render_auto_checkpoint_message(
    response: Mapping[str, Any],
    *,
    trigger_decision: Mapping[str, Any] | None = None,
) -> str:
    """Render V0-E's automatic checkpoint notice plus the validated packet."""
    decision = trigger_decision or {}
    prompt = _safe_int(decision.get("prompt_tokens"))
    ctx_len = _safe_int(decision.get("context_length"))
    threshold_tokens = _safe_int(decision.get("threshold_tokens"))
    reason = str(decision.get("reason") or "threshold")
    usage = ""
    if prompt and ctx_len:
        usage = f"Context usage: ~{prompt:,}/{ctx_len:,} tokens"
        if threshold_tokens:
            usage += f" (threshold: {threshold_tokens:,})"
    elif threshold_tokens:
        usage = f"Threshold: {threshold_tokens:,} tokens"

    lines = [
        "🛟 Automatic Context Protection (V0-E)",
        f"Trigger: {reason}.",
    ]
    if usage:
        lines.append(usage + ".")
    lines.extend([
        "No automatic injection or rotation has happened — this is a read-only handoff packet.",
        "Clean continuation action: start a fresh chat with `/new`, then paste the checkpoint below as the first message.",
        "",
        render_checkpoint_message(response),
    ])
    return "\n".join(lines)


__all__ = [
    "CheckpointBridgeError",
    "CHECKPOINT_CONTEXT_FIELDS",
    "TOKEN_ENV",
    "BRIDGE_PATH",
    "build_checkpoint_request",
    "evaluate_auto_checkpoint_trigger",
    "build_auto_checkpoint_context",
    "request_context_checkpoint",
    "validate_checkpoint_response",
    "render_checkpoint_message",
    "render_auto_checkpoint_message",
]

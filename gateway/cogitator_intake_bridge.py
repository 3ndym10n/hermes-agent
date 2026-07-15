"""Cogitator universal text-intake bridge helper (plain-text ``intake`` command).

Cal pastes messy raw material into chat as one message whose first line is
``intake`` (or ``intake lens <label>``); the body below is preserved verbatim.
This module is the deterministic front half: a strict parser (so ordinary prose
is never hijacked), a draft-only ``intake_review_packet`` bridge request POSTed
to Cogitator's HTTP bridge with a bearer token sourced **only** from the
environment, fail-closed response validation, and a compact chat rendering.

Deliberately out of scope: no link fetching, no research, no approval or
promotion — Cogitator reports ``research_performed``/``promotion_performed``
false and this helper *verifies* that before anything reaches chat. No secret
is ever logged; ``.env`` is never touched.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

# No logging in this module: the bearer token must never reach a log sink.

BRIDGE_PATH = "/api/cogitator_bridge"
TOKEN_ENV = "COGITATOR_BRIDGE_TOKEN"

_REQUESTED_ACTION = "intake_review_packet"
_USER_INTENT = "Turn one raw Telegram dump into a reviewable intake packet (Virgil intake)."
# Link intake runs auto-research synchronously on the Cogitator side; a live
# fetched_full+auto-research round trip measured 90.7s, so 45s surfaced
# BRIDGE_UNREACHABLE on healthy requests. 180s covers it with headroom.
# ponytail: full-engine research can take ~4min — that needs a budget/async slice, not more timeout.
_REQUEST_TIMEOUT_SECONDS = 180
MAX_BODY_CHARS = 16000  # matches Cogitator's front-door cap; bridge envelope is 20k
MAX_LENS_CHARS = 60

# First line must be exactly "intake" or "intake lens <label>" (case-insensitive)
# to count as a command; a first line that merely *starts* with "intake lens"
# but has a bad label is an attempted command and gets usage, never the model.
_LENS_RE = re.compile(r"^intake\s+lens\s+(.+)$", re.IGNORECASE)
_LENS_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]*$")
# One-line ``intake <url>`` — a bare URL right after the verb cannot be prose,
# so folding it into the body is hijack-safe. Cal's natural phone form.
_INLINE_URL_RE = re.compile(r"^intake\s+(https?://\S+)$", re.IGNORECASE)

# Response fields that would indicate research/promotion/approval execution.
_FORBIDDEN_RESPONSE_FIELDS: tuple[str, ...] = (
    "promoted",
    "approved",
    "approval_executed",
    "executed",
)


class IntakeBridgeError(Exception):
    """Stable, sanitized reason code (safe to surface); detail is for logs only."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


_RESEARCH_RE = re.compile(
    r"^intake\s+research\s+(?:(storage/intake/packets/\S+-packet\.md)\s+)?#?(\d{1,3})$",
    re.IGNORECASE,
)
_RESEARCH_ATTEMPT_RE = re.compile(r"^intake\s+research\b", re.IGNORECASE)


@dataclass(frozen=True)
class IntakeCommand:
    """Parsed plain-text intake command. ``error`` set → show usage/reason and
    stop; the message never reaches the model either way. ``research_number``
    set → this is a research verb, not a new dump."""

    raw_text: str = ""
    lens: str = ""
    research_number: int | None = None
    packet_path: str = ""  # optional explicit target for the research verb
    error: str = ""  # "", "missing_body", "oversized_body", "invalid_lens", "invalid_research"


def parse_intake_message(text: str) -> Optional[IntakeCommand]:
    """Strict string→intent parser for one chat message.

    Returns None for anything that is not an intake command attempt (slash
    commands, prose that merely contains the word) so normal handling is
    untouched. ``intake research <n>`` (optionally with an explicit packet
    path) is the research verb over the latest/addressed intake packet.
    """
    s = str(text or "")
    first, _, body = s.partition("\n")
    head = first.strip()
    if _RESEARCH_ATTEMPT_RE.match(head):
        match = _RESEARCH_RE.match(head)
        if not match or body.strip():
            return IntakeCommand(error="invalid_research")
        return IntakeCommand(research_number=int(match.group(2)),
                             packet_path=match.group(1) or "")
    if head.lower() == "intake":
        lens = ""
    else:
        inline_url = _INLINE_URL_RE.match(head)
        if inline_url:
            lens = ""
            body = inline_url.group(1) + ("\n" + body if body.strip() else "")
        else:
            match = _LENS_RE.match(head)
            if not match:
                return None
            lens = match.group(1).strip()
            if not _LENS_LABEL_RE.match(lens) or len(lens) > MAX_LENS_CHARS:
                return IntakeCommand(error="invalid_lens")
    raw_text = body.strip("\n")
    if not raw_text.strip():
        return IntakeCommand(lens=lens, error="missing_body")
    if len(raw_text) > MAX_BODY_CHARS:
        return IntakeCommand(lens=lens, error="oversized_body")
    return IntakeCommand(raw_text=raw_text, lens=lens)


_URL_LINE_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
MAX_LINK_LINES = 25


def is_url_only_body(raw_text: str) -> bool:
    """True when every non-empty line of the body is a bare URL — then the
    dump is a link list and routes through Cogitator's source-access seam."""
    lines = [line.strip() for line in str(raw_text or "").splitlines() if line.strip()]
    return bool(lines) and all(_URL_LINE_RE.match(line) for line in lines)


@dataclass(frozen=True)
class IntelligentIntake:
    """One message that should enter the V1 assessment path."""

    raw_text: str
    input_kind: str
    explicit: bool = False


_BARE_SUPPORTED_URL_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)
_OAUTH_ASSESSMENT_PROVIDERS = frozenset({
    "openai-codex",
    "xai-oauth",
    "qwen-oauth",
    "minimax-oauth",
    "google-gemini-cli",
})


def parse_intelligent_intake(
    text: str,
    *,
    forwarded: bool = False,
    transcript: bool = False,
) -> IntelligentIntake | None:
    """Route explicit intake, forwarded content, or one bare URL.

    A URL plus any direct question/prose is deliberately not matched and
    remains a normal Hermes conversation.
    """
    command = parse_intake_message(text)
    if command is not None:
        if command.error or command.research_number is not None:
            return None
        kind = "bare_url" if is_url_only_body(command.raw_text) else (
            "transcript" if transcript else "pasted_text"
        )
        return IntelligentIntake(command.raw_text, kind, explicit=True)
    raw = str(text or "").strip()
    if forwarded and raw:
        return IntelligentIntake(raw, "forwarded_post")
    if _BARE_SUPPORTED_URL_RE.fullmatch(raw):
        return IntelligentIntake(raw, "bare_url")
    return None


def _declared_event_value(value: Any, name: str) -> Any:
    """Avoid dynamic mock/proxy attributes becoming trusted routing metadata."""
    if value is None:
        return None
    declared = getattr(value, "__dict__", None)
    if isinstance(declared, Mapping) and name in declared:
        return declared[name]
    if type(value).__module__.startswith("telegram"):
        return getattr(value, name, None)
    return None


def intelligent_intake_event_flags(event: Any) -> dict[str, bool]:
    """Read only bounded routing metadata; never serialize the raw event."""
    raw = _declared_event_value(event, "raw_message")
    forward_origin = _declared_event_value(raw, "forward_origin")
    forward_date = _declared_event_value(raw, "forward_date")
    event_forwarded = _declared_event_value(event, "forwarded")
    event_transcript = _declared_event_value(event, "is_transcript")
    raw_transcript = _declared_event_value(raw, "transcript")
    forwarded = bool(
        forward_origin is not None
        or forward_date is not None
        or event_forwarded is True
    )
    transcript = bool(
        event_transcript is True
        or (isinstance(raw_transcript, str) and raw_transcript.strip())
    )
    return {"forwarded": forwarded, "transcript": transcript}


@dataclass(frozen=True)
class IntelligentReviewCommand:
    review_id: str = ""
    action: str = ""
    payload: str = ""
    error: str = ""


@dataclass(frozen=True)
class IntelligentOutcomeCommand:
    outcome: str = ""
    item_id: str = ""
    details: str = ""
    error: str = ""


_REVIEW_COMMAND_RE = re.compile(
    r"^(?:knowledge\s+)?"
    r"(approve|reject|archive|research|experiment|related|merge|update)\s+"
    r"(?:knowledge\s+)?(inote-[1-9][0-9]{0,9})(?:\s+(.+))?$",
    re.IGNORECASE,
)
_REVIEW_COMMAND_ATTEMPT_RE = re.compile(
    r"^(?:knowledge\s+(?:approve|reject|archive|research|experiment|related|"
    r"merge|update)|(?:approve|reject|archive|research|experiment|related|"
    r"merge|update)\s+knowledge)\b",
    re.IGNORECASE,
)
_REVIEW_DETAIL_RE = re.compile(
    r"^(?:show(?:\s+me)?(?:\s+the)?(?:\s+full)?\s+assessment(?:\s+for)?|"
    r"show\s+lifecycle(?:\s+for)?|view|review\s+details(?:\s+for)?)\s+"
    r"(inote-[1-9][0-9]{0,9})[.?!]?$",
    re.IGNORECASE,
)
_REVIEW_DETAIL_ATTEMPT_RE = re.compile(
    r"^(?:show(?:\s+me)?(?:\s+the)?(?:\s+full)?\s+assessment|"
    r"show\s+lifecycle|view|review\s+details)\b",
    re.IGNORECASE,
)
_OUTCOME_COMMAND_RE = re.compile(
    r"^knowledge\s+outcome\s+"
    r"(used|useful|partially[ _-]useful|not[ _-]useful|corrected|superseded)"
    r"(?:\s+(ki_[a-f0-9]{24}))?(?:\s+(.+))?$",
    re.IGNORECASE,
)
_OUTCOME_COMMAND_ATTEMPT_RE = re.compile(
    r"^knowledge\s+outcome\b", re.IGNORECASE
)
_REVIEW_ACTIONS = {
    "approve": "approve",
    "reject": "reject",
    "archive": "archive",
    "research": "request_explicit_research",
    "experiment": "convert_to_experiment",
    "related": "view_related",
    "merge": "update_or_merge",
    "update": "update_or_merge",
}


def parse_intelligent_review_command(text: str) -> IntelligentReviewCommand | None:
    raw = " ".join(str(text or "").strip().split())
    if raw.lower().rstrip(".?!") == "list review candidates":
        return IntelligentReviewCommand(action="list_candidates")
    detail = _REVIEW_DETAIL_RE.fullmatch(raw)
    if detail:
        payload = "" if raw.lower().startswith("show lifecycle") else "assessment"
        return IntelligentReviewCommand(detail.group(1), "view_related", payload)
    if _REVIEW_DETAIL_ATTEMPT_RE.match(raw) and re.search(
        r"\binote-\S+", raw, re.IGNORECASE
    ):
        return IntelligentReviewCommand(error="invalid_review_command")
    match = _REVIEW_COMMAND_RE.fullmatch(raw)
    if not match:
        if _REVIEW_COMMAND_ATTEMPT_RE.match(raw):
            return IntelligentReviewCommand(error="invalid_review_command")
        return None
    verb, review_id, payload = match.groups()
    action = _REVIEW_ACTIONS[verb.lower()]
    clean_payload = str(payload or "").strip()
    if action == "update_or_merge" and not clean_payload:
        return IntelligentReviewCommand(error="payload_required")
    return IntelligentReviewCommand(review_id, action, clean_payload)


def parse_intelligent_outcome_command(text: str) -> IntelligentOutcomeCommand | None:
    raw = " ".join(str(text or "").strip().split())
    match = _OUTCOME_COMMAND_RE.fullmatch(raw)
    if not match:
        if _OUTCOME_COMMAND_ATTEMPT_RE.match(raw):
            return IntelligentOutcomeCommand(error="invalid_outcome_command")
        return None
    outcome, item_id, details = match.groups()
    normalized = outcome.lower().replace("-", "_").replace(" ", "_")
    clean_details = str(details or "").strip()
    first_detail = clean_details.split(maxsplit=1)[0] if clean_details else ""
    if not item_id and first_detail.lower().startswith("ki_"):
        return IntelligentOutcomeCommand(error="invalid_item_id")
    if normalized in {"corrected", "superseded"} and not clean_details:
        return IntelligentOutcomeCommand(error="details_required")
    return IntelligentOutcomeCommand(
        normalized, str(item_id or "").lower(), clean_details
    )


_TASK_START_RE = re.compile(
    r"^(?:please\s+)?(?:help|plan|decide|compare|choose|draft|write|create|"
    r"review|assess|analy[sz]e|recommend|how|what|why|should|can|could|would|"
    r"find|prepare|build|fix|implement)\b",
    re.IGNORECASE,
)
_CHITCHAT = {
    "how are you", "how are you doing", "how are you doing today",
    "thanks very much", "thank you very much",
    "good morning there", "good afternoon there", "good evening there",
}


def is_intelligent_retrieval_eligible(
    text: str, *, platform: str, proxy: bool = False, internal: bool = False
) -> bool:
    """Pure gate for one external Telegram task/question API turn."""
    raw = str(text or "").strip()
    normalized = " ".join(raw.lower().split())
    if str(platform or "").lower() != "telegram" or proxy or internal or not raw:
        return False
    if raw.startswith("/") or normalized.startswith("intake"):
        return False
    if (
        is_intelligent_synthesis_request(raw)
        or parse_intelligent_review_command(raw) is not None
        or parse_intelligent_outcome_command(raw) is not None
        or _BARE_SUPPORTED_URL_RE.fullmatch(raw)
    ):
        return False
    words = re.findall(r"[A-Za-z0-9']+", raw)
    if len(words) < 4 or len(raw) < 20 or normalized.rstrip("!?.,") in _CHITCHAT:
        return False
    return "?" in raw or bool(_TASK_START_RE.match(raw))


def is_intelligent_synthesis_request(text: str) -> bool:
    return " ".join(str(text or "").strip().lower().split()) in {
        "synthesize brain",
        "brain synthesis",
    }


def build_intelligent_synthesis_request() -> dict[str, Any]:
    return {
        "source_agent": "hermes",
        "requested_action": "build_intelligent_synthesis",
        "user_intent": "Build one manual evidence-linked brain synthesis.",
        "content": "",
        "approval_status": "draft_only",
        "risk_level": "low",
        "context": {},
    }


def build_intelligent_review_request(
    command: IntelligentReviewCommand,
) -> dict[str, Any]:
    if (
        command.error
        or not command.action
        or (command.action != "list_candidates" and not command.review_id)
        or (command.action == "list_candidates" and command.review_id)
    ):
        raise IntakeBridgeError("REVIEW_COMMAND_INVALID")
    return {
        "source_agent": "hermes",
        "requested_action": "review_intelligent_knowledge",
        "user_intent": "Apply one explicit Cal review action.",
        "content": "",
        "approval_status": (
            "draft_only"
            if command.action in {"view_related", "list_candidates"}
            else "approved"
        ),
        "risk_level": "low",
        "context": {
            "review_id": command.review_id,
            "action": command.action,
            "payload": command.payload,
        },
    }


def build_intelligent_retrieval_request(task_description: str) -> dict[str, Any]:
    task = " ".join(str(task_description or "").split())
    if not task:
        raise IntakeBridgeError("RETRIEVAL_TASK_MISSING")
    return {
        "source_agent": "hermes",
        "requested_action": "retrieve_intelligent_knowledge",
        "user_intent": "Retrieve targeted current promoted knowledge before work.",
        "content": "",
        "approval_status": "draft_only",
        "risk_level": "low",
        "context": {"task_description": task},
    }


def build_intelligent_usage_request(
    *,
    retrieval_receipt_id: str,
    response_task_id: str,
    response_text: str,
    used_item_ids: list[str],
    other_context_sources: list[str],
    provider_path: str,
    paid_web_research_api_used: bool,
    refinement_item_id: str = "",
    refinement_text: str = "",
) -> dict[str, Any]:
    return {
        "source_agent": "hermes",
        "requested_action": "record_intelligent_response_usage",
        "user_intent": "Record honest post-response knowledge provenance.",
        "content": "",
        "approval_status": "draft_only",
        "risk_level": "low",
        "context": {
            "retrieval_receipt_id": retrieval_receipt_id,
            "response_task_id": response_task_id,
            "response_text": response_text,
            "used_item_ids": used_item_ids,
            "other_context_sources": other_context_sources,
            "provider_path": provider_path,
            "paid_web_research_api_used": paid_web_research_api_used,
            "refinement_item_id": refinement_item_id,
            "refinement_text": refinement_text,
        },
    }


def build_intelligent_outcome_request(
    command: IntelligentOutcomeCommand,
    *,
    retrieval_receipt_id: str,
    item_id: str,
) -> dict[str, Any]:
    if command.error or not command.outcome:
        raise IntakeBridgeError("OUTCOME_COMMAND_INVALID")
    return {
        "source_agent": "hermes",
        "requested_action": "record_intelligent_knowledge_outcome",
        "user_intent": "Record Cal's explicit outcome for retrieved knowledge.",
        "content": "",
        "approval_status": "approved",
        "risk_level": "low",
        "context": {
            "retrieval_receipt_id": str(retrieval_receipt_id or "").strip(),
            "item_id": str(item_id or "").strip(),
            "outcome": command.outcome,
            "details": command.details,
        },
    }


def build_intelligent_prepare_request(
    intake: IntelligentIntake,
    *,
    origin: Mapping[str, Any] | None = None,
    authenticated_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "raw_text": intake.raw_text,
        "input_kind": intake.input_kind,
    }
    if authenticated_source is not None:
        context["authenticated_source"] = dict(authenticated_source)
    if origin is not None:
        context["origin"] = _delivery_origin(origin)
    return {
        "source_agent": "hermes",
        "requested_action": "prepare_intelligent_intake",
        "user_intent": "Prepare one source-first, context-aware V1 assessment.",
        "content": "",
        "approval_status": "draft_only",
        "risk_level": "low",
        "context": context,
    }


def build_intelligent_finalize_request(
    *,
    preparation_id: str,
    model_response: str | None,
    provider_path: str,
    latency_ms: int,
    provider_invoked: bool,
    retry_count: int = 0,
    repair_attempt: bool = False,
) -> dict[str, Any]:
    if not str(preparation_id or "").strip():
        raise IntakeBridgeError("PREPARATION_ID_MISSING")
    return {
        "source_agent": "hermes",
        "requested_action": "finalize_intelligent_intake",
        "user_intent": "Validate and persist one V1 assessment.",
        "content": "",
        "approval_status": "draft_only",
        "risk_level": "low",
        "context": {
            "preparation_id": str(preparation_id).strip(),
            "model_response": model_response,
            "provider_path": str(provider_path or "").strip(),
            "latency_ms": int(latency_ms),
            "provider_invoked": bool(provider_invoked),
            "retry_count": max(0, int(retry_count or 0)),
            "repair_attempt": bool(repair_attempt),
        },
    }


def subscription_assessment_route(provider: str, api_mode: str = "") -> dict[str, Any] | None:
    """Return a pinned, tool-free route or None before any provider call."""
    normalized = str(provider or "").strip().lower()
    if normalized not in _OAUTH_ASSESSMENT_PROVIDERS:
        return None
    if normalized in {"openai-codex", "xai-oauth"} and api_mode != "codex_responses":
        return None
    return {
        "provider": normalized,
        "api_mode": str(api_mode or ""),
        "providers_allowed": [normalized],
        "providers_order": [normalized],
        "providers_ignored": ["openrouter"],
        "fallback_model": None,
        "enabled_toolsets": [],
        "disabled_toolsets": ["web", "research", "delegate", "browser"],
        "max_iterations": 1,
    }


def run_subscription_assessment(
    prompt: str,
    *,
    provider: str,
    api_mode: str,
    run_turn: Callable[[str, dict[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Run exactly one injected OAuth turn, or abstain before invocation."""
    route = subscription_assessment_route(provider, api_mode)
    if route is None:
        return {"provider_invoked": False, "reason": "no_eligible_oauth_route"}
    started = __import__("time").monotonic()
    try:
        result = run_turn(prompt, route)
    except Exception:
        result = None
    latency_ms = max(0, int((__import__("time").monotonic() - started) * 1000))
    provider_path = f"hermes:{route['provider']}:oauth"
    if not isinstance(result, Mapping) or result.get("tools"):
        return {
            "provider_invoked": True,
            "provider_path": provider_path,
            "latency_ms": latency_ms,
            "model_response": None,
            "processing_status": "failed_reasoning",
            "failure_category": (
                "tool_use_rejected" if isinstance(result, Mapping)
                else "provider_unavailable"
            ),
            "retry_count": 0,
            "http_status": 0,
        }
    final_response = str(result.get("final_response") or "").strip()
    provider_error = str(result.get("error") or "").strip()
    failure_text = provider_error or final_response
    retry_match = re.search(r"after\s+(\d+)\s+retr(?:y|ies)", failure_text, re.I)
    status_match = re.search(r"\bHTTP\s+(\d{3})\b", failure_text, re.I)
    lower = final_response.lower()
    failed = bool(
        result.get("failed")
        or provider_error
        or not final_response
        or lower.startswith("api call failed after")
        or lower.startswith("<!doctype html")
        or lower.startswith("<html")
        or lower.startswith("⚠️ proxy error")
        or lower.startswith("⚠️ proxy connection error")
    )
    if failed:
        category = (
            "empty_response"
            if not final_response and not provider_error
            else "provider_http_error"
            if status_match
            else "provider_retries_exhausted"
            if retry_match
            else "provider_error"
        )
        return {
            "provider_invoked": True,
            "provider_path": provider_path,
            "latency_ms": latency_ms,
            "model_response": None,
            "processing_status": "failed_reasoning",
            "failure_category": category,
            "retry_count": int(retry_match.group(1)) if retry_match else 0,
            "http_status": int(status_match.group(1)) if status_match else 0,
        }
    return {
        "provider_invoked": True,
        "provider_path": provider_path,
        "latency_ms": latency_ms,
        "model_response": final_response,
    }


def _validate_intelligent_response(
    response: Any,
    *,
    expected_action: str,
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise IntakeBridgeError(
            "BRIDGE_RESPONSE_INVALID", "response is not an object"
        )
    if response.get("requested_action") != expected_action:
        raise IntakeBridgeError("BRIDGE_ACTION_MISMATCH")
    status = str(response.get("status") or "")
    if status not in {
        "ready", "ok", "rejected", "failed_source",
        "failed_reasoning", "failed_validation",
    }:
        raise IntakeBridgeError("BRIDGE_STATUS_NOT_OK", f"status={status!r}")
    if (
        response.get("research_performed") not in (False, None)
        or response.get("promotion_performed") not in (False, None)
    ):
        raise IntakeBridgeError("BRIDGE_STATEFUL_RESPONSE")
    if expected_action == "finalize_intelligent_intake":
        expected_state = {
            "ok": {"complete_assessment", "partial_assessment"},
            "failed_reasoning": {"failed_reasoning"},
            "failed_validation": {"invalid_core_assessment"},
            "failed_source": {"needs_full_source"},
            "rejected": {"failed_reasoning"},
        }.get(status)
        if expected_state and str(response.get("final_state") or "") not in expected_state:
            raise IntakeBridgeError(
                "BRIDGE_RESPONSE_INVALID", "final state does not match status"
            )
    if expected_action == "prepare_intelligent_intake" and status == "ready":
        if (
            not str(response.get("preparation_id") or "").strip()
            or not str(response.get("prompt") or "").strip()
        ):
            raise IntakeBridgeError(
                "BRIDGE_RESPONSE_INVALID", "preparation receipt incomplete"
            )
    if expected_action == "finalize_intelligent_intake" and status == "ok":
        if (
            response.get("paid_api_used") is not False
            or float(response.get("estimated_paid_cost") or 0) != 0
            or not str(response.get("item_id") or "").strip()
            or not str(response.get("review_id") or "").strip()
        ):
            raise IntakeBridgeError(
                "BRIDGE_RESPONSE_INVALID", "final receipt incomplete"
            )
        message = str(response.get("telegram_message") or "")
        for heading in ("VALUE:", "REVIEW ID:", "PROVIDER:", "PAID API:"):
            if heading not in message:
                raise IntakeBridgeError(
                    "BRIDGE_RESPONSE_INVALID",
                    "final Telegram receipt incomplete",
                )
    return dict(response)


def request_intelligent_prepare(
    *,
    base_url: str,
    token: str,
    intake: IntelligentIntake,
    origin: Mapping[str, Any] | None = None,
    authenticated_source: Mapping[str, Any] | None = None,
    urlopen: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    packet = build_intelligent_prepare_request(
        intake, origin=origin, authenticated_source=authenticated_source
    )
    response = _post_bridge(
        packet,
        base_url=str(base_url or "").strip(),
        token=str(token or "").strip(),
        urlopen=urlopen,
    )
    return _validate_intelligent_response(
        response, expected_action="prepare_intelligent_intake"
    )


def request_intelligent_finalize(
    *,
    base_url: str,
    token: str,
    preparation_id: str,
    reasoning_result: Mapping[str, Any],
    repair_attempt: bool = False,
    urlopen: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    packet = build_intelligent_finalize_request(
        preparation_id=preparation_id,
        model_response=reasoning_result.get("model_response"),
        provider_path=str(reasoning_result.get("provider_path") or ""),
        latency_ms=int(reasoning_result.get("latency_ms") or 0),
        provider_invoked=bool(reasoning_result.get("provider_invoked")),
        retry_count=int(reasoning_result.get("retry_count") or 0),
        repair_attempt=repair_attempt,
    )
    response = _post_bridge(
        packet,
        base_url=str(base_url or "").strip(),
        token=str(token or "").strip(),
        urlopen=urlopen,
    )
    return _validate_intelligent_response(
        response, expected_action="finalize_intelligent_intake"
    )


def _validate_intelligent_action_response(
    response: Any,
    *,
    expected_action: str,
    allowed_statuses: set[str],
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    if response.get("requested_action") != expected_action:
        raise IntakeBridgeError("BRIDGE_ACTION_MISMATCH")
    if str(response.get("status") or "") not in allowed_statuses:
        raise IntakeBridgeError("BRIDGE_STATUS_NOT_OK")
    if (
        response.get("paid_api_used") is not False
        or response.get("research_performed") not in (False, None)
    ):
        raise IntakeBridgeError("BRIDGE_STATEFUL_RESPONSE")
    return dict(response)


def request_intelligent_review(
    *,
    base_url: str,
    token: str,
    command: IntelligentReviewCommand,
    urlopen: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    response = _post_bridge(
        build_intelligent_review_request(command),
        base_url=str(base_url or "").strip(),
        token=str(token or "").strip(),
        urlopen=urlopen,
    )
    result = _validate_intelligent_action_response(
        response,
        expected_action="review_intelligent_knowledge",
        allowed_statuses={
            "already_applied", "applied", "research_requested", "ok",
            "candidate_created", "blocked",
        },
    )
    status = str(result.get("status") or "")
    if status == "blocked":
        if (
            result.get("mutation_performed") is not False
            or not str(result.get("reason") or "").strip()
        ):
            raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
        return result
    expected_statuses = {
        "approve": {"applied", "already_applied"},
        "reject": {"applied", "already_applied"},
        "archive": {"applied", "already_applied"},
        "request_explicit_research": {"research_requested"},
        "view_related": {"ok"},
        "list_candidates": {"ok"},
        "convert_to_experiment": {"candidate_created"},
        "update_or_merge": {"candidate_created"},
    }
    if status not in expected_statuses.get(command.action, set()):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    if result.get("action") != command.action:
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    if command.action == "list_candidates":
        candidates = result.get("candidates")
        if not isinstance(candidates, list) or result.get("mutation_performed") is not False:
            raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
        for candidate in candidates:
            if (
                not isinstance(candidate, Mapping)
                or not re.fullmatch(
                    r"inote-[1-9][0-9]*", str(candidate.get("review_id") or "")
                )
            ):
                raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
        return result
    if not re.fullmatch(
        r"ki_[a-f0-9]{24}", str(result.get("item_id") or "")
    ):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    if status in {"applied", "already_applied"}:
        expected_lifecycle = {
            "approve": "promoted",
            "reject": "rejected",
            "archive": "archived",
        }.get(command.action)
        if result.get("lifecycle_state") != expected_lifecycle:
            raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
        if command.action == "approve" and not str(
            result.get("promoted_path") or ""
        ).strip():
            raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    elif status == "research_requested":
        if result.get("review_id") != command.review_id:
            raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    elif status == "candidate_created" and not re.fullmatch(
        r"inote-[1-9][0-9]*", str(result.get("candidate_review_id") or "")
    ):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    elif status == "ok":
        if (
            result.get("review_id") != command.review_id
            or not isinstance(result.get("relations"), list)
        ):
            raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
        if command.payload == "assessment":
            detail = result.get("assessment_detail")
            if (
                not isinstance(detail, Mapping)
                or detail.get("knowledge_item_id") != result.get("item_id")
                or detail.get("markdown_path") != result.get("markdown_path")
                or detail.get("lifecycle") != result.get("lifecycle_state")
                or result.get("mutation_performed") is not False
            ):
                raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    return result


def request_intelligent_retrieval(
    *,
    base_url: str,
    token: str,
    task_description: str,
    urlopen: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    response = _post_bridge(
        build_intelligent_retrieval_request(task_description),
        base_url=str(base_url or "").strip(),
        token=str(token or "").strip(),
        urlopen=urlopen,
    )
    result = _validate_intelligent_action_response(
        response,
        expected_action="retrieve_intelligent_knowledge",
        allowed_statuses={"ok"},
    )
    records = result.get("records")
    citations = result.get("citations")
    if not isinstance(records, list) or not isinstance(citations, list):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    if "receipt" in result:
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    for record in records:
        if (
            not isinstance(record, Mapping)
            or not re.fullmatch(r"ki_[a-f0-9]{24}", str(record.get("item_id") or ""))
            or not str(record.get("citation") or "").strip()
            or record.get("lifecycle_state")
            not in {"approved", "promoted"}
        ):
            raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    receipt_id = str(result.get("retrieval_receipt_id") or "")
    if records and not re.fullmatch(r"isbr_[A-Za-z0-9_-]{20,64}", receipt_id):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    if not records and receipt_id:
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    return result


def request_intelligent_response_usage(
    *,
    base_url: str,
    token: str,
    retrieval_receipt_id: str,
    response_task_id: str,
    response_text: str,
    used_item_ids: list[str],
    retrieved_item_ids: Optional[list[str]] = None,
    other_context_sources: list[str],
    provider_path: str,
    paid_web_research_api_used: bool,
    refinement_item_id: str = "",
    refinement_text: str = "",
    urlopen: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    response = _post_bridge(
        build_intelligent_usage_request(
            retrieval_receipt_id=retrieval_receipt_id,
            response_task_id=response_task_id,
            response_text=response_text,
            used_item_ids=used_item_ids,
            other_context_sources=other_context_sources,
            provider_path=provider_path,
            paid_web_research_api_used=paid_web_research_api_used,
            refinement_item_id=refinement_item_id,
            refinement_text=refinement_text,
        ),
        base_url=str(base_url or "").strip(),
        token=str(token or "").strip(),
        urlopen=urlopen,
    )
    if (
        not isinstance(response, Mapping)
        or response.get("requested_action")
        != "record_intelligent_response_usage"
        or response.get("status") not in {"recorded", "blocked"}
        or response.get("research_performed") not in (False, None)
    ):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    result = dict(response)
    if result["status"] == "blocked":
        if (
            result.get("mutation_performed") is not False
            or not str(result.get("reason") or "").strip()
        ):
            raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
        return result
    returned_used = result.get("used_item_ids")
    allowed_grounded = set(
        retrieved_item_ids if retrieved_item_ids is not None else used_item_ids
    )
    if (
        result.get("response_task_id") != response_task_id
        or not isinstance(returned_used, list)
        or any(
            not re.fullmatch(r"ki_[a-f0-9]{24}", str(item_id or ""))
            for item_id in returned_used
        )
        or not set(returned_used).issubset(allowed_grounded)
        or not str(result.get("provenance_markdown_path") or "").strip()
        or result.get("promotion_performed") is not False
        or result.get("paid_api_used") is not paid_web_research_api_used
    ):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    candidate_id = str(result.get("candidate_review_id") or "")
    if candidate_id and not re.fullmatch(r"inote-[1-9][0-9]*", candidate_id):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    return result


def request_intelligent_outcome(
    *,
    base_url: str,
    token: str,
    command: IntelligentOutcomeCommand,
    retrieval_receipt_id: str,
    item_id: str,
    urlopen: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    response = _post_bridge(
        build_intelligent_outcome_request(
            command,
            retrieval_receipt_id=retrieval_receipt_id,
            item_id=item_id,
        ),
        base_url=str(base_url or "").strip(),
        token=str(token or "").strip(),
        urlopen=urlopen,
    )
    result = _validate_intelligent_action_response(
        response,
        expected_action="record_intelligent_knowledge_outcome",
        allowed_statuses={"recorded", "blocked"},
    )
    if result.get("status") == "blocked":
        if (
            result.get("mutation_performed") is not False
            or not str(result.get("reason") or "").strip()
        ):
            raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
        return result
    if (
        result.get("item_id") != item_id
        or result.get("outcome") != command.outcome
        or not str(result.get("markdown_path") or "").strip()
        or result.get("original_preserved") is not True
    ):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    return result



def render_intelligent_retrieval_context(response: Mapping[str, Any]) -> str:
    records = response.get("records") or []
    if not records:
        return ""
    lines = [
        "[Targeted current/promoted Cogitator knowledge for this API turn]",
        "Use only when relevant. For every item actually used, append one exact "
        "internal line: COGITATOR USED: <item_id>. Do not declare merely retrieved "
        "items. The marker is removed before delivery; the user-facing answer does "
        "not need to expose an internal filesystem path. Hypotheses and experiments "
        "remain explicitly provisional.",
        "If the answer adds a substantive policy refinement, append exactly one "
        "line: PROPOSED REFINEMENT: <item_id> | <addition>. Omit it for wording "
        "changes or when no retrieved item was used. This marker is removed before "
        "delivery and creates only an approval-required candidate.",
    ]
    for record in records:
        lines.extend(
            [
                "",
                (
                    f"- [{record.get('label')} {record.get('item_id')}; "
                    f"{record.get('citation')}] {record.get('title')}"
                ),
                f"  Core idea: {record.get('core_idea')}",
                f"  Why relevant: {record.get('why_relevant')}",
                f"  Plan change: {record.get('plan_delta')}",
            ]
        )
    return "\n".join(lines)


def render_intelligent_review_message(response: Mapping[str, Any]) -> str:
    status = str(response.get("status") or "")
    if status == "blocked":
        return f"Knowledge review blocked: {response.get('reason') or 'not applicable'}."
    candidates = response.get("candidates")
    if isinstance(candidates, list):
        lines = ["Review candidates:"]
        for candidate in candidates[:25]:
            title = " ".join(str(candidate.get("title") or "Untitled").split())
            if len(title) > 120:
                title = f"{title[:117]}..."
            lines.append(
                f"- {candidate.get('review_id')} | "
                f"{candidate.get('disposition') or 'unknown'} | {title}"
            )
        if not candidates:
            lines.append("No review candidates are currently available.")
        elif len(candidates) > 25:
            lines.append(f"- {len(candidates) - 25} more")
        lines.append("PAID API: no")
        return "\n".join(lines)
    detail = response.get("assessment_detail")
    if isinstance(detail, Mapping):
        unavailable = "Not available in persisted assessment"

        def stored(value: Any) -> str:
            if value is None or value == "" or value == []:
                return unavailable
            if isinstance(value, bool):
                return "yes" if value else "no"
            if isinstance(value, list):
                return "; ".join(str(entry) for entry in value) or unavailable
            return str(value)

        related = [
            f"{relation_type} {item_id}"
            for item_id, relation_type in zip(
                detail.get("related_memory_ids") or [],
                detail.get("relation_types") or [],
            )
        ]
        if not related:
            current = str(detail.get("knowledge_item_id") or "")
            for relation in response.get("relations") or []:
                if not isinstance(relation, Mapping):
                    continue
                source = str(relation.get("source_item_id") or "")
                target = str(relation.get("target_item_id") or "")
                other = target if source == current else source
                if other:
                    related.append(
                        f"{relation.get('relation_type') or 'related'} {other}"
                    )
        comparison = stored(detail.get("duplication_status"))
        additions = []
        for label, field in (
            ("reinforcement", "reinforcement"),
            ("refinement", "refinement"),
            ("contradiction", "contradictions"),
        ):
            values = detail.get(field)
            if values:
                additions.append(f"{label}: {stored(values)}")
        if additions:
            comparison = f"{comparison}; {'; '.join(additions)}"
        evidence = stored(detail.get("evidence_quality"))
        confidence = stored(detail.get("confidence"))
        trust = (
            unavailable
            if evidence == unavailable and confidence == unavailable
            else f"{evidence} evidence; confidence {confidence}"
        )
        cost = detail.get("estimated_paid_cost")
        cost_text = f"{float(cost):.4f}" if isinstance(cost, (int, float)) else stored(cost)
        return "\n".join(
            [
                f"VALUE: {stored(detail.get('value'))}",
                f"TYPE: {stored(detail.get('content_type'))}",
                f"CORE IDEA: {stored(detail.get('core_idea'))}",
                f"TRUST: {trust}",
                f"WHY IT MATTERS: {stored(detail.get('why_it_matters'))}",
                f"RELATED KNOWLEDGE: {stored(related)}",
                f"NEW / DUPLICATE / REINFORCEMENT / REFINEMENT / CONTRADICTION: {comparison}",
                f"WHAT IT CHANGES: {stored(detail.get('what_this_changes'))}",
                f"RECOMMENDED ACTION: {stored(detail.get('recommended_action'))}",
                f"SMALLEST TEST: {stored(detail.get('smallest_test'))}",
                f"DO NOT: {stored(detail.get('do_not_do'))}",
                f"LIFECYCLE: {stored(detail.get('lifecycle'))}",
                f"KNOWLEDGE ITEM: {stored(detail.get('knowledge_item_id'))}",
                f"MARKDOWN: {stored(detail.get('markdown_path'))}",
                f"REVIEW ID: {stored(response.get('review_id'))}",
                f"PROVIDER: {stored(detail.get('provider_path'))}",
                f"PAID API: {stored(detail.get('paid_api_used'))} (estimated cost: {cost_text})",
            ]
        )
    if status == "ok" and response.get("lifecycle_state"):
        return "\n".join(
            [
                f"LIFECYCLE: {response['lifecycle_state']}",
                f"KNOWLEDGE ITEM: {response.get('item_id') or 'not available'}",
                f"MARKDOWN: {response.get('markdown_path') or 'not available'}",
                f"REVIEW ID: {response.get('review_id') or 'not available'}",
                "PAID API: no",
            ]
        )
    lines = [f"Knowledge review: {status.replace('_', ' ')}."]
    if response.get("promoted_path"):
        lines.append(f"Promoted Markdown: {response['promoted_path']}")
    if response.get("candidate_review_id"):
        lines.append(f"Review ID: {response['candidate_review_id']}")
    lines.append("PAID API: no")
    return "\n".join(lines)


def render_intelligent_outcome_message(response: Mapping[str, Any]) -> str:
    if response.get("status") == "blocked":
        return f"Knowledge outcome blocked: {response.get('reason') or 'receipt unavailable'}."
    lines = ["Knowledge outcome recorded."]
    if response.get("markdown_path"):
        lines.append(f"Saved to: {response['markdown_path']}")
    if response.get("candidate_review_id"):
        lines.append(f"Correction review ID: {response['candidate_review_id']}")
    lines.append("PAID API: no")
    return "\n".join(lines)


def request_intelligent_synthesis(
    *,
    base_url: str,
    token: str,
    urlopen: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    response = _post_bridge(
        build_intelligent_synthesis_request(),
        base_url=str(base_url or "").strip(),
        token=str(token or "").strip(),
        urlopen=urlopen,
    )
    if not isinstance(response, Mapping):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    if (
        response.get("requested_action") != "build_intelligent_synthesis"
        or response.get("status") != "ok"
        or response.get("paid_api_used") is not False
        or float(response.get("estimated_paid_cost") or 0) != 0
        or response.get("research_performed") not in (False, None)
        or response.get("promotion_performed") not in (False, None)
        or not str(response.get("synthesis") or "").strip()
    ):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID")
    return dict(response)


def render_intelligent_intake_message(response: Mapping[str, Any]) -> str:
    status = str(response.get("status") or "")
    if status == "ok":
        return str(response.get("telegram_message") or "").strip()
    source_failure = response.get("source_failure")
    source_status = (
        str(source_failure.get("status") or "")
        if isinstance(source_failure, Mapping)
        else ""
    )
    source_reason = {
        "auth_session_expired": "The burner X session expired.",
        "login_required": "The burner X session requires login.",
        "forbidden_to_burner": "The source is not visible to the burner account.",
        "source_not_found": "X did not return the requested source.",
        "identity_mismatch": "X returned a different source identity.",
        "partial_source_rejected": "X returned only partial source content.",
        "provider_unavailable": "The authenticated X provider was unavailable.",
    }.get(source_status, "Reliable full source content was unavailable.")
    validation_error = str(response.get("error") or "").strip()
    reason = {
        "failed_source": source_reason,
        "failed_reasoning": (
            "Source was recovered, but the reasoning provider was temporarily "
            "unavailable. The item was preserved for retry."
        ),
        "failed_validation": (
            "The model response could not safely support a core assessment."
            + (f" Field error: {validation_error}" if validation_error else "")
        ),
        "rejected": str(response.get("error") or "The preparation expired."),
    }.get(status, "The intelligent intake path was unavailable.")
    raw_path = str(response.get("raw_path") or "").strip()
    lines = [
        "Intelligent intake could not complete.",
        f"Reason: {reason}",
    ]
    if raw_path:
        lines.append(f"Raw input preserved at: {raw_path}")
    lines.append("No research or promotion was performed.")
    return "\n".join(lines)


def render_intelligent_synthesis_message(response: Mapping[str, Any]) -> str:
    synthesis = str(response.get("synthesis") or "").strip()
    return "\n".join(
        [
            synthesis,
            "",
            f"PROVIDER: {response.get('provider_path') or 'deterministic:no-provider'}",
            "PAID API: no (estimated cost: 0.0000)",
        ]
    )


def _delivery_origin(origin: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(origin, Mapping):
        raise IntakeBridgeError("BRIDGE_ROUTE_INVALID", "origin missing")
    allowed = {"platform", "chat_id", "chat_type", "thread_id", "message_id"}
    if set(origin) - allowed or origin.get("platform") != "telegram":
        raise IntakeBridgeError("BRIDGE_ROUTE_INVALID", "origin invalid")
    chat_id = str(origin.get("chat_id") or "")
    chat_type = str(origin.get("chat_type") or "")
    if not re.fullmatch(r"-?\d{1,20}", chat_id) or chat_type not in {"dm", "group", "channel"}:
        raise IntakeBridgeError("BRIDGE_ROUTE_INVALID", "origin invalid")
    clean = {"platform": "telegram", "chat_id": chat_id, "chat_type": chat_type}
    for field in ("thread_id", "message_id"):
        value = str(origin.get(field) or "")
        if value:
            if not re.fullmatch(r"\d{1,20}", value):
                raise IntakeBridgeError("BRIDGE_ROUTE_INVALID", "origin invalid")
            clean[field] = value
    return clean


def build_source_access_request(
    *, urls: list[str], context_label: str = "", dry_run: bool = False, origin=None
) -> dict[str, Any]:
    """Build the draft-only ``source_access_intake_packet`` bridge packet."""
    context: dict[str, Any] = {"urls": list(urls)}
    if origin is not None:
        context["origin"] = _delivery_origin(origin)
    if str(context_label or "").strip():
        context["context_label"] = str(context_label).strip()
    if dry_run:
        context["dry_run"] = True
    return {
        "source_agent": "hermes",
        "requested_action": "source_access_intake_packet",
        "user_intent": "Fetch full public sources for pasted links and build an intake packet.",
        "content": "",
        "approval_status": "draft_only",
        "risk_level": "low",
        "context": context,
    }


def request_source_access_intake(
    *,
    base_url: str,
    token: str,
    urls: list[str],
    context_label: str = "",
    dry_run: bool = False,
    urlopen: Optional[Callable[..., Any]] = None,
    origin: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """POST a link-list intake and validate the reply fail-closed."""
    if not str(base_url or "").strip():
        raise IntakeBridgeError("BRIDGE_NOT_CONFIGURED", "base_url missing")
    if not str(token or "").strip():
        raise IntakeBridgeError("BRIDGE_TOKEN_MISSING", "token missing")
    urls = [u for u in (str(x or "").strip() for x in urls or []) if u]
    if not urls:
        raise IntakeBridgeError("NO_BODY", "no urls supplied")
    if len(urls) > MAX_LINK_LINES:
        raise IntakeBridgeError("TOO_MANY_LINKS", f"max={MAX_LINK_LINES}")
    packet = build_source_access_request(
        urls=urls, context_label=context_label, dry_run=dry_run, origin=origin)
    response = _post_bridge(packet, base_url=base_url.strip(), token=token.strip(), urlopen=urlopen)
    return _validate_response(response, expected_action="source_access_intake_packet")


def _auto_research_lines(response: Mapping[str, Any]) -> list[str]:
    """Render the flag-gated auto-research verdict block (shared by both
    intake renderers) — research must never run invisibly."""
    auto = response.get("auto_research")
    if not isinstance(auto, Mapping):
        return []
    lines = [""]
    if auto.get("status") == "ok":
        verdict = str(auto.get("verdict") or "unknown")
        lines.append(
            f"{_VERDICT_EMOJI.get(verdict, '🔎')} Auto-research (claim "
            f"{auto.get('claim_number')}): {verdict}")
        lines.append(f"Claim: {auto.get('claim', '')}")
        lines.append(
            f"Evidence: {auto.get('evidence_quality', 'none')} "
            f"(engine: {auto.get('engine', '?')})")
        if auto.get("recommended_action"):
            lines.append(f"Recommended action: {auto['recommended_action']}")
        if auto.get("note_path"):
            lines.append(f"Research note: {auto['note_path']}")
    elif auto.get("status") in ("queued", "duplicate"):
        # async delivery (Cogitator #1008): the job runs in the background and
        # Cogitator pushes the verdict to this chat when it completes.
        lines.append(
            f"🔎 Auto-research (claim {auto.get('claim_number')}): "
            f"job {'already running' if auto.get('status') == 'duplicate' else 'started'}"
            f" — {auto.get('job_id', '?')}")
        lines.append("The result will arrive here when the research completes.")
    else:
        lines.append(
            f"⚠️ Auto-research failed: {auto.get('reason') or 'unknown error'} "
            "(intake itself succeeded)")
    return lines


def render_link_intake_message(response: Mapping[str, Any]) -> str:
    """Render a validated link-intake response: honest per-status counts first."""
    if response.get("status") == "rejected":
        return f"Link intake rejected: {response.get('message') or 'invalid input'}"
    status_counts = response.get("source_status_counts") or {}
    counts = response.get("counts") or {}
    lines = ["🔗 Link intake complete:"]
    for status in ("fetched_full", "needs_full_source", "cookie_required",
                   "fetch_failed", "unsupported", "skipped"):
        if status_counts.get(status):
            lines.append(f"- {status}: {status_counts[status]}")
    lines.append(f"- mined into packet: {response.get('mined_sources', 0)} source(s)")
    if response.get("packet_path"):
        lines += [
            "",
            f"Packet: {response['packet_path']}",
            f"- ideas {counts.get('high_value_ideas', 0)} · claims {counts.get('claims_to_verify', 0)}"
            f" · opportunities {counts.get('opportunities', 0)} · playbooks {counts.get('playbook_candidates', 0)}"
            f" · retrieval {counts.get('retrieval_candidates', 0)} · ignored {counts.get('ignored', 0)}",
        ]
        top = [str(t) for t in (response.get("top_outputs") or [])]
        if top:
            lines.append("Top outputs:")
            lines += [f"{i}. {t}" for i, t in enumerate(top, 1)]
    lines += _auto_research_lines(response)
    if response.get("next_action"):
        lines += ["", f"Next action: {response['next_action']}"]
    if response.get("bundle_path"):
        lines.append(f"Sources bundle: {response['bundle_path']}")
    if response.get("raw_path"):
        lines.append(f"Link list saved: {response['raw_path']}")
    return "\n".join(lines)


def build_intake_request(
    *, raw_text: str, context_label: str = "", dry_run: bool = False, origin=None
) -> dict[str, Any]:
    """Build the draft-only ``intake_review_packet`` bridge packet."""
    context: dict[str, Any] = {"raw_text": str(raw_text or "")}
    if origin is not None:
        context["origin"] = _delivery_origin(origin)
    if str(context_label or "").strip():
        context["context_label"] = str(context_label).strip()
    if dry_run:
        context["dry_run"] = True
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
        raise IntakeBridgeError("BRIDGE_HTTP_ERROR", f"status={exc.code}")
    except urllib.error.URLError as exc:
        raise IntakeBridgeError("BRIDGE_UNREACHABLE", type(exc).__name__)
    except Exception as exc:  # defensive: any transport failure fails closed
        raise IntakeBridgeError("BRIDGE_UNREACHABLE", type(exc).__name__)

    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return json.loads(text)
    except Exception as exc:
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID", type(exc).__name__)


def _validate_response(response: Any, *, expected_action: str) -> dict[str, Any]:
    """Shared fail-closed response validation for both intake actions.

    Enforces: ``status`` ok|rejected, matching action, ``promotion_performed``
    exactly false/absent, and no approval/promotion execution fields.
    ``research_performed`` may be true ONLY when the response carries an
    ``auto_research`` result (Cogitator's flag-gated auto-research of the top
    intake claim); any other researched intake response fails closed.
    """
    if not isinstance(response, Mapping):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID", "response is not an object")
    status = response.get("status")
    if status not in {"ok", "rejected"}:
        raise IntakeBridgeError("BRIDGE_STATUS_NOT_OK", f"status={status!r}")
    if response.get("requested_action") != expected_action:
        raise IntakeBridgeError("BRIDGE_ACTION_MISMATCH", f"action={response.get('requested_action')!r}")
    if response.get("promotion_performed") not in (False, None):
        raise IntakeBridgeError(
            "BRIDGE_STATEFUL_RESPONSE",
            f"promotion_performed={response.get('promotion_performed')!r}")
    researched = response.get("research_performed")
    if researched not in (False, None) and not (
            researched is True and isinstance(response.get("auto_research"), Mapping)):
        raise IntakeBridgeError("BRIDGE_STATEFUL_RESPONSE", f"research_performed={researched!r}")
    auto = response.get("auto_research")
    if isinstance(auto, Mapping) and auto.get("status") in {"queued", "duplicate"}:
        _validate_research_job(auto)
    stateful = [f for f in _FORBIDDEN_RESPONSE_FIELDS if f in response]
    if stateful:
        raise IntakeBridgeError("BRIDGE_STATEFUL_RESPONSE", f"fields={stateful}")
    return dict(response)


def validate_intake_response(response: Any) -> dict[str, Any]:
    """Validate an ``intake_review_packet`` response. Fails closed."""
    return _validate_response(response, expected_action=_REQUESTED_ACTION)


def request_intake_review(
    *,
    base_url: str,
    token: str,
    raw_text: str,
    context_label: str = "",
    dry_run: bool = False,
    urlopen: Optional[Callable[..., Any]] = None,
    origin: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the request, POST it, and validate the reply. Fails closed."""
    if not str(base_url or "").strip():
        raise IntakeBridgeError("BRIDGE_NOT_CONFIGURED", "base_url missing")
    if not str(token or "").strip():
        raise IntakeBridgeError("BRIDGE_TOKEN_MISSING", "token missing")
    if not str(raw_text or "").strip():
        raise IntakeBridgeError("NO_BODY", "no raw text supplied")
    if len(raw_text) > MAX_BODY_CHARS:
        raise IntakeBridgeError("BODY_TOO_LARGE", f"max={MAX_BODY_CHARS}")
    packet = build_intake_request(
        raw_text=raw_text, context_label=context_label, dry_run=dry_run, origin=origin)
    response = _post_bridge(packet, base_url=base_url.strip(), token=token.strip(), urlopen=urlopen)
    return validate_intake_response(response)


def build_intake_research_request(
    *, packet_path: str, item_number: int, dry_run: bool = False, origin=None
) -> dict[str, Any]:
    """Build the draft-only ``research_intake_item`` bridge packet."""
    context: dict[str, Any] = {"packet_path": str(packet_path), "item_number": int(item_number)}
    if origin is not None:
        context["origin"] = _delivery_origin(origin)
    if dry_run:
        context["dry_run"] = True
    return {
        "source_agent": "hermes",
        "requested_action": "research_intake_item",
        "user_intent": "Bounded research on one selected intake-packet claim.",
        "content": "",
        "approval_status": "draft_only",
        "risk_level": "low",
        "context": context,
    }


def validate_intake_research_response(response: Any) -> dict[str, Any]:
    """Fail-closed validation for the research verb: research is EXPECTED here,
    but promotion/approval must not have happened."""
    if not isinstance(response, Mapping):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID", "response is not an object")
    status = response.get("status")
    if status not in {"ok", "rejected"}:
        raise IntakeBridgeError("BRIDGE_STATUS_NOT_OK", f"status={status!r}")
    if response.get("requested_action") != "research_intake_item":
        raise IntakeBridgeError("BRIDGE_ACTION_MISMATCH", f"action={response.get('requested_action')!r}")
    if response.get("promotion_performed") not in (False, None):
        raise IntakeBridgeError("BRIDGE_STATEFUL_RESPONSE",
                                f"promotion_performed={response.get('promotion_performed')!r}")
    job = response.get("research_job")
    if job is not None:
        _validate_research_job(job)
        if response.get("research_performed") not in (False, None):
            raise IntakeBridgeError(
                "BRIDGE_STATEFUL_RESPONSE",
                f"research_performed={response.get('research_performed')!r}")
    stateful = [f for f in _FORBIDDEN_RESPONSE_FIELDS if f in response]
    if stateful:
        raise IntakeBridgeError("BRIDGE_STATEFUL_RESPONSE", f"fields={stateful}")
    return dict(response)


def _validate_research_job(job: Any) -> None:
    """Validate the bounded async receipt before Hermes tells Cal it started."""
    if not isinstance(job, Mapping):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID", "research job is not an object")
    if job.get("status") not in {"queued", "duplicate"}:
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID", "invalid research job status")
    job_id = job.get("job_id")
    claim = job.get("claim")
    claim_number = job.get("claim_number")
    if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 200:
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID", "invalid research job id")
    if not isinstance(claim, str) or not claim.strip():
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID", "invalid research claim")
    if (
        not isinstance(claim_number, int)
        or isinstance(claim_number, bool)
        or claim_number < 1
    ):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID", "invalid research claim number")


def request_intake_research(
    *,
    base_url: str,
    token: str,
    packet_path: str,
    item_number: int,
    dry_run: bool = False,
    urlopen: Optional[Callable[..., Any]] = None,
    origin: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """POST one bounded research request and validate the reply fail-closed."""
    if not str(base_url or "").strip():
        raise IntakeBridgeError("BRIDGE_NOT_CONFIGURED", "base_url missing")
    if not str(token or "").strip():
        raise IntakeBridgeError("BRIDGE_TOKEN_MISSING", "token missing")
    if not str(packet_path or "").strip():
        raise IntakeBridgeError("NO_PACKET", "packet_path missing")
    packet = build_intake_research_request(
        packet_path=packet_path, item_number=item_number, dry_run=dry_run,
        origin=origin)
    response = _post_bridge(packet, base_url=base_url.strip(), token=token.strip(),
                            urlopen=urlopen)
    return validate_intake_research_response(response)

def _request_research_delivery_action(
    *, base_url: str, token: str, action: str, context: dict,
    approval_status: str = "draft_only", urlopen=None,
) -> dict[str, Any]:
    packet = {
        "source_agent": "hermes", "requested_action": action,
        "user_intent": "Deliver one completed async research result.",
        "content": "", "approval_status": approval_status,
        "risk_level": "low", "context": context,
    }
    response = _post_bridge(
        packet, base_url=str(base_url or "").strip(),
        token=str(token or "").strip(), urlopen=urlopen)
    if not isinstance(response, Mapping) or response.get("status") not in {"ok", "rejected"}:
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID", "delivery response invalid")
    if response.get("requested_action") != action:
        raise IntakeBridgeError("BRIDGE_ACTION_MISMATCH", "delivery action mismatch")
    return dict(response)


def claim_research_delivery(*, base_url: str, token: str, worker_id: str, urlopen=None) -> dict | None:
    response = _request_research_delivery_action(
        base_url=base_url, token=token, action="claim_research_delivery",
        context={"worker_id": str(worker_id or "")[:100]}, urlopen=urlopen)
    delivery = response.get("delivery")
    if delivery is None:
        return None
    if not isinstance(delivery, Mapping):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID", "delivery claim invalid")
    required = ("job_id", "message", "origin", "lease_token", "version", "attempts")
    if any(key not in delivery for key in required):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID", "delivery claim incomplete")
    job_id = str(delivery.get("job_id") or "")
    message = str(delivery.get("message") or "")
    lease = str(delivery.get("lease_token") or "")
    if not job_id or len(job_id) > 200 or not message or len(message) > 4000:
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID", "delivery claim invalid")
    if not re.fullmatch(r"[a-f0-9]{32}", lease):
        raise IntakeBridgeError("BRIDGE_RESPONSE_INVALID", "delivery lease invalid")
    clean = dict(delivery)
    clean["origin"] = _delivery_origin(delivery.get("origin"))
    clean["version"] = int(delivery["version"])
    clean["attempts"] = int(delivery["attempts"])
    return clean


def ack_research_delivery(
    *, base_url: str, token: str, job_id: str, lease_token: str,
    expected_version: int, outcome: str, failure_category: str = "",
    remote_message_id: str = "", urlopen=None,
) -> dict:
    context = {
        "job_id": job_id, "lease_token": lease_token,
        "expected_version": int(expected_version), "outcome": outcome,
    }
    if failure_category:
        context["failure_category"] = failure_category
    if remote_message_id:
        context["remote_message_id"] = remote_message_id
    return _request_research_delivery_action(
        base_url=base_url, token=token, action="ack_research_delivery",
        context=context, urlopen=urlopen)


def get_research_delivery_status(
    *, base_url: str, token: str, job_id: str, urlopen=None
) -> dict:
    return _request_research_delivery_action(
        base_url=base_url, token=token, action="get_research_delivery_status",
        context={"job_id": job_id}, urlopen=urlopen)


def requeue_research_delivery(
    *, base_url: str, token: str, job_id: str, expected_version: int,
    confirm: bool, urlopen=None,
) -> dict:
    return _request_research_delivery_action(
        base_url=base_url, token=token, action="requeue_research_delivery",
        approval_status="approved", context={
            "job_id": job_id, "expected_version": int(expected_version),
            "confirm": bool(confirm),
        }, urlopen=urlopen)



_VERDICT_EMOJI = {
    "verified_enough": "✅",
    "plausible_unverified": "🟡",
    "contradicted": "❌",
    "needs_full_source": "🔒",
    "needs_more_evidence": "❓",
    "ignore_low_value": "🗑️",
}


def render_intake_research_message(response: Mapping[str, Any]) -> str:
    """Render a validated research response: verdict first, provenance visible.
    An async job-start response (Cogitator #1008) renders as a job receipt —
    the verdict is pushed to this chat by Cogitator when the job completes."""
    if response.get("status") == "rejected":
        return f"Research rejected: {response.get('message') or 'invalid selection'}"
    job = response.get("research_job")
    if isinstance(job, Mapping):
        started = "already queued/running" if job.get("status") == "duplicate" else "started"
        return "\n".join([
            f"🔎 Research job {started}: {job.get('job_id', '?')}",
            f"Claim: {job.get('claim', '')}",
            "The result will arrive here when the research completes.",
        ])
    verdict = str(response.get("verdict") or "unknown")
    lines = [
        f"{_VERDICT_EMOJI.get(verdict, '🔎')} Research verdict: {verdict}",
        f"Claim: {response.get('claim', '')}",
        f"Evidence quality: {response.get('evidence_quality', 'none')}",
    ]
    sources = response.get("sources_used") or []
    if sources:
        lines.append("")
        lines.append("Sources consulted:")
        for s in sources[:5]:
            lines.append(f"- [{s.get('stance', '?')}/{s.get('evidence_type', 'none')}] {s.get('url', '')}")
    missing = [str(m) for m in (response.get("missing_evidence") or [])]
    if missing:
        lines.append("")
        lines.append("Missing evidence:")
        lines += [f"- {m}" for m in missing[:4]]
    if response.get("recommended_action"):
        lines += ["", f"Recommended action: {response['recommended_action']}"]
    if response.get("note_path"):
        lines.append(f"Research note: {response['note_path']}")
    return "\n".join(lines)


def intake_help_text() -> str:
    """Compact usage shown for an attempted-but-malformed intake message."""
    return (
        "📥 intake — turn one raw dump into a reviewable intake packet.\n"
        "First line is the command, everything below is preserved verbatim:\n"
        "\n"
        "intake\n"
        "<pasted messy material — posts, notes, links as text>\n"
        "\n"
        "Optional provenance lens (letters/digits/space/dash, ≤60 chars):\n"
        "intake lens gpu-store\n"
        "<pasted material>\n"
        "\n"
        f"One message, up to {MAX_BODY_CHARS} characters. The lens is provenance "
        "only — it never forces classification. Nothing is researched, promoted, "
        "or approved."
    )


def save_local_copies(response: Mapping[str, Any], base_dir: str) -> list[str]:
    """Persist markdown content returned by the bridge to durable local disk.

    Cogitator's container storage is ephemeral; the bridge responses carry the
    full packet/bundle/note markdown so this host keeps the durable copy.
    Filenames are basename-only (no traversal); missing fields are skipped.
    Returns the paths written.
    """
    from pathlib import Path

    saved: list[str] = []
    for path_key, content_key, subdir in (
        ("packet_path", "packet_markdown", "intake/packets"),
        ("bundle_path", "bundle_markdown", "intake/extracted"),
        ("note_path", "note_markdown", "research_notes"),
    ):
        name = Path(str(response.get(path_key) or "")).name
        content = str(response.get(content_key) or "")
        if not name or not name.endswith(".md") or not content:
            continue
        target = Path(base_dir).expanduser() / subdir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        saved.append(str(target))
    return saved


def render_intake_message(response: Mapping[str, Any]) -> str:
    """Render a validated intake response into a compact chat summary."""
    if response.get("status") == "rejected":
        return f"Intake rejected: {response.get('message') or 'invalid input'}"

    counts = response.get("counts") or {}
    domains = [str(d) for d in (response.get("detected_domains") or [])]
    top = [str(t) for t in (response.get("top_outputs") or [])]
    dry = bool(response.get("dry_run"))
    lines = [
        "📥 Intake packet preview (dry run — nothing saved):" if dry else "📥 Intake packet created:",
        f"- high-value ideas: {counts.get('high_value_ideas', 0)}",
        f"- claims to verify: {counts.get('claims_to_verify', 0)}",
        f"- business/action opportunities: {counts.get('opportunities', 0)}",
        f"- playbook candidates: {counts.get('playbook_candidates', 0)}",
        f"- retrieval candidates: {counts.get('retrieval_candidates', 0)}",
        f"- ignored/low-value: {counts.get('ignored', 0)}",
    ]
    if domains:
        lines.append(f"- detected domains: {', '.join(domains[:5])}")
    if top:
        lines.append("")
        lines.append("Top outputs:")
        lines += [f"{i}. {t}" for i, t in enumerate(top, 1)]
    targets = response.get("research_targets") or []
    if targets:
        lines.append("")
        lines.append("Research targets (reply `intake research <n>`):")
        for t in targets:
            lines.append(f"{t.get('n')}. {t.get('claim', '')}")
    if response.get("next_action"):
        lines.append("")
        lines.append(f"Next action: {response['next_action']}")
    lines += _auto_research_lines(response)
    if not dry:
        lines.append("")
        lines.append(f"Raw saved: {response.get('raw_path', '')}")
        lines.append(f"Packet: {response.get('packet_path', '')}")
    return "\n".join(lines)


__all__ = [
    "IntakeBridgeError",
    "IntakeCommand",
    "TOKEN_ENV",
    "BRIDGE_PATH",
    "MAX_BODY_CHARS",
    "MAX_LENS_CHARS",
    "parse_intake_message",
    "build_intake_request",
    "request_intake_review",
    "validate_intake_response",
    "render_intake_message",
    "save_local_copies",
    "intake_help_text",
]

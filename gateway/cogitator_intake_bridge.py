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


def build_source_access_request(
    *, urls: list[str], context_label: str = "", dry_run: bool = False
) -> dict[str, Any]:
    """Build the draft-only ``source_access_intake_packet`` bridge packet."""
    context: dict[str, Any] = {"urls": list(urls)}
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
    packet = build_source_access_request(urls=urls, context_label=context_label, dry_run=dry_run)
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
    *, raw_text: str, context_label: str = "", dry_run: bool = False
) -> dict[str, Any]:
    """Build the draft-only ``intake_review_packet`` bridge packet."""
    context: dict[str, Any] = {"raw_text": str(raw_text or "")}
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
    packet = build_intake_request(raw_text=raw_text, context_label=context_label, dry_run=dry_run)
    response = _post_bridge(packet, base_url=base_url.strip(), token=token.strip(), urlopen=urlopen)
    return validate_intake_response(response)


def build_intake_research_request(
    *, packet_path: str, item_number: int, dry_run: bool = False
) -> dict[str, Any]:
    """Build the draft-only ``research_intake_item`` bridge packet."""
    context: dict[str, Any] = {"packet_path": str(packet_path), "item_number": int(item_number)}
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
    stateful = [f for f in _FORBIDDEN_RESPONSE_FIELDS if f in response]
    if stateful:
        raise IntakeBridgeError("BRIDGE_STATEFUL_RESPONSE", f"fields={stateful}")
    return dict(response)


def request_intake_research(
    *,
    base_url: str,
    token: str,
    packet_path: str,
    item_number: int,
    dry_run: bool = False,
    urlopen: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """POST one bounded research request and validate the reply fail-closed."""
    if not str(base_url or "").strip():
        raise IntakeBridgeError("BRIDGE_NOT_CONFIGURED", "base_url missing")
    if not str(token or "").strip():
        raise IntakeBridgeError("BRIDGE_TOKEN_MISSING", "token missing")
    if not str(packet_path or "").strip():
        raise IntakeBridgeError("NO_PACKET", "packet_path missing")
    packet = build_intake_research_request(
        packet_path=packet_path, item_number=item_number, dry_run=dry_run)
    response = _post_bridge(packet, base_url=base_url.strip(), token=token.strip(),
                            urlopen=urlopen)
    return validate_intake_research_response(response)


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

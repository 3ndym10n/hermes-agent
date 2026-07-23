#!/usr/bin/env python3
"""Bounded, private Linxio SENT-mail writing-style bootstrap."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import secrets
import stat
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Mapping, cast
from zoneinfo import ZoneInfo

import email_learning
import google_api
from google_auth import ensure_private_directory, private_state_path

TIMEZONE = "Australia/Brisbane"
LABEL = "SENT"
PROCESSING_VERSION = "linxio-sent-style-v1"
MAX_MESSAGES = 2_000
BATCH_SIZE = 50
MAX_LIST_MESSAGES = 10_000
MAX_LIST_PAGES = 20
MAX_MIME_PARTS = 100
MAX_MIME_DEPTH = 12
MAX_DECODED_BYTES = 250_000
MAX_ANALYSIS_CHARS = 8_000
MAX_SOURCE_REFS = 4
MAX_PROFILE_PATTERNS = 16
STATE_TTL_SECONDS = 7 * 24 * 60 * 60
APPROVAL_TTL_SECONDS = 15 * 60
MAX_STATE_BYTES = 4_000_000
RECORD_CONFIRM = "RECORD-APPROVED-SENT-STYLE"
STATE_DIR = private_state_path("linxio_sent_style")

EXCLUSION_REASONS = (
    "internal_only", "recipient_scope_unknown", "automated", "machine_generated",
    "empty_acknowledgement",
    "too_little_authored_text", "duplicate", "near_duplicate_template",
    "missing_body", "unsafe_mime", "not_sent",
)
CATEGORIES = (
    "initial_outreach", "follow_up", "proposal_quote", "pricing",
    "product_explanation", "installation", "information_request",
    "objection_handling", "scheduling", "deal_progression",
    "customer_support", "closing_next_step", "other",
)
PATTERNS = {
    "warm_direct_tone": ("tone_voice", "tone", "Use a warm, direct tone."),
    "formal_direct_tone": ("tone_voice", "tone", "Use a formal, direct tone."),
    "concise_email": ("email_length", "length", "Keep the email concise."),
    "detailed_when_needed": (
        "email_length", "length", "Include detail when the decision requires it.",
    ),
    "short_paragraphs": ("paragraph_structure", "formatting", "Use short paragraphs."),
    "contextual_paragraphs": (
        "paragraph_structure", "formatting",
        "Use fuller paragraphs when context is complex.",
    ),
    "brief_greeting": ("greeting", "greeting", "Use a brief greeting."),
    "brief_closing": ("closing", "closing", "Use a brief closing."),
    "structured_quote": (
        "proposal_quote", "proposal_quote",
        "Structure quotes around scope, price, terms, and next step.",
    ),
    "clear_follow_up": (
        "follow_up", "follow_up", "State the follow-up purpose and one clear next step.",
    ),
    "group_information_requests": (
        "information_request", "information_request",
        "Group information requests into a short checklist.",
    ),
    "explain_pricing_basis": (
        "pricing", "pricing", "Explain the pricing basis without inventing commercial facts.",
    ),
    "separate_payment_terms": (
        "payment_terms", "payment_terms",
        "Keep approved payment terms separate from style guidance.",
    ),
    "explain_installation_sequence": (
        "installation", "installation",
        "Explain the approved installation sequence clearly.",
    ),
    "acknowledge_objection": (
        "objection_handling", "objection_handling",
        "Acknowledge the objection before answering it.",
    ),
    "single_clear_call_to_action": (
        "call_to_action", "call_to_action", "End with one clear call to action.",
    ),
}
CONFLICTS = {
    "warm_direct_tone": "formal_direct_tone",
    "formal_direct_tone": "warm_direct_tone",
    "concise_email": "detailed_when_needed",
    "detailed_when_needed": "concise_email",
    "short_paragraphs": "contextual_paragraphs",
    "contextual_paragraphs": "short_paragraphs",
}

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d(). -]{6,}\d)(?!\w)")
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_AUTH_RE = re.compile(
    r"(?i)\b(?:authorization|bearer|oauth|access[_ -]?token|refresh[_ -]?token|"
    r"password|api[_ -]?key|account|invoice|payment)\b\s*(?::|=)?\s*\S*"
)
_IDENTIFIER_RE = re.compile(r"\b(?:[A-F0-9]{12,}|\d{8,})\b", re.I)
_QUOTE_CUTOFF_RE = re.compile(
    r"(?im)^(?:on .{0,200} wrote:|from:\s|-{2,}\s*original message\s*-{2,}|"
    r"begin forwarded message:|forwarded message)"
)
_SIGNATURE_CUTOFF_RE = re.compile(
    r"(?im)^(?:--\s*$|sent from my |confidential(?:ity)? notice|"
    r"this (?:email|message) and any attachments|unsubscribe|manage preferences|"
    r"begin:vcalendar)"
)
_AUTOMATED_RE = re.compile(
    r"(?i)\b(?:do not reply|no[- ]?reply|automated (?:message|notification)|"
    r"receipt|invoice attached|system report|delivery status notification|"
    r"calendar invitation)\b"
)
_EMPTY_ACK_RE = re.compile(
    r"(?i)^(?:thanks|thank you|noted|received|got it|okay|ok|sounds good|"
    r"will do|perfect|great)[.! ]*$"
)


class SentStyleError(RuntimeError):
    """Fail-closed workflow error."""


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha(value: object) -> str:
    payload = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _opaque(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not _ID_RE.fullmatch(text):
        raise SentStyleError(f"invalid {label}")
    return text


def _state_root() -> Path:
    if STATE_DIR.is_symlink():
        raise SentStyleError("private state directory is unsafe")
    try:
        ensure_private_directory(STATE_DIR)
    except (OSError, ValueError) as exc:
        raise SentStyleError("private state directory is unsafe") from exc
    return STATE_DIR.resolve()


def _state_path(job_id: str) -> Path:
    return _state_root() / f"{_opaque(job_id, 'job id')}.json"


def _integrity(state: Mapping) -> str:
    return _sha({key: value for key, value in state.items() if key != "state_integrity"})


def _write_state(state: dict) -> None:
    state = dict(state)
    state["state_integrity"] = _integrity(state)
    path = _state_path(state["job_id"])
    if path.is_symlink():
        raise SentStyleError("private state path is unsafe")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(state, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def _load_state(job_id: str) -> dict:
    path = _state_path(job_id)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(path, flags)
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                or metadata.st_size > MAX_STATE_BYTES):
            raise SentStyleError("private job state is unsafe")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            state = json.load(stream)
    except SentStyleError:
        raise
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SentStyleError("job state is absent or invalid") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if (not isinstance(state, dict) or state.get("job_id") != job_id
            or state.get("state_integrity") != _integrity(state)):
        raise SentStyleError("job state integrity check failed")
    if (state.get("status") != "cancelled"
            and (not isinstance(state.get("plan"), dict)
                 or state.get("plan_fingerprint") != _sha(state["plan"]))):
        raise SentStyleError("approved plan binding check failed")
    if (state.get("status") in {"running", "complete", "previewed", "recorded"}
            and state.get("approved_plan_fingerprint") != state.get("plan_fingerprint")):
        raise SentStyleError("approved plan binding check failed")
    if float(state.get("expires_at", 0)) < time.time():
        raise SentStyleError("job state is expired")
    return state


@contextmanager
def _job_lock(job_id: str):
    path = _state_root() / f"{_opaque(job_id, 'job id')}.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise SentStyleError("job lock is unsafe")
        os.ftruncate(fd, 1)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SentStyleError("job is already running") from exc
        yield
    finally:
        os.close(fd)


def _execute(request):
    for attempt in range(3):
        try:
            return request.execute()
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if attempt == 2 or (status is not None and status != 429 and status < 500):
                raise SentStyleError("safe Gmail read failed") from exc
            time.sleep(0.1 * (2 ** attempt))
    raise AssertionError("unreachable")


def _profile(service) -> tuple[str, str]:
    profile = _execute(service.users().getProfile(userId="me"))
    email_address = str(profile.get("emailAddress") or "").strip().lower()
    if not _EMAIL_RE.fullmatch(email_address):
        raise SentStyleError("connected Gmail account could not be verified")
    return email_address, _sha(email_address)


def _default_start(today: date) -> date:
    try:
        return today.replace(year=today.year - 1)
    except ValueError:
        return today.replace(year=today.year - 1, day=28)


def date_boundaries(start_value: str = "", end_value: str = "",
                    *, now: datetime | None = None) -> dict:
    zone = ZoneInfo(TIMEZONE)
    local_today = (now or datetime.now(zone)).astimezone(zone).date()
    try:
        start_day = date.fromisoformat(start_value) if start_value else _default_start(local_today)
        end_day = date.fromisoformat(end_value) if end_value else local_today
    except ValueError as exc:
        raise SentStyleError("dates must use YYYY-MM-DD") from exc
    if start_day > end_day:
        raise SentStyleError("start date must not be after end date")
    start_local = datetime.combine(start_day, datetime_time.min, zone)
    end_exclusive = datetime.combine(end_day + timedelta(days=1), datetime_time.min, zone)
    return {
        "timezone": TIMEZONE, "start_date": start_day.isoformat(),
        "end_date_inclusive": end_day.isoformat(),
        "start_local": start_local.isoformat(),
        "end_local_exclusive": end_exclusive.isoformat(),
        "start_utc": start_local.astimezone(timezone.utc).isoformat(),
        "end_utc_exclusive": end_exclusive.astimezone(timezone.utc).isoformat(),
        "start_epoch": int(start_local.timestamp()),
        "end_epoch_exclusive": int(end_exclusive.timestamp()),
    }


def _list_ids(service, query: str) -> list[str]:
    ids, page_token = [], None
    for _page in range(MAX_LIST_PAGES):
        kwargs = {
            "userId": "me", "labelIds": [LABEL], "q": query, "maxResults": 500,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        result = _execute(service.users().messages().list(**kwargs))
        ids.extend(_opaque(item.get("id"), "message id") for item in result.get("messages", []))
        if len(ids) > MAX_LIST_MESSAGES:
            raise SentStyleError("range is too large to count safely; use chronological sub-ranges")
        page_token = result.get("nextPageToken")
        if not page_token:
            if len(set(ids)) != len(ids):
                raise SentStyleError("Gmail pagination returned duplicate message ids")
            return ids
    raise SentStyleError("Gmail pagination exceeded the bounded plan limit")


def _headers(message: Mapping) -> dict[str, str]:
    return {
        str(item.get("name") or "").lower(): str(item.get("value") or "")
        for item in message.get("payload", {}).get("headers", [])
        if item.get("name")
    }


def _recipient_domains(headers: Mapping[str, str]) -> set[str]:
    addresses = getaddresses([
        value for key in ("to", "cc", "bcc") if (value := headers.get(key, ""))
    ])
    return {
        address.rsplit("@", 1)[1].lower()
        for _name, address in addresses if "@" in address
    }


def _metadata_exclusion(headers: Mapping[str, str], own_domain: str,
                        exclude_internal: bool) -> str:
    domains = _recipient_domains(headers)
    if not domains:
        return "recipient_scope_unknown"
    if exclude_internal and domains and domains <= {own_domain}:
        return "internal_only"
    auto = headers.get("auto-submitted", "").lower()
    precedence = headers.get("precedence", "").lower()
    if ((auto and auto != "no") or precedence in {"bulk", "junk", "list"}
            or headers.get("list-unsubscribe")):
        return "automated"
    return ""


def _message_metadata(service, message_id: str) -> dict:
    message = _execute(service.users().messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["To", "Cc", "Bcc", "Auto-Submitted", "Precedence", "List-Unsubscribe"],
    ))
    response_id = _opaque(message.get("id"), "message id")
    if response_id != message_id:
        raise SentStyleError("Gmail metadata response id changed")
    return {
        "id": response_id,
        "internal_date": int(message.get("internalDate") or 0),
        "headers": _headers(message),
    }


def cmd_plan(args) -> None:
    boundaries = date_boundaries(args.start, args.end)
    query = f"after:{boundaries['start_epoch'] - 1} before:{boundaries['end_epoch_exclusive']}"
    service = google_api.build_service("gmail", "v1")
    account, account_fingerprint = _profile(service)
    own_domain = account.rsplit("@", 1)[1]
    ids = _list_ids(service, query)
    if len(ids) > MAX_MESSAGES:
        print(json.dumps({
            "status": "range_too_large", "verified_connected_account": account,
            **boundaries, "gmail_label": LABEL, "gmail_query": query,
            "total_sent_message_count": len(ids), "maximum_message_cap": MAX_MESSAGES,
            "required_action": "choose explicit chronological sub-ranges",
        }))
        return
    metadata = [_message_metadata(service, message_id) for message_id in ids]
    if any(not boundaries["start_epoch"] * 1_000 <= item["internal_date"]
           < boundaries["end_epoch_exclusive"] * 1_000 for item in metadata):
        raise SentStyleError("Gmail returned a message outside the requested date range")
    metadata.sort(key=lambda item: (item["internal_date"], item["id"]))
    exclusions = Counter(
        reason for item in metadata
        if (reason := _metadata_exclusion(item["headers"], own_domain, not args.include_internal))
    )
    ordered_ids = [item["id"] for item in metadata]
    plan = {
        **boundaries, "query": query, "label": LABEL, "message_cap": MAX_MESSAGES,
        "batch_size": BATCH_SIZE, "exclude_internal": not args.include_internal,
        "processing_version": PROCESSING_VERSION,
        "account_fingerprint": account_fingerprint,
        "total_found": len(ordered_ids),
        "eligible_estimate": len(ordered_ids) - sum(exclusions.values()),
        "batch_count": (len(ordered_ids) + BATCH_SIZE - 1) // BATCH_SIZE,
        "message_ids": ordered_ids, "message_snapshot": _sha(ordered_ids),
    }
    plan_fingerprint = _sha(plan)
    token, job_id, now = secrets.token_urlsafe(24), secrets.token_urlsafe(18), time.time()
    state = {
        "job_id": job_id, "status": "planned", "plan": plan,
        "plan_fingerprint": plan_fingerprint, "approval_token_sha256": _sha(token),
        "approval_expires_at": now + APPROVAL_TTL_SECONDS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "expires_at": now + STATE_TTL_SECONDS, "processed_ids": [],
        "batch_number": 0, "included_count": 0, "excluded_counts": {},
        "category_counts": {}, "patterns": {}, "seen_exact": [], "seen_simhash": [],
    }
    _write_state(state)
    print(json.dumps({
        "status": "approval_required", "job_id": job_id,
        "approval_token": token, "plan_fingerprint": plan_fingerprint,
        "approval_expires_in_seconds": APPROVAL_TTL_SECONDS,
        "verified_connected_account": account,
        **boundaries, "gmail_label": LABEL, "gmail_query": query,
        "total_sent_message_count": len(ordered_ids),
        "estimated_eligible_count": plan["eligible_estimate"],
        "maximum_message_cap": MAX_MESSAGES, "batch_size": BATCH_SIZE,
        "batch_count": plan["batch_count"],
        "exclusions": list(EXCLUSION_REASONS),
        "internal_only_excluded": plan["exclude_internal"],
    }))


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0
        self.stack: list[tuple[str, bool]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        classes = str(dict(attrs).get("class") or "").lower().split()
        starts_ignored = tag in {"script", "style", "blockquote"} or bool(
            {"gmail_quote", "yahoo_quoted"} & set(classes)
        )
        if tag not in {"br", "img", "hr", "meta", "link", "input"}:
            self.stack.append((tag, starts_ignored))
        if starts_ignored:
            self.ignored += 1
        elif not self.ignored and tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        starts_ignored = self.stack.pop()[1] if self.stack else False
        was_ignored = bool(self.ignored)
        if starts_ignored and self.ignored:
            self.ignored -= 1
        if not was_ignored and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if not self.ignored and tag in {"br", "hr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)


def _html_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    return html.unescape("".join(parser.parts))


def extract_message_body(message: Mapping) -> str:
    candidates: list[tuple[str, str]] = []
    total = parts = 0

    def walk(part: Mapping, depth: int) -> None:
        nonlocal total, parts
        parts += 1
        if depth > MAX_MIME_DEPTH or parts > MAX_MIME_PARTS:
            raise SentStyleError("unsafe MIME nesting")
        disposition = next((
            str(item.get("value") or "").lower()
            for item in part.get("headers", []) if str(item.get("name") or "").lower() == "content-disposition"
        ), "")
        mime_type = str(part.get("mimeType") or "").lower()
        if not part.get("filename") and "attachment" not in disposition and mime_type in {"text/plain", "text/html", ""}:
            encoded = str(part.get("body", {}).get("data") or "")
            if encoded:
                if len(encoded) > (MAX_DECODED_BYTES * 4 // 3) + 4:
                    raise SentStyleError("Gmail message body is too large")
                try:
                    padded = encoded + ("=" * (-len(encoded) % 4))
                    decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
                except (ValueError, TypeError) as exc:
                    raise SentStyleError("invalid Gmail MIME body") from exc
                total += len(decoded)
                if total > MAX_DECODED_BYTES:
                    raise SentStyleError("Gmail message body is too large")
                candidates.append((mime_type, decoded.decode("utf-8", errors="replace")))
        for child in part.get("parts", []) or []:
            if not isinstance(child, Mapping):
                raise SentStyleError("invalid Gmail MIME part")
            walk(child, depth + 1)

    payload = message.get("payload", {})
    if not isinstance(payload, Mapping):
        raise SentStyleError("invalid Gmail MIME payload")
    walk(payload, 0)
    plain = "\n".join(value for mime, value in candidates if mime in {"text/plain", ""}).strip()
    if plain:
        return plain
    return "\n".join(_html_text(value) for mime, value in candidates if mime == "text/html").strip()


def isolate_authored_text(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    cutoffs = [
        match.start() for pattern in (_QUOTE_CUTOFF_RE, _SIGNATURE_CUTOFF_RE)
        if (match := pattern.search(text))
    ]
    if cutoffs:
        text = text[:min(cutoffs)]
    lines = [
        line.strip() for line in text.splitlines()
        if not line.lstrip().startswith(">") and not line.strip().startswith(("cid:", "data:image/"))
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines).strip())[:MAX_ANALYSIS_CHARS]


def sanitize_text(value: str, private_names: Iterable[str] = ()) -> str:
    text = value
    names: list[str] = list({item.strip() for item in private_names if len(item.strip()) >= 3})
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(re.escape(str(name)), "[NAME]", text, flags=re.I)
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _PHONE_RE.sub("[PHONE]", text)
    text = _URL_RE.sub("[URL]", text)
    text = _AUTH_RE.sub("[PRIVATE]", text)
    text = _IDENTIFIER_RE.sub("[ID]", text)
    return text[:MAX_ANALYSIS_CHARS]


def _simhash(text: str) -> int:
    words = re.findall(r"[a-z]+", text.lower())
    features = [" ".join(words[index:index + 3]) for index in range(max(1, len(words) - 2))]
    vector = [0] * 64
    for feature in features:
        value = int(hashlib.sha256(feature.encode()).hexdigest()[:16], 16)
        for bit in range(64):
            vector[bit] += 1 if value & (1 << bit) else -1
    return sum(1 << bit for bit, score in enumerate(vector) if score >= 0)


def _category(subject: str, body: str) -> str:
    text = f"{subject} {body}".lower()
    table = (
        ("proposal_quote", ("quote", "proposal", "scope of work")),
        ("pricing", ("price", "pricing", "cost", "rate")),
        ("installation", ("install", "installation", "technician")),
        ("information_request", ("could you provide", "please send", "need the following")),
        ("objection_handling", ("concern", "however", "understand your", "objection")),
        ("scheduling", ("meeting", "calendar", "available", "schedule")),
        ("follow_up", ("follow up", "following up", "checking in", "touching base")),
        ("product_explanation", ("feature", "product", "platform", "works by")),
        ("customer_support", ("support", "issue", "problem", "fix")),
        ("deal_progression", ("next stage", "proceed", "agreement", "onboarding")),
        ("closing_next_step", ("next step", "ready to", "confirm by")),
        ("initial_outreach", ("introduction", "reaching out", "noticed that")),
    )
    return next((category for category, terms in table if any(term in text for term in terms)), "other")


def deterministic_features(body: str, category: str) -> dict:
    words = re.findall(r"\b[\w']+\b", body)
    sentences = [item for item in re.split(r"(?<=[.!?])\s+", body) if item.strip()]
    paragraphs = [item for item in re.split(r"\n\s*\n", body) if item.strip()]
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    first, tail = (lines[0].lower() if lines else ""), " ".join(lines[-3:]).lower()
    greeting = bool(re.match(r"^(?:hi|hello|hey|good (?:morning|afternoon|evening))\b", first))
    closing = bool(re.search(r"\b(?:thanks|thank you|kind regards|regards|cheers)\b", tail))
    bullets = sum(bool(re.match(r"^(?:[-*•]|\d+[.)])\s+", line)) for line in lines)
    questions = body.count("?")
    cta_re = re.compile(
        r"(?i)\b(?:please (?:confirm|send|let me know)|can you|could you|"
        r"let me know|book (?:a )?(?:call|meeting)|next step)\b"
    )
    ctas = [sentence for sentence in sentences if cta_re.search(sentence)]
    lower = body.lower()
    pricing = any(term in lower for term in ("price", "pricing", "cost", "rate", "quote"))
    payment = any(term in lower for term in ("payment", "deposit", "invoice", "terms"))
    installation = any(term in lower for term in ("install", "installation", "technician"))
    objection = any(term in lower for term in ("understand your concern", "appreciate your concern", "however"))
    contractions = bool(re.search(r"(?i)\b(?:we're|i'm|you'll|can't|don't|it's|that's)\b", body))
    average_paragraph = len(words) / max(len(paragraphs), 1)
    codes = {
        "concise_email" if len(words) <= 150 else "detailed_when_needed",
        "short_paragraphs" if average_paragraph <= 55 else "contextual_paragraphs",
        "warm_direct_tone" if contractions or greeting else "formal_direct_tone",
    }
    if greeting and len(first.split()) <= 8:
        codes.add("brief_greeting")
    if closing:
        codes.add("brief_closing")
    if category == "proposal_quote" and pricing and ctas:
        codes.add("structured_quote")
    if category == "follow_up" and ctas:
        codes.add("clear_follow_up")
    if category == "information_request" and (bullets >= 2 or questions >= 2):
        codes.add("group_information_requests")
    if pricing and len(sentences) >= 2:
        codes.add("explain_pricing_basis")
    if payment and len(paragraphs) >= 2:
        codes.add("separate_payment_terms")
    if installation and any(term in lower for term in ("first", "then", "next", "after")):
        codes.add("explain_installation_sequence")
    if category == "objection_handling" and objection:
        codes.add("acknowledge_objection")
    if len(ctas) == 1:
        codes.add("single_clear_call_to_action")
    return {
        "word_count": len(words), "paragraph_count": len(paragraphs),
        "sentence_count": len(sentences), "question_count": questions,
        "bullet_count": bullets, "codes": sorted(codes),
    }


def _full_message(service, message_id: str) -> Mapping:
    message = _execute(service.users().messages().get(
        userId="me", id=message_id, format="full",
    ))
    if _opaque(message.get("id"), "message id") != message_id:
        raise SentStyleError("Gmail full response id changed")
    return message


def _process_message(state: dict, service, message_id: str, own_domain: str,
                     metadata_exclusion: str = "") -> None:
    excluded = Counter(state.get("excluded_counts") or {})
    if metadata_exclusion:
        excluded[metadata_exclusion] += 1
        state["excluded_counts"] = dict(excluded)
        return
    message = _full_message(service, message_id)
    reason = ""
    if LABEL not in message.get("labelIds", []):
        reason = "not_sent"
    headers = _headers(message)
    if not reason:
        reason = _metadata_exclusion(
            headers, own_domain, bool(state["plan"]["exclude_internal"]),
        )
    subject = str(headers.get("subject") or "")
    try:
        body = extract_message_body(message)
    except SentStyleError:
        reason = reason or "unsafe_mime"
        body = ""
    if not reason and not body:
        reason = "missing_body"
    if not reason and (_AUTOMATED_RE.search(subject) or _AUTOMATED_RE.search(body[:2_000])):
        reason = "machine_generated"
    authored = isolate_authored_text(body) if not reason else ""
    if not reason and _EMPTY_ACK_RE.fullmatch(authored.strip()):
        reason = "empty_acknowledgement"
    private_names = [name for name, _address in getaddresses([
        headers.get("from", ""), headers.get("to", ""), headers.get("cc", ""),
    ])]
    sanitized = sanitize_text(authored, private_names)
    if not reason and (len(sanitized) < 40 or len(re.findall(r"\b\w+\b", sanitized)) < 8):
        reason = "too_little_authored_text"
    normalized = re.sub(r"\W+", " ", sanitized.lower()).strip()
    exact, simhash = _sha(normalized), _simhash(normalized)
    if not reason and exact in state["seen_exact"]:
        reason = "duplicate"
    if not reason and any((simhash ^ int(value, 16)).bit_count() <= 3
                          for value in state["seen_simhash"]):
        reason = "near_duplicate_template"
    if reason:
        excluded[reason] += 1
        state["excluded_counts"] = dict(excluded)
        return
    state["seen_exact"].append(exact)
    state["seen_simhash"].append(f"{simhash:016x}")
    category = _category(subject, sanitized)
    features = deterministic_features(sanitized, category)
    state["included_count"] += 1
    category_counts = Counter(state.get("category_counts") or {})
    category_counts[category] += 1
    state["category_counts"] = dict(category_counts)
    source_ref = f"sha256:{_sha(message_id)}"
    for code in features["codes"]:
        pattern_id = f"{code}@{category}"
        pattern = state["patterns"].setdefault(pattern_id, {
            "code": code, "message_category": category, "count": 0, "source_refs": [],
        })
        pattern["count"] += 1
        if len(pattern["source_refs"]) < MAX_SOURCE_REFS:
            pattern["source_refs"].append(source_ref)


def _verify_plan_snapshot(service, state: Mapping) -> tuple[str, str, dict[str, str]]:
    account, fingerprint = _profile(service)
    if fingerprint != state["plan"]["account_fingerprint"]:
        raise SentStyleError("connected Gmail account changed after approval")
    ids = _list_ids(service, state["plan"]["query"])
    metadata = [_message_metadata(service, message_id) for message_id in ids]
    if any(not state["plan"]["start_epoch"] * 1_000 <= item["internal_date"]
           < state["plan"]["end_epoch_exclusive"] * 1_000 for item in metadata):
        raise SentStyleError("Gmail returned a message outside the approved date range")
    metadata.sort(key=lambda item: (item["internal_date"], item["id"]))
    ordered = [item["id"] for item in metadata]
    if ordered != state["plan"]["message_ids"] or _sha(ordered) != state["plan"]["message_snapshot"]:
        raise SentStyleError("approved Gmail range changed after planning")
    own_domain = account.rsplit("@", 1)[1]
    exclusions = {
        item["id"]: reason for item in metadata
        if (reason := _metadata_exclusion(
            item["headers"], own_domain, bool(state["plan"]["exclude_internal"]),
        ))
    }
    return account, own_domain, exclusions


def cmd_run(args) -> None:
    with _job_lock(args.job_id):
        state = _load_state(args.job_id)
        if state["status"] not in {"planned", "running"}:
            raise SentStyleError("job is not runnable")
        service = google_api.build_service("gmail", "v1")
        _account, own_domain, metadata_exclusions = _verify_plan_snapshot(service, state)
        if state["status"] == "planned":
            if (not args.approval_token
                    or _sha(args.approval_token) != state.get("approval_token_sha256")
                    or time.time() > float(state.get("approval_expires_at", 0))):
                raise SentStyleError("exact unexpired plan approval token is required")
            state.pop("approval_token_sha256", None)
            state.pop("approval_expires_at", None)
            state["approved_plan_fingerprint"] = state["plan_fingerprint"]
            state["status"] = "running"
            _write_state(state)
        processed = set(state["processed_ids"])
        remaining = [item for item in state["plan"]["message_ids"] if item not in processed]
        for offset in range(0, len(remaining), BATCH_SIZE):
            batch = remaining[offset:offset + BATCH_SIZE]
            for message_id in batch:
                _process_message(
                    state, service, message_id, own_domain,
                    metadata_exclusions.get(message_id, ""),
                )
                state["processed_ids"].append(message_id)
            state["batch_number"] += 1
            _write_state(state)
            print(json.dumps({
                "status": "running", "job_id": state["job_id"],
                "batch_number": state["batch_number"],
                "processed": len(state["processed_ids"]),
                "total": state["plan"]["total_found"],
                "included": state["included_count"],
                "excluded": sum(state["excluded_counts"].values()),
            }))
        state["status"] = "complete"
        state["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write_state(state)
        print(json.dumps(_status(state)))


def _confidence(count: int) -> str:
    return "weak" if count == 1 else "moderate" if count == 2 else "strong"


def _load_pattern_selection(path: str, available: set[str]) -> dict[str, str]:
    if not path:
        return {pattern_id: "candidate" for pattern_id in sorted(available)}
    value = email_learning._read_private_json(
        Path(path), 32_000, "pattern selection", require_private=False,
    )
    if not isinstance(value, dict) or set(value) != {"patterns"}:
        raise SentStyleError("pattern selection is invalid")
    value = cast(dict[str, object], value)
    raw_patterns = value["patterns"]
    if not isinstance(raw_patterns, list):
        raise SentStyleError("pattern selection is invalid")
    patterns = cast(list[object], raw_patterns)
    selected: dict[str, str] = {}
    for item in patterns:
        if not isinstance(item, dict):
            raise SentStyleError("pattern selection is invalid")
        record = cast(dict[str, object], item)
        pattern_id, status = record.get("pattern_id"), record.get("status")
        if (set(record) != {"pattern_id", "status"} or not isinstance(pattern_id, str)
                or pattern_id not in available or not isinstance(status, str)
                or status not in {"candidate", "tentative"}):
            raise SentStyleError("pattern selection is invalid")
        selected[pattern_id] = status
    if not selected:
        raise SentStyleError("pattern selection must retain at least one pattern")
    return selected


def _preview_packet(state: Mapping, selection: Mapping[str, str]) -> dict:
    included = int(state["included_count"])
    patterns = []
    for pattern_id, review_status in selection.items():
        evidence = state["patterns"][pattern_id]
        code = evidence["code"]
        _rule_key, category, description = PATTERNS[code]
        conflict = CONFLICTS.get(code, "")
        patterns.append({
            "pattern_id": pattern_id, "code": code, "description": description,
            "email_category": evidence["message_category"], "style_category": category,
            "evidence_count": evidence["count"], "eligible_message_denominator": included,
            "confidence": _confidence(evidence["count"]),
            "date_range": {
                "start_local": state["plan"]["start_local"],
                "end_local_exclusive": state["plan"]["end_local_exclusive"],
            },
            "supporting_source_refs": evidence["source_refs"],
            "conflicts": ([conflict] if f"{conflict}@{evidence['message_category']}" in selection
                          else []),
            "extraction_version": PROCESSING_VERSION, "review_status": review_status,
        })
    return {
        "asset_title": "Cal's Linxio Email Writing Profile",
        "overall_voice_and_tone": [
            item for item in patterns if item["style_category"] == "tone"
        ],
        "patterns": patterns, "message_category_counts": state["category_counts"],
        "omitted_tentative_pattern_count": max(0, len(state["patterns"]) - len(selection)),
        "confidence_and_evidence_summary": Counter(
            item["confidence"] for item in patterns
        ),
        "analysed_date_range": {
            "start_local": state["plan"]["start_local"],
            "end_local_exclusive": state["plan"]["end_local_exclusive"],
            "timezone": TIMEZONE,
        },
        "counts": {
            "found": state["plan"]["total_found"], "included": included,
            "excluded": sum(state["excluded_counts"].values()),
            "exclusion_reasons": state["excluded_counts"],
        },
        "last_reviewed_date": None, "contains_raw_email": False,
        "contains_customer_pii": False, "automatic_promotion": False,
    }


def cmd_preview(args) -> None:
    with _job_lock(args.job_id):
        state = _load_state(args.job_id)
        if state["status"] not in {"complete", "previewed"}:
            raise SentStyleError("job must complete before preview")
        ranked = sorted(
            state["patterns"], key=lambda key: (-state["patterns"][key]["count"], key),
        )[:MAX_PROFILE_PATTERNS]
        selection = _load_pattern_selection(args.patterns_file, set(ranked))
        packet = _preview_packet(state, selection)
        fingerprint = _sha(packet)
        state["status"] = "previewed"
        state["preview_packet"] = packet
        state["preview_fingerprint"] = fingerprint
        _write_state(state)
        print(json.dumps({
            "status": "profile_preview", "job_id": state["job_id"],
            "preview_fingerprint": fingerprint,
            "record_requires_confirm": RECORD_CONFIRM, "profile": packet,
        }))


def cmd_record(args) -> None:
    if args.confirm != RECORD_CONFIRM:
        raise SentStyleError("explicit profile approval is required")
    with _job_lock(args.job_id):
        state = _load_state(args.job_id)
        if state["status"] not in {"previewed", "recorded"}:
            raise SentStyleError("reviewed profile preview is required")
        packet = state.get("preview_packet")
        if (not isinstance(packet, dict) or args.preview_fingerprint != state.get("preview_fingerprint")
                or _sha(packet) != state.get("preview_fingerprint")):
            raise SentStyleError("profile preview changed after approval")
        lessons = [{
            "code": item["code"], "evidence_kind": "aggregate_sent_style",
            "evidence_count": item["evidence_count"],
            "eligible_denominator": item["eligible_message_denominator"],
            "confidence": item["confidence"], "status": item["review_status"],
            "source_refs": item["supporting_source_refs"],
            "message_category": item["email_category"],
        } for item in packet["patterns"]]
        context = {
            "source": {"kind": "sent_style_job", "id": state["job_id"]},
            "bootstrap_id": state["job_id"], "captured_at": state["completed_at"],
            "analysis": {
                "kind": "bulk_sent_style",
                "account_fingerprint": state["plan"]["account_fingerprint"],
                "timezone": TIMEZONE,
                "date_range": {
                    "start_local": state["plan"]["start_local"],
                    "end_local_exclusive": state["plan"]["end_local_exclusive"],
                    "start_epoch": state["plan"]["start_epoch"],
                    "end_epoch_exclusive": state["plan"]["end_epoch_exclusive"],
                },
                "counts": packet["counts"],
                "message_category_counts": state["category_counts"],
                "extraction_version": PROCESSING_VERSION,
            },
            "lessons": lessons, "outcomes": [],
        }
        context["packet_fingerprint"] = _sha(context)
        context["confirm"] = True
        token = os.environ.get(email_learning.BRIDGE_TOKEN_ENV, "").strip()
        url = os.environ.get(email_learning.BRIDGE_URL_ENV, "").strip()
        if not token or not url:
            raise SentStyleError("Cogitator bridge configuration is unavailable")
        result = email_learning._bridge_call(url, token, {
            "source_agent": "hermes",
            "requested_action": "record_email_lesson_candidates",
            "user_intent": "Record Cal-approved sanitized aggregate Sent-mail style candidates.",
            "content": "", "context_hint": "", "approval_status": "approved",
            "risk_level": "medium", "context": context,
        })
        candidate_ids = [_opaque(item, "candidate id") for item in result.get("candidate_ids", [])]
        state["status"] = "recorded"
        state["recorded_packet_fingerprint"] = context["packet_fingerprint"]
        state["candidate_ids"] = candidate_ids
        _write_state(state)
        print(json.dumps({
            "status": "recorded", "mutation_performed": bool(result.get("mutation_performed", True)),
            "candidate_ids": candidate_ids,
        }))


def _status(state: Mapping) -> dict:
    return {
        "status": state["status"], "job_id": state["job_id"],
        "plan_fingerprint": state["plan_fingerprint"],
        "total_found": state["plan"]["total_found"],
        "processed": len(state.get("processed_ids") or []),
        "included": state.get("included_count", 0),
        "excluded": sum((state.get("excluded_counts") or {}).values()),
        "exclusion_reasons": state.get("excluded_counts") or {},
        "batch_number": state.get("batch_number", 0),
        "expires_at": state["expires_at"],
    }


def cmd_status(args) -> None:
    print(json.dumps(_status(_load_state(args.job_id))))


def cmd_cancel(args) -> None:
    with _job_lock(args.job_id):
        state = _load_state(args.job_id)
        if state["status"] == "recorded":
            raise SentStyleError("recorded candidates must be reviewed or deleted in Cogitator")
        state.update(
            status="cancelled", processed_ids=[], patterns={}, seen_exact=[],
            seen_simhash=[], category_counts={}, included_count=0,
        )
        state["plan"]["message_ids"] = []
        _write_state(state)
        print(json.dumps(_status(state)))


def cmd_delete(args) -> None:
    with _job_lock(args.job_id):
        path = _state_path(args.job_id)
        if path.is_symlink():
            raise SentStyleError("private state path is unsafe")
        path.unlink()
    print(json.dumps({"status": "deleted", "job_id": args.job_id}))


def main() -> int:
    parser = argparse.ArgumentParser(description="Private Linxio SENT-mail style bootstrap")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("sent-style-plan")
    plan.add_argument("--start", default="", help="inclusive local date YYYY-MM-DD")
    plan.add_argument("--end", default="", help="inclusive local date YYYY-MM-DD")
    plan.add_argument("--include-internal", action="store_true")
    plan.set_defaults(func=cmd_plan)
    run = sub.add_parser("sent-style-run")
    run.add_argument("job_id"); run.add_argument("--approval-token", default="")
    run.set_defaults(func=cmd_run)
    preview = sub.add_parser("sent-style-preview")
    preview.add_argument("job_id"); preview.add_argument("--patterns-file", default="")
    preview.set_defaults(func=cmd_preview)
    record = sub.add_parser("sent-style-record")
    record.add_argument("job_id"); record.add_argument("--preview-fingerprint", required=True)
    record.add_argument("--confirm", required=True); record.set_defaults(func=cmd_record)
    cancel = sub.add_parser("sent-style-cancel")
    cancel.add_argument("job_id"); cancel.set_defaults(func=cmd_cancel)
    status_cmd = sub.add_parser("sent-style-status")
    status_cmd.add_argument("job_id"); status_cmd.set_defaults(func=cmd_status)
    delete = sub.add_parser("sent-style-delete")
    delete.add_argument("job_id"); delete.set_defaults(func=cmd_delete)
    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SentStyleError, email_learning.EmailLearningError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)

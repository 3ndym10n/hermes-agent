"""Durable operational Attention Queue for Hermes.

The queue stores bounded, pre-sanitized operational metadata only. Source
adapters call :func:`upsert_attention`; the mobile app uses the list/detail
and optimistic transition functions. No function in this module mutates a
source system.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, cast
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from hermes_constants import get_hermes_home


SCHEMA_VERSION = 3
BUSY_TIMEOUT_MS = 5_000
ACTIVE_RETENTION_DAYS = 30
ACTIVITY_RETENTION_DAYS = 90
MAX_REQUEST_BYTES = 16 * 1024
SYDNEY = ZoneInfo("Australia/Sydney")

SOURCE_TYPES = frozenset({
    "gmail",
    "hubspot",
    "softphone",
    "calendar",
    "cogitator",
    "ecommerce",
    "agent_job",
    "github",
    "system",
    "manual",
})
PROJECTS = frozenset({"linxio", "cogitator", "ecommerce", "personal", "system"})
PROJECT_ORDER = ("linxio", "cogitator", "ecommerce", "personal", "system")
OPERATIONAL_SOURCE_ORDER = (
    "gmail",
    "calendar",
    "cogitator",
    "github",
    "ecommerce",
    "system",
    "personal",
)
OPERATIONAL_SOURCES = frozenset(OPERATIONAL_SOURCE_ORDER)
SOURCE_STATUSES = frozenset({
    "active",
    "paused",
    "degraded",
    "failed",
    "unavailable",
    "blocked",
    "no_current_work",
    "healthy",
    "not_connected",
})
SOURCE_CONTROL_ACTIONS = frozenset({"pause", "resume", "disable", "enable"})
SOURCE_HISTORY_EVENTS = frozenset({
    "state_changed",
    "sync_failed",
    "sync_recovered",
    "control_changed",
})
CONNECTED_SOURCE_STATUSES = SOURCE_STATUSES - {"unavailable", "not_connected"}
FAILED_SOURCE_STATUSES = frozenset({"degraded", "failed", "unavailable", "blocked"})
SUCCESS_SOURCE_STATUSES = frozenset({"active", "healthy", "no_current_work"})
SOURCE_PROJECTS = {
    "gmail": "linxio",
    "calendar": "linxio",
    "cogitator": "cogitator",
    "ecommerce": "ecommerce",
    "system": "system",
    "personal": "personal",
}
SOURCE_DEFAULTS = {
    "gmail": ("unavailable", "Gmail is unavailable."),
    "calendar": ("unavailable", "Calendar is unavailable."),
    "cogitator": ("unavailable", "Cogitator is unavailable."),
    "github": ("unavailable", "GitHub is unavailable."),
    "ecommerce": (
        "unavailable",
        "No trustworthy ecommerce runtime feed is currently available.",
    ),
    "system": ("unavailable", "System health has not been checked."),
    "personal": ("not_connected", "Personal is not connected."),
}
ITEM_TYPES = frozenset({
    "customer_email",
    "automation_failure",
    "approval",
    "demonstration",
    "decision",
    "task",
    "research",
    "call",
    "calendar_event",
    "agent_job",
    "server_health",
})
PRIORITIES = frozenset({"urgent", "high", "normal", "low"})
STATUSES = frozenset({
    "new",
    "needs_cal",
    "prepared",
    "monitoring",
    "deferred",
    "resolved",
    "dismissed",
    "safety_hold",
    "stale",
})
WAITING_ON = frozenset({"cal", "virgil", "external", "none"})
REASON_CODES = frozenset({
    "reply_needed",
    "no_reply_needed",
    "human_decision",
    "low_confidence",
    "blocked_category",
    "missing_approved_fact",
    "missing_writing_guidance",
    "conflicting_facts",
    "cross_customer_risk",
    "unsafe_promise",
    "unclear_sender",
    "thread_changed",
    "later_reply",
    "newer_external_message",
    "existing_draft",
    "stale_existing_draft",
    "automated",
    "internal",
    "bulk",
    "calendar",
    "receipt_or_delivery",
    "spam_or_trash",
    "not_inbox",
    "empty_body",
    "thread_too_large",
    "malformed_model_output",
    "unsupported_claim",
    "rate_limit",
    "duplicate",
    "processing_failure",
    "product_verification",
    "oauth_failure",
    "wrong_account",
    "history_gap",
    "queue_stuck",
    "worker_overlap",
    "state_corruption",
    "notification_failure",
    "cogitator_bridge_failure",
    "daily_limit_reached",
    "polling_delay",
    "stale_checkpoint",
    "possible_duplicate_draft",
    "gmail_api_failure",
    "source_changed",
    "recovered",
    "manual",
})
EVENT_TYPES = frozenset({
    "created",
    "updated",
    "deferred",
    "reopened",
    "resolved",
    "dismissed",
    "source_state_changed",
    "safety_hold_triggered",
    "processing_failure_recovered",
})
ACTORS = frozenset({
    "virgil",
    "cal",
    "gmail_worker",
    "hubspot",
    "calendar",
    "agent_job",
    "system",
    "operator",
})

UPSERT_REQUIRED = frozenset({
    "source_type",
    "source_record_id",
    "source_event_id",
    "project",
    "item_type",
    "priority",
    "status",
    "title",
    "safe_summary",
    "recommended_action",
    "waiting_on",
    "reason_code",
    "confidence",
    "processing_version",
})
UPSERT_OPTIONAL = frozenset({
    "due_at",
    "expires_at",
    "source_deep_link",
    "prepared_artifact_deep_link",
})
UPSERT_KEYS = UPSERT_REQUIRED | UPSERT_OPTIONAL

_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_EMAIL_RE = re.compile(r"(?i)\b[^@\s]+@[^@\s]+\.[^@\s]+\b")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d(). -]{6,}\d)(?!\w)")
_URL_IN_TEXT_RE = re.compile(r"(?i)\b(?:https?://|www\.)")
_SECRET_RE = re.compile(
    r"(?i)(?:\b(?:api[_ -]?key|password|passwd|oauth|access[_ -]?token|"
    r"refresh[_ -]?token|authorization)\b\s*[:=]|\bbearer\s+[A-Za-z0-9._~-]{12,}|"
    r"\b(?:sk|pk|gh[opasu]|ya29)[-_][A-Za-z0-9_-]{12,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)
_COMMAND_RE = re.compile(
    r"(?i)(?:^\s*|\b(?:run|execute|type)\s+)(?:\$\s*)?"
    r"(?:sudo\b|curl\b|wget\b|python(?:3)?\b|bash\b|sh\b|powershell\b|"
    r"cmd\.exe\b|hermes\b|rm\b|chmod\b|chown\b|systemctl\b|javascript:)"
)
_CARD_CANDIDATE_RE = re.compile(r"(?:\d[ -]?){13,19}")
_CLOSED_STATUSES = frozenset({"resolved", "dismissed", "stale"})
_ACTIVE_STATUSES = STATUSES - _CLOSED_STATUSES
_ALLOWED_TRANSITIONS = {
    "new": STATUSES - {"new"},
    "needs_cal": STATUSES - {"new", "needs_cal"},
    "prepared": STATUSES - {"new", "prepared"},
    "monitoring": STATUSES - {"new", "monitoring"},
    "deferred": STATUSES - {"deferred"},
    "resolved": frozenset({
        "new",
        "needs_cal",
        "prepared",
        "monitoring",
        "safety_hold",
    }),
    "dismissed": frozenset({
        "new",
        "needs_cal",
        "prepared",
        "monitoring",
        "safety_hold",
    }),
    "safety_hold": STATUSES - {"new", "safety_hold"},
    "stale": frozenset({
        "new",
        "needs_cal",
        "prepared",
        "monitoring",
        "dismissed",
        "safety_hold",
    }),
}
_URL_HOSTS = {
    "gmail": frozenset({"mail.google.com"}),
    "hubspot": frozenset({"app.hubspot.com"}),
    "calendar": frozenset({"calendar.google.com", "www.google.com"}),
    "github": frozenset({"github.com"}),
    "ecommerce": frozenset({"github.com"}),
}


class AttentionError(RuntimeError):
    """Fail-closed queue error carrying a stable code and HTTP status."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def attention_db_path() -> Path:
    return get_hermes_home() / "attention" / "attention.db"


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _iso(value: float | None) -> str | None:
    if value is None:
        return None
    return (
        datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    )


def _parse_time(value: Any, *, field: str, optional: bool = True) -> float | None:
    if (value is None or value == "") and optional:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise AttentionError(f"invalid_{field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AttentionError(f"invalid_{field}") from exc
    if parsed.tzinfo is None:
        raise AttentionError(f"invalid_{field}")
    return parsed.astimezone(timezone.utc).timestamp()


def _luhn(value: str) -> bool:
    digits = [int(char) for char in value if char.isdigit()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _safe_text(value: Any, *, field: str, limit: int, command: bool = False) -> str:
    if not isinstance(value, str):
        raise AttentionError(f"invalid_{field}")
    text = unicodedata.normalize("NFC", value).strip()
    if not text or len(text) > limit:
        raise AttentionError(f"invalid_{field}")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise AttentionError(f"unsafe_{field}")
    if "<" in text or ">" in text or _URL_IN_TEXT_RE.search(text):
        raise AttentionError(f"unsafe_{field}")
    if _EMAIL_RE.search(text) or _PHONE_RE.search(text) or _SECRET_RE.search(text):
        raise AttentionError(f"unsafe_{field}")
    if any(_luhn(match.group(0)) for match in _CARD_CANDIDATE_RE.finditer(text)):
        raise AttentionError(f"unsafe_{field}")
    if command and _COMMAND_RE.search(text):
        raise AttentionError(f"unsafe_{field}")
    return text


def _opaque(value: Any, *, field: str) -> str:
    text = str(value or "")
    if not _OPAQUE_ID_RE.fullmatch(text):
        raise AttentionError(f"invalid_{field}")
    return text


def _enum(value: Any, allowed: frozenset[str], *, field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise AttentionError(f"invalid_{field}")
    return value


def _safe_url(value: Any, *, source_type: str, field: str) -> str | None:
    if value is None or value == "":
        return None
    if (
        not isinstance(value, str)
        or len(value) > 500
        or any(char.isspace() or ord(char) < 32 or char == "\\" for char in value)
    ):
        raise AttentionError(f"invalid_{field}")
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise AttentionError(f"invalid_{field}") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.hostname.casefold() not in _URL_HOSTS.get(source_type, frozenset())
        or (
            source_type == "calendar"
            and parsed.hostname.casefold() == "www.google.com"
            and parsed.path != "/calendar/event"
        )
    ):
        raise AttentionError(f"invalid_{field}")
    return value


def validate_upsert(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise AttentionError("invalid_payload")
    keys = frozenset(payload)
    if keys - UPSERT_KEYS or UPSERT_REQUIRED - keys:
        raise AttentionError("invalid_payload_keys")
    source_type = _enum(payload["source_type"], SOURCE_TYPES, field="source_type")
    confidence = payload["confidence"]
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise AttentionError("invalid_confidence")
        confidence = float(confidence)
        if not 0 <= confidence <= 1:
            raise AttentionError("invalid_confidence")
    version = str(payload["processing_version"] or "")
    if not _VERSION_RE.fullmatch(version):
        raise AttentionError("invalid_processing_version")
    reason = _enum(payload["reason_code"], REASON_CODES, field="reason_code")
    normalized = {
        "source_type": source_type,
        "source_record_id": _opaque(
            payload["source_record_id"], field="source_record_id"
        ),
        "source_event_id": _opaque(payload["source_event_id"], field="source_event_id"),
        "project": _enum(payload["project"], PROJECTS, field="project"),
        "item_type": _enum(payload["item_type"], ITEM_TYPES, field="item_type"),
        "priority": _enum(payload["priority"], PRIORITIES, field="priority"),
        "status": _enum(payload["status"], STATUSES, field="status"),
        "title": _safe_text(payload["title"], field="title", limit=180),
        "safe_summary": _safe_text(
            payload["safe_summary"], field="safe_summary", limit=500
        ),
        "recommended_action": _safe_text(
            payload["recommended_action"],
            field="recommended_action",
            limit=300,
            command=True,
        ),
        "waiting_on": _enum(payload["waiting_on"], WAITING_ON, field="waiting_on"),
        "reason_code": reason,
        "confidence": confidence,
        "due_at": _parse_time(payload.get("due_at"), field="due_at"),
        "expires_at": _parse_time(payload.get("expires_at"), field="expires_at"),
        "source_deep_link": _safe_url(
            payload.get("source_deep_link"),
            source_type=source_type,
            field="source_deep_link",
        ),
        "prepared_artifact_deep_link": _safe_url(
            payload.get("prepared_artifact_deep_link"),
            source_type=source_type,
            field="prepared_artifact_deep_link",
        ),
        "processing_version": version,
    }
    if normalized["status"] == "safety_hold" and reason not in {
        "processing_failure",
        "oauth_failure",
        "wrong_account",
        "history_gap",
        "queue_stuck",
        "worker_overlap",
        "state_corruption",
        "cross_customer_risk",
        "notification_failure",
        "cogitator_bridge_failure",
        "daily_limit_reached",
        "polling_delay",
        "stale_checkpoint",
        "possible_duplicate_draft",
        "gmail_api_failure",
    }:
        raise AttentionError("invalid_safety_hold_reason")
    return normalized


def _secure_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != getattr(os, "getuid", lambda: metadata.st_uid)()
        ):
            raise AttentionError("attention_state_corruption", 500)
    else:
        path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700, follow_symlinks=False)


def _secure_file(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != getattr(os, "getuid", lambda: metadata.st_uid)()
    ):
        raise AttentionError("attention_state_corruption", 500)
    path.chmod(0o600, follow_symlinks=False)


def _ensure_database_file(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    _secure_file(path)


def _secure_sidecars(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists() or candidate.is_symlink():
            _secure_file(candidate)


_V1_ITEM_TYPES = ITEM_TYPES - {"demonstration"}
_V1_REASON_CODES = REASON_CODES - {"product_verification"}
_V2_SOURCE_TYPES = SOURCE_TYPES - {"cogitator", "ecommerce"}


_MIGRATION_1 = f"""
CREATE TABLE attention_refs (
    item_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
CREATE TABLE attention_items (
    item_id TEXT PRIMARY KEY REFERENCES attention_refs(item_id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL CHECK (source_type IN ({",".join(repr(v) for v in sorted(_V2_SOURCE_TYPES))})),
    source_record_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    project TEXT NOT NULL CHECK (project IN ({",".join(repr(v) for v in sorted(PROJECTS))})),
    item_type TEXT NOT NULL CHECK (item_type IN ({",".join(repr(v) for v in sorted(_V1_ITEM_TYPES))})),
    priority TEXT NOT NULL CHECK (priority IN ({",".join(repr(v) for v in sorted(PRIORITIES))})),
    status TEXT NOT NULL CHECK (status IN ({",".join(repr(v) for v in sorted(STATUSES))})),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 180),
    safe_summary TEXT NOT NULL CHECK (length(safe_summary) BETWEEN 1 AND 500),
    recommended_action TEXT NOT NULL CHECK (length(recommended_action) BETWEEN 1 AND 300),
    waiting_on TEXT NOT NULL CHECK (waiting_on IN ({",".join(repr(v) for v in sorted(WAITING_ON))})),
    reason_code TEXT NOT NULL CHECK (reason_code IN ({",".join(repr(v) for v in sorted(_V1_REASON_CODES))})),
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    due_at REAL,
    deferred_until REAL,
    source_deep_link TEXT,
    prepared_artifact_deep_link TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    resolved_at REAL,
    dismissed_at REAL,
    processing_version TEXT NOT NULL,
    row_version INTEGER NOT NULL CHECK (row_version > 0)
);
CREATE INDEX attention_items_status_updated ON attention_items(status, updated_at DESC);
CREATE INDEX attention_items_project_status ON attention_items(project, status);
CREATE TABLE attention_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL REFERENCES attention_refs(item_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    event_type TEXT NOT NULL CHECK (event_type IN ({",".join(repr(v) for v in sorted(EVENT_TYPES))})),
    happened_at REAL NOT NULL,
    safe_description TEXT NOT NULL CHECK (length(safe_description) BETWEEN 1 AND 300),
    actor TEXT NOT NULL CHECK (actor IN ({",".join(repr(v) for v in sorted(ACTORS))})),
    prior_status TEXT CHECK (prior_status IS NULL OR prior_status IN ({",".join(repr(v) for v in sorted(STATUSES))})),
    new_status TEXT CHECK (new_status IS NULL OR new_status IN ({",".join(repr(v) for v in sorted(STATUSES))})),
    snapshot_version INTEGER NOT NULL CHECK (snapshot_version > 0),
    UNIQUE(item_id, sequence)
);
CREATE INDEX attention_events_time ON attention_events(happened_at DESC);
CREATE TRIGGER attention_events_no_update
BEFORE UPDATE ON attention_events
BEGIN
    SELECT RAISE(ABORT, 'attention events are append-only');
END;
CREATE TABLE attention_notifications (
    item_id TEXT PRIMARY KEY REFERENCES attention_items(item_id) ON DELETE CASCADE,
    telegram_message_id TEXT,
    last_notified_priority TEXT CHECK (
        last_notified_priority IS NULL OR last_notified_priority IN ({",".join(repr(v) for v in sorted(PRIORITIES))})
    ),
    last_notified_status TEXT CHECK (
        last_notified_status IS NULL OR last_notified_status IN ({",".join(repr(v) for v in sorted(STATUSES))})
    ),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    last_attempt_at REAL,
    last_success_at REAL
);
"""
_MIGRATION_2 = f"""
CREATE TEMP TABLE attention_notifications_v1 AS
SELECT * FROM attention_notifications;
DROP TABLE attention_notifications;
CREATE TABLE attention_items_v2 (
    item_id TEXT PRIMARY KEY REFERENCES attention_refs(item_id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL CHECK (source_type IN ({",".join(repr(v) for v in sorted(_V2_SOURCE_TYPES))})),
    source_record_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    project TEXT NOT NULL CHECK (project IN ({",".join(repr(v) for v in sorted(PROJECTS))})),
    item_type TEXT NOT NULL CHECK (item_type IN ({",".join(repr(v) for v in sorted(ITEM_TYPES))})),
    priority TEXT NOT NULL CHECK (priority IN ({",".join(repr(v) for v in sorted(PRIORITIES))})),
    status TEXT NOT NULL CHECK (status IN ({",".join(repr(v) for v in sorted(STATUSES))})),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 180),
    safe_summary TEXT NOT NULL CHECK (length(safe_summary) BETWEEN 1 AND 500),
    recommended_action TEXT NOT NULL CHECK (length(recommended_action) BETWEEN 1 AND 300),
    waiting_on TEXT NOT NULL CHECK (waiting_on IN ({",".join(repr(v) for v in sorted(WAITING_ON))})),
    reason_code TEXT NOT NULL CHECK (reason_code IN ({",".join(repr(v) for v in sorted(REASON_CODES))})),
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    due_at REAL,
    deferred_until REAL,
    source_deep_link TEXT,
    prepared_artifact_deep_link TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    resolved_at REAL,
    dismissed_at REAL,
    processing_version TEXT NOT NULL,
    row_version INTEGER NOT NULL CHECK (row_version > 0)
);
INSERT INTO attention_items_v2 SELECT * FROM attention_items;
DROP TABLE attention_items;
ALTER TABLE attention_items_v2 RENAME TO attention_items;
CREATE INDEX attention_items_status_updated ON attention_items(status, updated_at DESC);
CREATE INDEX attention_items_project_status ON attention_items(project, status);
CREATE TABLE attention_notifications (
    item_id TEXT PRIMARY KEY REFERENCES attention_items(item_id) ON DELETE CASCADE,
    telegram_message_id TEXT,
    last_notified_priority TEXT CHECK (
        last_notified_priority IS NULL OR last_notified_priority IN ({",".join(repr(v) for v in sorted(PRIORITIES))})
    ),
    last_notified_status TEXT CHECK (
        last_notified_status IS NULL OR last_notified_status IN ({",".join(repr(v) for v in sorted(STATUSES))})
    ),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    last_attempt_at REAL,
    last_success_at REAL
);
INSERT INTO attention_notifications
SELECT * FROM attention_notifications_v1;
DROP TABLE attention_notifications_v1;
"""

_MIGRATION_3 = f"""
CREATE TEMP TABLE attention_notifications_v2 AS
SELECT * FROM attention_notifications;
DROP TABLE attention_notifications;
CREATE TABLE attention_items_v3 (
    item_id TEXT PRIMARY KEY REFERENCES attention_refs(item_id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL CHECK (source_type IN ({",".join(repr(v) for v in sorted(SOURCE_TYPES))})),
    source_record_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    project TEXT NOT NULL CHECK (project IN ({",".join(repr(v) for v in sorted(PROJECTS))})),
    item_type TEXT NOT NULL CHECK (item_type IN ({",".join(repr(v) for v in sorted(ITEM_TYPES))})),
    priority TEXT NOT NULL CHECK (priority IN ({",".join(repr(v) for v in sorted(PRIORITIES))})),
    status TEXT NOT NULL CHECK (status IN ({",".join(repr(v) for v in sorted(STATUSES))})),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 180),
    safe_summary TEXT NOT NULL CHECK (length(safe_summary) BETWEEN 1 AND 500),
    recommended_action TEXT NOT NULL CHECK (length(recommended_action) BETWEEN 1 AND 300),
    waiting_on TEXT NOT NULL CHECK (waiting_on IN ({",".join(repr(v) for v in sorted(WAITING_ON))})),
    reason_code TEXT NOT NULL CHECK (reason_code IN ({",".join(repr(v) for v in sorted(REASON_CODES))})),
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    due_at REAL,
    expires_at REAL,
    deferred_until REAL,
    source_deep_link TEXT,
    prepared_artifact_deep_link TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    resolved_at REAL,
    dismissed_at REAL,
    processing_version TEXT NOT NULL,
    row_version INTEGER NOT NULL CHECK (row_version > 0)
);
INSERT INTO attention_items_v3(
    item_id, idempotency_key, source_type, source_record_id, source_event_id,
    project, item_type, priority, status, title, safe_summary,
    recommended_action, waiting_on, reason_code, confidence, due_at,
    expires_at, deferred_until, source_deep_link,
    prepared_artifact_deep_link, created_at, updated_at, resolved_at,
    dismissed_at, processing_version, row_version
)
SELECT
    item_id, idempotency_key, source_type, source_record_id, source_event_id,
    project, item_type, priority, status, title, safe_summary,
    recommended_action, waiting_on, reason_code, confidence, due_at,
    NULL, deferred_until, source_deep_link, prepared_artifact_deep_link,
    created_at, updated_at, resolved_at, dismissed_at, processing_version,
    row_version
FROM attention_items;
DROP TABLE attention_items;
ALTER TABLE attention_items_v3 RENAME TO attention_items;
CREATE INDEX attention_items_status_updated ON attention_items(status, updated_at DESC);
CREATE INDEX attention_items_project_status ON attention_items(project, status);
CREATE INDEX attention_items_source_status ON attention_items(source_type, status);
CREATE INDEX attention_items_expires ON attention_items(expires_at)
WHERE expires_at IS NOT NULL;
CREATE TABLE attention_notifications (
    item_id TEXT PRIMARY KEY REFERENCES attention_items(item_id) ON DELETE CASCADE,
    telegram_message_id TEXT,
    last_notified_priority TEXT CHECK (
        last_notified_priority IS NULL OR last_notified_priority IN ({",".join(repr(v) for v in sorted(PRIORITIES))})
    ),
    last_notified_status TEXT CHECK (
        last_notified_status IS NULL OR last_notified_status IN ({",".join(repr(v) for v in sorted(STATUSES))})
    ),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    last_attempt_at REAL,
    last_success_at REAL
);
INSERT INTO attention_notifications
SELECT * FROM attention_notifications_v2;
DROP TABLE attention_notifications_v2;
CREATE TABLE attention_source_status (
    source TEXT PRIMARY KEY CHECK (source IN ({",".join(repr(v) for v in sorted(OPERATIONAL_SOURCES))})),
    status TEXT NOT NULL CHECK (status IN ({",".join(repr(v) for v in sorted(SOURCE_STATUSES))})),
    last_attempted_sync_at REAL,
    last_successful_sync_at REAL,
    next_scheduled_sync_at REAL,
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    error_code TEXT CHECK (error_code IS NULL OR length(error_code) BETWEEN 1 AND 80),
    message TEXT NOT NULL CHECK (length(message) BETWEEN 1 AND 300),
    updated_at REAL NOT NULL
);
CREATE TABLE attention_source_control (
    source TEXT PRIMARY KEY REFERENCES attention_source_status(source) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    updated_at REAL NOT NULL,
    CHECK (enabled = 1 OR paused = 0)
);
CREATE TABLE attention_source_history (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL REFERENCES attention_source_status(source) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN ({",".join(repr(v) for v in sorted(SOURCE_HISTORY_EVENTS))})),
    happened_at REAL NOT NULL,
    prior_status TEXT CHECK (prior_status IS NULL OR prior_status IN ({",".join(repr(v) for v in sorted(SOURCE_STATUSES))})),
    new_status TEXT CHECK (new_status IS NULL OR new_status IN ({",".join(repr(v) for v in sorted(SOURCE_STATUSES))})),
    message TEXT NOT NULL CHECK (length(message) BETWEEN 1 AND 300),
    error_code TEXT CHECK (error_code IS NULL OR length(error_code) BETWEEN 1 AND 80)
);
CREATE INDEX attention_source_history_time
ON attention_source_history(happened_at DESC, event_id DESC);
CREATE TRIGGER attention_source_history_no_update
BEFORE UPDATE ON attention_source_history
BEGIN
    SELECT RAISE(ABORT, 'attention source history is append-only');
END;
"""


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    applied = {
        int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")
    }
    if any(version > SCHEMA_VERSION for version in applied):
        raise AttentionError("attention_schema_too_new", 500)
    if 1 not in applied:
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{_MIGRATION_1}")
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (1, _now()),
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise AttentionError("attention_migration_failed", 500) from exc
    if 2 not in applied:
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{_MIGRATION_2}")
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (2, _now()),
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise AttentionError("attention_migration_failed", 500) from exc
    if 3 not in applied:
        try:
            migrated_at = _now()
            conn.executescript(f"BEGIN IMMEDIATE;\n{_MIGRATION_3}")
            conn.executemany(
                "INSERT INTO attention_source_status("
                "source, status, message, updated_at) VALUES(?, ?, ?, ?)",
                [
                    (source, *SOURCE_DEFAULTS[source], migrated_at)
                    for source in sorted(OPERATIONAL_SOURCES)
                ],
            )
            conn.executemany(
                "INSERT INTO attention_source_control("
                "source, enabled, paused, updated_at) VALUES(?, ?, 0, ?)",
                [
                    (source, 0 if source == "personal" else 1, migrated_at)
                    for source in sorted(OPERATIONAL_SOURCES)
                ],
            )
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (3, migrated_at),
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise AttentionError("attention_migration_failed", 500) from exc


@contextmanager
def _connect(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    path = (Path(db_path) if db_path is not None else attention_db_path()).absolute()
    try:
        _secure_directory(path.parent)
        _ensure_database_file(path)
        uri = f"file:{quote(path.as_posix(), safe='/')}?mode=rw&nofollow=1"
        conn = sqlite3.connect(
            uri,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            uri=True,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA trusted_schema = OFF")
        conn.execute("PRAGMA journal_mode = WAL")
        _apply_migrations(conn)
        check = conn.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise AttentionError("attention_state_corruption", 500)
        yield conn
        _secure_sidecars(path)
    except AttentionError:
        raise
    except (OSError, sqlite3.DatabaseError) as exc:
        raise AttentionError("attention_state_corruption", 500) from exc
    finally:
        if "conn" in locals():
            conn.close()
        _secure_sidecars(path)


@contextmanager
def _write(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _idempotency_key(source_type: str, source_record_id: str) -> str:
    return hashlib.sha256(f"{source_type}\0{source_record_id}".encode()).hexdigest()


def _append_event(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    event_type: str,
    description: str,
    actor: str,
    prior_status: str | None,
    new_status: str | None,
    snapshot_version: int,
    happened_at: float,
) -> None:
    event_type = _enum(event_type, EVENT_TYPES, field="event_type")
    actor = _enum(actor, ACTORS, field="actor")
    safe_description = _safe_text(description, field="event_description", limit=300)
    sequence = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM attention_events WHERE item_id=?",
        (item_id,),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO attention_events(
            item_id, sequence, event_type, happened_at, safe_description,
            actor, prior_status, new_status, snapshot_version
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            sequence,
            event_type,
            happened_at,
            safe_description,
            actor,
            prior_status,
            new_status,
            snapshot_version,
        ),
    )


def _item_dict(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for field in (
        "due_at",
        "expires_at",
        "deferred_until",
        "created_at",
        "updated_at",
        "resolved_at",
        "dismissed_at",
    ):
        result[field] = _iso(result[field])
    return result


def _status_time(value: Any, *, field: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return _parse_time(value, field=field, optional=False)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AttentionError(f"invalid_{field}")
    timestamp = float(value)
    if not 0 <= timestamp <= 32_503_680_000:
        raise AttentionError(f"invalid_{field}")
    return timestamp


def _source_error_code(value: Any) -> str | None:
    if value is None or value == "":
        return None
    code = str(value)
    if not _VERSION_RE.fullmatch(code):
        raise AttentionError("invalid_source_error_code")
    return code


def _source_status_dict(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["enabled"] = bool(result["enabled"])
    result["paused"] = bool(result["paused"])
    result["reported_status"] = result["status"]
    if not result["enabled"] and result["source"] != "personal":
        result["status"] = "unavailable"
        result["message"] = "Source synchronization is disabled."
    elif result["paused"]:
        result["status"] = "paused"
        result["message"] = "Source synchronization is paused."
    for field in (
        "last_attempted_sync_at",
        "last_successful_sync_at",
        "next_scheduled_sync_at",
        "updated_at",
    ):
        result[field] = _iso(result[field])
    return result


def list_source_statuses(*, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Return sanitized operational status for every supported source."""

    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT s.*, c.enabled, c.paused "
            "FROM attention_source_status s "
            "JOIN attention_source_control c USING(source)"
        ).fetchall()
    by_source = {str(row["source"]): _source_status_dict(row) for row in rows}
    return [by_source[source] for source in OPERATIONAL_SOURCE_ORDER]


def update_source_status(
    source: str,
    status: str,
    *,
    last_attempted_sync_at: str | float | None = None,
    last_successful_sync_at: str | float | None = None,
    next_scheduled_sync_at: str | float | None = None,
    failure_count: int | None = None,
    error_code: str | None = None,
    message: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Record one bounded source result without touching the source system."""

    source = _enum(source, OPERATIONAL_SOURCES, field="source")
    status = _enum(status, SOURCE_STATUSES, field="source_status")
    attempted_at = _status_time(last_attempted_sync_at, field="last_attempted_sync_at")
    successful_at = _status_time(
        last_successful_sync_at, field="last_successful_sync_at"
    )
    next_at = _status_time(next_scheduled_sync_at, field="next_scheduled_sync_at")
    if failure_count is not None:
        if (
            isinstance(failure_count, bool)
            or not isinstance(failure_count, int)
            or not 0 <= failure_count <= 1_000_000
        ):
            raise AttentionError("invalid_source_failure_count")
    normalized_error = _source_error_code(error_code)
    now = _now()
    attempt = attempted_at if attempted_at is not None else now
    with _connect(db_path) as conn:
        with _write(conn):
            prior = conn.execute(
                "SELECT * FROM attention_source_status WHERE source=?", (source,)
            ).fetchone()
            if prior is None:
                raise AttentionError("source_status_not_found", 404)
            normalized_message = _safe_text(
                message if message is not None else prior["message"],
                field="source_message",
                limit=300,
            )
            if status in SUCCESS_SOURCE_STATUSES:
                next_success = successful_at if successful_at is not None else attempt
                next_failures = 0
                next_error = None
            elif status in FAILED_SOURCE_STATUSES:
                next_success = (
                    successful_at
                    if successful_at is not None
                    else prior["last_successful_sync_at"]
                )
                next_failures = (
                    failure_count
                    if failure_count is not None
                    else int(prior["failure_count"]) + 1
                )
                next_error = normalized_error
            else:
                next_success = (
                    successful_at
                    if successful_at is not None
                    else prior["last_successful_sync_at"]
                )
                next_failures = (
                    failure_count
                    if failure_count is not None
                    else int(prior["failure_count"])
                )
                next_error = (
                    normalized_error if error_code is not None else prior["error_code"]
                )
            next_schedule = (
                next_at if next_at is not None else prior["next_scheduled_sync_at"]
            )
            conn.execute(
                "UPDATE attention_source_status SET status=?, "
                "last_attempted_sync_at=?, last_successful_sync_at=?, "
                "next_scheduled_sync_at=?, failure_count=?, error_code=?, "
                "message=?, updated_at=? WHERE source=?",
                (
                    status,
                    attempt,
                    next_success,
                    next_schedule,
                    next_failures,
                    next_error,
                    normalized_message,
                    now,
                    source,
                ),
            )
            meaningful = (
                prior["status"] != status
                or prior["message"] != normalized_message
                or prior["error_code"] != next_error
            )
            if meaningful:
                if (
                    prior["last_attempted_sync_at"] is not None
                    and prior["status"] in FAILED_SOURCE_STATUSES
                    and status in SUCCESS_SOURCE_STATUSES
                ):
                    event_type = "sync_recovered"
                elif status in FAILED_SOURCE_STATUSES:
                    event_type = "sync_failed"
                else:
                    event_type = "state_changed"
                conn.execute(
                    "INSERT INTO attention_source_history("
                    "source, event_type, happened_at, prior_status, new_status, "
                    "message, error_code) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        source,
                        event_type,
                        now,
                        prior["status"],
                        status,
                        normalized_message,
                        next_error,
                    ),
                )
            row = conn.execute(
                "SELECT s.*, c.enabled, c.paused "
                "FROM attention_source_status s "
                "JOIN attention_source_control c USING(source) WHERE s.source=?",
                (source,),
            ).fetchone()
    return _source_status_dict(row)


def control_source(
    source: str,
    action: str,
    *,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Pause, resume, disable, or enable one synchronization source."""

    source = _enum(source, OPERATIONAL_SOURCES, field="source")
    action = _enum(action, SOURCE_CONTROL_ACTIONS, field="source_control_action")
    enabled, paused = {
        "pause": (1, 1),
        "resume": (1, 0),
        "disable": (0, 0),
        "enable": (1, 0),
    }[action]
    now = _now()
    with _connect(db_path) as conn:
        with _write(conn):
            prior = conn.execute(
                "SELECT s.*, c.enabled, c.paused "
                "FROM attention_source_status s "
                "JOIN attention_source_control c USING(source) WHERE s.source=?",
                (source,),
            ).fetchone()
            if prior is None:
                raise AttentionError("source_status_not_found", 404)
            changed = int(prior["enabled"]) != enabled or int(prior["paused"]) != paused
            if changed:
                conn.execute(
                    "UPDATE attention_source_control "
                    "SET enabled=?, paused=?, updated_at=? WHERE source=?",
                    (enabled, paused, now, source),
                )
                conn.execute(
                    "INSERT INTO attention_source_history("
                    "source, event_type, happened_at, prior_status, new_status, "
                    "message, error_code) VALUES(?, 'control_changed', ?, ?, ?, ?, NULL)",
                    (
                        source,
                        now,
                        prior["status"],
                        prior["status"],
                        _safe_text(
                            f"Source synchronization {action}d."
                            if action != "resume"
                            else "Source synchronization resumed.",
                            field="source_message",
                            limit=300,
                        ),
                    ),
                )
            row = conn.execute(
                "SELECT s.*, c.enabled, c.paused "
                "FROM attention_source_status s "
                "JOIN attention_source_control c USING(source) WHERE s.source=?",
                (source,),
            ).fetchone()
    return _source_status_dict(row)


def list_source_history(
    source: str | None = None,
    *,
    limit: int = 100,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    if source is not None:
        source = _enum(source, OPERATIONAL_SOURCES, field="source")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise AttentionError("invalid_limit")
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM attention_source_history "
            "WHERE (? IS NULL OR source=?) "
            "ORDER BY happened_at DESC, event_id DESC LIMIT ?",
            (source, source, limit),
        ).fetchall()
    result = []
    for row in rows:
        event = dict(row)
        event["happened_at"] = _iso(event["happened_at"])
        result.append(event)
    return result


def available_projects(*, db_path: Path | str | None = None) -> list[str]:
    """Return projects backed by a connected source or retained queue item."""

    with _connect(db_path) as conn:
        available = {
            str(row[0])
            for row in conn.execute("SELECT DISTINCT project FROM attention_items")
        }
        for row in conn.execute(
            "SELECT s.source, s.status, c.enabled "
            "FROM attention_source_status s "
            "JOIN attention_source_control c USING(source)"
        ):
            project = SOURCE_PROJECTS.get(str(row["source"]))
            if (
                project
                and bool(row["enabled"])
                and row["status"] in CONNECTED_SOURCE_STATUSES
            ):
                available.add(project)
    return ["all", *(project for project in PROJECT_ORDER if project in available)]


def validate_public_url(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 500
        or any(char.isspace() or ord(char) < 32 or char == "\\" for char in value)
    ):
        raise AttentionError("invalid_attention_public_url", 500)
    parsed = urlparse(value)
    try:
        parsed.port
    except ValueError as exc:
        raise AttentionError("invalid_attention_public_url", 500) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.casefold().endswith(".ts.net")
        or parsed.hostname.casefold() == "ts.net"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise AttentionError("invalid_attention_public_url", 500)
    return value.rstrip("/")


def attention_public_url() -> str:
    try:
        from hermes_cli.config import cfg_get, load_config

        value = str(cfg_get(load_config(), "attention", "public_url", default="") or "")
    except Exception as exc:
        raise AttentionError("attention_public_url_missing", 503) from exc
    if not value:
        raise AttentionError("attention_public_url_missing", 503)
    return validate_public_url(value)


def deep_link_for(item_id: str, public_url: str | None = None) -> str:
    item_id = _opaque(item_id, field="item_id")
    return f"{validate_public_url(public_url) if public_url else attention_public_url()}/item/{item_id}"


def _should_interrupt(item: Mapping[str, Any]) -> bool:
    status = item["status"]
    priority = item["priority"]
    return (
        status == "safety_hold"
        or (status == "needs_cal" and priority in {"urgent", "high"})
        or (status == "prepared" and priority == "high")
        or (
            item["item_type"] in {"automation_failure", "server_health"}
            and priority in {"urgent", "high"}
        )
    )


def _notification_plan(
    conn: sqlite3.Connection,
    item: Mapping[str, Any],
    *,
    changed: bool,
    public_url: str | None,
) -> dict[str, Any]:
    notification = conn.execute(
        "SELECT * FROM attention_notifications WHERE item_id=?", (item["item_id"],)
    ).fetchone()
    message_id = str(notification["telegram_message_id"] or "") if notification else ""
    failed_delivery = bool(notification and int(notification["failure_count"] or 0) > 0)
    if not changed and not failed_delivery:
        return {"action": "none"}
    if not message_id and not _should_interrupt(item):
        return {"action": "none"}
    try:
        deep_link = deep_link_for(item["item_id"], public_url)
    except AttentionError:
        if _should_interrupt(item):
            return {"action": "blocked", "reason": "attention_public_url_missing"}
        return {"action": "none"}
    return {
        "action": "edit" if message_id else "send",
        "message_id": message_id or None,
        "deep_link": deep_link,
    }


def _assert_transition(prior: str, new: str) -> None:
    if prior != new and new not in _ALLOWED_TRANSITIONS[prior]:
        raise AttentionError("invalid_status_transition", 409)


def upsert_attention(
    payload: Mapping[str, Any],
    *,
    db_path: Path | str | None = None,
    public_url: str | None = None,
) -> dict[str, Any]:
    """Create or meaningfully update one idempotent Attention item."""

    data = validate_upsert(payload)
    key = _idempotency_key(data["source_type"], data["source_record_id"])
    now = _now()
    actor = (
        "gmail_worker"
        if data["source_type"] == "gmail"
        else (data["source_type"] if data["source_type"] in ACTORS else "virgil")
    )
    with _connect(db_path) as conn:
        with _write(conn):
            existing = conn.execute(
                "SELECT * FROM attention_items WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is None:
                item_id = uuid.uuid4().hex
                version = 1
                conn.execute(
                    "INSERT INTO attention_refs(item_id, created_at) VALUES(?, ?)",
                    (item_id, now),
                )
                conn.execute(
                    """
                    INSERT INTO attention_items(
                        item_id, idempotency_key, source_type, source_record_id,
                        source_event_id, project, item_type, priority, status,
                        title, safe_summary, recommended_action, waiting_on,
                        reason_code, confidence, due_at, expires_at, deferred_until,
                        source_deep_link, prepared_artifact_deep_link,
                        created_at, updated_at, resolved_at, dismissed_at,
                        processing_version, row_version
                    ) VALUES(
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        item_id,
                        key,
                        data["source_type"],
                        data["source_record_id"],
                        data["source_event_id"],
                        data["project"],
                        data["item_type"],
                        data["priority"],
                        data["status"],
                        data["title"],
                        data["safe_summary"],
                        data["recommended_action"],
                        data["waiting_on"],
                        data["reason_code"],
                        data["confidence"],
                        data["due_at"],
                        data["expires_at"],
                        None,
                        data["source_deep_link"],
                        data["prepared_artifact_deep_link"],
                        now,
                        now,
                        now if data["status"] == "resolved" else None,
                        now if data["status"] == "dismissed" else None,
                        data["processing_version"],
                        version,
                    ),
                )
                event_type = (
                    "safety_hold_triggered"
                    if data["status"] == "safety_hold"
                    else "created"
                )
                _append_event(
                    conn,
                    item_id=item_id,
                    event_type=event_type,
                    description=(
                        "Safety hold created."
                        if event_type == "safety_hold_triggered"
                        else "Attention item created."
                    ),
                    actor=actor,
                    prior_status=None,
                    new_status=data["status"],
                    snapshot_version=version,
                    happened_at=now,
                )
                changed = True
            else:
                item_id = str(existing["item_id"])
                if (
                    existing["status"] in _CLOSED_STATUSES
                    and data["source_event_id"] == existing["source_event_id"]
                ):
                    data["status"] = str(existing["status"])
                comparable = {
                    field: existing[field]
                    for field in (
                        "source_event_id",
                        "project",
                        "item_type",
                        "priority",
                        "status",
                        "title",
                        "safe_summary",
                        "recommended_action",
                        "waiting_on",
                        "reason_code",
                        "confidence",
                        "due_at",
                        "expires_at",
                        "source_deep_link",
                        "prepared_artifact_deep_link",
                        "processing_version",
                    )
                }
                next_values = {field: data[field] for field in comparable}
                changed = comparable != next_values
                if changed:
                    prior_status = str(existing["status"])
                    _assert_transition(prior_status, data["status"])
                    version = int(existing["row_version"]) + 1
                    conn.execute(
                        """
                        UPDATE attention_items SET
                            source_event_id=?, project=?, item_type=?, priority=?,
                            status=?, title=?, safe_summary=?, recommended_action=?,
                            waiting_on=?, reason_code=?, confidence=?, due_at=?,
                            expires_at=?, deferred_until=NULL, source_deep_link=?,
                            prepared_artifact_deep_link=?, updated_at=?,
                            resolved_at=?, dismissed_at=?, processing_version=?,
                            row_version=?
                        WHERE item_id=?
                        """,
                        (
                            data["source_event_id"],
                            data["project"],
                            data["item_type"],
                            data["priority"],
                            data["status"],
                            data["title"],
                            data["safe_summary"],
                            data["recommended_action"],
                            data["waiting_on"],
                            data["reason_code"],
                            data["confidence"],
                            data["due_at"],
                            data["expires_at"],
                            data["source_deep_link"],
                            data["prepared_artifact_deep_link"],
                            now,
                            (
                                existing["resolved_at"]
                                if data["status"] == "resolved"
                                else None
                            ),
                            (
                                existing["dismissed_at"]
                                if data["status"] == "dismissed"
                                else None
                            ),
                            data["processing_version"],
                            version,
                            item_id,
                        ),
                    )
                    if (
                        prior_status in _CLOSED_STATUSES
                        and data["status"] in _ACTIVE_STATUSES
                    ):
                        event_type = "reopened"
                        description = "Attention item reopened by a newer source event."
                    elif (
                        data["status"] == "safety_hold"
                        and prior_status != "safety_hold"
                    ):
                        event_type = "safety_hold_triggered"
                        description = "Safety hold triggered."
                    elif data["source_event_id"] != existing["source_event_id"]:
                        event_type = "source_state_changed"
                        description = "Source state changed."
                    else:
                        event_type = "updated"
                        description = "Attention item updated from its source."
                    _append_event(
                        conn,
                        item_id=item_id,
                        event_type=event_type,
                        description=description,
                        actor=actor,
                        prior_status=prior_status,
                        new_status=data["status"],
                        snapshot_version=version,
                        happened_at=now,
                    )
            row = conn.execute(
                "SELECT * FROM attention_items WHERE item_id=?", (item_id,)
            ).fetchone()
            item = _item_dict(row)
            notification = _notification_plan(
                conn, item, changed=changed, public_url=public_url
            )
        return {"item": item, "changed": changed, "notification": notification}


def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["timestamp"] = _iso(cast(float, result.pop("happened_at")))
    return result


def get_attention(
    item_id: str,
    *,
    db_path: Path | str | None = None,
    include_history: bool = True,
) -> dict[str, Any]:
    item_id = _opaque(item_id, field="item_id")
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM attention_items WHERE item_id=?", (item_id,)
        ).fetchone()
        if row is None:
            raise AttentionError("attention_item_not_found", 404)
        result = _item_dict(row)
        if include_history:
            result["activity"] = [
                _event_dict(event)
                for event in conn.execute(
                    "SELECT sequence, event_type, happened_at, safe_description, "
                    "actor, prior_status, new_status, snapshot_version "
                    "FROM attention_events WHERE item_id=? ORDER BY sequence",
                    (item_id,),
                )
            ]
        return result


def _relevant(item: Mapping[str, Any], now: float) -> bool:
    if item["status"] in _CLOSED_STATUSES:
        return False
    expires = item["expires_at"]
    if expires is not None and expires <= now:
        return False
    deferred = item["deferred_until"]
    return not deferred or deferred <= now


def _sydney_day(value: float) -> Any:
    return datetime.fromtimestamp(value, timezone.utc).astimezone(SYDNEY).date()


def _is_calendar_item(item: Mapping[str, Any]) -> bool:
    return item["source_type"] == "calendar" or item["item_type"] == "calendar_event"


def _is_system_degradation(item: Mapping[str, Any]) -> bool:
    return (
        item["source_type"] == "system"
        or item["item_type"] in {"automation_failure", "server_health"}
    ) and item["priority"] in {"urgent", "high"}


def _included_today(item: Mapping[str, Any], now: float) -> bool:
    if not _relevant(item, now):
        return False
    due = item["due_at"]
    due_today = due is not None and _sydney_day(float(due)) == _sydney_day(now)
    return bool(
        item["priority"] == "urgent"
        or item["status"] == "safety_hold"
        or (due is not None and due <= now)
        or (
            item["status"] in {"needs_cal", "deferred"}
            and item["waiting_on"] == "cal"
            and item["priority"] == "high"
        )
        or (
            _is_calendar_item(item)
            and due is not None
            and (due_today or now <= due <= now + 7200)
        )
        or _is_system_degradation(item)
        or (item["status"] == "prepared" and item["priority"] == "high")
        or due_today
        or item["status"] == "deferred"
    )


def _today_key(item: Mapping[str, Any], now: float) -> tuple[Any, ...]:
    due = item["due_at"]
    due_today = due is not None and _sydney_day(float(due)) == _sydney_day(now)
    if item["priority"] == "urgent":
        bucket = 0
    elif item["status"] == "safety_hold":
        bucket = 1
    elif due is not None and due <= now:
        bucket = 2
    elif (
        item["status"] in {"needs_cal", "deferred"}
        and item["waiting_on"] == "cal"
        and item["priority"] == "high"
    ):
        bucket = 3
    elif _is_calendar_item(item) and due is not None and now <= due <= now + 7200:
        bucket = 4
    elif item["status"] == "prepared" and item["priority"] == "high":
        bucket = 5
    elif due_today:
        bucket = 6
    else:
        bucket = 7
    return (
        bucket,
        float(due) if due is not None else float("inf"),
        -float(item["updated_at"]),
        item["item_id"],
    )


def list_attention(
    *,
    view: str = "today",
    project: str = "all",
    limit: int = 200,
    now: float | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    if view not in {"today", "needs-you", "prepared"}:
        raise AttentionError("invalid_attention_view")
    if project != "all" and project not in PROJECTS:
        raise AttentionError("invalid_project")
    try:
        normalized_limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise AttentionError("invalid_limit") from exc
    if isinstance(limit, bool) or not 1 <= normalized_limit <= 200:
        raise AttentionError("invalid_limit")
    current = _now() if now is None else float(now)
    with _connect(db_path) as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM attention_items"
                + ("" if project == "all" else " WHERE project=?"),
                () if project == "all" else (project,),
            )
        ]
    if view == "today":
        rows = [item for item in rows if _included_today(item, current)]
    elif view == "needs-you":
        rows = [
            item
            for item in rows
            if _relevant(item, current)
            and item["waiting_on"] == "cal"
            and item["status"] in {"needs_cal", "safety_hold", "deferred"}
        ]
    else:
        rows = [
            item
            for item in rows
            if _relevant(item, current) and item["status"] == "prepared"
        ]
    rows.sort(key=lambda item: _today_key(item, current))
    return [_item_dict(row) for row in rows[:normalized_limit]]


def attention_brief(
    *,
    now: float | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Build the Today brief deterministically from bounded queue fields."""

    current = _now() if now is None else float(now)
    with _connect(db_path) as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM attention_items")]
        system_row = conn.execute(
            "SELECT s.status, c.enabled, c.paused "
            "FROM attention_source_status s "
            "JOIN attention_source_control c USING(source) WHERE s.source='system'"
        ).fetchone()
    relevant = [item for item in rows if _relevant(item, current)]
    needs_you = [
        item
        for item in relevant
        if item["waiting_on"] == "cal"
        and item["status"] in {"needs_cal", "safety_hold", "deferred"}
    ]
    prepared = [item for item in relevant if item["status"] == "prepared"]
    today = [item for item in relevant if _included_today(item, current)]
    needs_you.sort(key=lambda item: _today_key(item, current))
    prepared.sort(key=lambda item: _today_key(item, current))
    today.sort(key=lambda item: _today_key(item, current))
    calendar = [
        item
        for item in relevant
        if _is_calendar_item(item)
        and item["due_at"] is not None
        and item["due_at"] >= current
    ]
    calendar.sort(key=lambda item: (item["due_at"], item["item_id"]))
    next_event = None
    if calendar:
        event = calendar[0]
        next_event = {
            "item_id": event["item_id"],
            "title": event["title"],
            "start_at": _iso(event["due_at"]),
        }
    active_jobs = sum(
        1
        for item in relevant
        if item["status"] == "monitoring"
        and (item["source_type"] == "agent_job" or item["item_type"] == "agent_job")
    )
    severe_system_item = any(_is_system_degradation(item) for item in relevant)
    if severe_system_item:
        system_health = "degraded"
    elif system_row is None or not bool(system_row["enabled"]):
        system_health = "unknown"
    elif bool(system_row["paused"]) or system_row["status"] in FAILED_SOURCE_STATUSES:
        system_health = "degraded"
    elif system_row["status"] in SUCCESS_SOURCE_STATUSES:
        system_health = "normal"
    else:
        system_health = "unknown"
    recommendation_item = (
        today[0]
        if today
        else needs_you[0]
        if needs_you
        else prepared[0]
        if prepared
        else None
    )
    needs_count = len(needs_you)
    prepared_count = len(prepared)
    sentences = [
        f"{needs_count} item{'s' if needs_count != 1 else ''} "
        f"{'need' if needs_count != 1 else 'needs'} you.",
        f"{prepared_count} prepared item{'s are' if prepared_count != 1 else ' is'} ready.",
    ]
    if needs_you:
        sentences.append(f"{needs_you[0]['title']} is highest priority.")
    if calendar:
        start = datetime.fromtimestamp(
            float(calendar[0]["due_at"]), timezone.utc
        ).astimezone(SYDNEY)
        meridiem = "am" if start.hour < 12 else "pm"
        hour = start.hour % 12 or 12
        sentences.append(
            f"Your next meeting is at {hour}:{start.minute:02d} {meridiem}."
        )
    sentences.append(
        f"{active_jobs} agent job{'s are' if active_jobs != 1 else ' is'} active."
    )
    sentences.append(f"System health is {system_health}.")
    return {
        "summary": " ".join(sentences),
        "needs_you_count": needs_count,
        "prepared_count": prepared_count,
        "next_calendar_event": next_event,
        "active_agent_jobs": active_jobs,
        "system_health": system_health,
        "recommended_action": (
            recommendation_item["recommended_action"]
            if recommendation_item is not None
            else None
        ),
    }


def list_activity(
    *,
    limit: int = 100,
    project: str = "all",
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    if project != "all" and project not in PROJECTS:
        raise AttentionError("invalid_project")
    try:
        normalized_limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise AttentionError("invalid_limit") from exc
    if isinstance(limit, bool) or not 1 <= normalized_limit <= 200:
        raise AttentionError("invalid_limit")
    source_projects = {**SOURCE_PROJECTS, "github": "system"}
    with _connect(db_path) as conn:
        item_rows = conn.execute(
            """
            SELECT e.item_id, e.sequence, e.event_type, e.happened_at,
                   e.safe_description, e.actor, e.prior_status, e.new_status,
                   e.snapshot_version, i.project, i.title
            FROM attention_events e
            LEFT JOIN attention_items i ON i.item_id=e.item_id
            WHERE (?='all' OR i.project=?)
            ORDER BY e.happened_at DESC, e.event_id DESC
            LIMIT ?
            """,
            (project, project, normalized_limit),
        ).fetchall()
        selected_sources = [
            source
            for source, source_project in source_projects.items()
            if project == "all" or source_project == project
        ]
        placeholders = ",".join("?" for _ in selected_sources)
        source_rows = (
            conn.execute(
                "SELECT event_id, source, event_type, happened_at, prior_status, "
                "new_status, message FROM attention_source_history "
                f"WHERE source IN ({placeholders}) "
                "ORDER BY happened_at DESC, event_id DESC LIMIT ?",
                (*selected_sources, normalized_limit),
            ).fetchall()
            if selected_sources
            else []
        )
    activity = [dict(row) for row in item_rows]
    activity.extend(
        {
            "item_id": None,
            "sequence": row["event_id"],
            "event_type": row["event_type"],
            "happened_at": row["happened_at"],
            "safe_description": row["message"],
            "actor": row["source"],
            "prior_status": row["prior_status"],
            "new_status": row["new_status"],
            "snapshot_version": None,
            "project": source_projects[row["source"]],
            "title": f"{str(row['source']).replace('_', ' ').title()} source",
        }
        for row in source_rows
    )
    activity.sort(
        key=lambda event: (
            float(event["happened_at"]),
            int(event["sequence"] or 0),
            str(event["actor"]),
        ),
        reverse=True,
    )
    return [_event_dict(event) for event in activity[:normalized_limit]]


def transition_attention(
    item_id: str,
    action: str,
    *,
    expected_row_version: int,
    deferred_until: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    item_id = _opaque(item_id, field="item_id")
    if action not in {"resolve", "defer", "dismiss", "reopen"}:
        raise AttentionError("invalid_attention_action")
    try:
        normalized_version = int(expected_row_version)
    except (TypeError, ValueError) as exc:
        raise AttentionError("invalid_row_version") from exc
    if isinstance(expected_row_version, bool) or normalized_version < 1:
        raise AttentionError("invalid_row_version")
    now = _now()
    deferred = None
    if action == "defer":
        deferred = _parse_time(deferred_until, field="deferred_until", optional=False)
        if deferred is None or not now < deferred <= now + 366 * 86400:
            raise AttentionError("invalid_deferred_until")
    with _connect(db_path) as conn:
        with _write(conn):
            row = conn.execute(
                "SELECT * FROM attention_items WHERE item_id=?", (item_id,)
            ).fetchone()
            if row is None:
                raise AttentionError("attention_item_not_found", 404)
            if int(row["row_version"]) != normalized_version:
                raise AttentionError("stale_attention_item", 409)
            prior = str(row["status"])
            if action == "resolve":
                if prior in _CLOSED_STATUSES:
                    raise AttentionError("invalid_status_transition", 409)
                new = "resolved"
            elif action == "dismiss":
                if prior in _CLOSED_STATUSES:
                    raise AttentionError("invalid_status_transition", 409)
                new = "dismissed"
            elif action == "defer":
                if prior in _CLOSED_STATUSES:
                    raise AttentionError("invalid_status_transition", 409)
                new = "deferred"
            else:
                if prior not in {"resolved", "dismissed", "stale", "deferred"}:
                    raise AttentionError("invalid_status_transition", 409)
                previous = conn.execute(
                    "SELECT prior_status FROM attention_events "
                    "WHERE item_id=? AND event_type IN ('resolved','dismissed','deferred') "
                    "AND prior_status IS NOT NULL ORDER BY sequence DESC LIMIT 1",
                    (item_id,),
                ).fetchone()
                candidate = str(previous["prior_status"]) if previous else ""
                new = (
                    candidate
                    if candidate in _ACTIVE_STATUSES - {"deferred"}
                    else (
                        "needs_cal"
                        if row["waiting_on"] == "cal"
                        else "prepared"
                        if row["waiting_on"] == "virgil"
                        else "new"
                    )
                )
            _assert_transition(prior, new)
            version = int(row["row_version"]) + 1
            event_type = {
                "resolve": "resolved",
                "defer": "deferred",
                "dismiss": "dismissed",
                "reopen": "reopened",
            }[action]
            conn.execute(
                """
                UPDATE attention_items SET status=?, deferred_until=?, updated_at=?,
                    resolved_at=?, dismissed_at=?, row_version=?
                WHERE item_id=?
                """,
                (
                    new,
                    deferred if action == "defer" else None,
                    now,
                    now if action == "resolve" else None,
                    now if action == "dismiss" else None,
                    version,
                    item_id,
                ),
            )
            _append_event(
                conn,
                item_id=item_id,
                event_type=event_type,
                description={
                    "resolve": "Attention item resolved.",
                    "defer": "Attention item deferred.",
                    "dismiss": "Attention item dismissed.",
                    "reopen": "Attention item reopened.",
                }[action],
                actor="cal",
                prior_status=prior,
                new_status=new,
                snapshot_version=version,
                happened_at=now,
            )
        return get_attention(item_id, db_path=db_path)


def resolve_attention_by_source(
    source_type: str,
    source_record_id: str,
    source_event_id: str,
    *,
    recovered: bool = False,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    source_type = _enum(source_type, SOURCE_TYPES, field="source_type")
    source_record_id = _opaque(source_record_id, field="source_record_id")
    source_event_id = _opaque(source_event_id, field="source_event_id")
    key = _idempotency_key(source_type, source_record_id)
    now = _now()
    actor = (
        "gmail_worker"
        if source_type == "gmail"
        else (source_type if source_type in ACTORS else "system")
    )
    with _connect(db_path) as conn:
        with _write(conn):
            row = conn.execute(
                "SELECT * FROM attention_items WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is None or row["status"] not in _ACTIVE_STATUSES:
                return None
            prior = str(row["status"])
            version = int(row["row_version"]) + 1
            conn.execute(
                "UPDATE attention_items SET source_event_id=?, status='resolved', "
                "deferred_until=NULL, updated_at=?, resolved_at=?, dismissed_at=NULL, "
                "row_version=? WHERE item_id=?",
                (source_event_id, now, now, version, row["item_id"]),
            )
            _append_event(
                conn,
                item_id=row["item_id"],
                event_type=(
                    "processing_failure_recovered"
                    if recovered
                    else "source_state_changed"
                ),
                description=(
                    "Processing failure recovered."
                    if recovered
                    else "Source system indicates the item is resolved."
                ),
                actor=actor,
                prior_status=prior,
                new_status="resolved",
                snapshot_version=version,
                happened_at=now,
            )
            item_id = str(row["item_id"])
        return get_attention(item_id, db_path=db_path)


def list_source_records(
    source_type: str,
    *,
    active_only: bool = False,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Read queue records for one source without consulting that source."""

    source_type = _enum(source_type, SOURCE_TYPES, field="source_type")
    if not isinstance(active_only, bool):
        raise AttentionError("invalid_active_only")
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM attention_items WHERE source_type=? "
            "ORDER BY updated_at DESC, item_id",
            (source_type,),
        ).fetchall()
    if active_only:
        rows = [row for row in rows if row["status"] in _ACTIVE_STATUSES]
    return [_item_dict(row) for row in rows]


def _resolve_reconciled_rows(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    source_event_id: str,
    description: str,
    now: float,
    public_url: str | None,
) -> list[dict[str, Any]]:
    results = []
    for row in rows:
        prior = str(row["status"])
        version = int(row["row_version"]) + 1
        conn.execute(
            "UPDATE attention_items SET source_event_id=?, status='resolved', "
            "deferred_until=NULL, updated_at=?, resolved_at=?, dismissed_at=NULL, "
            "row_version=? WHERE item_id=?",
            (source_event_id, now, now, version, row["item_id"]),
        )
        _append_event(
            conn,
            item_id=row["item_id"],
            event_type="source_state_changed",
            description=description,
            actor="system",
            prior_status=prior,
            new_status="resolved",
            snapshot_version=version,
            happened_at=now,
        )
        updated = conn.execute(
            "SELECT * FROM attention_items WHERE item_id=?", (row["item_id"],)
        ).fetchone()
        item = _item_dict(updated)
        results.append({
            "item": item,
            "notification": _notification_plan(
                conn, item, changed=True, public_url=public_url
            ),
        })
    return results


def resolve_missing_source_records(
    source_type: str,
    seen_record_ids: Any,
    source_event_id: str,
    *,
    record_prefix: str = "",
    expires_before: str | float | None = None,
    db_path: Path | str | None = None,
    public_url: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve records absent from a caller-confirmed complete source snapshot."""

    source_type = _enum(source_type, SOURCE_TYPES, field="source_type")
    source_event_id = _opaque(source_event_id, field="source_event_id")
    if not isinstance(record_prefix, str):
        raise AttentionError("invalid_record_prefix")
    if record_prefix:
        try:
            record_prefix = _opaque(record_prefix, field="record_prefix")
        except AttentionError as exc:
            raise AttentionError("invalid_record_prefix") from exc
    if isinstance(seen_record_ids, (str, bytes, Mapping)):
        raise AttentionError("invalid_seen_record_ids")
    try:
        seen = {_opaque(value, field="source_record_id") for value in seen_record_ids}
    except (TypeError, AttentionError) as exc:
        raise AttentionError("invalid_seen_record_ids") from exc
    if len(seen) > 10_000:
        raise AttentionError("invalid_seen_record_ids")
    expiry = _status_time(expires_before, field="expires_before")
    now = _now()
    with _connect(db_path) as conn:
        with _write(conn):
            candidates = conn.execute(
                "SELECT * FROM attention_items WHERE source_type=?",
                (source_type,),
            ).fetchall()
            rows = [
                row
                for row in candidates
                if row["status"] in _ACTIVE_STATUSES
                and row["source_record_id"].startswith(record_prefix)
                and row["source_record_id"] not in seen
                and (
                    expiry is None
                    or (row["expires_at"] is not None and row["expires_at"] <= expiry)
                )
            ]
            return _resolve_reconciled_rows(
                conn,
                rows,
                source_event_id=source_event_id,
                description="Item resolved after source reconciliation.",
                now=now,
                public_url=public_url,
            )


def resolve_expired_attention(
    *,
    now: str | float | None = None,
    source_type: str | None = None,
    db_path: Path | str | None = None,
    public_url: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve active records whose adapter-supplied expiry has elapsed."""

    if source_type is not None:
        source_type = _enum(source_type, SOURCE_TYPES, field="source_type")
    current = _status_time(now, field="now") if now is not None else _now()
    assert current is not None
    with _connect(db_path) as conn:
        with _write(conn):
            rows = [
                row
                for row in conn.execute(
                    "SELECT * FROM attention_items "
                    "WHERE expires_at IS NOT NULL AND expires_at<=? "
                    "AND (? IS NULL OR source_type=?)",
                    (current, source_type, source_type),
                )
                if row["status"] in _ACTIVE_STATUSES
            ]
            return _resolve_reconciled_rows(
                conn,
                rows,
                source_event_id=f"expiry:{int(current)}",
                description="Item resolved after its operational expiry.",
                now=current,
                public_url=public_url,
            )


def record_notification(
    item_id: str,
    *,
    success: bool,
    message_id: str | None = None,
    db_path: Path | str | None = None,
) -> None:
    item_id = _opaque(item_id, field="item_id")
    if message_id is not None and message_id != "":
        message_id = _opaque(message_id, field="telegram_message_id")
    now = _now()
    with _connect(db_path) as conn:
        with _write(conn):
            item = conn.execute(
                "SELECT priority, status FROM attention_items WHERE item_id=?",
                (item_id,),
            ).fetchone()
            if item is None:
                raise AttentionError("attention_item_not_found", 404)
            existing = conn.execute(
                "SELECT * FROM attention_notifications WHERE item_id=?", (item_id,)
            ).fetchone()
            stored_message_id = (
                message_id
                or (str(existing["telegram_message_id"] or "") if existing else "")
                or None
            )
            failures = (
                0 if success else int(existing["failure_count"] if existing else 0) + 1
            )
            conn.execute(
                """
                INSERT INTO attention_notifications(
                    item_id, telegram_message_id, last_notified_priority,
                    last_notified_status, failure_count, last_attempt_at,
                    last_success_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    telegram_message_id=excluded.telegram_message_id,
                    last_notified_priority=excluded.last_notified_priority,
                    last_notified_status=excluded.last_notified_status,
                    failure_count=excluded.failure_count,
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=excluded.last_success_at
                """,
                (
                    item_id,
                    stored_message_id,
                    item["priority"],
                    item["status"],
                    failures,
                    now,
                    now
                    if success
                    else (existing["last_success_at"] if existing else None),
                ),
            )


def prune_attention(
    *,
    now: float | None = None,
    db_path: Path | str | None = None,
) -> dict[str, int]:
    current = _now() if now is None else float(now)
    with _connect(db_path) as conn:
        with _write(conn):
            closed = conn.execute(
                "DELETE FROM attention_items WHERE status IN ('resolved','dismissed','stale') "
                "AND COALESCE(resolved_at, dismissed_at, updated_at) < ?",
                (current - ACTIVE_RETENTION_DAYS * 86400,),
            ).rowcount
            events = conn.execute(
                "DELETE FROM attention_events WHERE happened_at < ?",
                (current - ACTIVITY_RETENTION_DAYS * 86400,),
            ).rowcount
            refs = conn.execute(
                "DELETE FROM attention_refs WHERE item_id NOT IN "
                "(SELECT item_id FROM attention_items) AND item_id NOT IN "
                "(SELECT item_id FROM attention_events)"
            ).rowcount
    return {"items_pruned": closed, "events_pruned": events, "refs_pruned": refs}


def backup_attention(
    output: Path | str | None = None,
    *,
    db_path: Path | str | None = None,
) -> Path:
    source = Path(db_path) if db_path is not None else attention_db_path()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = (
        Path(output) if output else source.parent / "backups" / f"attention-{stamp}.db"
    )
    _secure_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise AttentionError("attention_backup_exists")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(destination, flags, 0o600)
    os.close(fd)
    try:
        with _connect(source) as conn:
            target = sqlite3.connect(destination)
            try:
                conn.backup(target)
            finally:
                target.close()
        _secure_file(destination)
        return destination
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def delete_attention(
    item_id: str,
    *,
    db_path: Path | str | None = None,
) -> bool:
    item_id = _opaque(item_id, field="item_id")
    with _connect(db_path) as conn:
        with _write(conn):
            return bool(
                conn.execute(
                    "DELETE FROM attention_refs WHERE item_id=?", (item_id,)
                ).rowcount
            )


def _read_payload(path: str) -> Mapping[str, Any]:
    if path == "-":
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    else:
        candidate = Path(path)
        if candidate.is_symlink() or not candidate.is_file():
            raise AttentionError("invalid_payload_file")
        if candidate.stat().st_size > MAX_REQUEST_BYTES:
            raise AttentionError("payload_too_large")
        raw = candidate.read_bytes()
    if len(raw) > MAX_REQUEST_BYTES:
        raise AttentionError("payload_too_large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AttentionError("invalid_payload") from exc
    if not isinstance(value, Mapping):
        raise AttentionError("invalid_payload")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes Attention Queue operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    upsert = sub.add_parser("upsert", help="Validate and upsert bounded metadata")
    upsert.add_argument("--file", required=True, help="JSON file, or - for stdin")
    listing = sub.add_parser("list", help="List operational items")
    listing.add_argument(
        "--view", choices=["today", "needs-you", "prepared"], default="today"
    )
    listing.add_argument("--project", choices=["all", *sorted(PROJECTS)], default="all")
    show = sub.add_parser("show", help="Show one item and sanitized history")
    show.add_argument("item_id")
    sub.add_parser("prune", help="Apply bounded retention")
    backup = sub.add_parser("backup", help="Create a consistent SQLite backup")
    backup.add_argument("--output")
    delete = sub.add_parser("delete", help="Permanently delete one item and its events")
    delete.add_argument("item_id")
    delete.add_argument("--confirm", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "upsert":
            result: Any = upsert_attention(_read_payload(args.file))
        elif args.command == "list":
            result = list_attention(view=args.view, project=args.project)
        elif args.command == "show":
            result = get_attention(args.item_id)
        elif args.command == "prune":
            result = prune_attention()
        elif args.command == "backup":
            result = {"backup": str(backup_attention(args.output))}
        else:
            if args.confirm != f"DELETE-{args.item_id}":
                raise AttentionError("delete_confirmation_mismatch")
            result = {"deleted": delete_attention(args.item_id)}
        print(json.dumps(result, sort_keys=True))
        return 0
    except AttentionError as exc:
        print(json.dumps({"error": exc.code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

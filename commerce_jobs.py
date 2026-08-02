"""Durable, deterministic runtime state for governed commerce launch jobs.

Cogitator remains authoritative for business facts and all financial
governance records. This module stores only Hermes operational state and
references to those external records. It performs no network or provider work.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from hermes_constants import get_hermes_home

SCHEMA_VERSION = 2
STATE_MACHINE_VERSION = 2
BUSY_TIMEOUT_MS = 5_000
DEFAULT_STATE_TIMEOUT = timedelta(hours=72)
HANDOFF_TTL = timedelta(minutes=30)
ACTION_APPROVAL_TTL = timedelta(minutes=15)

STATES = frozenset({
    "requested",
    "planning",
    "ready",
    "executing_read_only",
    "awaiting_purchase_approval",
    "awaiting_dns_approval",
    "awaiting_publication_approval",
    "awaiting_cal",
    "executing",
    "resuming",
    "verifying",
    "uncertain_external_state",
    "reconciliation_required",
    "timed_out",
    "paused",
    "completed",
    "cancelled",
    "failed",
})
TERMINAL_STATES = frozenset({"completed", "cancelled", "failed"})
UNCERTAINTY_STATES = frozenset({
    "uncertain_external_state",
    "reconciliation_required",
})
TIMEOUT_EXCLUDED_STATES = (
    TERMINAL_STATES | UNCERTAINTY_STATES | {"executing", "timed_out", "paused"}
)
EXECUTION_STATES = frozenset({"executing_read_only", "executing"})
RECOVERABLE_EFFECT_CLASSES = frozenset({"read_only", "idempotent_write"})

_CORE_TRANSITIONS: dict[str, set[str]] = {
    "requested": {"planning"},
    "planning": {"ready", "awaiting_cal"},
    "ready": {
        "awaiting_purchase_approval",
        "executing_read_only",
        # Idempotent writes need no approval, so `ready` must be able to begin
        # one directly -- otherwise resuming a paused/timed-out job whose next
        # step is a write dead-ends. Consequential actions are still barred
        # here: dispatch requires a live approval row (_dispatch_action_in_tx).
        "executing",
    },
    "executing_read_only": {
        "ready",
        "awaiting_dns_approval",
        "executing",
        "awaiting_cal",
        "verifying",
        "completed",
    },
    "awaiting_purchase_approval": {"executing", "cancelled"},
    "awaiting_dns_approval": {"executing", "cancelled"},
    "awaiting_publication_approval": {"executing", "paused"},
    "awaiting_cal": {"resuming", "timed_out"},
    "executing": {
        "executing_read_only",
        "ready",
        "uncertain_external_state",
        "awaiting_cal",
    },
    "resuming": {
        "planning",
        "ready",
        "executing_read_only",
        "awaiting_purchase_approval",
        "awaiting_dns_approval",
        "awaiting_publication_approval",
        "awaiting_cal",
        "executing",
        "verifying",
    },
    "verifying": {"awaiting_publication_approval", "ready"},
    "uncertain_external_state": {"reconciliation_required"},
    "reconciliation_required": {"ready", "failed"},
    "timed_out": {"ready", "cancelled"},
    "paused": {"ready", "cancelled"},
}

# Pause, cancellation, and timeouts are controls, not shortcuts around uncertainty.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {}
for _state in STATES:
    _targets = set(_CORE_TRANSITIONS.get(_state, set()))
    if _state not in (
        TERMINAL_STATES | UNCERTAINTY_STATES | {"executing", "paused", "timed_out"}
    ):
        _targets.update({"paused", "cancelled", "timed_out"})
    ALLOWED_TRANSITIONS[_state] = frozenset(_targets)

_PLAN_FIELDS = (
    "domain",
    "tld",
    "registrar",
    "provider_account",
    "prices",
    "currencies",
    "term_length",
    "auto_renew",
    "subscription_recurrence",
    "shopify_plan_tier",
    "product_price",
    "launch_date",
)
_ACTION_RUNTIME_FIELDS = frozenset({
    "attempt",
    "attempt_count",
    "created_at",
    "dispatched_at",
    "finished_at",
    "started_at",
    "terminal_at",
    "timestamp",
    "updated_at",
})
_SHORT_FORBIDDEN_KEYS = frozenset({"pan", "cvv", "cvc", "otp", "mfa", "3ds"})
_SHORT_FORBIDDEN_ALIASES = frozenset({
    "pannumber",
    "paymentpan",
    "primaryaccountnumber",
    "cardpan",
    "cardcvv",
    "cardcvc",
    "onetimepassword",
    "mfacode",
    "3dscode",
    "3dsvalue",
})
_FORBIDDEN_KEY_PARTS = frozenset({
    "cardnumber",
    "expiry",
    "expiration",
    "expdate",
    "securitycode",
    "password",
    "passcode",
    "bankingcredential",
    "bankcredential",
    "banktoken",
    "revolutcredential",
    "privatekey",
    "secretkey",
    "apikey",
    "accesstoken",
    "refreshtoken",
    "authtoken",
    "sessioncookie",
    "sessiontoken",
    "browserformvalue",
    "liveformvalue",
    "decryptedcredential",
    "identitydocument",
    "identitydoc",
    "passportnumber",
    "driverlicence",
    "driverlicense",
    "customercard",
})
# Boundaries are non-alphanumeric, not merely non-digit. A 13-19 digit run
# buried inside a longer hex token is a sha256 or an evidence digest, not a
# card number -- roughly 5% of receipts carry one that also passes Luhn, and
# screening those out rejected legitimate receipts. Real card data is always
# delimited (quote, colon, equals, space), so it still matches.
_PAN_RE = re.compile(r"(?<![0-9A-Za-z])(?:\d[ -]?){13,19}(?![0-9A-Za-z])")
_EXPIRY_RE = re.compile(r"(?<![\d/-])(?:0[1-9]|1[0-2])\s*[/\-]\s*\d{2,4}(?![\d/-])")
_CVV_RE = re.compile(
    r"\b(?:cvv|cvc|security\s*code)\s*[:=]?\s*\d{3,4}\b", re.IGNORECASE
)
_AUTH_RE = re.compile(
    r"\b(?:password|passcode|mfa|3ds|otp|bank(?:ing)?\s*token)"
    r"\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"
    r"|\b(?:sk|rk|pk)_(?:live|prod)_[A-Za-z0-9]{8,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|cgh_[A-Za-z0-9_-]{43}"
    r"|\b(?:sessionid|session_token|auth_token)\s*=\s*[^\s;]+)",
    re.IGNORECASE,
)


class CommerceJobError(ValueError):
    """Stable safe error containing only a reason code and field name."""

    def __init__(self, code: str, field: str = ""):
        self.code = code
        self.field = field
        suffix = f":{field}" if field else ""
        super().__init__(f"{code}{suffix}")


class CommerceConfigurationError(CommerceJobError):
    pass


class CommerceNotFoundError(CommerceJobError):
    pass


class CommerceInvalidTransitionError(CommerceJobError):
    pass


class CommerceStaleVersionError(CommerceJobError):
    pass


class CommerceForbiddenDataError(CommerceJobError):
    pass


class CommerceActionError(CommerceJobError):
    pass


class CommerceGateError(CommerceJobError):
    pass


class CommerceRecoveryError(CommerceJobError):
    pass


def _fail(error_type: type[CommerceJobError], code: str, field: str = "") -> None:
    raise error_type(code, field)


def default_db_path() -> Path:
    return get_hermes_home() / "commerce" / "commerce_jobs.db"


def _utc(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        _fail(CommerceConfigurationError, "timezone_required", "now")
    return result.astimezone(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    reject_forbidden_data(value)
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        _fail(CommerceConfigurationError, "invalid_json", "payload")
    raise AssertionError("unreachable")


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_objective(value: str) -> str:
    text = _safe_text(value, "objective", required=True, max_chars=4_000)
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def objective_fingerprint(value: str) -> str:
    return _fingerprint({"normalized_objective": normalize_objective(value)})


def plan_fingerprint(plan: Mapping[str, Any]) -> str:
    _mapping(plan, "plan")
    material = {field: plan.get(field) for field in _PLAN_FIELDS}
    return _fingerprint(material)


def _without_runtime_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_runtime_fields(child)
            for key, child in value.items()
            if str(key).lower() not in _ACTION_RUNTIME_FIELDS
        }
    if isinstance(value, list):
        return [_without_runtime_fields(child) for child in value]
    return value


def action_fingerprint(
    *,
    action_type: str,
    provider: str,
    effect_class: str,
    target_state: str,
    request: Mapping[str, Any],
) -> str:
    envelope = {
        "action_type": _safe_text(
            action_type, "action_type", required=True, max_chars=120
        ),
        "provider": _safe_text(provider, "provider", max_chars=120),
        "effect_class": effect_class,
        "target_state": target_state,
        "request": _without_runtime_fields(_mapping(request, "request")),
    }
    if effect_class not in RECOVERABLE_EFFECT_CLASSES | {"consequential"}:
        _fail(CommerceActionError, "invalid_effect_class", "effect_class")
    if target_state and target_state not in STATES:
        _fail(CommerceActionError, "unknown_state", "target_state")
    return _fingerprint(envelope)


def _key_compact(key: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(key).casefold())


def _luhn(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        number = int(char)
        if index % 2 == parity:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def _reject_text(value: str, field: str) -> None:
    if (
        _EXPIRY_RE.search(value)
        or _CVV_RE.search(value)
        or _AUTH_RE.search(value)
        or _SECRET_VALUE_RE.search(value)
    ):
        _fail(CommerceForbiddenDataError, "forbidden_sensitive_value", field)
    for match in _PAN_RE.finditer(value):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            _fail(CommerceForbiddenDataError, "forbidden_card_value", field)


def reject_forbidden_data(value: Any, field: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_field = f"{field}.[key]"
            if not isinstance(key, str):
                _fail(CommerceConfigurationError, "invalid_json_key", key_field)
            _reject_text(key, key_field)
            compact = _key_compact(key)
            if (
                compact in _SHORT_FORBIDDEN_KEYS
                or compact in _SHORT_FORBIDDEN_ALIASES
                or any(token in compact for token in _FORBIDDEN_KEY_PARTS)
            ):
                _fail(
                    CommerceForbiddenDataError,
                    "forbidden_sensitive_field",
                    key_field,
                )
            reject_forbidden_data(child, f"{field}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            reject_forbidden_data(child, f"{field}[{index}]")
    elif isinstance(value, str):
        _reject_text(value, field)


def _safe_text(
    value: Any,
    field: str,
    *,
    required: bool = False,
    max_chars: int = 500,
) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        _fail(CommerceConfigurationError, "invalid_text", field)
    result = " ".join(value.strip().split())
    if required and not result:
        _fail(CommerceConfigurationError, "missing_field", field)
    if len(result) > max_chars:
        _fail(CommerceConfigurationError, "field_too_long", field)
    _reject_text(result, field)
    return result


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(CommerceConfigurationError, "invalid_object", field)
    reject_forbidden_data(value, field)
    return value


def _display_json(raw: Any) -> Any:
    if not isinstance(raw, str):
        return {"redacted": True, "reason": "unsafe_or_invalid_persisted_json"}
    try:
        value = json.loads(raw)
        reject_forbidden_data(value)
        return value
    except (json.JSONDecodeError, CommerceJobError):
        return {"redacted": True, "reason": "unsafe_or_invalid_persisted_json"}


def _new_id(prefix: str) -> str:
    # Break UUIDs at non-PAN separators so generated IDs cannot resemble cards.
    return f"{prefix}_{str(uuid.uuid4()).replace('-', '_')}"


def _state_sql() -> str:
    return ",".join(f"'{state}'" for state in sorted(STATES))


_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    requester TEXT NOT NULL,
    original_objective TEXT NOT NULL,
    normalized_objective TEXT NOT NULL,
    objective_fingerprint TEXT NOT NULL,
    state_machine_version INTEGER NOT NULL,
    current_state TEXT NOT NULL CHECK(current_state IN ({_state_sql()})),
    current_step TEXT NOT NULL DEFAULT '',
    current_gate_id TEXT NOT NULL DEFAULT '',
    browser_session TEXT NOT NULL,
    substatus TEXT NOT NULL DEFAULT '',
    plan_json TEXT NOT NULL,
    plan_fingerprint TEXT NOT NULL,
    paused_from_state TEXT NOT NULL DEFAULT '',
    state_entered_at TEXT NOT NULL,
    deadline_at TEXT,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    row_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active_objective
    ON jobs(requester, objective_fingerprint) WHERE active=1;
CREATE INDEX IF NOT EXISTS idx_jobs_state_active ON jobs(current_state, active);

CREATE TABLE IF NOT EXISTS job_events (
    event_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    event_type TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, sequence)
);
CREATE TRIGGER IF NOT EXISTS job_events_no_update
BEFORE UPDATE ON job_events
BEGIN
    SELECT RAISE(ABORT, 'job_events_append_only');
END;
CREATE TRIGGER IF NOT EXISTS job_events_no_delete
BEFORE DELETE ON job_events
BEGIN
    SELECT RAISE(ABORT, 'job_events_append_only');
END;
CREATE TRIGGER IF NOT EXISTS job_events_strict_sequence
BEFORE INSERT ON job_events
WHEN NEW.sequence != COALESCE(
    (SELECT MAX(sequence) + 1 FROM job_events WHERE job_id=NEW.job_id), 1
)
BEGIN
    SELECT RAISE(ABORT, 'job_events_invalid_sequence');
END;

CREATE TABLE IF NOT EXISTS job_actions (
    action_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    action_type TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    effect_class TEXT NOT NULL CHECK(effect_class IN
        ('read_only','idempotent_write','consequential')),
    target_state TEXT NOT NULL DEFAULT '',
    action_status TEXT NOT NULL CHECK(action_status IN
        ('planned','dispatched','recoverable','succeeded','failed','uncertain')),
    idempotency_key TEXT NOT NULL,
    action_fingerprint TEXT NOT NULL,
    plan_fingerprint TEXT NOT NULL,
    approval_reference TEXT NOT NULL DEFAULT '',
    approval_fingerprint TEXT NOT NULL DEFAULT '',
    approval_status TEXT NOT NULL DEFAULT 'unbound'
        CHECK(approval_status IN ('unbound','live','stale')),
    approval_expires_at TEXT,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    dispatched_at TEXT,
    started_at TEXT,
    terminal_at TEXT,
    uncertainty INTEGER NOT NULL DEFAULT 0 CHECK(uncertainty IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_job_actions_recovery
    ON job_actions(action_status, effect_class);

CREATE TABLE IF NOT EXISTS provider_accounts (
    account_reference TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    environment_tag TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    identity_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gates (
    gate_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    gate_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('open','completed','consumed','expired','invalidated')),
    human_action TEXT NOT NULL,
    provider_truth_reference TEXT NOT NULL,
    browser_session TEXT NOT NULL,
    handoff_token_hash TEXT NOT NULL DEFAULT '',
    handoff_expires_at TEXT,
    done_requested_at TEXT,
    approval_reference TEXT NOT NULL DEFAULT '',
    approval_fingerprint TEXT NOT NULL DEFAULT '',
    opening_evidence_json TEXT NOT NULL,
    completion_evidence_json TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    expires_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_gates_job_status ON gates(job_id, status);
"""


class CommerceJobStore:
    def __init__(self, db_path: str | Path | None = None):
        self._default_path = db_path is None
        self.path = Path(db_path) if db_path is not None else default_db_path()

    def _prepare_path(self) -> None:
        if self._default_path:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.parent.chmod(0o700)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                )
                os.close(descriptor)
            except FileExistsError:
                pass
        if self._default_path:
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode):
                _fail(
                    CommerceConfigurationError,
                    "database_not_regular_file",
                    "db_path",
                )
            self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return connection

    def initialize(self) -> None:
        self._prepare_path()
        connection = self._connect()
        try:
            # Read the table before the version, never the other way round.
            # A concurrent initializer creates the tables and stamps
            # user_version in one transaction, so a stale `has_jobs` is
            # harmless but a stale `version` read before a fresh `has_jobs`
            # read looks exactly like a downgrade and fails a healthy fresh
            # database. Reading in this order makes `version` no older than
            # `has_jobs`, so that false positive cannot occur; a genuinely
            # stale database still fails both checks below.
            has_jobs = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
            ).fetchone()
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                _fail(
                    CommerceConfigurationError,
                    "future_schema_version",
                    "user_version",
                )
            if has_jobs and version < SCHEMA_VERSION:
                _fail(
                    CommerceConfigurationError,
                    "unsupported_schema_version",
                    "user_version",
                )
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + _SCHEMA_SQL
                + f"\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;"
            )
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _read(self) -> sqlite3.Connection:
        self.initialize()
        return self._connect()

    @staticmethod
    def _job_row(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            _fail(CommerceNotFoundError, "job_not_found", "job_id")
        return row

    @staticmethod
    def _action_row(connection: sqlite3.Connection, action_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM job_actions WHERE action_id=?", (action_id,)
        ).fetchone()
        if row is None:
            _fail(CommerceNotFoundError, "action_not_found", "action_id")
        return row

    @staticmethod
    def _gate_row(connection: sqlite3.Connection, gate_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM gates WHERE gate_id=?", (gate_id,)
        ).fetchone()
        if row is None:
            _fail(CommerceNotFoundError, "gate_not_found", "gate_id")
        return row

    @staticmethod
    def _unresolved_action(
        connection: sqlite3.Connection, job_id: str
    ) -> sqlite3.Row | None:
        rows = connection.execute(
            """SELECT * FROM job_actions
               WHERE job_id=?
                 AND action_status IN ('dispatched','recoverable','uncertain')
               ORDER BY created_at,action_id LIMIT 2""",
            (job_id,),
        ).fetchall()
        if len(rows) > 1:
            _fail(CommerceActionError, "multiple_inflight_actions", "job_id")
        return rows[0] if rows else None

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        event_type: str,
        from_state: str,
        to_state: str,
        actor: str,
        reason_code: str,
        evidence: Mapping[str, Any] | None,
        now: datetime | None,
    ) -> None:
        safe_actor = _safe_text(actor, "actor", required=True, max_chars=120)
        safe_reason = _safe_text(
            reason_code, "reason_code", required=True, max_chars=120
        )
        payload = _mapping(evidence or {}, "evidence")
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM job_events WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO job_events
               (event_id,job_id,sequence,event_type,from_state,to_state,actor,
                reason_code,evidence_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                _new_id("ce"),
                job_id,
                sequence,
                event_type,
                from_state,
                to_state,
                safe_actor,
                safe_reason,
                canonical_json(payload),
                iso_utc(now),
            ),
        )

    @staticmethod
    def _decode_job(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["active"] = bool(result["active"])
        raw_plan = result.pop("plan_json")
        for key in ("requester", "original_objective", "normalized_objective"):
            try:
                reject_forbidden_data(result[key], key)
            except CommerceForbiddenDataError:
                result[key] = "[REDACTED_UNSAFE_VALUE]"
        result["plan"] = _display_json(raw_plan)
        return result

    @staticmethod
    def _decode_action(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["uncertainty"] = bool(result["uncertainty"])
        result["request"] = _display_json(result.pop("request_json"))
        result["result"] = _display_json(result.pop("result_json"))
        return result

    @staticmethod
    def _decode_gate(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result.pop("handoff_token_hash", None)
        result["opening_evidence"] = _display_json(result.pop("opening_evidence_json"))
        result["completion_evidence"] = _display_json(
            result.pop("completion_evidence_json")
        )
        return result

    @staticmethod
    def _handoff_digest(token: Any) -> str:
        if (
            not isinstance(token, str)
            or re.fullmatch(r"cgh_[A-Za-z0-9_-]{43}", token) is None
        ):
            _fail(CommerceGateError, "invalid_handoff_token", "handoff_token")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _active_handoff_gate(
        self,
        connection: sqlite3.Connection,
        gate_id: str,
        *,
        now: datetime,
        token: str | None = None,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        gate = self._gate_row(connection, gate_id)
        job = self._job_row(connection, gate["job_id"])
        timestamp = iso_utc(now)
        token_matches = True
        if token is not None:
            supplied_digest = self._handoff_digest(token)
            stored_digest = str(gate["handoff_token_hash"] or "")
            comparable_digest = (
                stored_digest
                if re.fullmatch(r"[0-9a-f]{64}", stored_digest)
                else "0" * 64
            )
            token_matches = hmac.compare_digest(comparable_digest, supplied_digest)
        if gate["status"] != "open":
            _fail(CommerceGateError, "gate_not_open", "status")
        if gate["gate_type"] == "action_approval":
            _fail(CommerceGateError, "gate_not_handoff_eligible", "gate_type")
        if gate["expires_at"] and gate["expires_at"] <= timestamp:
            _fail(CommerceGateError, "gate_expired", "expires_at")
        if (
            not job["active"]
            or job["current_state"] != "awaiting_cal"
            or job["current_gate_id"] != gate["gate_id"]
        ):
            _fail(CommerceGateError, "gate_not_active_handoff", "gate_id")
        if gate["browser_session"] != job["browser_session"]:
            _fail(CommerceGateError, "gate_session_mismatch", "browser_session")
        if token is not None:
            if not gate["handoff_expires_at"]:
                _fail(CommerceGateError, "handoff_not_issued", "handoff_expires_at")
            if gate["handoff_expires_at"] <= timestamp:
                _fail(CommerceGateError, "handoff_expired", "handoff_expires_at")
            if not token_matches:
                _fail(CommerceGateError, "invalid_handoff_token", "handoff_token")
        return gate, job

    @staticmethod
    def _handoff_expiry(gate: sqlite3.Row, now: datetime) -> str:
        expiry = iso_utc(now + HANDOFF_TTL)
        if gate["expires_at"] and gate["expires_at"] < expiry:
            return str(gate["expires_at"])
        return expiry

    def create_or_attach_job(
        self,
        *,
        requester: str,
        objective: str,
        origin: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        requester_text = _safe_text(
            requester, "requester", required=True, max_chars=200
        )
        original = _safe_text(objective, "objective", required=True, max_chars=4_000)
        origin_payload = (
            dict(_mapping(origin, "origin")) if origin is not None else None
        )
        normalized = normalize_objective(original)
        fingerprint = _fingerprint({"normalized_objective": normalized})
        timestamp = _utc(now)
        with self._write() as connection:
            existing = connection.execute(
                """SELECT * FROM jobs
                   WHERE requester=? AND objective_fingerprint=? AND active=1""",
                (requester_text, fingerprint),
            ).fetchone()
            if existing is not None:
                result = self._decode_job(existing)
                result["attached"] = True
                return result
            job_id = _new_id("cj")
            created = iso_utc(timestamp)
            deadline = iso_utc(timestamp + DEFAULT_STATE_TIMEOUT)
            empty_plan: dict[str, Any] = {}
            connection.execute(
                """INSERT INTO jobs
                   (job_id,requester,original_objective,normalized_objective,
                    objective_fingerprint,state_machine_version,current_state,
                    current_step,current_gate_id,browser_session,substatus,plan_json,
                    plan_fingerprint,paused_from_state,state_entered_at,deadline_at,
                    active,row_version,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    requester_text,
                    original,
                    normalized,
                    fingerprint,
                    STATE_MACHINE_VERSION,
                    "requested",
                    "",
                    "",
                    f"commerce_{job_id}",
                    "",
                    canonical_json(empty_plan),
                    plan_fingerprint(empty_plan),
                    "",
                    created,
                    deadline,
                    1,
                    0,
                    created,
                    created,
                ),
            )
            self._append_event(
                connection,
                job_id=job_id,
                event_type="job_created",
                from_state="",
                to_state="requested",
                actor=requester_text,
                reason_code="request_accepted",
                evidence=(
                    {"origin": origin_payload} if origin_payload is not None else {}
                ),
                now=timestamp,
            )
            row = self._job_row(connection, job_id)
            result = self._decode_job(row)
            result["attached"] = False
            return result

    def get_job(self, job_id: str) -> dict[str, Any]:
        connection = self._read()
        try:
            return self._decode_job(self._job_row(connection, job_id))
        finally:
            connection.close()

    def list_jobs(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        connection = self._read()
        try:
            where = " WHERE active=1" if active_only else ""
            rows = connection.execute(
                f"SELECT * FROM jobs{where} ORDER BY created_at,job_id"
            ).fetchall()
            return [self._decode_job(row) for row in rows]
        finally:
            connection.close()

    def list_events(self, job_id: str) -> list[dict[str, Any]]:
        connection = self._read()
        try:
            self._job_row(connection, job_id)
            rows = connection.execute(
                "SELECT * FROM job_events WHERE job_id=? ORDER BY sequence",
                (job_id,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["evidence"] = _display_json(item.pop("evidence_json"))
                result.append(item)
            return result
        finally:
            connection.close()

    def delivery_snapshot(self, job_id: str) -> dict[str, Any]:
        """Read one internally consistent job/watcher snapshot."""
        connection = self._read()
        try:
            connection.execute("BEGIN")
            job = self._decode_job(self._job_row(connection, job_id))
            action_rows = connection.execute(
                """SELECT * FROM job_actions
                   WHERE job_id=? ORDER BY created_at,action_id""",
                (job_id,),
            ).fetchall()
            gate_rows = connection.execute(
                """SELECT * FROM gates
                   WHERE job_id=? ORDER BY opened_at,gate_id""",
                (job_id,),
            ).fetchall()
            event_rows = connection.execute(
                "SELECT * FROM job_events WHERE job_id=? ORDER BY sequence",
                (job_id,),
            ).fetchall()
            events: list[dict[str, Any]] = []
            for row in event_rows:
                event = dict(row)
                event["evidence"] = _display_json(event.pop("evidence_json"))
                events.append(event)
            return {
                "job": job,
                "actions": [self._decode_action(row) for row in action_rows],
                "gates": [self._decode_gate(row) for row in gate_rows],
                "events": events,
            }
        finally:
            connection.close()

    def record_delivery(
        self,
        job_id: str,
        delivery_key: str,
        *,
        actor: str,
        now: datetime | None = None,
    ) -> bool:
        """Persist an at-least-once gateway cursor without changing job state."""
        key = _safe_text(
            delivery_key,
            "delivery_key",
            required=True,
            max_chars=64,
        )
        if re.fullmatch(r"[0-9a-f]{64}", key) is None:
            _fail(CommerceConfigurationError, "invalid_delivery_key", "delivery_key")
        timestamp = _utc(now)
        with self._write() as connection:
            job = self._job_row(connection, job_id)
            latest = connection.execute(
                """SELECT evidence_json FROM job_events
                   WHERE job_id=? AND event_type='commerce_gateway_delivered'
                   ORDER BY sequence DESC LIMIT 1""",
                (job_id,),
            ).fetchone()
            if latest is not None:
                evidence = _display_json(latest["evidence_json"])
                if isinstance(evidence, dict) and evidence.get("delivery_key") == key:
                    return False
            self._append_event(
                connection,
                job_id=job_id,
                event_type="commerce_gateway_delivered",
                from_state=job["current_state"],
                to_state=job["current_state"],
                actor=actor,
                reason_code="commerce_status_delivered",
                evidence={"delivery_key": key},
                now=timestamp,
            )
            return True

    def list_actions(self, job_id: str) -> list[dict[str, Any]]:
        connection = self._read()
        try:
            self._job_row(connection, job_id)
            rows = connection.execute(
                """SELECT * FROM job_actions
                   WHERE job_id=? ORDER BY created_at,action_id""",
                (job_id,),
            ).fetchall()
            return [self._decode_action(row) for row in rows]
        finally:
            connection.close()

    def list_gates(self, job_id: str) -> list[dict[str, Any]]:
        connection = self._read()
        try:
            self._job_row(connection, job_id)
            rows = connection.execute(
                """SELECT * FROM gates
                   WHERE job_id=? ORDER BY opened_at,gate_id""",
                (job_id,),
            ).fetchall()
            return [self._decode_gate(row) for row in rows]
        finally:
            connection.close()

    def record_facts(
        self,
        job_id: str,
        facts: Mapping[str, Any],
        *,
        expected_version: int,
        actor: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        payload = dict(_mapping(facts, "facts"))
        timestamp = _utc(now)
        updated_at = iso_utc(timestamp)
        with self._write() as connection:
            row = self._job_row(connection, job_id)
            if int(row["row_version"]) != expected_version:
                _fail(
                    CommerceStaleVersionError,
                    "stale_row_version",
                    "expected_version",
                )
            cursor = connection.execute(
                """UPDATE jobs SET row_version=row_version+1,updated_at=?
                   WHERE job_id=? AND row_version=?""",
                (updated_at, job_id, expected_version),
            )
            if cursor.rowcount != 1:
                _fail(
                    CommerceStaleVersionError,
                    "stale_row_version",
                    "expected_version",
                )
            self._append_event(
                connection,
                job_id=job_id,
                event_type="facts_answered",
                from_state=row["current_state"],
                to_state=row["current_state"],
                actor=actor,
                reason_code="trusted_facts_recorded",
                evidence={"facts": payload},
                now=timestamp,
            )
            return self._decode_job(self._job_row(connection, job_id))

    def latest_facts(self, job_id: str) -> dict[str, Any]:
        connection = self._read()
        try:
            self._job_row(connection, job_id)
            row = connection.execute(
                """SELECT evidence_json FROM job_events
                   WHERE job_id=? AND event_type='facts_answered'
                   ORDER BY sequence DESC LIMIT 1""",
                (job_id,),
            ).fetchone()
            if row is None:
                return {}
            evidence = _display_json(row["evidence_json"])
            facts = evidence.get("facts") if isinstance(evidence, dict) else None
            return dict(facts) if isinstance(facts, dict) else {}
        finally:
            connection.close()

    def _validate_version(
        self,
        row: sqlite3.Row,
        *,
        expected_state: str,
        expected_version: int,
    ) -> None:
        if expected_state not in STATES:
            _fail(
                CommerceInvalidTransitionError,
                "unknown_state",
                "expected_state",
            )
        if row["current_state"] != expected_state:
            _fail(
                CommerceInvalidTransitionError,
                "unexpected_current_state",
                "expected_state",
            )
        if int(row["row_version"]) != expected_version:
            _fail(
                CommerceStaleVersionError,
                "stale_row_version",
                "expected_version",
            )

    def _transition_in_tx(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        to_state: str,
        expected_state: str,
        expected_version: int,
        actor: str,
        reason_code: str,
        evidence: Mapping[str, Any] | None,
        action_id: str,
        gate_id: str,
        now: datetime | None,
        event_type: str = "state_transition",
    ) -> dict[str, Any]:
        if to_state not in STATES:
            _fail(CommerceInvalidTransitionError, "unknown_state", "to_state")
        self._validate_version(
            row,
            expected_state=expected_state,
            expected_version=expected_version,
        )
        source = str(row["current_state"])
        timestamp = _utc(now)
        if to_state not in ALLOWED_TRANSITIONS[source]:
            _fail(
                CommerceInvalidTransitionError,
                "transition_not_allowed",
                f"{source}->{to_state}",
            )
        if source in TERMINAL_STATES:
            _fail(
                CommerceInvalidTransitionError,
                "terminal_state",
                "current_state",
            )
        if to_state == "timed_out" and (
            event_type != "state_timed_out"
            or not row["deadline_at"]
            or row["deadline_at"] > iso_utc(timestamp)
        ):
            _fail(
                CommerceInvalidTransitionError,
                "timeout_not_due",
                "deadline_at",
            )
        bound_gate_id = ""
        if to_state == "awaiting_cal":
            if not gate_id:
                _fail(
                    CommerceInvalidTransitionError,
                    "open_gate_required",
                    "gate_id",
                )
            bound_gate_id = _safe_text(gate_id, "gate_id", required=True, max_chars=240)
            opened_gate = connection.execute(
                """SELECT * FROM gates
                   WHERE gate_id=? AND job_id=? AND status='open'""",
                (bound_gate_id, row["job_id"]),
            ).fetchone()
            if opened_gate is None or not row["active"]:
                _fail(
                    CommerceInvalidTransitionError,
                    "open_gate_required",
                    "gate_id",
                )
            if opened_gate["gate_type"] == "action_approval":
                _fail(
                    CommerceInvalidTransitionError,
                    "handoff_gate_required",
                    "gate_type",
                )
            if opened_gate["expires_at"] and opened_gate["expires_at"] <= iso_utc(
                timestamp
            ):
                _fail(
                    CommerceInvalidTransitionError,
                    "gate_expired",
                    "expires_at",
                )
            if opened_gate["browser_session"] != row["browser_session"]:
                _fail(
                    CommerceInvalidTransitionError,
                    "gate_session_mismatch",
                    "browser_session",
                )
        if source == "awaiting_cal" and to_state == "resuming":
            bound_gate_id = str(row["current_gate_id"])
            verified_gate = connection.execute(
                """SELECT 1 FROM gates
                   WHERE gate_id=? AND job_id=? AND status='completed'
                     AND completed_at>=?""",
                (bound_gate_id, row["job_id"], row["state_entered_at"]),
            ).fetchone()
            if not bound_gate_id or verified_gate is None:
                _fail(
                    CommerceInvalidTransitionError,
                    "verified_gate_required",
                    "current_gate_id",
                )
        bound_action: sqlite3.Row | None = None
        if to_state in EXECUTION_STATES:
            if not action_id:
                _fail(
                    CommerceInvalidTransitionError,
                    "action_required",
                    "action_id",
                )
            bound_action = self._action_row(connection, action_id)
            if bound_action["approval_status"] == "stale":
                _fail(
                    CommerceInvalidTransitionError,
                    "stale_action_approval",
                    "action_id",
                )
            expected_effects = (
                {"read_only"}
                if to_state == "executing_read_only"
                else {"idempotent_write", "consequential"}
            )
            if (
                bound_action["job_id"] != row["job_id"]
                or bound_action["target_state"] != to_state
                or bound_action["effect_class"] not in expected_effects
                or bound_action["action_status"] != "dispatched"
            ):
                _fail(
                    CommerceInvalidTransitionError,
                    "action_not_bound",
                    "action_id",
                )
            if (
                to_state == "executing"
                and bound_action["effect_class"] == "consequential"
                and bound_action["approval_status"] != "live"
            ):
                _fail(
                    CommerceInvalidTransitionError,
                    "action_approval_required",
                    "action_id",
                )
        unresolved_action = self._unresolved_action(connection, row["job_id"])
        if unresolved_action is not None:
            if unresolved_action["action_status"] == "uncertain":
                if to_state not in UNCERTAINTY_STATES:
                    _fail(
                        CommerceInvalidTransitionError,
                        "unresolved_action",
                        "action_id",
                    )
            else:
                wrapper_states = EXECUTION_STATES | {"awaiting_cal", "resuming"}
                if (
                    source in wrapper_states
                    and to_state not in EXECUTION_STATES
                    and row["current_step"] != unresolved_action["action_type"]
                ):
                    _fail(
                        CommerceInvalidTransitionError,
                        "inflight_action_not_bound",
                        "current_step",
                    )
                if to_state in EXECUTION_STATES:
                    if (
                        bound_action is None
                        or bound_action["action_id"] != unresolved_action["action_id"]
                    ):
                        _fail(
                            CommerceInvalidTransitionError,
                            "inflight_action_not_bound",
                            "action_id",
                        )
                elif to_state not in {"awaiting_cal", "resuming"}:
                    _fail(
                        CommerceInvalidTransitionError,
                        "inflight_action_not_terminal",
                        "action_id",
                    )
        entered = iso_utc(timestamp)
        active = 0 if to_state in TERMINAL_STATES else 1
        deadline = (
            None
            if to_state in TERMINAL_STATES | {"paused", "timed_out"}
            else iso_utc(timestamp + DEFAULT_STATE_TIMEOUT)
        )
        paused_from = source if to_state == "paused" else row["paused_from_state"]
        current_step = (
            str(bound_action["action_type"])
            if bound_action
            else (
                str(row["current_step"])
                if to_state
                in {
                    "awaiting_cal",
                    "resuming",
                    "uncertain_external_state",
                    "reconciliation_required",
                    "paused",
                    "timed_out",
                }
                else ""
            )
        )
        current_gate_id = bound_gate_id if to_state == "awaiting_cal" else ""
        cursor = connection.execute(
            """UPDATE jobs
               SET current_state=?,current_step=?,current_gate_id=?,paused_from_state=?,
                   state_entered_at=?,deadline_at=?,active=?,
                   row_version=row_version+1,updated_at=?
               WHERE job_id=? AND row_version=?""",
            (
                to_state,
                current_step,
                current_gate_id,
                paused_from,
                entered,
                deadline,
                active,
                entered,
                row["job_id"],
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            _fail(
                CommerceStaleVersionError,
                "stale_row_version",
                "expected_version",
            )
        if row["current_gate_id"] and source == "awaiting_cal":
            gate_status = "consumed" if to_state == "resuming" else "invalidated"
            connection.execute(
                """UPDATE gates SET status=?,handoff_token_hash='',
                   handoff_expires_at=NULL
                   WHERE gate_id=? AND status IN ('open','completed')""",
                (gate_status, row["current_gate_id"]),
            )
        if to_state in TERMINAL_STATES | {"paused", "timed_out"}:
            connection.execute(
                """UPDATE gates
                   SET status=CASE WHEN status IN ('open','completed')
                                   THEN 'invalidated' ELSE status END,
                       handoff_token_hash='',handoff_expires_at=NULL
                   WHERE job_id=?""",
                (row["job_id"],),
            )
        self._append_event(
            connection,
            job_id=row["job_id"],
            event_type=event_type,
            from_state=source,
            to_state=to_state,
            actor=actor,
            reason_code=reason_code,
            evidence=evidence,
            now=timestamp,
        )
        return self._decode_job(self._job_row(connection, row["job_id"]))

    def transition(
        self,
        job_id: str,
        to_state: str,
        *,
        expected_state: str,
        expected_version: int,
        actor: str,
        reason_code: str,
        evidence: Mapping[str, Any] | None = None,
        action_id: str = "",
        gate_id: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._write() as connection:
            row = self._job_row(connection, job_id)
            return self._transition_in_tx(
                connection,
                row=row,
                to_state=to_state,
                expected_state=expected_state,
                expected_version=expected_version,
                actor=actor,
                reason_code=reason_code,
                evidence=evidence,
                action_id=action_id,
                gate_id=gate_id,
                now=now,
            )

    def pause(
        self,
        job_id: str,
        *,
        expected_version: int,
        actor: str,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        safe_reason = _safe_text(reason, "reason", required=True, max_chars=500)
        with self._write() as connection:
            row = self._job_row(connection, job_id)
            return self._transition_in_tx(
                connection,
                row=row,
                to_state="paused",
                expected_state=row["current_state"],
                expected_version=expected_version,
                actor=actor,
                reason_code="operator_pause",
                evidence={"reason": safe_reason},
                action_id="",
                gate_id="",
                now=now,
            )

    def resume(
        self,
        job_id: str,
        *,
        expected_version: int,
        actor: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._write() as connection:
            row = self._job_row(connection, job_id)
            if row["current_state"] not in {"paused", "timed_out"}:
                _fail(
                    CommerceInvalidTransitionError,
                    "job_not_resumable",
                    "current_state",
                )
            return self._transition_in_tx(
                connection,
                row=row,
                to_state="ready",
                expected_state=row["current_state"],
                expected_version=expected_version,
                actor=actor,
                reason_code="operator_resume",
                evidence={},
                action_id="",
                gate_id="",
                now=now,
            )

    def cancel(
        self,
        job_id: str,
        *,
        expected_version: int,
        actor: str,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        safe_reason = _safe_text(reason, "reason", required=True, max_chars=500)
        with self._write() as connection:
            row = self._job_row(connection, job_id)
            if row["current_state"] in UNCERTAINTY_STATES:
                _fail(
                    CommerceInvalidTransitionError,
                    "reconciliation_required",
                    "current_state",
                )
            unresolved = connection.execute(
                """SELECT 1 FROM job_actions
                   WHERE job_id=? AND effect_class='consequential'
                     AND action_status IN ('dispatched','uncertain')
                   LIMIT 1""",
                (job_id,),
            ).fetchone()
            if unresolved:
                _fail(
                    CommerceInvalidTransitionError,
                    "consequential_action_unresolved",
                    "job_id",
                )
            return self._transition_in_tx(
                connection,
                row=row,
                to_state="cancelled",
                expected_state=row["current_state"],
                expected_version=expected_version,
                actor=actor,
                reason_code="operator_cancel",
                evidence={"reason": safe_reason},
                action_id="",
                gate_id="",
                now=now,
            )

    def set_plan(
        self,
        job_id: str,
        plan: Mapping[str, Any],
        *,
        expected_version: int,
        actor: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        payload = _mapping(plan, "plan")
        serialized = canonical_json(payload)
        fingerprint = plan_fingerprint(payload)
        timestamp = iso_utc(now)
        with self._write() as connection:
            row = self._job_row(connection, job_id)
            if int(row["row_version"]) != expected_version:
                _fail(
                    CommerceStaleVersionError,
                    "stale_row_version",
                    "expected_version",
                )
            old_fingerprint = str(row["plan_fingerprint"])
            connection.execute(
                """UPDATE jobs SET plan_json=?,plan_fingerprint=?,
                   row_version=row_version+1,updated_at=?
                   WHERE job_id=? AND row_version=?""",
                (serialized, fingerprint, timestamp, job_id, expected_version),
            )
            if old_fingerprint != fingerprint:
                connection.execute(
                    """UPDATE job_actions SET approval_status='stale',updated_at=?
                       WHERE job_id=? AND approval_status='live'
                         AND plan_fingerprint=?""",
                    (timestamp, job_id, old_fingerprint),
                )
                connection.execute(
                    """UPDATE gates SET status='invalidated',handoff_token_hash='',
                       handoff_expires_at=NULL
                       WHERE job_id=? AND status IN ('open','completed')
                         AND (
                           approval_fingerprint=?
                           OR approval_fingerprint IN (
                             SELECT action_fingerprint FROM job_actions
                             WHERE job_id=? AND plan_fingerprint=?
                           )
                         )""",
                    (job_id, old_fingerprint, job_id, old_fingerprint),
                )
            self._append_event(
                connection,
                job_id=job_id,
                event_type="plan_updated",
                from_state=row["current_state"],
                to_state=row["current_state"],
                actor=actor,
                reason_code="plan_fingerprinted",
                evidence={"material_change": old_fingerprint != fingerprint},
                now=now,
            )
            return self._decode_job(self._job_row(connection, job_id))

    def set_blocked_on_cogitator(
        self,
        job_id: str,
        blocked: bool,
        *,
        expected_version: int,
        actor: str,
        reason_code: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        substatus = "blocked_on_cogitator" if blocked else ""
        timestamp = iso_utc(now)
        with self._write() as connection:
            row = self._job_row(connection, job_id)
            if int(row["row_version"]) != expected_version:
                _fail(
                    CommerceStaleVersionError,
                    "stale_row_version",
                    "expected_version",
                )
            if row["substatus"] == substatus:
                _fail(
                    CommerceInvalidTransitionError,
                    "duplicate_substatus",
                    "substatus",
                )
            connection.execute(
                """UPDATE jobs SET substatus=?,row_version=row_version+1,updated_at=?
                   WHERE job_id=? AND row_version=?""",
                (substatus, timestamp, job_id, expected_version),
            )
            self._append_event(
                connection,
                job_id=job_id,
                event_type="substatus_set" if blocked else "substatus_cleared",
                from_state=row["current_state"],
                to_state=row["current_state"],
                actor=actor,
                reason_code=reason_code,
                evidence={},
                now=now,
            )
            return self._decode_job(self._job_row(connection, job_id))

    def record_action(
        self,
        job_id: str,
        *,
        action_type: str,
        provider: str,
        effect_class: str,
        idempotency_key: str,
        request: Mapping[str, Any],
        target_state: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        action_name = _safe_text(
            action_type, "action_type", required=True, max_chars=120
        )
        provider_name = _safe_text(provider, "provider", max_chars=120)
        key = _safe_text(
            idempotency_key, "idempotency_key", required=True, max_chars=240
        )
        payload = _mapping(request, "request")
        fingerprint = action_fingerprint(
            action_type=action_name,
            provider=provider_name,
            effect_class=effect_class,
            target_state=target_state,
            request=payload,
        )
        timestamp = iso_utc(now)
        with self._write() as connection:
            job = self._job_row(connection, job_id)
            existing = connection.execute(
                "SELECT * FROM job_actions WHERE job_id=? AND idempotency_key=?",
                (job_id, key),
            ).fetchone()
            if existing is not None:
                if existing["action_fingerprint"] != fingerprint:
                    _fail(
                        CommerceActionError,
                        "idempotency_conflict",
                        "idempotency_key",
                    )
                result = self._decode_action(existing)
                result["idempotent_replay"] = True
                return result
            action_id = _new_id("ca")
            connection.execute(
                """INSERT INTO job_actions
                   (action_id,job_id,action_type,provider,effect_class,target_state,
                    action_status,idempotency_key,action_fingerprint,plan_fingerprint,
                    approval_reference,approval_fingerprint,approval_status,
                    approval_expires_at,
                    request_json,result_json,uncertainty,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    action_id,
                    job_id,
                    action_name,
                    provider_name,
                    effect_class,
                    target_state,
                    "planned",
                    key,
                    fingerprint,
                    job["plan_fingerprint"],
                    "",
                    "",
                    "unbound",
                    None,
                    canonical_json(payload),
                    canonical_json({}),
                    0,
                    timestamp,
                    timestamp,
                ),
            )
            result = self._decode_action(self._action_row(connection, action_id))
            result["idempotent_replay"] = False
            return result

    def get_action(self, action_id: str) -> dict[str, Any]:
        connection = self._read()
        try:
            return self._decode_action(self._action_row(connection, action_id))
        finally:
            connection.close()

    def authorize_action(
        self,
        action_id: str,
        *,
        gate_id: str,
        actor: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        safe_gate_id = _safe_text(gate_id, "gate_id", required=True, max_chars=240)
        timestamp = iso_utc(now)
        with self._write() as connection:
            action = self._action_row(connection, action_id)
            if action["effect_class"] != "consequential":
                _fail(CommerceActionError, "approval_not_required", "effect_class")
            if action["action_status"] != "planned":
                _fail(
                    CommerceActionError,
                    "action_not_approvable",
                    "action_status",
                )
            approval_expired = action["approval_status"] == "live" and (
                not action["approval_expires_at"]
                or action["approval_expires_at"] <= timestamp
            )
            if action["approval_status"] != "unbound" and not approval_expired:
                _fail(
                    CommerceActionError,
                    "action_already_approved",
                    "approval_status",
                )
            gate = connection.execute(
                "SELECT * FROM gates WHERE gate_id=?", (safe_gate_id,)
            ).fetchone()
            if gate is None:
                _fail(CommerceNotFoundError, "gate_not_found", "gate_id")
            if (
                gate["job_id"] != action["job_id"]
                or gate["gate_type"] != "action_approval"
                or gate["status"] != "completed"
            ):
                _fail(CommerceActionError, "approval_gate_not_bound", "gate_id")
            if not gate["expires_at"] or gate["expires_at"] <= timestamp:
                _fail(CommerceActionError, "approval_gate_expired", "gate_id")
            if gate["approval_fingerprint"] != action["action_fingerprint"]:
                _fail(
                    CommerceActionError,
                    "approval_fingerprint_mismatch",
                    "gate_id",
                )
            completion = json.loads(gate["completion_evidence_json"])
            if completion.get("approval_granted") is not True:
                _fail(CommerceActionError, "approval_not_granted", "gate_id")
            job = self._job_row(connection, action["job_id"])
            if action["plan_fingerprint"] != job["plan_fingerprint"]:
                _fail(
                    CommerceActionError,
                    "stale_action_approval",
                    "action_id",
                )
            connection.execute(
                """UPDATE job_actions
                   SET approval_reference=?,approval_fingerprint=?,
                       approval_status='live',approval_expires_at=?,updated_at=?
                   WHERE action_id=?""",
                (
                    gate["approval_reference"],
                    action["action_fingerprint"],
                    gate["expires_at"],
                    timestamp,
                    action_id,
                ),
            )
            connection.execute(
                """UPDATE gates SET status='consumed',handoff_token_hash='',
                   handoff_expires_at=NULL WHERE gate_id=?""",
                (safe_gate_id,),
            )
            self._append_event(
                connection,
                job_id=action["job_id"],
                event_type="action_approved",
                from_state=job["current_state"],
                to_state=job["current_state"],
                actor=actor,
                reason_code="action_fingerprint_approved",
                evidence={"action_id": action_id, "gate_id": safe_gate_id},
                now=now,
            )
            return self._decode_action(self._action_row(connection, action_id))

    def _dispatch_action_in_tx(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        job: sqlite3.Row,
        timestamp: str,
    ) -> tuple[sqlite3.Row, bool]:
        action_id = str(row["action_id"])
        unresolved_action = self._unresolved_action(connection, row["job_id"])
        if (
            unresolved_action is not None
            and unresolved_action["action_id"] != action_id
        ):
            _fail(CommerceActionError, "inflight_action_exists", "job_id")
        recoverable_action = (
            row["action_status"] == "recoverable"
            and row["effect_class"] in RECOVERABLE_EFFECT_CLASSES
        )
        recovery_redispatch = (
            recoverable_action
            and job["current_state"] == row["target_state"]
            and job["current_step"] == row["action_type"]
        )
        if row["action_status"] != "planned" and not recoverable_action:
            _fail(
                CommerceActionError,
                "action_not_dispatchable",
                "action_status",
            )
        if row["target_state"] not in EXECUTION_STATES or (
            row["target_state"] not in ALLOWED_TRANSITIONS[job["current_state"]]
            and not recovery_redispatch
        ):
            _fail(
                CommerceActionError,
                "job_not_ready_for_action",
                "current_state",
            )
        if row["approval_status"] == "stale":
            _fail(
                CommerceActionError,
                "stale_action_approval",
                "action_id",
            )
        if row["effect_class"] == "consequential" and (
            row["approval_status"] != "live"
        ):
            _fail(CommerceActionError, "action_approval_required", "action_id")
        if row["effect_class"] == "consequential" and (
            not row["approval_expires_at"] or row["approval_expires_at"] <= timestamp
        ):
            _fail(CommerceActionError, "action_approval_expired", "action_id")
        connection.execute(
            """UPDATE job_actions SET action_status='dispatched',
               dispatched_at=?,started_at=?,updated_at=?
               WHERE action_id=?""",
            (timestamp, timestamp, timestamp, action_id),
        )
        return self._action_row(connection, action_id), recovery_redispatch

    def dispatch_action(
        self,
        action_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = iso_utc(now)
        with self._write() as connection:
            row = self._action_row(connection, action_id)
            job = self._job_row(connection, row["job_id"])
            dispatched, _ = self._dispatch_action_in_tx(
                connection,
                row=row,
                job=job,
                timestamp=timestamp,
            )
            return self._decode_action(dispatched)

    def dispatch_and_transition(
        self,
        action_id: str,
        *,
        expected_state: str,
        expected_version: int,
        actor: str,
        reason_code: str = "action_dispatched",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically dispatch an action and bind its execution state.

        A recovered idempotent action is already bound to its execution state,
        so redispatch changes only the action row and leaves the job version
        untouched. Initial dispatch updates both rows in one transaction.
        """
        timestamp = _utc(now)
        with self._write() as connection:
            row = self._action_row(connection, action_id)
            job = self._job_row(connection, row["job_id"])
            self._validate_version(
                job,
                expected_state=expected_state,
                expected_version=expected_version,
            )
            dispatched, recovery_redispatch = self._dispatch_action_in_tx(
                connection,
                row=row,
                job=job,
                timestamp=iso_utc(timestamp),
            )
            if recovery_redispatch:
                return {
                    "action": self._decode_action(dispatched),
                    "job": self._decode_job(job),
                    "redispatched": True,
                }
            updated_job = self._transition_in_tx(
                connection,
                row=job,
                to_state=str(dispatched["target_state"]),
                expected_state=expected_state,
                expected_version=expected_version,
                actor=actor,
                reason_code=reason_code,
                evidence={"action_id": action_id},
                action_id=action_id,
                gate_id="",
                now=timestamp,
            )
            return {
                "action": self._decode_action(self._action_row(connection, action_id)),
                "job": updated_job,
                "redispatched": False,
            }

    def finish_action(
        self,
        action_id: str,
        *,
        status: str,
        result: Mapping[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if status not in {"succeeded", "failed", "uncertain"}:
            _fail(CommerceActionError, "invalid_action_status", "status")
        payload = _mapping(result, "result")
        timestamp = iso_utc(now)
        with self._write() as connection:
            row = self._action_row(connection, action_id)
            if row["action_status"] != "dispatched":
                _fail(
                    CommerceActionError,
                    "action_not_terminalizable",
                    "action_status",
                )
            connection.execute(
                """UPDATE job_actions SET action_status=?,result_json=?,
                   terminal_at=?,uncertainty=?,updated_at=? WHERE action_id=?""",
                (
                    status,
                    canonical_json(payload),
                    timestamp,
                    1 if status == "uncertain" else 0,
                    timestamp,
                    action_id,
                ),
            )
            return self._decode_action(self._action_row(connection, action_id))

    def resolve_uncertain_action(
        self,
        action_id: str,
        *,
        status: str,
        evidence: Mapping[str, Any],
        expected_version: int,
        actor: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if status not in {"succeeded", "failed"}:
            _fail(CommerceActionError, "invalid_resolution_status", "status")
        payload = _mapping(evidence, "reconciliation_evidence")
        if payload.get("provider_truth_verified") is not True:
            _fail(
                CommerceActionError,
                "reconciliation_verification_required",
                "reconciliation_evidence",
            )
        target_state = "ready" if status == "succeeded" else "failed"
        timestamp = _utc(now)
        with self._write() as connection:
            action = self._action_row(connection, action_id)
            job = self._job_row(connection, action["job_id"])
            current_result = json.loads(action["result_json"])
            if action["action_status"] == status:
                resolution_event = connection.execute(
                    """SELECT 1 FROM job_events
                       WHERE job_id=? AND event_type='action_reconciled'
                         AND evidence_json=? LIMIT 1""",
                    (
                        job["job_id"],
                        canonical_json({"action_id": action_id, "status": status}),
                    ),
                ).fetchone()
                if (
                    current_result.get("reconciliation") == payload
                    and resolution_event is not None
                ):
                    decoded_action = self._decode_action(action)
                    decoded_action["idempotent_replay"] = True
                    return {
                        "action": decoded_action,
                        "job": self._decode_job(job),
                    }
                _fail(CommerceActionError, "resolution_conflict", "action_id")
            if (
                action["action_status"] != "uncertain"
                or job["current_state"] != "reconciliation_required"
                or job["current_step"] != action["action_type"]
            ):
                _fail(CommerceActionError, "action_not_reconcilable", "action_id")
            unresolved = self._unresolved_action(connection, job["job_id"])
            if unresolved is None or unresolved["action_id"] != action_id:
                _fail(CommerceActionError, "action_not_reconcilable", "action_id")
            resolved_result = {
                "uncertain_result": current_result,
                "reconciliation": payload,
            }
            resolved_at = iso_utc(timestamp)
            connection.execute(
                """UPDATE job_actions
                   SET action_status=?,result_json=?,terminal_at=?,
                       uncertainty=0,updated_at=?
                   WHERE action_id=?""",
                (
                    status,
                    canonical_json(resolved_result),
                    resolved_at,
                    resolved_at,
                    action_id,
                ),
            )
            self._append_event(
                connection,
                job_id=job["job_id"],
                event_type="action_reconciled",
                from_state=job["current_state"],
                to_state=job["current_state"],
                actor=actor,
                reason_code=f"uncertain_action_{status}",
                evidence={"action_id": action_id, "status": status},
                now=timestamp,
            )
            resolved_job = self._transition_in_tx(
                connection,
                row=job,
                to_state=target_state,
                expected_state="reconciliation_required",
                expected_version=expected_version,
                actor=actor,
                reason_code=f"reconciliation_{status}",
                evidence={"action_id": action_id},
                action_id="",
                gate_id="",
                now=timestamp,
            )
            decoded_action = self._decode_action(
                self._action_row(connection, action_id)
            )
            decoded_action["idempotent_replay"] = False
            return {"action": decoded_action, "job": resolved_job}

    def open_gate(
        self,
        job_id: str,
        *,
        gate_type: str,
        human_action: str,
        provider_truth_reference: str,
        opening_evidence: Mapping[str, Any],
        approval_reference: str = "",
        approval_fingerprint: str = "",
        expires_at: datetime | None = None,
        actor: str = "system",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        gate_name = _safe_text(gate_type, "gate_type", required=True, max_chars=120)
        action = _safe_text(
            human_action, "human_action", required=True, max_chars=1_000
        )
        truth = _safe_text(
            provider_truth_reference,
            "provider_truth_reference",
            required=True,
            max_chars=1_000,
        )
        approval = _safe_text(approval_reference, "approval_reference", max_chars=240)
        if not isinstance(approval_fingerprint, str):
            _fail(CommerceConfigurationError, "invalid_text", "approval_fingerprint")
        approval_fp = approval_fingerprint.strip()
        if bool(approval) != bool(approval_fp):
            _fail(
                CommerceGateError,
                "approval_binding_incomplete",
                "approval_reference",
            )
        if approval_fp and re.fullmatch(r"[0-9a-f]{64}", approval_fp) is None:
            _fail(
                CommerceGateError,
                "invalid_approval_fingerprint",
                "approval_fingerprint",
            )
        evidence = _mapping(opening_evidence, "opening_evidence")
        timestamp = _utc(now)
        if gate_name == "action_approval":
            latest_expiry = timestamp + ACTION_APPROVAL_TTL
            if expires_at is not None and _utc(expires_at) > latest_expiry:
                _fail(
                    CommerceGateError,
                    "action_approval_ttl_exceeded",
                    "expires_at",
                )
            expiry = iso_utc(expires_at or latest_expiry)
        else:
            expiry = iso_utc(expires_at) if expires_at else None
        with self._write() as connection:
            row = self._job_row(connection, job_id)
            if not row["active"] or row["current_state"] in {
                "paused",
                "timed_out",
            }:
                _fail(CommerceGateError, "gate_job_inactive", "job_id")
            if gate_name == "action_approval":
                expired_gates = connection.execute(
                    """SELECT gate_id FROM gates
                       WHERE job_id=? AND gate_type='action_approval'
                         AND status IN ('open','completed')
                         AND expires_at IS NOT NULL AND expires_at<=?""",
                    (job_id, iso_utc(timestamp)),
                ).fetchall()
                for expired_gate in expired_gates:
                    expired_gate_id = str(expired_gate["gate_id"])
                    connection.execute(
                        """UPDATE gates SET status='expired',
                           handoff_token_hash='',handoff_expires_at=NULL
                           WHERE gate_id=?""",
                        (expired_gate_id,),
                    )
                    self._append_event(
                        connection,
                        job_id=job_id,
                        event_type="gate_expired",
                        from_state=row["current_state"],
                        to_state=row["current_state"],
                        actor=actor,
                        reason_code="action_approval_expired",
                        evidence={"gate_id": expired_gate_id},
                        now=timestamp,
                    )
            if connection.execute(
                "SELECT 1 FROM gates WHERE job_id=? AND status IN ('open','completed')",
                (job_id,),
            ).fetchone():
                _fail(CommerceGateError, "active_gate_exists", "job_id")
            gate_id = _new_id("cg")
            connection.execute(
                """INSERT INTO gates
                   (gate_id,job_id,gate_type,status,human_action,
                    provider_truth_reference,browser_session,handoff_token_hash,
                    handoff_expires_at,done_requested_at,approval_reference,
                    approval_fingerprint,opening_evidence_json,
                    completion_evidence_json,opened_at,expires_at,completed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                (
                    gate_id,
                    job_id,
                    gate_name,
                    "open",
                    action,
                    truth,
                    row["browser_session"],
                    "",
                    None,
                    None,
                    approval,
                    approval_fp,
                    canonical_json(evidence),
                    canonical_json({}),
                    iso_utc(timestamp),
                    expiry,
                ),
            )
            self._append_event(
                connection,
                job_id=job_id,
                event_type="gate_opened",
                from_state=row["current_state"],
                to_state=row["current_state"],
                actor=actor,
                reason_code="human_gate_opened",
                evidence={"gate_id": gate_id, "gate_type": gate_name},
                now=timestamp,
            )
            gate = connection.execute(
                "SELECT * FROM gates WHERE gate_id=?", (gate_id,)
            ).fetchone()
            return self._decode_gate(gate)

    def get_gate(self, gate_id: str) -> dict[str, Any]:
        connection = self._read()
        try:
            return self._decode_gate(self._gate_row(connection, gate_id))
        finally:
            connection.close()

    def issue_gate_handoff(
        self,
        gate_id: str,
        *,
        actor: str = "system",
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], str]:
        timestamp = _utc(now)
        with self._write() as connection:
            gate, job = self._active_handoff_gate(connection, gate_id, now=timestamp)
            token = f"cgh_{secrets.token_urlsafe(32)}"
            connection.execute(
                """UPDATE gates SET handoff_token_hash=?,handoff_expires_at=?
                   WHERE gate_id=?""",
                (
                    hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    self._handoff_expiry(gate, timestamp),
                    gate_id,
                ),
            )
            self._append_event(
                connection,
                job_id=job["job_id"],
                event_type="gate_handoff_issued",
                from_state=job["current_state"],
                to_state=job["current_state"],
                actor=actor,
                reason_code="gate_handoff_issued",
                evidence={"gate_id": gate_id},
                now=timestamp,
            )
            updated = self._gate_row(connection, gate_id)
            return self._decode_gate(updated), token

    def authorize_gate_handoff(
        self,
        gate_id: str,
        token: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = _utc(now)
        connection = self._read()
        try:
            gate, _ = self._active_handoff_gate(
                connection, gate_id, now=timestamp, token=token
            )
            return self._decode_gate(gate)
        finally:
            connection.close()

    def renew_gate_handoff(
        self,
        gate_id: str,
        token: str,
        *,
        actor: str = "system",
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], str]:
        timestamp = _utc(now)
        with self._write() as connection:
            gate, job = self._active_handoff_gate(
                connection, gate_id, now=timestamp, token=token
            )
            renewed_token = f"cgh_{secrets.token_urlsafe(32)}"
            connection.execute(
                """UPDATE gates SET handoff_token_hash=?,handoff_expires_at=?
                   WHERE gate_id=?""",
                (
                    hashlib.sha256(renewed_token.encode("utf-8")).hexdigest(),
                    self._handoff_expiry(gate, timestamp),
                    gate_id,
                ),
            )
            self._append_event(
                connection,
                job_id=job["job_id"],
                event_type="gate_handoff_renewed",
                from_state=job["current_state"],
                to_state=job["current_state"],
                actor=actor,
                reason_code="gate_handoff_renewed",
                evidence={"gate_id": gate_id},
                now=timestamp,
            )
            updated = self._gate_row(connection, gate_id)
            return self._decode_gate(updated), renewed_token

    def request_gate_done(
        self,
        gate_id: str,
        token: str,
        *,
        actor: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = _utc(now)
        with self._write() as connection:
            gate, job = self._active_handoff_gate(
                connection, gate_id, now=timestamp, token=token
            )
            if gate["done_requested_at"]:
                return self._decode_gate(gate)
            connection.execute(
                "UPDATE gates SET done_requested_at=? WHERE gate_id=?",
                (iso_utc(timestamp), gate_id),
            )
            self._append_event(
                connection,
                job_id=job["job_id"],
                event_type="gate_done_requested",
                from_state=job["current_state"],
                to_state=job["current_state"],
                actor=actor,
                reason_code="human_requested_gate_verification",
                evidence={"gate_id": gate_id},
                now=timestamp,
            )
            return self._decode_gate(self._gate_row(connection, gate_id))

    def complete_gate(
        self,
        gate_id: str,
        *,
        evidence: Mapping[str, Any],
        actor: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        payload = _mapping(evidence, "completion_evidence")
        if payload.get("provider_truth_verified") is not True:
            _fail(
                CommerceGateError,
                "provider_truth_verification_required",
                "completion_evidence",
            )
        timestamp = _utc(now)
        with self._write() as connection:
            gate = self._gate_row(connection, gate_id)
            if gate["status"] != "open":
                _fail(CommerceGateError, "gate_not_open", "status")
            if gate["expires_at"] and gate["expires_at"] <= iso_utc(timestamp):
                _fail(CommerceGateError, "gate_expired", "expires_at")
            job = self._job_row(connection, gate["job_id"])
            if gate["gate_type"] != "action_approval":
                if (
                    not job["active"]
                    or job["current_state"] != "awaiting_cal"
                    or job["current_gate_id"] != gate["gate_id"]
                    or gate["browser_session"] != job["browser_session"]
                ):
                    _fail(CommerceGateError, "gate_not_active_handoff", "gate_id")
                if gate["gate_type"] != "facts" and not gate["done_requested_at"]:
                    _fail(CommerceGateError, "gate_done_required", "gate_id")
            connection.execute(
                """UPDATE gates SET status='completed',completion_evidence_json=?,
                   completed_at=?,handoff_token_hash='',handoff_expires_at=NULL
                   WHERE gate_id=?""",
                (canonical_json(payload), iso_utc(timestamp), gate_id),
            )
            self._append_event(
                connection,
                job_id=gate["job_id"],
                event_type="gate_completed",
                from_state=job["current_state"],
                to_state=job["current_state"],
                actor=actor,
                reason_code="human_gate_completed",
                evidence={"gate_id": gate_id},
                now=timestamp,
            )
            updated = connection.execute(
                "SELECT * FROM gates WHERE gate_id=?", (gate_id,)
            ).fetchone()
            return self._decode_gate(updated)

    def expire_gate(
        self,
        gate_id: str,
        *,
        now: datetime,
        actor: str = "system",
    ) -> dict[str, Any]:
        timestamp = _utc(now)
        with self._write() as connection:
            gate = self._gate_row(connection, gate_id)
            if gate["status"] != "open":
                _fail(CommerceGateError, "gate_not_open", "status")
            if not gate["expires_at"] or gate["expires_at"] > iso_utc(timestamp):
                _fail(CommerceGateError, "gate_not_expired", "expires_at")
            connection.execute(
                """UPDATE gates SET status='expired',handoff_token_hash='',
                   handoff_expires_at=NULL WHERE gate_id=?""",
                (gate_id,),
            )
            job = self._job_row(connection, gate["job_id"])
            self._append_event(
                connection,
                job_id=gate["job_id"],
                event_type="gate_expired",
                from_state=job["current_state"],
                to_state=job["current_state"],
                actor=actor,
                reason_code="human_gate_expired",
                evidence={"gate_id": gate_id},
                now=timestamp,
            )
            updated = connection.execute(
                "SELECT * FROM gates WHERE gate_id=?", (gate_id,)
            ).fetchone()
            return self._decode_gate(updated)

    def upsert_provider_account(
        self,
        *,
        account_reference: str,
        provider: str,
        environment_tag: str,
        identity: Mapping[str, Any],
        status: str,
        metadata: Mapping[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        reference = _safe_text(
            account_reference, "account_reference", required=True, max_chars=240
        )
        provider_name = _safe_text(provider, "provider", required=True, max_chars=120)
        environment = _safe_text(
            environment_tag, "environment_tag", required=True, max_chars=80
        )
        safe_status = _safe_text(status, "status", required=True, max_chars=80)
        identity_payload = _mapping(identity, "identity")
        metadata_payload = _mapping(metadata, "metadata")
        identity_json = canonical_json(identity_payload)
        identity_fp = _fingerprint(identity_payload)
        timestamp = iso_utc(now)
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM provider_accounts WHERE account_reference=?",
                (reference,),
            ).fetchone()
            if existing and existing["identity_fingerprint"] != identity_fp:
                _fail(
                    CommerceActionError,
                    "provider_identity_conflict",
                    "account_reference",
                )
            connection.execute(
                """INSERT INTO provider_accounts
                   (account_reference,provider,environment_tag,identity_json,
                    identity_fingerprint,status,metadata_json,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_reference) DO UPDATE SET
                    status=excluded.status,metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at""",
                (
                    reference,
                    provider_name,
                    environment,
                    identity_json,
                    identity_fp,
                    safe_status,
                    canonical_json(metadata_payload),
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM provider_accounts WHERE account_reference=?",
                (reference,),
            ).fetchone()
            result = dict(row)
            result["identity"] = _display_json(result.pop("identity_json"))
            result["metadata"] = _display_json(result.pop("metadata_json"))
            return result

    def sweep_timeouts(self, *, now: datetime, actor: str = "system") -> list[str]:
        timestamp = _utc(now)
        timed_out: list[str] = []
        with self._write() as connection:
            rows = connection.execute(
                """SELECT * FROM jobs
                   WHERE active=1 AND deadline_at IS NOT NULL AND deadline_at<=?
                   ORDER BY job_id""",
                (iso_utc(timestamp),),
            ).fetchall()
            for row in rows:
                if (
                    row["current_state"] in TIMEOUT_EXCLUDED_STATES
                    or self._unresolved_action(connection, row["job_id"]) is not None
                ):
                    continue
                self._transition_in_tx(
                    connection,
                    row=row,
                    to_state="timed_out",
                    expected_state=row["current_state"],
                    expected_version=int(row["row_version"]),
                    actor=actor,
                    reason_code="state_timeout",
                    evidence={},
                    action_id="",
                    gate_id="",
                    now=timestamp,
                    event_type="state_timed_out",
                )
                timed_out.append(row["job_id"])
        return timed_out

    def recover(self, *, now: datetime, actor: str = "system") -> dict[str, int]:
        timestamp = _utc(now)
        counts = {"recoverable": 0, "uncertain": 0, "inconsistent": 0}
        inconsistent_job = ""
        with self._write() as connection:
            actions = connection.execute(
                """SELECT a.*,j.current_state,j.active,j.row_version
                   FROM job_actions a JOIN jobs j ON j.job_id=a.job_id
                   WHERE a.action_status IN ('dispatched','uncertain')
                   ORDER BY a.created_at,a.action_id"""
            ).fetchall()
            for action in actions:
                job = self._job_row(connection, action["job_id"])
                if not job["active"]:
                    evidence = {"action_id": action["action_id"]}
                    already_recorded = connection.execute(
                        """SELECT 1 FROM job_events
                           WHERE job_id=? AND event_type='recovery_inconsistent'
                             AND evidence_json=? LIMIT 1""",
                        (job["job_id"], canonical_json(evidence)),
                    ).fetchone()
                    if not already_recorded:
                        self._append_event(
                            connection,
                            job_id=job["job_id"],
                            event_type="recovery_inconsistent",
                            from_state=job["current_state"],
                            to_state=job["current_state"],
                            actor=actor,
                            reason_code="terminal_job_has_inflight_action",
                            evidence=evidence,
                            now=timestamp,
                        )
                    counts["inconsistent"] += 1
                    inconsistent_job = job["job_id"]
                    continue
                if action["effect_class"] in RECOVERABLE_EFFECT_CLASSES:
                    connection.execute(
                        """UPDATE job_actions SET action_status='recoverable',
                           updated_at=? WHERE action_id=?""",
                        (iso_utc(timestamp), action["action_id"]),
                    )
                    self._append_event(
                        connection,
                        job_id=job["job_id"],
                        event_type="action_recoverable",
                        from_state=job["current_state"],
                        to_state=job["current_state"],
                        actor=actor,
                        reason_code=(
                            "read_action_interrupted"
                            if action["effect_class"] == "read_only"
                            else "idempotent_write_interrupted"
                        ),
                        evidence={"action_id": action["action_id"]},
                        now=timestamp,
                    )
                    counts["recoverable"] += 1
                    continue
                if action["action_status"] == "dispatched":
                    connection.execute(
                        """UPDATE job_actions SET action_status='uncertain',
                           uncertainty=1,terminal_at=?,updated_at=?
                           WHERE action_id=?""",
                        (
                            iso_utc(timestamp),
                            iso_utc(timestamp),
                            action["action_id"],
                        ),
                    )
                if job["current_state"] not in UNCERTAINTY_STATES:
                    entered = iso_utc(timestamp)
                    connection.execute(
                        """UPDATE jobs
                           SET current_state='uncertain_external_state',
                               current_step=?,current_gate_id='',
                               state_entered_at=?,deadline_at=?,
                               row_version=row_version+1,updated_at=?
                           WHERE job_id=? AND row_version=?""",
                        (
                            action["action_type"],
                            entered,
                            iso_utc(timestamp + DEFAULT_STATE_TIMEOUT),
                            entered,
                            job["job_id"],
                            int(job["row_version"]),
                        ),
                    )
                    connection.execute(
                        """UPDATE gates
                           SET status=CASE WHEN status IN ('open','completed')
                                           THEN 'invalidated' ELSE status END,
                               handoff_token_hash='',handoff_expires_at=NULL
                           WHERE job_id=?""",
                        (job["job_id"],),
                    )
                    self._append_event(
                        connection,
                        job_id=job["job_id"],
                        event_type="recovery_uncertain",
                        from_state=job["current_state"],
                        to_state="uncertain_external_state",
                        actor=actor,
                        reason_code="consequential_action_interrupted",
                        evidence={"action_id": action["action_id"]},
                        now=timestamp,
                    )
                elif action["action_status"] == "uncertain":
                    continue
                counts["uncertain"] += 1
        if inconsistent_job:
            _fail(
                CommerceRecoveryError,
                "inconsistent_recovery_state",
                inconsistent_job,
            )
        return counts

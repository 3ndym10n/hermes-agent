"""Read-only operational source synchronization for Virgil Mobile.

Adapters return bounded facts. Deterministic policy maps those facts into the
existing Attention Queue; adapters never mutate their source systems.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from attention_telegram import deliver_attention_result
from gateway.cogitator_decision_batch_bridge import (
    DecisionBatchBridgeError,
    request_decision_batch,
)
from hermes_attention import (
    AttentionError,
    attention_db_path,
    control_source,
    list_source_statuses,
    resolve_expired_attention,
    resolve_missing_source_records,
    update_source_status,
    upsert_attention,
)
from hermes_cli.config import load_config_readonly
from hermes_constants import get_hermes_home


PROCESSING_VERSION = "virgil-operational-sources-v1"
SYDNEY = ZoneInfo("Australia/Sydney")
SOURCE_INTERVALS = {
    "calendar": 300,
    "cogitator": 300,
    "github": 300,
    "ecommerce": 300,
    "system": 120,
}
REPOSITORIES = {
    "3ndym10n/hermes-agent": "linxio",
    "3ndym10n/Cogitator": "cogitator",
}
_PREP_WORDS = re.compile(
    r"\b(?:prep|prepare|review|demo|proposal|interview|presentation|workshop|qbr)\b",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"(?i)\b[^@\s]+@[^@\s]+\.[^@\s]+\b")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_LONG_NUMBER = re.compile(r"(?<!\w)\+?[\d(). -]{7,}\d(?!\w)")
_SECRET = re.compile(
    r"(?i)\b(?:api[_ -]?key|password|oauth|access[_ -]?token|refresh[_ -]?token)"
    r"\b\s*[:=]\s*\S+"
)


class SourceError(RuntimeError):
    """Adapter failure carrying only a stable, safe code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class WorkerBusy(SourceError):
    def __init__(self):
        super().__init__("worker_overlap")


@dataclass(frozen=True)
class SourceFact:
    source_type: str
    record_id: str
    event_id: str
    kind: str
    title: str
    summary: str
    recommended_action: str
    domain: str = ""
    due_at: str | None = None
    expires_at: str | None = None
    deep_link: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class ReconcileScope:
    source_type: str
    record_prefix: str
    seen_record_ids: frozenset[str]


@dataclass(frozen=True)
class SourceResult:
    status: str
    message: str
    facts: tuple[SourceFact, ...] = ()
    reconcile: tuple[ReconcileScope, ...] = ()


@dataclass(frozen=True)
class Policy:
    project: str
    item_type: str
    priority: str
    status: str
    waiting_on: str
    reason_code: str


@dataclass
class SourceContext:
    now: datetime
    config: Mapping[str, Any]
    cache: dict[str, Any] = field(default_factory=dict)


POLICIES: dict[str, Policy] = {
    "calendar_upcoming": Policy(
        "linxio", "calendar_event", "normal", "monitoring", "none", "calendar"
    ),
    "calendar_today": Policy(
        "linxio", "calendar_event", "normal", "monitoring", "none", "calendar"
    ),
    "calendar_soon": Policy(
        "linxio", "calendar_event", "high", "monitoring", "none", "calendar"
    ),
    "calendar_prepare": Policy(
        "linxio", "calendar_event", "normal", "prepared", "cal", "calendar"
    ),
    "calendar_prepare_soon": Policy(
        "linxio", "calendar_event", "high", "prepared", "cal", "calendar"
    ),
    "calendar_conflict": Policy(
        "linxio", "calendar_event", "high", "needs_cal", "cal", "calendar"
    ),
    "calendar_cancelled": Policy(
        "linxio", "calendar_event", "normal", "resolved", "none", "source_changed"
    ),
    "cogitator_decision": Policy(
        "cogitator", "decision", "high", "needs_cal", "cal", "human_decision"
    ),
    "cogitator_promotion": Policy(
        "cogitator", "approval", "high", "needs_cal", "cal", "human_decision"
    ),
    "cogitator_ready": Policy(
        "cogitator", "research", "normal", "prepared", "cal", "source_changed"
    ),
    "cogitator_running": Policy(
        "cogitator", "research", "normal", "monitoring", "virgil", "source_changed"
    ),
    "cogitator_failed": Policy(
        "cogitator",
        "automation_failure",
        "high",
        "needs_cal",
        "cal",
        "processing_failure",
    ),
    "cogitator_quiet": Policy(
        "cogitator", "research", "low", "monitoring", "external", "source_changed"
    ),
    "cogitator_closed": Policy(
        "cogitator", "research", "normal", "resolved", "none", "source_changed"
    ),
    "github_failing": Policy(
        "linxio", "automation_failure", "high", "needs_cal", "cal", "processing_failure"
    ),
    "github_review": Policy(
        "linxio", "approval", "high", "needs_cal", "cal", "human_decision"
    ),
    "github_ready": Policy(
        "linxio", "agent_job", "normal", "prepared", "cal", "source_changed"
    ),
    "github_open": Policy(
        "linxio", "agent_job", "normal", "monitoring", "virgil", "source_changed"
    ),
    "github_merged": Policy(
        "linxio", "agent_job", "normal", "resolved", "none", "source_changed"
    ),
    "repo_risk": Policy(
        "linxio", "automation_failure", "high", "needs_cal", "cal", "state_corruption"
    ),
    "commerce_gate": Policy(
        "ecommerce", "approval", "high", "needs_cal", "cal", "human_decision"
    ),
    "commerce_ready": Policy(
        "ecommerce", "task", "normal", "prepared", "cal", "source_changed"
    ),
    "commerce_active": Policy(
        "ecommerce", "task", "normal", "monitoring", "virgil", "source_changed"
    ),
    "commerce_failed": Policy(
        "ecommerce",
        "automation_failure",
        "high",
        "needs_cal",
        "cal",
        "processing_failure",
    ),
    "commerce_uncertain": Policy(
        "ecommerce",
        "automation_failure",
        "high",
        "safety_hold",
        "cal",
        "state_corruption",
    ),
    "commerce_complete": Policy(
        "ecommerce", "task", "normal", "resolved", "none", "source_changed"
    ),
    "system_severe": Policy(
        "system", "server_health", "urgent", "safety_hold", "cal", "state_corruption"
    ),
    "system_degraded": Policy(
        "system", "server_health", "high", "needs_cal", "cal", "processing_failure"
    ),
    "system_recovered": Policy(
        "system", "server_health", "normal", "resolved", "none", "recovered"
    ),
    "source_failure": Policy(
        "system", "automation_failure", "high", "needs_cal", "cal", "processing_failure"
    ),
}


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any, *, zone: ZoneInfo = SYDNEY) -> datetime:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceError("source_time_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(timezone.utc)


def _hash(value: Any, length: int = 32) -> str:
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:length]


def _event_id(*parts: Any) -> str:
    return _hash("\0".join(str(part) for part in parts))


def _safe_text(value: Any, *, fallback: str, limit: int) -> str:
    """Bound external labels before the stricter queue validator sees them."""

    text = unicodedata.normalize("NFC", html.unescape(str(value or ""))).strip()
    text = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in text)
    text = text.replace("<", "").replace(">", "")
    text = _SECRET.sub("[redacted]", text)
    text = _EMAIL.sub("[email redacted]", text)
    text = _URL.sub("[link redacted]", text)
    text = _LONG_NUMBER.sub("[number redacted]", text)
    text = " ".join(text.split())
    return (text[:limit].rstrip() or fallback)[:limit]


def _nested(config: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = config
    for key in keys:
        if not isinstance(current, Mapping):
            return default
        current = current.get(key)
    return default if current is None else current


def _require_private_file(path: Path, code: str) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SourceError(code) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise SourceError(code)


def policy_for(fact: SourceFact) -> Policy:
    try:
        policy = POLICIES[fact.kind]
    except KeyError as exc:
        raise SourceError("unsupported_source_fact") from exc
    if fact.source_type == "github" and fact.domain == "3ndym10n/Cogitator":
        return Policy(
            "cogitator",
            policy.item_type,
            policy.priority,
            policy.status,
            policy.waiting_on,
            policy.reason_code,
        )
    return policy


def apply_fact(
    fact: SourceFact,
    *,
    db_path: Path | str | None = None,
    notify: bool = True,
) -> dict[str, Any]:
    policy = policy_for(fact)
    result = upsert_attention(
        {
            "source_type": fact.source_type,
            "source_record_id": fact.record_id,
            "source_event_id": fact.event_id,
            "project": policy.project,
            "item_type": policy.item_type,
            "priority": policy.priority,
            "status": policy.status,
            "title": _safe_text(
                fact.title, fallback="Operational source update", limit=180
            ),
            "safe_summary": _safe_text(
                fact.summary,
                fallback="The source reported an operational update.",
                limit=500,
            ),
            "recommended_action": _safe_text(
                fact.recommended_action,
                fallback="Review this item in Virgil.",
                limit=300,
            ),
            "waiting_on": policy.waiting_on,
            "reason_code": policy.reason_code,
            "confidence": fact.confidence,
            "due_at": fact.due_at,
            "expires_at": fact.expires_at,
            "source_deep_link": fact.deep_link,
            "prepared_artifact_deep_link": None,
            "processing_version": PROCESSING_VERSION,
        },
        db_path=db_path,
    )
    if notify and db_path is None:
        deliver_attention_result(result)
    return result


def _calendar_time(value: Mapping[str, Any]) -> datetime:
    if value.get("dateTime"):
        return _parse_iso(value["dateTime"])
    try:
        day = date.fromisoformat(str(value.get("date") or ""))
    except ValueError as exc:
        raise SourceError("calendar_response_invalid") from exc
    return datetime.combine(day, wall_time.min, SYDNEY).astimezone(timezone.utc)


def _calendar_video(event: Mapping[str, Any]) -> bool:
    if event.get("hangoutLink"):
        return True
    conference = event.get("conferenceData")
    entries = (
        conference.get("entryPoints", []) if isinstance(conference, Mapping) else []
    )
    return any(
        isinstance(entry, Mapping) and entry.get("entryPointType") == "video"
        for entry in entries
    )


def calendar_facts(
    events: Sequence[Mapping[str, Any]], now: datetime
) -> tuple[SourceFact, ...]:
    """Map a bounded Calendar response to facts, including deterministic clashes."""

    now = now.astimezone(timezone.utc)
    today = now.astimezone(SYDNEY).date()
    facts: list[SourceFact] = []
    intervals: list[tuple[datetime, datetime, str, str, str]] = []
    for event in events:
        raw_id = str(event.get("id") or "")
        if not raw_id or len(raw_id) > 1024:
            continue
        status = str(event.get("status") or "confirmed")
        start_data = event.get("start")
        end_data = event.get("end")
        if not isinstance(start_data, Mapping) or not isinstance(end_data, Mapping):
            if status == "cancelled":
                continue
            raise SourceError("calendar_response_invalid")
        start = _calendar_time(start_data)
        end = _calendar_time(end_data)
        if end <= start:
            raise SourceError("calendar_response_invalid")
        soon = (
            not start_data.get("date")
            and start <= now + timedelta(hours=2)
            and end >= now
        )
        title = _safe_text(event.get("summary"), fallback="Calendar event", limit=180)
        record_id = f"calendar:{_hash(raw_id)}"
        updated = str(event.get("updated") or "")
        attendees = event.get("attendees")
        attendee_count = 0
        if isinstance(attendees, list):
            attendee_count = len(attendees) + sum(
                max(0, int(entry.get("additionalGuests") or 0))
                for entry in attendees
                if isinstance(entry, Mapping)
            )
        local_start = start.astimezone(SYDNEY)
        local_end = end.astimezone(SYDNEY)
        summary = (
            f"Starts {local_start.strftime('%a %-d %b at %-I:%M %p')} Australia Sydney time; "
            f"ends {local_end.strftime('%-I:%M %p')}. "
            f"Attendees: {attendee_count}. Location: {'yes' if event.get('location') else 'no'}. "
            f"Video link: {'yes' if _calendar_video(event) else 'no'}. Status: {status}."
        )
        if status == "cancelled":
            kind = "calendar_cancelled"
            action = "No action is required unless the cancellation was unexpected."
        elif _PREP_WORDS.search(title):
            kind = "calendar_prepare_soon" if soon else "calendar_prepare"
            action = "Review the meeting purpose and prepare the necessary material."
        elif soon:
            kind = "calendar_soon"
            action = "Check that you are ready before the meeting starts."
        elif local_start.date() == today:
            kind = "calendar_today"
            action = "Keep this meeting in view today."
        else:
            kind = "calendar_upcoming"
            action = "No action is required unless preparation becomes necessary."
        link = str(event.get("htmlLink") or "")
        parsed_link = urlparse(link)
        if not (
            parsed_link.scheme == "https"
            and (
                parsed_link.hostname == "calendar.google.com"
                or (
                    parsed_link.hostname == "www.google.com"
                    and parsed_link.path == "/calendar/event"
                )
            )
        ):
            link = None
        facts.append(
            SourceFact(
                "calendar",
                record_id,
                _event_id(status, updated, _iso(start), _iso(end)),
                kind,
                title,
                summary,
                action,
                due_at=_iso(start),
                expires_at=_iso(end),
                deep_link=link,
            )
        )
        if status != "cancelled" and event.get("transparency") != "transparent":
            intervals.append((start, end, record_id, title, updated))

    intervals.sort(key=lambda value: (value[0], value[1], value[2]))
    for index, first in enumerate(intervals):
        for second in intervals[index + 1 :]:
            if second[0] >= first[1]:
                break
            pair = sorted((first[2], second[2]))
            record_id = f"calendar-conflict:{_hash('|'.join(pair))}"
            title = f"Calendar conflict: {first[3]} and {second[3]}"
            facts.append(
                SourceFact(
                    "calendar",
                    record_id,
                    _event_id(first[4], second[4], _iso(first[0]), _iso(second[0])),
                    "calendar_conflict",
                    title,
                    "Two non-transparent events overlap in your Calendar.",
                    "Review the conflict and decide which commitment to keep.",
                    due_at=_iso(max(first[0], second[0])),
                    expires_at=_iso(max(first[1], second[1])),
                )
            )
    return tuple(facts)


def _calendar_service() -> tuple[Any, Path]:
    scripts = (
        Path(__file__).resolve().parent / "skills/productivity/google-workspace/scripts"
    )
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from google_auth import SERVICE_PROFILES, oauth_token_path

        token_path = oauth_token_path()
        _require_private_file(token_path, "calendar_auth_failed")
        credentials = Credentials.from_authorized_user_file(
            str(token_path), list(SERVICE_PROFILES["linxio"])
        )
        if not credentials.valid or not credentials.has_scopes(
            SERVICE_PROFILES["linxio"]
        ):
            raise SourceError("calendar_auth_failed")
        return build(
            "calendar", "v3", credentials=credentials, cache_discovery=False
        ), token_path
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("calendar_auth_failed") from exc


def _gmail_account_fingerprint(token_path: Path) -> str:
    path = token_path.parent / "incoming-autodraft" / "state.db"
    _require_private_file(path, "calendar_account_unverified")
    try:
        uri = f"file:{quote(path.absolute().as_posix(), safe='/')}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='verified_account_fingerprint'"
            ).fetchone()
        value = str(row[0] if row else "")
    except (OSError, sqlite3.DatabaseError) as exc:
        raise SourceError("calendar_account_unverified") from exc
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise SourceError("calendar_account_unverified")
    return value


def sync_calendar(context: SourceContext) -> SourceResult:
    service, token_path = context.cache.get("calendar_service") or _calendar_service()
    try:
        primary = (
            service.calendarList().get(calendarId="primary", fields="id").execute()
        )
        calendar_id = str(primary.get("id") or "").strip().casefold()
        if not calendar_id or hashlib.sha256(
            calendar_id.encode()
        ).hexdigest() != _gmail_account_fingerprint(token_path):
            raise SourceError("calendar_wrong_account")

        end = context.now + timedelta(days=7)
        events: list[Mapping[str, Any]] = []
        page_token = None
        fields = (
            "nextPageToken,items(id,summary,status,htmlLink,updated,start,end,"
            "transparency,location,hangoutLink,conferenceData(entryPoints(entryPointType)),"
            "attendees(additionalGuests))"
        )
        for _ in range(5):
            response = (
                service
                .events()
                .list(
                    calendarId="primary",
                    timeMin=_iso(context.now),
                    timeMax=_iso(end),
                    singleEvents=True,
                    orderBy="startTime",
                    showDeleted=True,
                    maxResults=250,
                    pageToken=page_token,
                    fields=fields,
                )
                .execute()
            )
            batch = response.get("items", [])
            if not isinstance(batch, list):
                raise SourceError("calendar_response_invalid")
            events.extend(event for event in batch if isinstance(event, Mapping))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        else:
            raise SourceError("calendar_response_too_large")
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("calendar_api_failed") from exc

    facts = calendar_facts(events, context.now)
    seen = frozenset(fact.record_id for fact in facts)
    message = (
        "Calendar is connected; no events are scheduled in the next seven days."
        if not events
        else f"Calendar is active with {len(events)} event records in the next seven days."
    )
    return SourceResult(
        "active",
        message,
        facts,
        (
            ReconcileScope("calendar", "calendar:", seen),
            ReconcileScope("calendar", "calendar-conflict:", seen),
        ),
    )


def _validate_operational_item(item: Any) -> Mapping[str, Any]:
    expected = {
        "source_id": str,
        "title": str,
        "item_type": str,
        "created_at": str,
        "review_status": str,
        "current_action": str,
        "evidence_quality": str,
        "research_status": str,
        "research_updated_at": str,
        "research_stalled": bool,
        "research_has_artifact": bool,
        "promotion_candidate_ready": bool,
        "promotion_approved": bool,
        "high_risk": bool,
        "blocked": bool,
    }
    if not isinstance(item, Mapping) or set(item) != set(expected):
        raise SourceError("cogitator_snapshot_invalid")
    for key, value_type in expected.items():
        if not isinstance(item[key], value_type):
            raise SourceError("cogitator_snapshot_invalid")
        if value_type is str and len(item[key]) > 500:
            raise SourceError("cogitator_snapshot_invalid")
    if not item["source_id"] or not item["title"]:
        raise SourceError("cogitator_snapshot_invalid")
    return item


def _cogitator_kind(item: Mapping[str, Any]) -> str:
    research = str(item["research_status"]).casefold()
    review = str(item["review_status"]).casefold()
    action = str(item["current_action"]).casefold()
    if item["blocked"] or item["high_risk"] or item["research_stalled"]:
        return "cogitator_failed"
    if any(word in research for word in ("fail", "error", "blocked")):
        return "cogitator_failed"
    if any(word in review for word in ("approved", "rejected", "skipped", "complete")):
        return "cogitator_closed"
    if item["promotion_candidate_ready"] and not item["promotion_approved"]:
        return "cogitator_promotion"
    if any(
        word in review or word in action
        for word in ("needs your decision", "needs_cal", "approve", "reject", "promote")
    ):
        return "cogitator_decision"
    if item["research_has_artifact"] and any(
        word in research for word in ("complete", "ready", "done")
    ):
        return "cogitator_ready"
    if any(
        word in research
        for word in ("running", "researching", "started", "queued", "pending")
    ):
        return "cogitator_running"
    return "cogitator_quiet"


def sync_cogitator(context: SourceContext) -> SourceResult:
    enabled = bool(_nested(context.config, "decision_batch", "enabled", default=False))
    base_url = str(
        _nested(context.config, "decision_batch", "base_url", default="") or ""
    )
    token = os.environ.get("COGITATOR_BRIDGE_TOKEN", "").strip()
    if not enabled or not base_url or not token:
        return SourceResult(
            "failed", "Cogitator is unavailable because its bridge is not configured."
        )
    try:
        response = request_decision_batch(base_url=base_url, token=token)
    except DecisionBatchBridgeError as exc:
        if exc.code in {"BRIDGE_UNREACHABLE", "BRIDGE_HTTP_ERROR"}:
            raise SourceError("cogitator_bridge_unreachable") from exc
        raise SourceError("cogitator_bridge_failed") from exc
    raw_items = response.get("operational_items")
    if raw_items is None:
        return SourceResult(
            "degraded",
            "Cogitator is reachable; its bounded operational snapshot is not available yet.",
        )
    if not isinstance(raw_items, list) or len(raw_items) > 500:
        raise SourceError("cogitator_snapshot_invalid")

    facts: list[SourceFact] = []
    for raw in raw_items:
        item = _validate_operational_item(raw)
        kind = _cogitator_kind(item)
        research = _safe_text(item["research_status"], fallback="not running", limit=80)
        review = _safe_text(item["review_status"], fallback="not reviewed", limit=80)
        summary = (
            f"Review status: {review}. Research status: {research}. "
            f"Evidence quality: {_safe_text(item['evidence_quality'], fallback='unknown', limit=80)}."
        )
        action = {
            "cogitator_decision": "Review this candidate in the Cogitator Decision Inbox.",
            "cogitator_promotion": "Review the promotion candidate in Cogitator.",
            "cogitator_ready": "Review the completed research in Cogitator.",
            "cogitator_failed": "Review the blocked or failed Cogitator item.",
            "cogitator_running": "No action is required while research is running.",
            "cogitator_quiet": "No action is required while Cogitator monitors this item.",
            "cogitator_closed": "No action is required.",
        }[kind]
        event_material = json.dumps(item, sort_keys=True, separators=(",", ":"))
        facts.append(
            SourceFact(
                "cogitator",
                f"cogitator:{_hash(item['source_id'])}",
                _event_id(event_material),
                kind,
                item["title"],
                summary,
                action,
                confidence=None,
            )
        )
    seen = frozenset(fact.record_id for fact in facts)
    return SourceResult(
        "active",
        f"Cogitator is active with {len(facts)} bounded operational records.",
        tuple(facts),
        (ReconcileScope("cogitator", "cogitator:", seen),),
    )


_FAILURE_CONCLUSIONS = {
    "ACTION_REQUIRED",
    "CANCELLED",
    "FAILURE",
    "STARTUP_FAILURE",
    "TIMED_OUT",
}
STALE_WORKTREE_AGE = timedelta(days=14)


def _run_json(args: Sequence[str], *, timeout: int = 20) -> Any:
    try:
        result = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError("source_command_failed") from exc
    if result.returncode:
        raise SourceError("source_command_failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SourceError("source_response_invalid") from exc


_REVIEW_THREADS_QUERY = """query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100){nodes{isResolved}pageInfo{hasNextPage}}}}}"""


def _unresolved_review_threads(repository: str, number: int) -> int:
    owner, name = repository.split("/", 1)
    response = _run_json([
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_REVIEW_THREADS_QUERY}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"number={number}",
    ])
    try:
        threads = response["data"]["repository"]["pullRequest"]["reviewThreads"]
        nodes = threads["nodes"]
        if threads["pageInfo"]["hasNextPage"] or not isinstance(nodes, list):
            raise SourceError("github_review_response_incomplete")
        return sum(
            1
            for node in nodes
            if isinstance(node, Mapping) and node.get("isResolved") is False
        )
    except (KeyError, TypeError) as exc:
        raise SourceError("github_review_response_invalid") from exc


def _commerce_pr(pr: Mapping[str, Any]) -> bool:
    labels = pr.get("labels")
    names = (
        " ".join(
            str(label.get("name") or "")
            for label in labels
            if isinstance(label, Mapping)
        )
        if isinstance(labels, list)
        else ""
    )
    material = f"{pr.get('title', '')} {pr.get('headRefName', '')} {names}".casefold()
    return any(
        word in material for word in ("ecommerce", "commerce", "purchase-executor")
    )


def github_pr_facts(
    repository: str,
    open_prs: Sequence[Mapping[str, Any]],
    merged_prs: Sequence[Mapping[str, Any]],
) -> tuple[SourceFact, ...]:
    facts: list[SourceFact] = []
    for pr in [*open_prs, *merged_prs]:
        number = pr.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            continue
        if _commerce_pr(pr):
            continue
        merged_at = str(pr.get("mergedAt") or "")
        checks = pr.get("statusCheckRollup")
        failed = isinstance(checks, list) and any(
            isinstance(check, Mapping)
            and str(check.get("conclusion") or "").upper() in _FAILURE_CONCLUSIONS
            for check in checks
        )
        review = str(pr.get("reviewDecision") or "")
        if merged_at:
            kind = "github_merged"
            summary = (
                "The pull request merged successfully and needs no further action."
            )
            action = "Review the completed result if useful."
        elif failed:
            kind = "github_failing"
            summary = "A required pull request check is failing."
            action = "Review the failing check and decide how to proceed."
        elif (
            review in {"CHANGES_REQUESTED", "REVIEW_REQUIRED"}
            or int(pr.get("unresolvedReviewThreads") or 0) > 0
        ):
            kind = "github_review"
            summary = "The pull request has an unresolved review decision."
            action = "Review the pull request findings and decide the next step."
        elif bool(pr.get("isDraft")):
            kind = "github_open"
            summary = "Implementation work is active in a draft pull request."
            action = "No action is required while implementation is active."
        else:
            kind = "github_ready"
            summary = "The pull request is open and ready for review."
            action = "Review the completed implementation in GitHub."
        url = str(pr.get("url") or "")
        if not url.startswith(f"https://github.com/{repository}/pull/"):
            url = None
        updated = str(pr.get("updatedAt") or merged_at)
        facts.append(
            SourceFact(
                "github",
                f"github:{_hash(repository, 16)}:pr:{number}",
                _event_id(
                    updated,
                    merged_at,
                    review,
                    failed,
                    pr.get("isDraft"),
                    pr.get("unresolvedReviewThreads"),
                ),
                kind,
                f"{repository} PR #{number}: {pr.get('title') or 'Untitled pull request'}",
                summary,
                action,
                domain=repository,
                deep_link=url,
            )
        )
    return tuple(facts)


def _repository_risk(repository: str, path: Path, remote_sha: str) -> SourceFact | None:
    if (
        not path.is_dir()
        or not (path / ".git").exists()
        and not (path / ".git").is_file()
    ):
        return None
    try:
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if branch.returncode or head.returncode or status.returncode:
        return None
    lines = status.stdout.splitlines()
    tracked_dirty = any(not line.startswith("?? ") for line in lines)
    risky_untracked = any(
        line.startswith("?? ")
        and not line[3:].startswith(("plans/", "docs/", "storage/"))
        and Path(line[3:]).suffix
        in {".py", ".js", ".ts", ".tsx", ".yaml", ".yml", ".service"}
        for line in lines
    )
    main_diverged = (
        branch.stdout.strip() == "main" and head.stdout.strip() != remote_sha
    )
    if not (tracked_dirty or risky_untracked or main_diverged):
        return None
    reason = (
        "The production checkout differs from the authoritative main branch."
        if main_diverged
        else "The production checkout has tracked or executable local changes."
    )
    return SourceFact(
        "github",
        f"github-repo:{_hash(repository, 16)}",
        _event_id(head.stdout.strip(), remote_sha, tracked_dirty, risky_untracked),
        "repo_risk",
        f"Repository risk: {repository}",
        reason,
        "Review the checkout without resetting or deleting unrelated work.",
        domain=repository,
        deep_link=f"https://github.com/{repository}",
    )


def _stale_worktree_facts(
    repository: str,
    path: Path,
    remote_sha: str,
    open_branches: set[str],
    now: datetime,
) -> tuple[SourceFact, ...]:
    try:
        listed = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if listed.returncode:
        return ()
    facts = []
    # ponytail: dirty + 14 days is the V1 stale signal; add branch activity
    # history only if this conservative rule misses real abandoned work.
    for block in listed.stdout.strip().split("\n\n")[:100]:
        metadata = dict(
            line.partition(" ")[::2] for line in block.splitlines() if " " in line
        )
        branch = str(metadata.get("branch") or "").removeprefix("refs/heads/")
        location = Path(str(metadata.get("worktree") or ""))
        if (
            not branch
            or branch in open_branches
            or location.absolute() == path.absolute()
            or any(
                word in branch.casefold()
                for word in ("ecommerce", "commerce", "purchase-executor")
            )
            or _repository_risk(repository, location, remote_sha) is None
        ):
            continue
        try:
            committed = subprocess.run(
                ["git", "log", "-1", "--format=%ct"],
                cwd=location,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            committed_at = datetime.fromtimestamp(
                float(committed.stdout.strip()), timezone.utc
            )
        except (OSError, subprocess.TimeoutExpired, OverflowError, ValueError):
            continue
        if committed.returncode or now - committed_at < STALE_WORKTREE_AGE:
            continue
        facts.append(
            SourceFact(
                "github",
                f"github-worktree:{_hash(f'{repository}:{location.absolute()}')}",
                _event_id(metadata.get("HEAD"), committed.stdout),
                "repo_risk",
                f"Stale implementation worktree: {repository}",
                "A dirty implementation worktree has no open pull request and no recent commit.",
                "Review the worktree safely without resetting or deleting unrelated work.",
                domain=repository,
                deep_link=f"https://github.com/{repository}",
            )
        )
    return tuple(facts)


def sync_github(context: SourceContext) -> SourceResult:
    if not shutil.which("gh"):
        return SourceResult(
            "unavailable",
            "GitHub is unavailable because authenticated CLI access is missing.",
        )
    since = context.now.date() - timedelta(days=7)
    all_open: dict[str, list[Mapping[str, Any]]] = {}
    facts: list[SourceFact] = []
    fields = "number,title,url,isDraft,updatedAt,createdAt,reviewDecision,statusCheckRollup,headRefName,labels"
    merged_fields = "number,title,url,mergedAt,updatedAt,headRefName,labels"
    repo_paths = {
        "3ndym10n/hermes-agent": Path(__file__).resolve().parent,
        "3ndym10n/Cogitator": Path(
            str(
                _nested(
                    context.config,
                    "attention",
                    "operational_sources",
                    "cogitator_repo_path",
                    default="~/Projects/Cogitator_clean",
                )
            )
        ).expanduser(),
    }
    for repository in REPOSITORIES:
        open_prs = _run_json([
            "gh",
            "pr",
            "list",
            "--repo",
            repository,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            fields,
        ])
        merged_prs = _run_json([
            "gh",
            "pr",
            "list",
            "--repo",
            repository,
            "--state",
            "merged",
            "--search",
            f"merged:>={since.isoformat()}",
            "--limit",
            "100",
            "--json",
            merged_fields,
        ])
        if not isinstance(open_prs, list) or not isinstance(merged_prs, list):
            raise SourceError("github_response_invalid")
        enriched: list[Mapping[str, Any]] = []
        for raw_pr in open_prs:
            if not isinstance(raw_pr, Mapping):
                continue
            pr = dict(raw_pr)
            number = pr.get("number")
            if isinstance(number, int) and not isinstance(number, bool):
                pr["unresolvedReviewThreads"] = _unresolved_review_threads(
                    repository, number
                )
            enriched.append(pr)
        all_open[repository] = enriched
        facts.extend(
            github_pr_facts(
                repository,
                enriched,
                [pr for pr in merged_prs if isinstance(pr, Mapping)],
            )
        )
        remote = _run_json(["gh", "api", f"repos/{repository}/commits/main"])
        remote_sha = str(remote.get("sha") or "") if isinstance(remote, Mapping) else ""
        if re.fullmatch(r"[0-9a-f]{40}", remote_sha):
            risk = _repository_risk(repository, repo_paths[repository], remote_sha)
            if risk:
                facts.append(risk)
            facts.extend(
                _stale_worktree_facts(
                    repository,
                    repo_paths[repository],
                    remote_sha,
                    {str(pr.get("headRefName") or "") for pr in enriched},
                    context.now,
                )
            )
    context.cache["github_open_prs"] = all_open
    seen = frozenset(fact.record_id for fact in facts)
    return SourceResult(
        "active",
        f"GitHub is active with {sum(len(value) for value in all_open.values())} relevant open pull requests checked.",
        tuple(facts),
        (
            ReconcileScope("github", "github:", seen),
            ReconcileScope("github", "github-repo:", seen),
            ReconcileScope("github", "github-worktree:", seen),
        ),
    )


def _open_commerce_db(path: Path) -> sqlite3.Connection:
    _require_private_file(path, "commerce_database_invalid")
    uri = f"file:{quote(path.absolute().as_posix(), safe='/')}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        check = conn.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise SourceError("commerce_database_invalid")
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"jobs", "gates"}.issubset(tables):
            raise SourceError("commerce_database_unsupported")
        required = {
            "jobs": {
                "job_id",
                "current_state",
                "substatus",
                "deadline_at",
                "active",
                "updated_at",
            },
            "gates": {
                "gate_id",
                "job_id",
                "gate_type",
                "status",
                "opened_at",
                "expires_at",
            },
        }
        for table, columns in required.items():
            actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not columns.issubset(actual):
                raise SourceError("commerce_database_unsupported")
        return conn
    except SourceError:
        if "conn" in locals():
            conn.close()
        raise
    except (OSError, sqlite3.DatabaseError) as exc:
        if "conn" in locals():
            conn.close()
        raise SourceError("commerce_database_invalid") from exc


def _commerce_state_kind(state: str, active: bool) -> str:
    value = state.casefold()
    if any(word in value for word in ("fail", "error", "rolled_back")):
        return "commerce_failed"
    if any(
        word in value
        for word in ("uncertain", "unknown", "indeterminate", "reconciliation")
    ):
        return "commerce_uncertain"
    if any(word in value for word in ("complete", "ready", "done", "deployed", "live")):
        return "commerce_ready"
    if "cancelled" in value:
        return "commerce_complete"
    return "commerce_active"


def sync_ecommerce(context: SourceContext) -> SourceResult:
    facts: list[SourceFact] = []
    open_by_repo = context.cache.get("github_open_prs")
    open_prs: list[Mapping[str, Any]] = []
    if isinstance(open_by_repo, Mapping) and "3ndym10n/hermes-agent" in open_by_repo:
        open_prs = list(open_by_repo["3ndym10n/hermes-agent"])
    elif shutil.which("gh"):
        fields = "number,title,url,isDraft,updatedAt,reviewDecision,statusCheckRollup,headRefName,labels"
        response = _run_json([
            "gh",
            "pr",
            "list",
            "--repo",
            "3ndym10n/hermes-agent",
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            fields,
        ])
        if not isinstance(response, list):
            raise SourceError("github_response_invalid")
        open_prs = [pr for pr in response if isinstance(pr, Mapping)]
    for raw_pr in open_prs:
        if not _commerce_pr(raw_pr):
            continue
        pr = dict(raw_pr)
        number = pr.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            continue
        checks = pr.get("statusCheckRollup")
        failed = isinstance(checks, list) and any(
            isinstance(check, Mapping)
            and str(check.get("conclusion") or "").upper() in _FAILURE_CONCLUSIONS
            for check in checks
        )
        review = str(pr.get("reviewDecision") or "")
        if failed:
            kind = "commerce_failed"
            action = "Review the failing ecommerce check and decide how to proceed."
        elif review in {"CHANGES_REQUESTED", "REVIEW_REQUIRED"}:
            kind = "commerce_gate"
            action = "Review the ecommerce pull request and decide the next step."
        elif pr.get("isDraft"):
            kind = "commerce_active"
            action = "No action is required while implementation is active."
        else:
            kind = "commerce_ready"
            action = "Review the completed ecommerce implementation in GitHub."
        url = str(pr.get("url") or "")
        if not url.startswith("https://github.com/3ndym10n/hermes-agent/pull/"):
            url = None
        facts.append(
            SourceFact(
                "ecommerce",
                f"ecommerce:github-pr:{number}",
                _event_id(pr.get("updatedAt"), review, failed, pr.get("isDraft")),
                kind,
                f"Ecommerce PR #{number}: {pr.get('title') or 'Implementation work'}",
                "Trustworthy ecommerce implementation state is available in GitHub.",
                action,
                deep_link=url,
            )
        )

    configured = _nested(
        context.config,
        "attention",
        "operational_sources",
        "commerce_db_path",
        default=str(get_hermes_home() / "commerce" / "commerce_jobs.db"),
    )
    db_path = Path(str(configured)).expanduser()
    database_available = db_path.is_file()
    if database_available:
        conn = _open_commerce_db(db_path)
        try:
            jobs = conn.execute(
                "SELECT job_id,current_state,substatus,deadline_at,active,updated_at "
                "FROM jobs WHERE active=1 OR updated_at>=? ORDER BY updated_at DESC LIMIT 200",
                (_iso(context.now - timedelta(days=7)),),
            ).fetchall()
            gates = conn.execute(
                "SELECT gate_id,job_id,gate_type,status,opened_at,expires_at "
                "FROM gates WHERE status='open' "
                "ORDER BY opened_at DESC LIMIT 200"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise SourceError("commerce_database_invalid") from exc
        finally:
            conn.close()
        for row in jobs:
            state = str(row["current_state"] or "unknown")
            kind = _commerce_state_kind(state, bool(row["active"]))
            facts.append(
                SourceFact(
                    "ecommerce",
                    f"ecommerce:job:{_hash(row['job_id'])}",
                    _event_id(
                        state, row["substatus"], row["updated_at"], row["active"]
                    ),
                    kind,
                    "Ecommerce implementation job",
                    f"Job state: {_safe_text(state, fallback='unknown', limit=80)}. Active: {'yes' if row['active'] else 'no'}.",
                    {
                        "commerce_failed": "Review the failed ecommerce job and decide how to proceed.",
                        "commerce_uncertain": "Verify external state before authorizing any further action.",
                        "commerce_ready": "Review the completed ecommerce result.",
                        "commerce_complete": "No action is required.",
                        "commerce_active": "No action is required while implementation is active.",
                    }[kind],
                    due_at=_coerce_db_time(row["deadline_at"]),
                )
            )
        for row in gates:
            gate_type = _safe_text(row["gate_type"], fallback="human", limit=80)
            facts.append(
                SourceFact(
                    "ecommerce",
                    f"ecommerce:gate:{_hash(row['gate_id'])}",
                    _event_id(row["status"], row["opened_at"], row["expires_at"]),
                    "commerce_gate",
                    f"Ecommerce {gate_type} gate",
                    "A durable ecommerce job is waiting at a human or external gate.",
                    "Review the gate and complete only the approved external step.",
                    due_at=_coerce_db_time(row["expires_at"]),
                )
            )

    seen = frozenset(fact.record_id for fact in facts)
    if not database_available and not facts:
        status = "unavailable"
        message = "No trustworthy ecommerce runtime feed is currently available."
    elif any(policy_for(fact).status in {"needs_cal", "safety_hold"} for fact in facts):
        status = "blocked"
        message = (
            "Ecommerce has trustworthy current work with a blocking gate or failure."
        )
    elif facts:
        status = "active"
        message = "Ecommerce has trustworthy current work from GitHub or its job store."
    else:
        status = "no_current_work"
        message = "Ecommerce is connected with no current work."
    return SourceResult(
        status,
        message,
        tuple(facts),
        (ReconcileScope("ecommerce", "ecommerce:", seen),),
    )


def _coerce_db_time(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _iso(datetime.fromtimestamp(float(value), timezone.utc))
        return _iso(_parse_iso(value))
    except (OverflowError, OSError, SourceError, ValueError):
        return None


def _unit_state(unit: str, *, user: bool) -> dict[str, str]:
    command = ["systemctl"]
    if user:
        command.append("--user")
    command.extend([
        "show",
        unit,
        "--no-pager",
        "--property=LoadState",
        "--property=ActiveState",
        "--property=SubState",
        "--property=Result",
        "--property=NRestarts",
        "--property=ExecMainStatus",
        "--property=LastTriggerUSec",
    ])
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"LoadState": "error"}
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def _unit_problem(
    unit: str, state: Mapping[str, str], *, severe: bool, required: bool = True
) -> SourceFact | None:
    if state.get("LoadState") in {"not-found", "error", ""}:
        if state.get("LoadState") == "not-found" and not required:
            return None
        return SourceFact(
            "system",
            f"health:unit:{unit}",
            _event_id("missing"),
            "system_severe" if severe else "system_degraded",
            f"Service unavailable: {unit}",
            "An expected operational service is not installed or could not be inspected.",
            "Review the service state and restore the approved unit.",
        )
    active = state.get("ActiveState")
    result = state.get("Result")
    timer = unit.endswith(".timer")
    oneshot = unit in {
        "linxio-incoming-autodraft.service",
        "virgil-operational-sources.service",
    }
    unhealthy = active == "failed" or result in {"failed", "timeout", "watchdog"}
    if timer or not oneshot:
        unhealthy = unhealthy or active != "active"
    try:
        restarts = int(state.get("NRestarts") or 0)
    except ValueError:
        restarts = 0
    if not unhealthy and restarts < 3:
        return None
    summary = (
        "The service is not in its expected healthy state."
        if unhealthy
        else "The service has restarted repeatedly."
    )
    return SourceFact(
        "system",
        f"health:unit:{unit}",
        _event_id(active, result, restarts, state.get("ExecMainStatus")),
        "system_severe" if severe else "system_degraded",
        f"Service degradation: {unit}",
        summary,
        "Review the service status and recent sanitized logs.",
    )


def _systemd_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S UTC").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


def _gmail_meta() -> dict[str, str]:
    token_dir = get_hermes_home() / "secrets" / "google"
    path = token_dir / "incoming-autodraft" / "state.db"
    _require_private_file(path, "gmail_state_invalid")
    try:
        uri = f"file:{quote(path.absolute().as_posix(), safe='/')}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            check = conn.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise SourceError("gmail_state_invalid")
            meta = {
                str(row[0]): str(row[1])
                for row in conn.execute(
                    "SELECT key,value FROM meta WHERE key IN ("
                    "'mode','authentication_health','last_successful_poll',"
                    "'history_watermark','verified_account_fingerprint',"
                    "'account_intervention_required','history_gap_intervention_required',"
                    "'shadow_safety_hold')"
                )
            }
            # Count only. Terminal rows no longer gate the worker, so surface them
            # here rather than letting a historical failure disappear silently.
            meta["terminal_failed_count"] = str(
                conn.execute(
                    "SELECT COUNT(*) FROM messages "
                    "WHERE state='failed' AND CAST(retry_count AS INTEGER) >= 3"
                ).fetchone()[0]
            )
            return meta
    except SourceError:
        raise
    except (OSError, sqlite3.DatabaseError) as exc:
        raise SourceError("gmail_state_invalid") from exc


def sync_gmail_health(context: SourceContext) -> SourceResult:
    meta = _gmail_meta()
    timer = _unit_state("linxio-incoming-autodraft.timer", user=True)
    if meta.get("authentication_health") not in {"ok", ""}:
        return SourceResult("failed", "Gmail authentication is failing.")
    if (
        timer.get("ActiveState") != "active"
        or meta.get("mode", "disabled") == "disabled"
    ):
        return SourceResult("paused", "Gmail Attention synchronization is paused.")
    if (
        meta.get("account_intervention_required") == "1"
        or meta.get("history_gap_intervention_required") == "1"
        or meta.get("shadow_safety_hold")
    ):
        return SourceResult(
            "degraded", "Gmail Attention synchronization requires review."
        )
    if not meta.get("history_watermark") or not meta.get(
        "verified_account_fingerprint"
    ):
        return SourceResult(
            "degraded", "Gmail synchronization has no verified checkpoint."
        )
    try:
        last_poll = float(meta.get("last_successful_poll") or 0)
    except ValueError:
        last_poll = 0
    if context.now.timestamp() - last_poll > 300:
        return SourceResult(
            "degraded", "Gmail synchronization has not completed recently."
        )
    terminal = meta.get("terminal_failed_count", "0")
    if terminal not in {"", "0"}:
        return SourceResult(
            "active",
            "Gmail Attention synchronization is active in shadow mode with "
            f"{terminal} historical failures retained for review.",
        )
    return SourceResult(
        "active", "Gmail Attention synchronization is active in shadow mode."
    )


def _tailscale_private_ok(status: Any) -> bool:
    if not isinstance(status, Mapping):
        return False
    tcp = status.get("TCP")
    web = status.get("Web")
    if not isinstance(tcp, Mapping) or not isinstance(web, Mapping):
        return False
    if not any(str(key).endswith("8443") for key in tcp):
        return False
    handler = next(
        (value for key, value in web.items() if str(key).endswith(":8443")), None
    )
    if not isinstance(handler, Mapping):
        return False
    routes = handler.get("Handlers", handler)
    root = routes.get("/") if isinstance(routes, Mapping) else None
    proxy = root.get("Proxy") if isinstance(root, Mapping) else None
    if not str(proxy or "").endswith(":8788"):
        return False
    funnel = status.get("AllowFunnel")
    return not (
        isinstance(funnel, Mapping)
        and any(
            str(key).endswith(":8443") and bool(value) for key, value in funnel.items()
        )
    )


def _attention_db_healthy(path: Path) -> bool:
    try:
        _require_private_file(path, "attention_database_invalid")
        uri = f"file:{quote(path.absolute().as_posix(), safe='/')}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            quick = conn.execute("PRAGMA quick_check").fetchone()
            foreign = conn.execute("PRAGMA foreign_key_check").fetchone()
        return bool(quick and quick[0] == "ok" and foreign is None)
    except (OSError, sqlite3.DatabaseError, SourceError):
        return False


def sync_system(context: SourceContext) -> SourceResult:
    facts: list[SourceFact] = []
    units = (
        ("virgil-mobile.service", True, True, True),
        ("linxio-incoming-autodraft.timer", True, False, True),
        ("linxio-incoming-autodraft.service", True, False, True),
        ("virgil-operational-sources.service", True, False, True),
        ("virgil-operational-sources.timer", True, False, True),
        ("hermes-gateway.service", False, True, True),
        ("hermes-research-bridge.service", False, False, False),
    )
    states: dict[str, dict[str, str]] = {}
    for unit, user, severe, required in units:
        state = _unit_state(unit, user=user)
        states[unit] = state
        problem = _unit_problem(unit, state, severe=severe, required=required)
        if problem:
            facts.append(problem)
    for unit, max_age in (
        ("linxio-incoming-autodraft.timer", 300),
        ("virgil-operational-sources.timer", 600),
    ):
        state = states[unit]
        last_trigger = _systemd_time(state.get("LastTriggerUSec", ""))
        if (
            state.get("ActiveState") == "active"
            and last_trigger is not None
            and context.now - last_trigger > timedelta(seconds=max_age)
        ):
            facts.append(
                SourceFact(
                    "system",
                    f"health:timer-stale:{unit}",
                    _event_id(int(last_trigger.timestamp() // max_age)),
                    "system_degraded",
                    f"Timer execution is stale: {unit}",
                    "An operational timer has not triggered within policy.",
                    "Review the timer and its last service result.",
                )
            )

    if not _attention_db_healthy(attention_db_path()):
        facts.append(
            SourceFact(
                "system",
                "health:attention-db",
                _event_id("invalid"),
                "system_severe",
                "Attention database integrity failure",
                "The durable Attention Queue failed its integrity check.",
                "Stop queue writers and inspect the protected database backup path.",
            )
        )

    try:
        tailscale = _run_json(["tailscale", "serve", "status", "--json"])
    except SourceError:
        tailscale = None
    if not _tailscale_private_ok(tailscale):
        facts.append(
            SourceFact(
                "system",
                "health:tailscale-8443",
                _event_id("unavailable"),
                "system_severe",
                "Private Virgil access unavailable",
                "The private Tailscale route for Virgil Mobile is unavailable or unsafe.",
                "Restore private port 8443 without enabling Funnel.",
            )
        )

    threshold = int(
        _nested(
            context.config,
            "attention",
            "operational_sources",
            "minimum_free_disk_bytes",
            default=5 * 1024**3,
        )
    )
    try:
        free = shutil.disk_usage(get_hermes_home()).free
    except OSError:
        free = 0
    if free < threshold:
        facts.append(
            SourceFact(
                "system",
                "health:disk",
                _event_id(free // (100 * 1024**2)),
                "system_severe" if free < 1024**3 else "system_degraded",
                "Low disk space",
                "Available disk space is below the configured operational threshold.",
                "Free disk space without deleting protected application state.",
            )
        )

    try:
        gmail = _gmail_meta()
    except SourceError:
        gmail = {}
        facts.append(
            SourceFact(
                "system",
                "health:gmail-state",
                _event_id("unavailable"),
                "system_degraded",
                "Gmail checkpoint unavailable",
                "The Gmail worker state could not be read safely.",
                "Review the protected Gmail worker state and restore its checkpoint.",
            )
        )
    if gmail.get("authentication_health") not in {"ok", ""}:
        facts.append(
            SourceFact(
                "system",
                "health:gmail-auth",
                _event_id(gmail.get("authentication_health")),
                "system_degraded",
                "Gmail authentication failure",
                "The existing Gmail worker reports an authentication failure.",
                "Review the approved Google account connection.",
            )
        )
    try:
        last_poll = float(gmail.get("last_successful_poll") or 0)
    except ValueError:
        last_poll = 0
    if gmail and (
        not gmail.get("history_watermark")
        or not gmail.get("verified_account_fingerprint")
        or not last_poll
    ):
        facts.append(
            SourceFact(
                "system",
                "health:gmail-checkpoint",
                _event_id("missing"),
                "system_degraded",
                "Gmail checkpoint missing",
                "The Gmail worker has lost a required account or history checkpoint.",
                "Pause processing and review the protected Gmail checkpoint state.",
            )
        )
    # A deliberate pause or fail-closed safety hold stops polling by design, so
    # staleness then only duplicates the worker's own hold item. sync_gmail_health
    # already guards this; keep the signal for a worker that should be running.
    deliberately_idle = bool(
        gmail.get("mode", "disabled") == "disabled" or gmail.get("shadow_safety_hold")
    )
    if last_poll and not deliberately_idle and context.now.timestamp() - last_poll > 300:
        facts.append(
            SourceFact(
                "system",
                "health:gmail-stale",
                _event_id(int(last_poll // 300)),
                "system_degraded",
                "Gmail worker is stale",
                "The Gmail worker has not completed a successful poll within policy.",
                "Review the Gmail timer and worker status.",
            )
        )

    seen = frozenset(fact.record_id for fact in facts)
    return SourceResult(
        "healthy" if not facts else "degraded",
        "System health is normal."
        if not facts
        else f"System health is degraded with {len(facts)} current findings.",
        tuple(facts),
        (ReconcileScope("system", "health:", seen),),
    )


ADAPTERS: dict[str, Callable[[SourceContext], SourceResult]] = {
    "gmail": sync_gmail_health,
    "calendar": sync_calendar,
    "cogitator": sync_cogitator,
    "github": sync_github,
    "ecommerce": sync_ecommerce,
    "system": sync_system,
}
SOURCE_INTERVALS["gmail"] = 120


def _acquire_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
        ):
            raise OSError("unsafe lock")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BlockingIOError as exc:
        if "descriptor" in locals():
            os.close(descriptor)
        raise WorkerBusy() from exc
    except OSError as exc:
        if "descriptor" in locals():
            os.close(descriptor)
        raise SourceError("worker_lock_failed") from exc


def _due(status: Mapping[str, Any], now: datetime, force: bool) -> bool:
    if not status.get("enabled") or status.get("paused"):
        return False
    if force or not status.get("next_scheduled_sync_at"):
        return True
    try:
        return _parse_iso(status["next_scheduled_sync_at"]) <= now
    except SourceError:
        return True


def _deliver_resolution(
    result: Mapping[str, Any], *, notify: bool, db_path: Any
) -> None:
    if notify and db_path is None:
        deliver_attention_result(dict(result))


def _reconcile_source_failure(
    source: str, now: datetime, *, db_path: Any, notify: bool
) -> None:
    resolved = resolve_missing_source_records(
        "system",
        (),
        f"source-recovered:{source}:{int(now.timestamp())}",
        record_prefix=f"source-failure:{source}:",
        db_path=db_path,
    )
    for result in resolved:
        _deliver_resolution(result, notify=notify, db_path=db_path)


def _record_source_failure(
    source: str,
    code: str,
    failures: int,
    *,
    db_path: Any,
    notify: bool,
) -> None:
    threshold = (
        1
        if any(
            marker in code
            for marker in (
                "auth",
                "wrong_account",
                "bridge_unreachable",
                "state_invalid",
            )
        )
        else 3
    )
    if failures < threshold:
        return
    fact = SourceFact(
        "system",
        f"source-failure:{source}:sync",
        _event_id(code, failures),
        "source_failure",
        f"{source.title()} source synchronization failed",
        "An operational source failed repeatedly or could not be authenticated.",
        "Review the source connection and worker status.",
    )
    apply_fact(fact, db_path=db_path, notify=notify)


def run_sources(
    *,
    force: bool = False,
    sources: Sequence[str] | None = None,
    db_path: Path | str | None = None,
    notify: bool = True,
    adapters: Mapping[str, Callable[[SourceContext], SourceResult]] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Run due adapters independently and persist only safe source outcomes."""

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    selected = tuple(sources or ADAPTERS)
    unknown = set(selected) - set(ADAPTERS)
    if unknown:
        raise SourceError("unknown_source")
    statuses = {row["source"]: row for row in list_source_statuses(db_path=db_path)}
    database = Path(db_path) if db_path is not None else attention_db_path()
    lock_descriptor = _acquire_lock(database.parent / ".operational-sources.lock")
    context = SourceContext(current, load_config_readonly())
    implementations = dict(ADAPTERS if adapters is None else adapters)
    output: list[dict[str, Any]] = []
    try:
        for source in selected:
            status = statuses[source]
            if not _due(status, current, force):
                output.append({"source": source, "status": "skipped"})
                continue
            next_sync = current + timedelta(seconds=SOURCE_INTERVALS[source])
            result: SourceResult | None = None
            error_code = "source_sync_failed"
            for _ in range(2):
                try:
                    candidate = implementations[source](context)
                    for fact in candidate.facts:
                        apply_fact(fact, db_path=db_path, notify=notify)
                    for scope in candidate.reconcile:
                        resolved = resolve_missing_source_records(
                            scope.source_type,
                            scope.seen_record_ids,
                            f"reconcile:{source}:{int(current.timestamp())}",
                            record_prefix=scope.record_prefix,
                            db_path=db_path,
                        )
                        for resolution in resolved:
                            _deliver_resolution(
                                resolution, notify=notify, db_path=db_path
                            )
                    update_source_status(
                        source,
                        candidate.status,
                        last_attempted_sync_at=_iso(current),
                        last_successful_sync_at=(
                            None
                            if candidate.status in {"failed", "paused"}
                            else _iso(current)
                        ),
                        next_scheduled_sync_at=_iso(next_sync),
                        error_code=None,
                        message=candidate.message,
                        db_path=db_path,
                    )
                    _reconcile_source_failure(
                        source, current, db_path=db_path, notify=notify
                    )
                    result = candidate
                    break
                except SourceError as exc:
                    error_code = exc.code
                except AttentionError:
                    error_code = "queue_ingestion_failed"
                except Exception:
                    error_code = "source_sync_failed"
            if result is None:
                failure_count = int(status.get("failure_count") or 0) + 1
                updated = update_source_status(
                    source,
                    "failed",
                    last_attempted_sync_at=_iso(current),
                    next_scheduled_sync_at=_iso(next_sync),
                    failure_count=failure_count,
                    error_code=error_code,
                    message=f"{source.title()} synchronization failed safely.",
                    db_path=db_path,
                )
                _record_source_failure(
                    source,
                    error_code,
                    int(updated["failure_count"]),
                    db_path=db_path,
                    notify=notify,
                )
                output.append({
                    "source": source,
                    "status": "failed",
                    "error_code": error_code,
                })
                continue
            output.append({
                "source": source,
                "status": result.status,
                "facts": len(result.facts),
            })

        for resolution in resolve_expired_attention(now=_iso(current), db_path=db_path):
            _deliver_resolution(resolution, notify=notify, db_path=db_path)
        return output
    finally:
        os.close(lock_descriptor)


def doctor(*, db_path: Path | str | None = None) -> dict[str, Any]:
    checks = {
        "attention_database": _attention_db_healthy(
            Path(db_path) if db_path is not None else attention_db_path()
        ),
        "github_authentication": False,
        "tailscale_cli": bool(shutil.which("tailscale")),
        "calendar_credentials": (
            get_hermes_home() / "secrets" / "google" / "google_token.json"
        ).is_file(),
        "cogitator_bridge": bool(
            _nested(load_config_readonly(), "decision_batch", "enabled", default=False)
            and os.environ.get("COGITATOR_BRIDGE_TOKEN")
        ),
    }
    if shutil.which("gh"):
        try:
            subprocess.run(
                ["gh", "auth", "status"], capture_output=True, timeout=10, check=True
            )
            checks["github_authentication"] = True
        except (OSError, subprocess.SubprocessError):
            pass
    return {"ok": all(checks.values()), "checks": checks}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "reconcile"):
        command = commands.add_parser(name)
        command.add_argument("--source", action="append", choices=tuple(ADAPTERS))
    commands.add_parser("status")
    commands.add_parser("doctor")
    for name in ("pause", "resume", "disable", "enable"):
        command = commands.add_parser(name)
        command.add_argument("source", choices=tuple(ADAPTERS))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command in {"run", "reconcile"}:
            result: Any = run_sources(
                force=args.command == "reconcile", sources=args.source
            )
        elif args.command == "status":
            result = list_source_statuses()
        elif args.command == "doctor":
            result = doctor()
        else:
            result = control_source(args.source, args.command)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (AttentionError, SourceError) as exc:
        print(json.dumps({"ok": False, "error": getattr(exc, "code", "worker_failed")}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

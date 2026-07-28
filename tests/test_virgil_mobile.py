from __future__ import annotations

import importlib.util
import os
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import hermes_attention as attention
from gmail_attention import upsert_gmail_outcome
from virgil_mobile_server import create_app


ROOT = Path(__file__).resolve().parents[1]
AUTODRAFT_SCRIPT = (
    ROOT
    / "skills"
    / "productivity"
    / "google-workspace"
    / "scripts"
    / "incoming_autodraft.py"
)
SPEC = importlib.util.spec_from_file_location(
    "virgil_mobile_test_autodraft", AUTODRAFT_SCRIPT
)
assert SPEC and SPEC.loader
autodraft = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(autodraft)

PUBLIC_URL = "https://virgil.example.ts.net:8443"


def payload(**changes):
    value = {
        "source_type": "manual",
        "source_record_id": "record-1",
        "source_event_id": "event-1",
        "project": "personal",
        "item_type": "task",
        "priority": "high",
        "status": "needs_cal",
        "title": "Review a safe task",
        "safe_summary": "A bounded operational task needs review.",
        "recommended_action": "Review the task.",
        "waiting_on": "cal",
        "reason_code": "manual",
        "confidence": 0.9,
        "due_at": None,
        "source_deep_link": None,
        "prepared_artifact_deep_link": None,
        "processing_version": "test-v1",
    }
    value.update(changes)
    return value


def test_queue_is_idempotent_versioned_private_and_retryable(tmp_path):
    db = tmp_path / "attention" / "attention.db"
    created = attention.upsert_attention(payload(), db_path=db, public_url=PUBLIC_URL)
    item_id = created["item"]["item_id"]

    assert created["changed"] is True
    assert created["notification"]["action"] == "send"
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db.stat().st_mode) == 0o600

    unchanged = attention.upsert_attention(payload(), db_path=db, public_url=PUBLIC_URL)
    assert unchanged["item"]["item_id"] == item_id
    assert unchanged["changed"] is False
    assert unchanged["notification"]["action"] == "none"

    attention.record_notification(item_id, success=False, db_path=db)
    retry = attention.upsert_attention(payload(), db_path=db, public_url=PUBLIC_URL)
    assert retry["notification"]["action"] == "send"

    attention.record_notification(item_id, success=True, message_id="42", db_path=db)
    updated_payload = payload(
        source_event_id="event-2",
        safe_summary="A newer bounded source event needs review.",
    )
    updated = attention.upsert_attention(
        updated_payload, db_path=db, public_url=PUBLIC_URL
    )
    assert updated["item"]["row_version"] == 2
    assert updated["notification"] == {
        "action": "edit",
        "message_id": "42",
        "deep_link": f"{PUBLIC_URL}/item/{item_id}",
    }
    attention.record_notification(item_id, success=False, db_path=db)
    retry_edit = attention.upsert_attention(
        updated_payload, db_path=db, public_url=PUBLIC_URL
    )
    assert retry_edit["notification"]["action"] == "edit"

    with pytest.raises(attention.AttentionError, match="stale_attention_item"):
        attention.transition_attention(
            item_id, "resolve", expected_row_version=1, db_path=db
        )
    resolved = attention.transition_attention(
        item_id, "resolve", expected_row_version=2, db_path=db
    )
    assert resolved["status"] == "resolved"

    unchanged_closed = attention.upsert_attention(
        updated_payload, db_path=db, public_url=PUBLIC_URL
    )
    assert unchanged_closed["changed"] is False
    assert unchanged_closed["item"]["status"] == "resolved"
    assert unchanged_closed["item"]["row_version"] == resolved["row_version"]

    reopened = attention.upsert_attention(
        payload(
            source_event_id="event-3",
            safe_summary="A new source event reopened the task.",
        ),
        db_path=db,
        public_url=PUBLIC_URL,
    )
    assert reopened["item"]["item_id"] == item_id
    assert reopened["item"]["status"] == "needs_cal"
    assert (
        attention.get_attention(item_id, db_path=db)["activity"][-1]["event_type"]
        == "reopened"
    )


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"unexpected": "value"}, "invalid_payload_keys"),
        ({"safe_summary": "Contact person@example.com"}, "unsafe_safe_summary"),
        ({"recommended_action": "sudo delete everything"}, "unsafe_recommended_action"),
        (
            {"recommended_action": "Please run curl example"},
            "unsafe_recommended_action",
        ),
        (
            {"source_deep_link": "https://evil.example/thread"},
            "invalid_source_deep_link",
        ),
        (
            {"source_deep_link": "https://mail.google.com:444/thread"},
            "invalid_source_deep_link",
        ),
        (
            {
                "source_type": "calendar",
                "source_deep_link": "https://www.google.com/calendar/settings",
            },
            "invalid_source_deep_link",
        ),
        ({"status": "whatever"}, "invalid_status"),
    ],
)
def test_queue_contract_fails_closed(tmp_path, changes, code):
    with pytest.raises(attention.AttentionError, match=code):
        attention.upsert_attention(payload(**changes), db_path=tmp_path / "a.db")


def test_source_deep_link_allowlist_accepts_calendar_and_ecommerce_routes(tmp_path):
    db = tmp_path / "attention.db"
    calendar = attention.upsert_attention(
        payload(
            source_type="calendar",
            source_record_id="event:linked",
            project="linxio",
            item_type="calendar_event",
            source_deep_link="https://www.google.com/calendar/event?eid=opaque",
        ),
        db_path=db,
    )["item"]
    ecommerce = attention.upsert_attention(
        payload(
            source_type="ecommerce",
            source_record_id="pr:linked",
            project="ecommerce",
            source_deep_link="https://github.com/3ndym10n/hermes-agent/pull/88",
        ),
        db_path=db,
    )["item"]

    assert calendar["source_deep_link"].startswith(
        "https://www.google.com/calendar/event?"
    )
    assert ecommerce["source_deep_link"].startswith(
        "https://github.com/3ndym10n/hermes-agent/pull/"
    )


def test_today_order_is_deterministic(tmp_path, monkeypatch):
    db = tmp_path / "attention.db"
    now = datetime(2033, 5, 18, 0, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(attention, "_now", lambda: now)
    due_past = attention._iso(now - 60)
    for record_id, priority, status, due, extra in (
        ("normal-future", "normal", "needs_cal", None, {}),
        ("high-prepared", "high", "prepared", None, {}),
        ("urgent", "urgent", "prepared", None, {}),
        (
            "safety",
            "normal",
            "safety_hold",
            None,
            {"reason_code": "processing_failure"},
        ),
        ("low-overdue", "low", "needs_cal", due_past, {}),
        ("high-needs", "high", "needs_cal", None, {}),
        (
            "calendar-soon",
            "normal",
            "monitoring",
            attention._iso(now + 3600),
            {
                "source_type": "calendar",
                "project": "linxio",
                "item_type": "calendar_event",
            },
        ),
        (
            "normal-due-today",
            "normal",
            "monitoring",
            attention._iso(now + 14_400),
            {},
        ),
    ):
        attention.upsert_attention(
            payload(
                source_record_id=record_id,
                source_event_id=f"{record_id}-event",
                priority=priority,
                status=status,
                due_at=due,
                title=f"Task {record_id}",
                **extra,
            ),
            db_path=db,
            public_url=PUBLIC_URL,
        )
    assert [
        item["source_record_id"]
        for item in attention.list_attention(db_path=db, now=now)
    ] == [
        "urgent",
        "safety",
        "low-overdue",
        "high-needs",
        "calendar-soon",
        "high-prepared",
        "normal-due-today",
    ]


def test_today_includes_calendar_within_two_hours_across_sydney_midnight(
    tmp_path, monkeypatch
):
    db = tmp_path / "attention.db"
    now = datetime(2033, 5, 18, 13, 30, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(attention, "_now", lambda: now)
    attention.upsert_attention(
        payload(
            source_type="calendar",
            source_record_id="calendar-next-day",
            source_event_id="calendar-next-day-v1",
            project="linxio",
            item_type="calendar_event",
            status="monitoring",
            due_at=attention._iso(now + 3600),
        ),
        db_path=db,
    )

    assert [
        item["source_record_id"]
        for item in attention.list_attention(db_path=db, now=now)
    ] == ["calendar-next-day"]


def test_elapsed_high_deferral_keeps_high_needs_you_order(tmp_path, monkeypatch):
    db = tmp_path / "attention.db"
    now = datetime(2033, 5, 18, 0, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(attention, "_now", lambda: now)
    deferred = attention.upsert_attention(
        payload(
            source_record_id="high-deferred",
            source_event_id="high-deferred-v1",
            priority="high",
            status="needs_cal",
        ),
        db_path=db,
    )["item"]
    attention.transition_attention(
        deferred["item_id"],
        "defer",
        expected_row_version=deferred["row_version"],
        deferred_until=attention._iso(now + 60),
        db_path=db,
    )
    attention.upsert_attention(
        payload(
            source_type="calendar",
            source_record_id="calendar-soon",
            source_event_id="calendar-soon-v1",
            project="linxio",
            item_type="calendar_event",
            status="monitoring",
            due_at=attention._iso(now + 3600),
        ),
        db_path=db,
    )

    assert [
        item["source_record_id"]
        for item in attention.list_attention(db_path=db, now=now + 120)
    ] == ["high-deferred", "calendar-soon"]


def test_queue_rejects_a_symlink_database(tmp_path):
    target = tmp_path / "target.db"
    target.touch(mode=0o600)
    link = tmp_path / "attention.db"
    link.symlink_to(target)

    with pytest.raises(attention.AttentionError, match="attention_state_corruption"):
        attention.list_attention(db_path=link)


def test_v1_database_migrates_demonstration_values(tmp_path):
    db = tmp_path / "attention.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        conn.executescript(attention._MIGRATION_1)
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
            (1, 0),
        )
    db.chmod(0o600)

    created = attention.upsert_attention(
        payload(
            item_type="demonstration",
            reason_code="product_verification",
            title="Virgil Mobile test",
        ),
        db_path=db,
    )["item"]

    assert created["item_type"] == "demonstration"
    with sqlite3.connect(db) as conn:
        assert [
            row[0] for row in conn.execute("SELECT version FROM schema_migrations")
        ] == list(range(1, attention.SCHEMA_VERSION + 1))
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v2_migration_preserves_queue_events_and_notifications(tmp_path):
    db = tmp_path / "attention.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        conn.executescript(attention._MIGRATION_1)
        conn.executescript(attention._MIGRATION_2)
        conn.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES(?, 0)",
            [(1,), (2,)],
        )
        conn.execute(
            "INSERT INTO attention_refs(item_id, created_at) VALUES('legacy', 1)"
        )
        conn.execute(
            """
            INSERT INTO attention_items(
                item_id, idempotency_key, source_type, source_record_id,
                source_event_id, project, item_type, priority, status, title,
                safe_summary, recommended_action, waiting_on, reason_code,
                confidence, due_at, deferred_until, source_deep_link,
                prepared_artifact_deep_link, created_at, updated_at, resolved_at,
                dismissed_at, processing_version, row_version
            ) VALUES(
                'legacy', ?, 'gmail', 'thread-legacy', 'message-legacy',
                'linxio', 'customer_email', 'high', 'needs_cal',
                'Review legacy thread', 'A safe legacy summary.',
                'Review the thread.', 'cal', 'reply_needed', 0.9, NULL, NULL,
                NULL, NULL, 1, 1, NULL, NULL, 'legacy-v1', 1
            )
            """,
            (attention._idempotency_key("gmail", "thread-legacy"),),
        )
        conn.execute(
            """
            INSERT INTO attention_events(
                item_id, sequence, event_type, happened_at, safe_description,
                actor, prior_status, new_status, snapshot_version
            ) VALUES(
                'legacy', 1, 'created', 1, 'Attention item created.',
                'gmail_worker', NULL, 'needs_cal', 1
            )
            """
        )
        conn.execute(
            "INSERT INTO attention_notifications("
            "item_id, telegram_message_id, failure_count) VALUES('legacy', '77', 2)"
        )
    db.chmod(0o600)

    statuses = attention.list_source_statuses(db_path=db)

    assert len(statuses) == len(attention.OPERATIONAL_SOURCES)
    assert attention.get_attention("legacy", db_path=db)["expires_at"] is None
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM attention_items").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM attention_events").fetchone()[0] == 1
        assert conn.execute(
            "SELECT telegram_message_id, failure_count "
            "FROM attention_notifications WHERE item_id='legacy'"
        ).fetchone() == ("77", 2)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_source_status_controls_history_and_dynamic_projects(tmp_path):
    db = tmp_path / "attention.db"
    statuses = {
        row["source"]: row for row in attention.list_source_statuses(db_path=db)
    }

    assert statuses["personal"]["status"] == "not_connected"
    assert statuses["personal"]["enabled"] is False
    assert attention.available_projects(db_path=db) == ["all"]

    calendar = attention.update_source_status(
        "calendar",
        "active",
        last_attempted_sync_at="2033-05-18T00:00:00Z",
        last_successful_sync_at="2033-05-18T00:00:00Z",
        next_scheduled_sync_at="2033-05-18T00:05:00Z",
        message="Calendar is connected with no upcoming events.",
        db_path=db,
    )
    assert calendar["status"] == "active"
    assert calendar["failure_count"] == 0
    assert calendar["last_successful_sync_at"] == "2033-05-18T00:00:00Z"
    assert attention.available_projects(db_path=db) == ["all", "linxio"]

    paused = attention.control_source("calendar", "pause", db_path=db)
    assert paused["status"] == "paused"
    assert paused["enabled"] is True and paused["paused"] is True
    disabled = attention.control_source("calendar", "disable", db_path=db)
    assert disabled["enabled"] is False and disabled["paused"] is False
    enabled = attention.control_source("calendar", "enable", db_path=db)
    assert enabled["enabled"] is True and enabled["paused"] is False

    failed = attention.update_source_status(
        "cogitator",
        "failed",
        error_code="bridge_unreachable",
        message="Cogitator bridge is unreachable.",
        db_path=db,
    )
    assert failed["failure_count"] == 1
    assert failed["error_code"] == "bridge_unreachable"
    assert (
        attention.list_source_history("cogitator", db_path=db)[0]["event_type"]
        == "sync_failed"
    )
    source_event = attention.list_activity(project="cogitator", db_path=db)[0]
    assert source_event["item_id"] is None
    assert source_event["event_type"] == "sync_failed"
    assert source_event["project"] == "cogitator"
    assert source_event["title"] == "Cogitator source"
    with pytest.raises(attention.AttentionError, match="unsafe_source_message"):
        attention.update_source_status(
            "github",
            "failed",
            message="Contact cal@example.com",
            db_path=db,
        )

    retained = attention.upsert_attention(
        payload(source_record_id="retained-personal"), db_path=db
    )["item"]
    attention.transition_attention(
        retained["item_id"],
        "resolve",
        expected_row_version=retained["row_version"],
        db_path=db,
    )
    assert attention.available_projects(db_path=db) == [
        "all",
        "linxio",
        "cogitator",
        "personal",
    ]


def test_attention_brief_is_deterministic_and_truthful(tmp_path, monkeypatch):
    db = tmp_path / "attention.db"
    now = datetime(2033, 5, 18, 0, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(attention, "_now", lambda: now)
    attention.update_source_status(
        "system", "healthy", message="System health is normal.", db_path=db
    )
    for values in (
        {
            "source_record_id": "decision",
            "source_event_id": "decision-event",
            "title": "Review customer decision",
            "status": "needs_cal",
            "priority": "high",
            "recommended_action": "Review the customer decision.",
        },
        {
            "source_record_id": "prepared",
            "source_event_id": "prepared-event",
            "title": "Review prepared packet",
            "status": "prepared",
            "priority": "high",
        },
        {
            "source_type": "calendar",
            "source_record_id": "event:next",
            "source_event_id": "calendar-event",
            "project": "linxio",
            "item_type": "calendar_event",
            "title": "Planning meeting",
            "status": "monitoring",
            "priority": "normal",
            "waiting_on": "none",
            "due_at": attention._iso(now + 3600),
        },
        {
            "source_type": "agent_job",
            "source_record_id": "agent:active",
            "source_event_id": "agent-event",
            "project": "system",
            "item_type": "agent_job",
            "title": "Implementation agent",
            "status": "monitoring",
            "priority": "normal",
            "waiting_on": "virgil",
        },
    ):
        attention.upsert_attention(payload(**values), db_path=db, public_url=PUBLIC_URL)

    brief = attention.attention_brief(now=now, db_path=db)

    assert brief == {
        "summary": (
            "1 item needs you. 1 prepared item is ready. "
            "Review customer decision is highest priority. "
            "Your next meeting is at 11:00 am. 1 agent job is active. "
            "System health is normal."
        ),
        "needs_you_count": 1,
        "prepared_count": 1,
        "next_calendar_event": {
            "item_id": brief["next_calendar_event"]["item_id"],
            "title": "Planning meeting",
            "start_at": attention._iso(now + 3600),
        },
        "active_agent_jobs": 1,
        "system_health": "normal",
        "recommended_action": "Review the customer decision.",
    }


def test_missing_source_reconciliation_isolated_by_record_prefix(tmp_path):
    db = tmp_path / "attention.db"
    for record_id in ("event:one", "conflict:one"):
        attention.upsert_attention(
            payload(
                source_type="calendar",
                source_record_id=record_id,
                source_event_id=f"{record_id}:v1",
                project="linxio",
                item_type="calendar_event",
                status="monitoring",
                waiting_on="none",
            ),
            db_path=db,
        )

    resolved = attention.resolve_missing_source_records(
        "calendar",
        [],
        "snapshot:events:2",
        record_prefix="event:",
        db_path=db,
    )

    assert [result["item"]["source_record_id"] for result in resolved] == ["event:one"]
    records = {
        item["source_record_id"]: item
        for item in attention.list_source_records("calendar", db_path=db)
    }
    assert records["event:one"]["status"] == "resolved"
    assert records["conflict:one"]["status"] == "monitoring"
    with pytest.raises(attention.AttentionError, match="invalid_record_prefix"):
        attention.resolve_missing_source_records(
            "calendar", [], "snapshot:bad", record_prefix=":", db_path=db
        )


def test_gmail_shadow_adapter_labels_without_claiming_a_draft(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    result = upsert_gmail_outcome(
        thread_id="thread1",
        message_id="message1",
        kind="shadowed",
        subject="Product question",
        sender_name="Daniel",
        company="Example",
        received_time="2026-07-28 09:00:00 AEST",
        category="product_question",
        reason="reply_needed",
        confidence=0.91,
        processing_version="test-v1",
    )

    assert result == "queued"
    item = attention.list_attention(view="prepared")[0]
    assert item["title"].startswith("SHADOW ONLY — NO GMAIL DRAFT CREATED")
    assert item["prepared_artifact_deep_link"] is None


def test_sent_history_event_resolves_without_calling_gmail(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    state = tmp_path / "gmail-state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(autodraft, "_state_dir", lambda: state)

    initial = attention.upsert_attention(
        payload(
            source_type="gmail",
            source_record_id="thread1",
            source_event_id="inbound1",
            project="linxio",
            item_type="customer_email",
            source_deep_link="https://mail.google.com/mail/u/0/#inbox/thread1",
        ),
        public_url=PUBLIC_URL,
    )
    conn = autodraft._open_state()
    try:
        assert autodraft._process_event(
            conn,
            object(),
            {
                "message_id": "sent1",
                "thread_id": "thread1",
                "history_id": "10",
                "kind": "sent",
            },
            autodraft.ACCOUNT_FINGERPRINT,
        )
    finally:
        conn.close()
    assert attention.get_attention(initial["item"]["item_id"])["status"] == "resolved"


def test_history_collection_includes_inbox_and_sent_without_label_filter():
    captured = {}
    response = {
        "historyId": "11",
        "history": [
            {
                "id": "10",
                "messagesAdded": [
                    {
                        "message": {
                            "id": "inbound1",
                            "threadId": "thread1",
                            "labelIds": ["INBOX"],
                        }
                    },
                    {
                        "message": {
                            "id": "sent1",
                            "threadId": "thread1",
                            "labelIds": ["SENT"],
                        }
                    },
                ],
            }
        ],
    }

    class Request:
        def execute(self, **_kwargs):
            return response

    def list_history(**kwargs):
        captured.update(kwargs)
        return Request()

    service = SimpleNamespace(
        users=lambda: SimpleNamespace(
            history=lambda: SimpleNamespace(list=list_history)
        )
    )
    events, watermark = autodraft.collect_history_events(service, "9")

    assert watermark == "11"
    assert "labelId" not in captured
    assert {event["kind"] for event in events} == {"inbox", "sent"}


def test_private_api_requires_identity_origin_csrf_and_fresh_version(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("virgil_mobile_server._gmail_watcher_state", lambda: "active")
    db = tmp_path / "attention.db"
    item = attention.upsert_attention(payload(), db_path=db, public_url=PUBLIC_URL)[
        "item"
    ]
    app = create_app(
        "cal@example.com",
        PUBLIC_URL,
        db_path=db,
        trusted_proxy_hosts=frozenset({"testclient"}),
    )
    client = TestClient(app)
    base_headers = {
        "host": "virgil.example.ts.net:8443",
        "tailscale-user-login": "cal@example.com",
    }
    assert client.get("/healthz", headers={"host": "127.0.0.1:8788"}).status_code == 200

    denied = client.get("/api/session", headers={"host": base_headers["host"]})
    assert denied.status_code == 401
    assert "frame-ancestors 'none'" in denied.headers["content-security-policy"]

    session = client.get("/api/session", headers=base_headers)
    assert session.status_code == 200
    assert session.headers["cache-control"] == "no-store"
    session_data = session.json()
    csrf = session_data["csrf_token"]
    assert session_data["connection"]["virgil"] == "connected"
    assert session_data["connection"]["gmail_watcher"] == "active"
    assert session_data["connection"]["last_successful_queue_ingestion_at"]

    bad_origin = client.post(
        f"/api/items/{item['item_id']}/action",
        headers={
            **base_headers,
            "x-csrf-token": csrf,
            "origin": "https://evil.example",
        },
        json={"action": "resolve", "expected_row_version": 1},
    )
    assert bad_origin.status_code == 403

    resolved = client.post(
        f"/api/items/{item['item_id']}/action",
        headers={
            **base_headers,
            "x-csrf-token": csrf,
            "origin": PUBLIC_URL,
        },
        json={"action": "resolve", "expected_row_version": 1},
    )
    assert resolved.status_code == 200
    assert resolved.json()["item"]["status"] == "resolved"

    stale = client.post(
        f"/api/items/{item['item_id']}/action",
        headers={
            **base_headers,
            "x-csrf-token": csrf,
            "origin": PUBLIC_URL,
        },
        json={"action": "dismiss", "expected_row_version": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["error"] == "stale_attention_item"


def test_service_worker_caches_shell_only():
    worker = (ROOT / "virgil_mobile" / "sw.js").read_text()
    assert 'const SHELL = "virgil-shell-v4"' in worker
    assets = worker.split("const ASSETS =", 1)[1].split(";", 1)[0]
    assert "/api/" not in assets
    assert "/item/" not in assets
    assert 'url.pathname.startsWith("/api/")' in worker
    assert 'url.pathname.startsWith("/item/")' in worker
    assert (
        "Virgil is offline. Live operational items are unavailable."
        in (ROOT / "virgil_mobile" / "index.html").read_text()
    )
    shell = (ROOT / "virgil_mobile" / "index.html").read_text()
    client = (ROOT / "virgil_mobile" / "app.js").read_text()
    assert 'data-view="needs-you"' in shell
    assert '"needs-you":' in client
    for message in (
        "You’re clear for now. Connected sources will add important work here automatically.",
        "No decisions are currently waiting on you.",
        "Virgil has no prepared work waiting for review.",
        "No recent operational changes.",
        "No trustworthy ecommerce runtime feed is currently available.",
        "Personal is not connected.",
    ):
        assert message in client
    assert "Gmail watcher" in client
    assert "Virgil could not load live items" in client
    assert '"stale", "deferred"].includes(item.status)' in client
    assert "state.sources" in client
    assert "event.title" in client
    assert "event.project" in client

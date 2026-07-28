from __future__ import annotations

import importlib.util
import os
import sqlite3
import stat
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
        ({"status": "whatever"}, "invalid_status"),
    ],
)
def test_queue_contract_fails_closed(tmp_path, changes, code):
    with pytest.raises(attention.AttentionError, match=code):
        attention.upsert_attention(payload(**changes), db_path=tmp_path / "a.db")


def test_today_order_is_deterministic(tmp_path, monkeypatch):
    db = tmp_path / "attention.db"
    now = 2_000_000_000.0
    monkeypatch.setattr(attention, "_now", lambda: now)
    due_past = attention._iso(now - 60)
    for record_id, priority, status, due in (
        ("normal-future", "normal", "needs_cal", None),
        ("high-future", "high", "prepared", None),
        ("urgent", "urgent", "prepared", None),
        ("low-overdue", "low", "needs_cal", due_past),
    ):
        attention.upsert_attention(
            payload(
                source_record_id=record_id,
                source_event_id=f"{record_id}-event",
                priority=priority,
                status=status,
                due_at=due,
                title=f"Task {record_id}",
            ),
            db_path=db,
            public_url=PUBLIC_URL,
        )
    assert [
        item["source_record_id"]
        for item in attention.list_attention(db_path=db, now=now)
    ] == ["urgent", "high-future", "low-overdue", "normal-future"]


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
        ] == [1, 2]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


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
    assert 'const SHELL = "virgil-shell-v2"' in worker
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
        "You’re clear for now. New Gmail and Virgil events will appear here automatically.",
        "No decisions are waiting on you.",
        "Virgil has no prepared work waiting for review.",
        "No operational activity has been recorded yet.",
    ):
        assert message in client
    assert "Gmail watcher" in client
    assert "Virgil could not load live items" in client

from __future__ import annotations

import base64
import importlib.util
import json
import stat
from contextlib import nullcontext
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT
    / "skills"
    / "productivity"
    / "google-workspace"
    / "scripts"
    / "incoming_autodraft.py"
)
SPEC = importlib.util.spec_from_file_location("incoming_autodraft", SCRIPT)
assert SPEC and SPEC.loader
autodraft = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(autodraft)


@pytest.fixture
def private_state(tmp_path, monkeypatch):
    root = tmp_path / "incoming-autodraft"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(autodraft, "_state_dir", lambda: root)
    monkeypatch.setattr(autodraft, "_worker_lock", nullcontext)
    monkeypatch.setattr(
        autodraft,
        "_systemd_state",
        lambda: {"is_active": "inactive", "is_enabled": "disabled"},
    )
    return root


def message(
    *,
    message_id="m1",
    thread_id="t1",
    sender="Customer <person@example.com>",
    subject="Question",
    body="Could you help?",
    labels=None,
    extra_headers=None,
    internal_date="1",
):
    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": autodraft.EXPECTED_ACCOUNT},
        {"name": "Subject", "value": subject},
        {"name": "Message-ID", "value": f"<{message_id}@example.com>"},
    ]
    headers.extend(
        {"name": key, "value": value}
        for key, value in (extra_headers or {}).items()
    )
    return {
        "id": message_id,
        "threadId": thread_id,
        "internalDate": internal_date,
        "labelIds": labels or ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {
                "data": base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
            },
        },
    }


def entry(**changes):
    value = {
        "id": "m1",
        "thread_id": "t1",
        "name": "Customer",
        "address": "person@example.com",
        "subject": "Question",
        "message_id": "<m1@example.com>",
        "references": "",
        "labels": {"INBOX"},
        "text": "Could you help?",
        "internal_date": 1,
    }
    value.update(changes)
    return value


def approve_policy(conn):
    autodraft._set_meta(conn, "policy_fingerprint", autodraft.POLICY_FINGERPRINT)
    autodraft._set_meta(
        conn, "policy_account_fingerprint", autodraft.ACCOUNT_FINGERPRINT
    )
    autodraft._set_meta(conn, "policy_approver", "Cal")
    autodraft._set_meta(conn, "policy_approved_at", "now")
    conn.commit()


def test_first_run_sets_current_watermark_without_replay(private_state, monkeypatch):
    monkeypatch.setattr(
        autodraft,
        "_profile",
        lambda service: {
            "history_id": "900",
            "account_fingerprint": autodraft.ACCOUNT_FINGERPRINT,
        },
    )
    monkeypatch.setattr(
        autodraft,
        "collect_history_events",
        lambda *_: pytest.fail("first run must not read history"),
    )

    result = autodraft.run_once(service=object())

    assert result == {
        "status": "baseline_initialized",
        "mode": "disabled",
        "historical_messages_processed": 0,
    }
    conn = autodraft._open_state()
    assert autodraft._meta(conn, "history_watermark") == "900"
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    conn.close()


def test_wrong_account_fails_closed_before_history(private_state, monkeypatch):
    alerts = []
    monkeypatch.setattr(
        autodraft,
        "_profile",
        lambda service: (_ for _ in ()).throw(
            autodraft.AutodraftError("wrong_account")
        ),
    )
    monkeypatch.setattr(
        autodraft, "_alert", lambda conn, code, **kw: alerts.append(code)
    )
    monkeypatch.setattr(
        autodraft,
        "collect_history_events",
        lambda *_: pytest.fail("wrong account must not read history"),
    )

    with pytest.raises(autodraft.AutodraftError, match="wrong_account"):
        autodraft.run_once(service=object())
    assert alerts == ["wrong_account"]


def test_deterministic_exclusions_use_multiple_signals():
    assert autodraft._metadata_exclusion(
        message(sender="Cal <caleb.bacon@linxio.com>")
    ) == "internal"
    assert autodraft._metadata_exclusion(
        message(labels=["INBOX", "CATEGORY_PROMOTIONS"])
    ) == "bulk"
    automated = message(
        sender="no-reply@example.com",
        subject="Delivery notification",
        extra_headers={"Auto-Submitted": "auto-generated"},
    )
    assert autodraft._metadata_exclusion(automated) == "receipt_or_delivery"
    auto_header_alone = message(extra_headers={"Auto-Submitted": "auto-generated"})
    assert autodraft._metadata_exclusion(auto_header_alone) == ""
    assert autodraft._company("person@gmail.com") == "company unknown"


def test_thread_reader_bounds_mime_and_ignores_attachments():
    source = message(body="New question\n\n> old quote\n-- \nSignature")
    source["payload"] = {
        "mimeType": "multipart/mixed",
        "headers": source["payload"]["headers"],
        "parts": [
            {
                "mimeType": "text/plain",
                "filename": "",
                "headers": [],
                "body": {
                    "data": base64.urlsafe_b64encode(
                        b"New question\n\n> old quote\n-- \nSignature"
                    )
                    .decode()
                    .rstrip("=")
                },
            },
            {
                "mimeType": "text/plain",
                "filename": "secret.txt",
                "headers": [
                    {"name": "Content-Disposition", "value": "attachment"}
                ],
                "body": {
                    "data": base64.urlsafe_b64encode(b"attachment secret")
                    .decode()
                    .rstrip("=")
                },
            },
        ],
    }

    entries = autodraft._thread_entries({"messages": [source]})

    assert entries[0]["text"] == "New question"
    assert "attachment secret" not in entries[0]["text"]


def valid_classification():
    return {
        "decision": "draft_reply",
        "category": "information_request",
        "confidence": 0.95,
        "reason_code": "reply_needed",
        "questions_detected": 1,
        "requires_commercial_fact": False,
        "requires_human_exception": False,
        "missing_fact_categories": [],
        "draft_body": "",
    }


def test_classifier_requires_exact_closed_schema(monkeypatch):
    malformed = valid_classification()
    malformed["extra"] = "command"
    monkeypatch.setattr(autodraft, "_llm_json", lambda *a, **k: malformed)
    with pytest.raises(autodraft.AutodraftError, match="malformed_model_output"):
        autodraft.classify_reply([entry()])

    low = valid_classification()
    low["confidence"] = 0.4
    monkeypatch.setattr(autodraft, "_llm_json", lambda *a, **k: low)
    result = autodraft.classify_reply([entry()])
    assert result["decision"] == "decision_required"
    assert result["reason_code"] == "low_confidence"


def draft_output(body="Thanks for your enquiry."):
    return {
        "decision": "draft_reply",
        "category": "information_request",
        "confidence": 0.96,
        "subject": "Re: Question",
        "body": body,
        "supporting_customer_fact_references": ["c1"],
        "supporting_approved_fact_references": [],
        "applied_writing_guidance_references": [],
        "missing_facts": [],
        "risk_flags": [],
    }


def test_draft_validation_rejects_unsupported_commercial_claim(monkeypatch):
    monkeypatch.setattr(
        autodraft, "_llm_json", lambda *a, **k: draft_output("The price is $99.")
    )
    with pytest.raises(autodraft.AutodraftError, match="unsupported_claim"):
        autodraft.generate_draft(
            [entry()], valid_classification(), approved_facts=[], guidance=[]
        )


class Request:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def execute(self, **_kwargs):
        if self.error:
            raise self.error
        return self.result


class DraftResource:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.created = None

    def create(self, **kwargs):
        self.created = kwargs
        return Request(self.result, self.error)


class Users:
    def __init__(self, drafts):
        self._drafts = drafts

    def drafts(self):
        return self._drafts


class Service:
    def __init__(self, drafts):
        self._users = Users(drafts)

    def users(self):
        return self._users


def test_reply_mime_is_one_recipient_threaded_and_draft_only():
    drafts = DraftResource({"id": "d1", "message": {"threadId": "t1"}})
    target = entry()

    draft_id, reconciled = autodraft._create_reply_draft(
        Service(drafts), target, "t1", "Re: Question", "Thanks."
    )

    payload = drafts.created["body"]["message"]
    parsed = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(payload["raw"])
    )
    assert draft_id == "d1" and reconciled is False
    assert payload["threadId"] == "t1"
    assert parsed["To"] == "person@example.com"
    assert parsed["In-Reply-To"] == "<m1@example.com>"
    assert parsed["References"] == "<m1@example.com>"
    assert parsed["Cc"] is None and parsed["Bcc"] is None
    assert list(parsed.iter_attachments()) == []
    assert "messages().send" not in SCRIPT.read_text()
    assert "drafts().send" not in SCRIPT.read_text()


def test_uncertain_create_reconciles_instead_of_retrying(monkeypatch):
    drafts = DraftResource(error=TimeoutError("unknown outcome"))
    monkeypatch.setattr(
        autodraft, "_thread_drafts", lambda service, thread_id: [{"id": "d1"}]
    )
    assert autodraft._create_reply_draft(
        Service(drafts), entry(), "t1", "Re: Question", "Thanks."
    ) == ("d1", True)


def test_policy_material_change_requires_fresh_approval(private_state, monkeypatch):
    monkeypatch.setattr(
        autodraft,
        "_profile",
        lambda service: {
            "history_id": "1",
            "account_fingerprint": autodraft.ACCOUNT_FINGERPRINT,
        },
    )
    preview = autodraft.policy_preview(service=object())
    approved = autodraft.policy_approve(
        preview["approval_token"], "Cal", service=object()
    )
    assert approved["status"] == "approved"
    assert autodraft.set_mode("draft")["mode"] == "draft"

    monkeypatch.setattr(autodraft, "POLICY_FINGERPRINT", "changed")
    with pytest.raises(autodraft.AutodraftError, match="policy_not_approved"):
        autodraft.set_mode("draft")


def test_reservation_is_atomic_and_rate_limited(private_state):
    conn = autodraft._open_state()
    approve_policy(conn)
    autodraft._set_meta(conn, "mode", "draft")
    now = autodraft._now()
    for index in range(5):
        conn.execute(
            "INSERT INTO messages(message_id,thread_id,state,created_at,updated_at) "
            "VALUES(?,?,'drafted',?,?)",
            (f"old{index}", f"t{index}", now, now),
        )
    conn.execute(
        "INSERT INTO messages(message_id,thread_id,state,created_at,updated_at) "
        "VALUES('new','tn','processing',?,?)",
        (now, now),
    )
    conn.commit()
    assert autodraft._reserve(conn, "new") == "hourly"
    assert (
        conn.execute("SELECT state FROM messages WHERE message_id='new'").fetchone()[0]
        == "processing"
    )
    conn.close()


def test_shadow_pipeline_generates_but_never_creates_draft(
    private_state, monkeypatch
):
    conn = autodraft._open_state()
    autodraft._set_meta(conn, "mode", "shadow")
    conn.commit()
    metadata = message()
    thread = {"messages": [metadata]}
    entries = [entry()]
    monkeypatch.setattr(autodraft, "_metadata", lambda *a: metadata)
    monkeypatch.setattr(autodraft, "_metadata_exclusion", lambda value: "")
    monkeypatch.setattr(autodraft, "_thread", lambda *a: thread)
    monkeypatch.setattr(autodraft, "_thread_entries", lambda value: entries)
    monkeypatch.setattr(autodraft, "_thread_drafts", lambda *a: [])
    monkeypatch.setattr(autodraft, "classify_reply", lambda value: valid_classification())
    monkeypatch.setattr(autodraft, "_approved_facts", lambda category: [])
    monkeypatch.setattr(autodraft, "_writing_guidance", lambda category: [])
    monkeypatch.setattr(
        autodraft,
        "generate_draft",
        lambda *a: draft_output(),
    )
    monkeypatch.setattr(autodraft, "_notify", lambda *a, **k: True)
    monkeypatch.setattr(
        autodraft,
        "_create_reply_draft",
        lambda *a: pytest.fail("shadow mode cannot create a Gmail draft"),
    )

    complete = autodraft._process_event(
        conn,
        object(),
        {"message_id": "m1", "thread_id": "t1", "history_id": "8"},
        autodraft.ACCOUNT_FINGERPRINT,
    )

    row = conn.execute(
        "SELECT state,draft_id,response_fingerprint FROM messages"
    ).fetchone()
    assert complete is True
    assert row["state"] == "shadowed"
    assert row["draft_id"] == ""
    assert row["response_fingerprint"] == autodraft._sha("Thanks for your enquiry.")
    conn.close()


def test_existing_draft_stops_before_model(private_state, monkeypatch):
    conn = autodraft._open_state()
    autodraft._set_meta(conn, "mode", "shadow")
    conn.commit()
    metadata = message()
    monkeypatch.setattr(autodraft, "_metadata", lambda *a: metadata)
    monkeypatch.setattr(autodraft, "_metadata_exclusion", lambda value: "")
    monkeypatch.setattr(autodraft, "_thread", lambda *a: {"messages": [metadata]})
    monkeypatch.setattr(autodraft, "_thread_entries", lambda value: [entry()])
    monkeypatch.setattr(autodraft, "_thread_drafts", lambda *a: [{"id": "d1"}])
    monkeypatch.setattr(
        autodraft,
        "classify_reply",
        lambda value: pytest.fail("existing draft must stop before the model"),
    )
    assert autodraft._process_event(
        conn,
        object(),
        {"message_id": "m1", "thread_id": "t1", "history_id": "8"},
        autodraft.ACCOUNT_FINGERPRINT,
    )
    row = conn.execute("SELECT state,reason_code FROM messages").fetchone()
    assert tuple(row) == ("ignored", "existing_draft")
    conn.close()


def test_reserved_crash_reconciles_existing_draft(private_state, monkeypatch):
    conn = autodraft._open_state()
    now = autodraft._now()
    conn.execute(
        "INSERT INTO messages("
        "message_id,thread_id,history_id,state,category,confidence_bucket,"
        "response_fingerprint,created_at,updated_at"
        ") VALUES('m1','t1','8','reserved','information_request','high',?,?,?)",
        (autodraft._sha("draft"), now, now),
    )
    conn.commit()
    metadata = message()
    monkeypatch.setattr(autodraft, "_metadata", lambda *a: metadata)
    monkeypatch.setattr(
        autodraft, "_thread_drafts", lambda *a: [{"id": "recovered-draft"}]
    )
    monkeypatch.setattr(autodraft, "_notify", lambda *a, **k: True)
    monkeypatch.setattr(
        autodraft,
        "_thread",
        lambda *a: pytest.fail("recovery must stop after finding the draft"),
    )
    monkeypatch.setattr(
        autodraft,
        "_create_reply_draft",
        lambda *a: pytest.fail("recovery must not create a second draft"),
    )

    assert autodraft._process_event(
        conn,
        object(),
        {"message_id": "m1", "thread_id": "t1", "history_id": "8"},
        autodraft.ACCOUNT_FINGERPRINT,
    )

    row = conn.execute(
        "SELECT state,draft_id,notification_state FROM messages"
    ).fetchone()
    assert tuple(row) == ("drafted", "recovered-draft", "sent")
    assert int(autodraft._meta(conn, "uncertain_creates_reconciled")) == 1
    conn.close()


def test_state_never_contains_raw_mail_or_draft(private_state):
    conn = autodraft._open_state()
    event = {"message_id": "m1", "thread_id": "t1", "history_id": "5"}
    assert autodraft._start_message(conn, event, "t1")[0]
    autodraft._finish(
        conn,
        "m1",
        state="shadowed",
        category="information_request",
        reason="reply_needed",
        confidence=0.95,
        fingerprint=autodraft._sha("SECRET DRAFT BODY"),
    )
    conn.close()
    raw = (private_state / "state.db").read_bytes()
    assert b"SECRET DRAFT BODY" not in raw
    assert b"Customer subject" not in raw
    assert b"person@example.com" not in raw
    assert stat.S_IMODE((private_state / "state.db").stat().st_mode) == 0o600


def test_natural_language_routing_is_execution_first():
    from gateway.cogitator_intake_bridge import parse_intelligent_intake

    watch = "Watch my Linxio inbox and prepare reply drafts"
    playbook = "Save this autodraft policy as a playbook"
    run_and_remember = "Run this policy and remember it"
    assert autodraft.route_control(watch) == {
        "route": "autodraft",
        "action": "policy_preview",
    }
    assert autodraft.route_control(playbook)["route"] == "intake"
    routed = autodraft.route_control(run_and_remember)
    assert routed["route"] == "autodraft_then_intake"
    assert routed["execution_required_first"] is True
    assert all(
        parse_intelligent_intake(text) is None
        for text in (watch, playbook, run_and_remember)
    )


def test_cross_customer_thread_is_a_closed_stop():
    first = entry(
        id="m0",
        address="first@example.com",
        internal_date=0,
        text="Earlier customer data",
    )
    assert (
        autodraft._target_state([first, entry()], "m1")
        == "cross_customer_risk"
    )


def test_read_state_change_does_not_change_thread_fingerprint():
    first = {
        "messages": [
            {"id": "m1", "internalDate": "1", "labelIds": ["INBOX", "UNREAD"]}
        ]
    }
    second = {
        "messages": [{"id": "m1", "internalDate": "1", "labelIds": ["INBOX"]}]
    }
    assert autodraft._thread_fingerprint(first) == autodraft._thread_fingerprint(
        second
    )


def test_wrong_account_latches_until_explicit_reverification(
    private_state, monkeypatch
):
    conn = autodraft._open_state()
    autodraft._set_meta(conn, "mode", "shadow")
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        autodraft,
        "_profile",
        lambda service: (_ for _ in ()).throw(
            autodraft.AutodraftError("wrong_account")
        ),
    )
    monkeypatch.setattr(autodraft, "_alert", lambda *a, **k: None)
    with pytest.raises(autodraft.AutodraftError, match="wrong_account"):
        autodraft.run_once(service=object())
    conn = autodraft._open_state()
    assert autodraft._mode(conn) == "disabled"
    assert autodraft._meta(conn, "account_intervention_required") == "1"
    conn.close()

    monkeypatch.setattr(
        autodraft,
        "_profile",
        lambda service: {
            "history_id": "99",
            "account_fingerprint": autodraft.ACCOUNT_FINGERPRINT,
        },
    )
    with pytest.raises(
        autodraft.AutodraftError, match="operator_intervention_required"
    ):
        autodraft.run_once(service=object())
    result = autodraft.account_reverify(
        confirm="REVERIFY-CAL-LINXIO-GMAIL", service=object()
    )
    assert result["mode"] == "disabled"
    conn = autodraft._open_state()
    assert autodraft._meta(conn, "account_intervention_required") == ""
    conn.close()


def test_final_reread_aborts_when_new_external_message_arrives(
    private_state, monkeypatch
):
    conn = autodraft._open_state()
    approve_policy(conn)
    autodraft._set_meta(conn, "mode", "draft")
    conn.commit()
    metadata = message()
    base_thread = {"messages": [metadata]}
    changed_thread = {
        "messages": [
            metadata,
            {
                "id": "m2",
                "internalDate": "2",
                "labelIds": ["INBOX"],
            },
        ]
    }
    threads = iter([base_thread, base_thread, changed_thread])
    monkeypatch.setattr(autodraft, "_metadata", lambda *a: metadata)
    monkeypatch.setattr(autodraft, "_metadata_exclusion", lambda value: "")
    monkeypatch.setattr(autodraft, "_thread", lambda *a: next(threads))
    monkeypatch.setattr(
        autodraft,
        "_thread_entries",
        lambda value: (
            [entry()]
            if len(value["messages"]) == 1
            else [
                entry(),
                entry(
                    id="m2",
                    text="One more question",
                    internal_date=2,
                ),
            ]
        ),
    )
    monkeypatch.setattr(autodraft, "_thread_drafts", lambda *a: [])
    monkeypatch.setattr(
        autodraft, "classify_reply", lambda value: valid_classification()
    )
    monkeypatch.setattr(autodraft, "_approved_facts", lambda category: [])
    monkeypatch.setattr(autodraft, "_writing_guidance", lambda category: [])
    monkeypatch.setattr(autodraft, "generate_draft", lambda *a: draft_output())
    monkeypatch.setattr(
        autodraft,
        "_profile",
        lambda service: {
            "history_id": "99",
            "account_fingerprint": autodraft.ACCOUNT_FINGERPRINT,
        },
    )
    monkeypatch.setattr(autodraft, "_notify", lambda *a, **k: True)
    monkeypatch.setattr(autodraft, "_alert", lambda *a, **k: None)
    monkeypatch.setattr(
        autodraft,
        "_create_reply_draft",
        lambda *a: pytest.fail("changed thread cannot create a draft"),
    )

    assert autodraft._process_event(
        conn,
        object(),
        {"message_id": "m1", "thread_id": "t1", "history_id": "8"},
        autodraft.ACCOUNT_FINGERPRINT,
    )
    row = conn.execute("SELECT state,reason_code FROM messages").fetchone()
    assert tuple(row) == ("decision_required", "newer_external_message")
    conn.close()


def test_disabled_mode_refreshes_baseline_without_listing_mail(
    private_state, monkeypatch
):
    conn = autodraft._open_state()
    autodraft._set_meta(conn, "history_watermark", "100")
    autodraft._set_meta(conn, "mode", "disabled")
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        autodraft,
        "_profile",
        lambda service: {
            "history_id": "200",
            "account_fingerprint": autodraft.ACCOUNT_FINGERPRINT,
        },
    )
    monkeypatch.setattr(
        autodraft,
        "collect_history_events",
        lambda *a: pytest.fail("disabled mode must not list message history"),
    )

    assert autodraft.run_once(service=object()) == {
        "status": "disabled",
        "mode": "disabled",
    }
    conn = autodraft._open_state()
    assert autodraft._meta(conn, "history_watermark") == "200"
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    conn.close()

def test_customer_question_is_not_an_approved_business_fact(monkeypatch):
    customer_entry = entry(text="Is the product available?")
    unsupported = draft_output("The product is available.")
    monkeypatch.setattr(
        autodraft, "_llm_json", lambda *args, **kwargs: unsupported
    )

    with pytest.raises(
        autodraft.AutodraftError, match="unsupported_claim"
    ):
        autodraft.generate_draft(
            [customer_entry], valid_classification(), [], []
        )

    grounded = draft_output("The product is available.")
    grounded["supporting_approved_fact_references"] = ["a1"]
    monkeypatch.setattr(
        autodraft, "_llm_json", lambda *args, **kwargs: grounded
    )
    result = autodraft.generate_draft(
        [customer_entry],
        valid_classification(),
        [{"ref": "a1", "text": "The product is available."}],
        [],
    )
    assert result["decision"] == "draft_reply"

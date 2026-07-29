"""Synthetic tests for the bounded [LINXIO FACT] ingestion channel.

No real mailbox, no network. A fake Gmail service raises on every mutating call,
so any attempt to draft, send, modify, trash or label fails the test rather than
being asserted about after the fact.
"""

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "skills/productivity/google-workspace/scripts")
)

import linxio_fact_intake as intake  # noqa: E402

ACCOUNT = intake.EXPECTED_ACCOUNT


@pytest.fixture
def private_state(tmp_path, monkeypatch):
    root = tmp_path / "linxio-fact-intake"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(intake, "_state_dir", lambda: root)
    return root


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def message(
    *,
    message_id="m1",
    subject="[LINXIO FACT] pricing + plans",
    to=ACCOUNT,
    body="Plan A includes 1 tracker and 1 SIM.",
    sender=ACCOUNT,
    attachments=(),
):
    parts = [{"mimeType": "text/plain", "filename": "", "body": {"data": b64(body)}}]
    for index, (filename, mime, size, data) in enumerate(attachments, 1):
        part = {
            "mimeType": mime,
            "filename": filename,
            "body": {"attachmentId": f"att{index}", "size": size},
        }
        parts.append(part)
    return {
        "id": message_id,
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "To", "value": to},
                {"name": "From", "value": sender},
                {"name": "Date", "value": "Wed, 29 Jul 2026 21:00:00 +1000"},
            ],
            "parts": parts,
        },
        "_attachment_data": {
            f"att{i}": data for i, (_, _, _, data) in enumerate(attachments, 1)
        },
    }


class FakeGmail:
    """Read-only by construction: every mutating verb fails the test."""

    def __init__(self, messages):
        self._messages = {m["id"]: m for m in messages}
        self.calls = []

    # -- read paths ---------------------------------------------------------
    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return _Request(
            {"messages": [{"id": key} for key in self._messages]}
        )

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return _Request(self._messages[kwargs["id"]])

    def attachments(self):
        return _Attachments(self._messages, self.calls)

    # -- every mutation is a test failure -----------------------------------
    def _forbidden(self, name):
        def call(*args, **kwargs):
            pytest.fail(f"the fact channel must never call Gmail {name}")

        return call

    def __getattr__(self, name):
        if name in {"modify", "trash", "untrash", "delete", "send", "drafts", "batchModify"}:
            return self._forbidden(name)
        raise AttributeError(name)


class _Attachments:
    def __init__(self, messages, calls):
        self._messages = messages
        self._calls = calls

    def get(self, **kwargs):
        self._calls.append(("attachment", kwargs))
        data = self._messages[kwargs["messageId"]]["_attachment_data"][kwargs["id"]]
        return _Request({"data": b64(data)})


class _Request:
    def __init__(self, payload):
        self._payload = payload

    def execute(self, **kwargs):
        return self._payload


def run(service, monkeypatch, *, facts=None, extract=True):
    monkeypatch.setattr(intake, "_load_runtime_env", lambda: None)
    if facts is not None:
        monkeypatch.setattr(intake, "propose_facts", lambda source: list(facts))
    return intake.scan(service=service, extract=extract)


def test_only_marked_messages_addressed_to_the_account_are_read(
    private_state, monkeypatch
):
    service = FakeGmail(
        [
            message(message_id="m1"),
            # Right marker, wrong recipient: an ordinary customer thread.
            message(message_id="m2", to="customer@example.com"),
            # Marker not at the start: not the agreed channel.
            message(message_id="m3", subject="Re: [LINXIO FACT] pricing"),
            # No marker at all.
            message(message_id="m4", subject="Quick question about pricing"),
            # Near miss on the marker itself.
            message(message_id="m5", subject="[LINXIO FACTS] pricing"),
            # Correct marker and recipient, but sent by someone else. The marker
            # is not a secret, so this must not be able to inject facts.
            message(message_id="m6", sender="attacker@example.com"),
        ]
    )

    report = run(service, monkeypatch, facts=[])

    assert report["messages_matched"] == 1
    assert report["messages_rejected"] == 5
    assert report["gmail_mutations"] == 0
    # The wider mailbox is never searched: the query is scoped and bounded.
    query = next(kwargs for name, kwargs in service.calls if name == "list")
    assert query["q"] == intake.GMAIL_QUERY
    assert f"to:{ACCOUNT}" in query["q"]
    assert intake.SUBJECT_MARKER in query["q"]
    assert query["maxResults"] == intake.MAX_MESSAGES


def test_proposed_facts_carry_every_required_field_and_are_never_approved(
    private_state, monkeypatch
):
    proposed = [
        {
            "wording": "Plan A includes one tracker and one SIM.",
            "fact_category": "plan_inclusions",
            "scope": "linxio",
            "source_reference": "Plan comparison table",
            "effective_date": "2026-07-01",
            "provenance": "Supplied price list, section 2",
            "risk_if_wrong": "high",
            "conflict_result": "",
            "approval_status": "proposed",
        }
    ]
    service = FakeGmail([message()])

    report = run(service, monkeypatch, facts=proposed)
    packet = intake.review_packet()

    assert report["facts_proposed"] == 1
    fact = packet["proposed_facts"][0]
    for field in (
        "wording", "fact_category", "scope", "source_reference",
        "effective_date", "provenance", "risk_if_wrong", "conflict_result",
        "approval_status",
    ):
        assert field in fact
    # Nothing is approved or promoted by ingestion.
    assert fact["approval_status"] == "proposed"
    assert packet["approved_facts"] == []
    assert packet["promotion_performed"] is False


def test_reprocessing_the_same_message_creates_no_duplicates(
    private_state, monkeypatch
):
    proposed = [
        {
            "wording": "Prices exclude GST.",
            "fact_category": "gst",
            "scope": "linxio",
            "source_reference": "Price list",
            "effective_date": "",
            "provenance": "Header",
            "risk_if_wrong": "high",
            "conflict_result": "",
            "approval_status": "proposed",
        }
    ]
    service = FakeGmail([message()])

    first = run(service, monkeypatch, facts=proposed)
    second = run(service, monkeypatch, facts=proposed)

    assert first["sources_new"] == 1 and first["facts_proposed"] == 1
    assert second["sources_new"] == 0
    assert second["sources_duplicate"] == 1
    assert second["facts_proposed"] == 0
    assert len(intake.review_packet()["proposed_facts"]) == 1


def test_unreadable_attachments_are_recorded_but_never_guessed(
    private_state, monkeypatch
):
    """A PDF is preserved in Gmail and surfaced as a gap, not parsed by guesswork."""
    service = FakeGmail(
        [
            message(
                attachments=[
                    ("prices.pdf", "application/pdf", 2048, "%PDF-1.7 binary"),
                    ("plans.csv", "text/csv", 40, "plan,price\nA,10\n"),
                    ("scan.jpg", "image/jpeg", 900, "\xff\xd8\xff binary"),
                ]
            )
        ]
    )
    seen = []

    def capture(source):
        seen.append(source["filename"])
        return []

    monkeypatch.setattr(intake, "propose_facts", capture)
    report = run(service, monkeypatch)
    packet = intake.review_packet()

    # Only the readable sources ever reach extraction.
    assert sorted(seen) == ["(email body)", "plans.csv"]
    assert report["sources_unreadable"] == 2
    unreadable = {s["filename"] for s in packet["unreadable_sources"]}
    assert unreadable == {"prices.pdf", "scan.jpg"}
    # Their bytes were never fetched.
    fetched = {kwargs["id"] for name, kwargs in service.calls if name == "attachment"}
    assert fetched == {"att2"}


def test_oversized_attachment_is_skipped_without_fetching(private_state, monkeypatch):
    service = FakeGmail(
        [
            message(
                attachments=[
                    ("huge.csv", "text/csv", intake.MAX_ATTACHMENT_BYTES + 1, "x" * 10)
                ]
            )
        ]
    )

    report = run(service, monkeypatch, facts=[])

    assert report["sources_unreadable"] == 1
    assert not [name for name, _ in service.calls if name == "attachment"]


def test_raw_source_text_is_never_persisted(private_state, monkeypatch):
    secret = "Plan A costs 1234 dollars and the customer is Acme Pty Ltd"
    service = FakeGmail([message(body=secret)])

    run(service, monkeypatch, facts=[])

    stored = (private_state / "state.db").read_bytes()
    assert b"Acme Pty Ltd" not in stored
    assert b"1234 dollars" not in stored
    # Only the digest and the identifiers needed to re-fetch the original.
    packet = intake.review_packet()
    assert packet["sources"][0]["filename"] == "(email body)"
    assert "text" not in packet["sources"][0]


def test_malformed_model_output_is_rejected(private_state, monkeypatch):
    monkeypatch.setattr(intake, "_load_runtime_env", lambda: None)
    monkeypatch.setattr(
        intake, "_llm_json", lambda *a, **k: {"facts": [{"wording": "no other fields"}]}
    )
    with pytest.raises(intake.FactIntakeError, match="malformed_model_output"):
        intake.propose_facts({"filename": "x.csv", "text": "some text"})

    for bad in (
        {"fact_category": "not_a_category"},
        {"scope": "everything"},
        {"risk_if_wrong": "catastrophic"},
        {"wording": ""},
    ):
        fact = {
            "wording": "Prices exclude GST.", "fact_category": "gst",
            "scope": "linxio", "source_reference": "x", "effective_date": "",
            "provenance": "x", "risk_if_wrong": "high", "conflict_result": "",
        }
        fact.update(bad)
        monkeypatch.setattr(intake, "_llm_json", lambda *a, **k: {"facts": [fact]})
        with pytest.raises(intake.FactIntakeError, match="malformed_model_output"):
            intake.propose_facts({"filename": "x.csv", "text": "some text"})

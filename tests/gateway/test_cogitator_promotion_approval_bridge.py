"""Unit tests for the Cogitator promotion-approval bridge helper and the
approve-candidate cockpit parser (Scope D).

Cover: the deterministic parser (approve-candidate valid/bulk/malformed shapes,
strict — bare ``approve <n>`` is NOT a match), the request builder (preview vs
confirm, never an implicit confirm), fail-closed response validation (accepted
statuses; preview/blocked/already_approved must report no write; approved may
report a write), and compact rendering. No real endpoint, no provider call; the
bearer token stays in the Authorization header and never reaches output.
"""

import json

import pytest

from gateway.cogitator_promotion_approval_bridge import (
    ApprovalBridgeError,
    build_approval_request,
    render_approval_message,
    request_promotion_approval,
    validate_approval_response,
)
from gateway.decision_inbox_cockpit import parse_inbox_reply


# --- parser ----------------------------------------------------------------
@pytest.mark.parametrize("text,number,mode", [
    ("approve-candidate 3 preview", 3, "preview"),
    ("approve-candidate #3 preview", 3, "preview"),
    ("approve-candidate 3 confirm", 3, "confirm"),
    ("approve candidate 3 preview", 3, "preview"),
    ("approve candidate 3 confirm", 3, "confirm"),
    ("APPROVE-CANDIDATE 12 PREVIEW", 12, "preview"),
])
def test_parses_valid_approve_candidate(text, number, mode):
    r = parse_inbox_reply(text)
    assert r is not None and r.verb == "approve_candidate"
    assert r.number == number and r.mode == mode


@pytest.mark.parametrize("text", ["approve-candidate all confirm", "approve candidate all", "approve-candidate all"])
def test_bulk_attempt_parses_as_rejected_marker(text):
    r = parse_inbox_reply(text)
    assert r is not None and r.verb == "approve_candidate" and r.number is None and r.mode == "all"


@pytest.mark.parametrize("text", ["approve-candidate 3", "approve-candidate 3 yes", "approve-candidate 3 approve"])
def test_malformed_approve_candidate_parses_as_usage_marker(text):
    r = parse_inbox_reply(text)
    assert r is not None and r.verb == "approve_candidate" and r.number is None and r.mode is None


@pytest.mark.parametrize("text", [
    "approve 3", "approve 3 preview", "approve 3 confirm", "approve all",
    "research three papers on X", "/approve-candidate 3 preview",
])
def test_non_approve_candidate_does_not_match(text):
    assert parse_inbox_reply(text) is None


def test_existing_verbs_unchanged():
    assert parse_inbox_reply("research 3").verb == "research"
    assert parse_inbox_reply("show #2").number == 2
    assert parse_inbox_reply("refresh").verb == "refresh"


# --- request builder -------------------------------------------------------
def test_preview_packet_shape():
    pkt = build_approval_request(item_id="xnote-373", mode="preview", confirm=True, expected_snapshot_id="snap-1")
    assert pkt["requested_action"] == "approve_promotion_candidate"
    assert pkt["source_agent"] == "hermes" and pkt["approval_status"] == "draft_only"
    c = pkt["context"]
    assert c["item_id"] == "xnote-373" and c["mode"] == "preview"
    assert c["confirm"] is False  # preview is never an implicit confirm
    assert c["expected_snapshot_id"] == "snap-1"


def test_confirm_packet_shape():
    c = build_approval_request(item_id="x", mode="confirm", confirm=True)["context"]
    assert c["mode"] == "confirm" and c["confirm"] is True
    assert "expected_snapshot_id" not in c  # omitted when empty


def test_confirm_false_when_mode_preview():
    assert build_approval_request(item_id="x", mode="preview", confirm=True)["context"]["confirm"] is False


# --- response validation (fail closed) -------------------------------------
def _err(resp):
    with pytest.raises(ApprovalBridgeError):
        validate_approval_response(resp)


def test_unknown_status_rejected():
    _err({"status": "weird", "requested_action": "approve_promotion_candidate"})


def test_action_mismatch_rejected():
    _err({"status": "preview", "requested_action": "research_decision_item"})


@pytest.mark.parametrize("status", ["preview", "blocked", "already_approved"])
def test_no_write_status_reporting_a_write_rejected(status):
    _err({"status": status, "requested_action": "approve_promotion_candidate", "mutation_performed": True})


def test_approved_with_mutation_is_allowed():
    ok = validate_approval_response({
        "status": "approved", "requested_action": "approve_promotion_candidate",
        "mutation_performed": True, "approved_retrieval_record_path": "storage/promoted/x.md"})
    assert ok["status"] == "approved"


def test_non_object_rejected():
    _err("not a dict")


# --- transport (injected urlopen; token never in output) -------------------
class _Resp:
    def __init__(self, payload): self._b = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


def test_request_posts_and_validates(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["body"] = json.loads(request.data.decode())
        return _Resp({"status": "preview", "requested_action": "approve_promotion_candidate",
                      "target_path": "p", "would_be_record": {"title": "T", "trigger_phrases": ["a"]}})

    out = request_promotion_approval(
        base_url="https://cog.example", token="secret-token", item_id="cand-x",
        mode="preview", confirm=False, expected_snapshot_id="", urlopen=fake_urlopen)
    assert out["status"] == "preview"
    assert seen["auth"] == "Bearer secret-token"
    assert "secret-token" not in json.dumps(out)  # token never echoed in the result
    assert seen["body"]["context"]["mode"] == "preview"


def test_missing_config_fails_closed():
    with pytest.raises(ApprovalBridgeError):
        request_promotion_approval(base_url="", token="t", item_id="x", mode="preview", confirm=False)


# --- rendering -------------------------------------------------------------
def test_render_preview_with_disabled_banner():
    out = render_approval_message({
        "status": "preview", "approval_enabled": False, "target_path": "storage/promoted/x.md",
        "would_be_record": {"record_type": "virgil_retrieval_record", "title": "T",
                            "trigger_phrases": ["a"], "distinctive_tokens": ["b"],
                            "why_relevant": "w", "plan_delta": "d", "verification_obligation": "v"},
    }, number=3)
    assert "Approval preview for #3" in out and "No write performed." in out
    assert "disabled" in out.lower() and "approve-candidate 3 confirm" in out


def test_render_approved():
    out = render_approval_message({"status": "approved", "approved_retrieval_record_path": "storage/promoted/x.md"}, number=3)
    assert "Approved. Wrote:" in out and "storage/promoted/x.md" in out and "refresh" in out.lower()


def test_render_blocked():
    out = render_approval_message({"status": "blocked", "message": "approval disabled"}, number=3)
    assert "Blocked:" in out and "No record written." in out


def test_render_already_approved():
    out = render_approval_message({"status": "already_approved", "approved_retrieval_record_path": "p"}, number=3)
    assert "Already approved" in out and "No duplicate written." in out

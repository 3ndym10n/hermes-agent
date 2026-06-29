"""Decision Inbox cockpit routing + per-session context (reply-context, no new
slash command).

Exercises the mixin glue: context set on /decision_batch, TTL expiry, and that a
parsed reply routes to the right bridge call (research → research action, refresh
→ decision batch, skip → read-only notice). Bridge HTTP is monkeypatched; no real
endpoint, no provider call.
"""

import time

import pytest

import gateway.cogitator_decision_batch_bridge as db_bridge
import gateway.cogitator_promotion_approval_bridge as approval_bridge
import gateway.cogitator_research_bridge as research_bridge
from gateway.config import Platform
from gateway.decision_inbox_cockpit import InboxReply
from gateway.session import SessionSource
from gateway.slash_commands import GatewaySlashCommandsMixin


class _Cockpit(GatewaySlashCommandsMixin):
    """Minimal harness: only the cockpit methods are exercised."""

    def _decision_batch_config(self):
        return True, "https://cog.example"


class _Ev:
    def __init__(self, chat_id="c1"):
        self.source = SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id)
        self.text = ""


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("COGITATOR_BRIDGE_TOKEN", "secret-token")


def test_context_set_active_and_expires():
    c, ev = _Cockpit(), _Ev()
    assert c.has_active_decision_inbox(ev) is False
    c._set_decision_inbox_context(ev, "snap-1")
    assert c.has_active_decision_inbox(ev) is True
    assert c._active_decision_inbox_context(ev)["snapshot_id"] == "snap-1"
    # expire it
    c._decision_inbox_states()[next(iter(c._decision_inbox_states()))]["ts"] = (
        time.time() - c._DECISION_INBOX_CONTEXT_TTL_SECONDS - 1
    )
    assert c.has_active_decision_inbox(ev) is False


def test_context_is_per_session():
    c = _Cockpit()
    a, b = _Ev("chatA"), _Ev("chatB")
    c._set_decision_inbox_context(a, "snap-A")
    assert c.has_active_decision_inbox(a) is True
    assert c.has_active_decision_inbox(b) is False


@pytest.mark.asyncio
async def test_research_reply_calls_research_action_with_snapshot(monkeypatch):
    c, ev = _Cockpit(), _Ev()
    c._set_decision_inbox_context(ev, "snap-1")
    captured = {}

    def fake_request(*, base_url, token, item_id, expected_snapshot_id, **kw):
        captured.update(item_id=item_id, snapshot=expected_snapshot_id, token=token)
        return {
            "status": "ok", "requested_action": "research_decision_item",
            "title": "Hermes /learn workspace pattern", "research_status": "complete",
            "recommendation": "watchlist", "confidence": "moderate",
            "evidence_for": ["useful"], "evidence_against": [], "contradictions": [],
            "missing_evidence": [], "risk_if_wrong": "low", "sources_checked": ["u"],
            "promotion_performed": False,
        }

    monkeypatch.setattr(research_bridge, "request_research_decision_item", fake_request)
    out = await c.handle_decision_inbox_reply(ev, InboxReply(verb="research", number=3))
    assert captured["item_id"] == "3"
    assert captured["snapshot"] == "snap-1"  # the snapshot the cockpit was shown under
    assert "Research started for:" in out


def _ok_research():
    return {
        "status": "ok", "requested_action": "research_decision_item",
        "title": "Hermes /learn workspace pattern", "research_status": "complete",
        "recommendation": "watchlist", "confidence": "moderate",
        "evidence_for": ["useful"], "evidence_against": [], "contradictions": [],
        "missing_evidence": [], "risk_if_wrong": "low", "sources_checked": ["u"],
        "promotion_performed": False,
    }


@pytest.mark.asyncio
async def test_research_resolves_stable_id_and_drops_snapshot(monkeypatch):
    # inbox_index present → research by durable candidate id, not the display
    # number, and with NO snapshot (identity, not fragile whole-batch equality).
    c, ev = _Cockpit(), _Ev()
    c._set_decision_inbox_context(
        ev, "snap-1",
        inbox_index=[{"n": 5, "candidate_id": "cand-x", "section": "needs_research", "title": "T"}],
    )
    captured = {}

    def fake_request(*, base_url, token, item_id, expected_snapshot_id, **kw):
        captured.update(item_id=item_id, snapshot=expected_snapshot_id)
        return _ok_research()

    monkeypatch.setattr(research_bridge, "request_research_decision_item", fake_request)
    out = await c.handle_decision_inbox_reply(ev, InboxReply(verb="research", number=5))
    assert captured["item_id"] == "cand-x"  # stable id, not "5"
    assert captured["snapshot"] == ""  # researched by identity, snapshot dropped
    assert "Research started for:" in out


@pytest.mark.asyncio
async def test_research_number_absent_from_known_inbox_points_to_refresh(monkeypatch):
    # inbox known but the number isn't in it → the item is gone; refuse locally,
    # don't fire a bridge call against a number that no longer maps to anything.
    c, ev = _Cockpit(), _Ev()
    c._set_decision_inbox_context(ev, "snap-1", inbox_index=[{"n": 1, "candidate_id": "cand-a"}])
    called = {"n": 0}

    def fake_request(**kw):
        called["n"] += 1
        return _ok_research()

    monkeypatch.setattr(research_bridge, "request_research_decision_item", fake_request)
    out = await c.handle_decision_inbox_reply(ev, InboxReply(verb="research", number=9))
    assert called["n"] == 0  # no bridge call for a vanished number
    assert "no item 9" in out.lower()
    assert "refresh" in out.lower()


@pytest.mark.asyncio
async def test_refresh_reply_repins_snapshot(monkeypatch):
    c, ev = _Cockpit(), _Ev()
    c._set_decision_inbox_context(ev, "old-snap")

    def fake_batch(*, base_url, token, detail_id="", **kw):
        return {"status": "ok", "requested_action": "render_decision_batch",
                "mutated": False, "execution_authorized": False,
                "snapshot_id": "new-snap", "rendered_batch": "Decision Inbox\n\nNeeds research:\n1. x"}

    monkeypatch.setattr(db_bridge, "request_decision_batch", fake_batch)
    out = await c.handle_decision_inbox_reply(ev, InboxReply(verb="refresh"))
    assert "Decision Inbox" in out
    assert c._active_decision_inbox_context(ev)["snapshot_id"] == "new-snap"


@pytest.mark.asyncio
async def test_skip_reply_is_read_only_notice(monkeypatch):
    c, ev = _Cockpit(), _Ev()
    c._set_decision_inbox_context(ev, "snap-1")
    out = await c.handle_decision_inbox_reply(ev, InboxReply(verb="skip", number=2))
    assert "read-only" in out.lower() or "isn't enabled" in out.lower()


@pytest.mark.asyncio
async def test_disabled_config_short_circuits():
    class _Disabled(_Cockpit):
        def _decision_batch_config(self):
            return False, ""

    out = await _Disabled().handle_decision_inbox_reply(_Ev(), InboxReply(verb="research", number=1))
    assert "disabled" in out.lower()


# --- approve-candidate routing (Scope D) -----------------------------------
def _ctx_with_index(c, ev):
    c._set_decision_inbox_context(
        ev, "snap-1",
        inbox_index=[{"n": 3, "candidate_id": "cand-x", "section": "watchlist", "title": "T"}])


@pytest.mark.asyncio
async def test_approve_candidate_preview_builds_preview_packet(monkeypatch):
    c, ev = _Cockpit(), _Ev()
    _ctx_with_index(c, ev)
    captured = {}

    def fake_request(*, base_url, token, item_id, mode, confirm, expected_snapshot_id, **kw):
        captured.update(item_id=item_id, mode=mode, confirm=confirm, snapshot=expected_snapshot_id)
        return {"status": "preview", "requested_action": "approve_promotion_candidate",
                "approval_enabled": False, "target_path": "storage/promoted/x.md",
                "would_be_record": {"title": "T", "trigger_phrases": ["a"]}}

    monkeypatch.setattr(approval_bridge, "request_promotion_approval", fake_request)
    out = await c.handle_decision_inbox_reply(ev, InboxReply("approve_candidate", 3, "preview"))
    assert captured["item_id"] == "cand-x"  # resolved stable id, not "3"
    assert captured["mode"] == "preview" and captured["confirm"] is False
    assert captured["snapshot"] == ""  # by identity, snapshot dropped
    assert "Approval preview for #3" in out


@pytest.mark.asyncio
async def test_approve_candidate_confirm_sets_confirm_true(monkeypatch):
    c, ev = _Cockpit(), _Ev()
    _ctx_with_index(c, ev)
    captured = {}

    def fake_request(*, base_url, token, item_id, mode, confirm, expected_snapshot_id, **kw):
        captured.update(mode=mode, confirm=confirm)
        return {"status": "approved", "requested_action": "approve_promotion_candidate",
                "mutation_performed": True, "approved_retrieval_record_path": "storage/promoted/x.md"}

    monkeypatch.setattr(approval_bridge, "request_promotion_approval", fake_request)
    out = await c.handle_decision_inbox_reply(ev, InboxReply("approve_candidate", 3, "confirm"))
    assert captured["mode"] == "confirm" and captured["confirm"] is True
    assert "Approved. Wrote:" in out


@pytest.mark.asyncio
async def test_approve_candidate_bulk_rejected_no_bridge_call(monkeypatch):
    c, ev = _Cockpit(), _Ev()
    _ctx_with_index(c, ev)
    called = {"n": 0}
    monkeypatch.setattr(approval_bridge, "request_promotion_approval",
                        lambda **kw: called.__setitem__("n", called["n"] + 1))
    out = await c.handle_decision_inbox_reply(ev, InboxReply("approve_candidate", None, "all"))
    assert called["n"] == 0
    assert "bulk approval is not supported" in out.lower()


@pytest.mark.asyncio
async def test_approve_candidate_malformed_shows_usage(monkeypatch):
    c, ev = _Cockpit(), _Ev()
    _ctx_with_index(c, ev)
    called = {"n": 0}
    monkeypatch.setattr(approval_bridge, "request_promotion_approval",
                        lambda **kw: called.__setitem__("n", called["n"] + 1))
    out = await c.handle_decision_inbox_reply(ev, InboxReply("approve_candidate", None, None))
    assert called["n"] == 0 and "usage:" in out.lower()


@pytest.mark.asyncio
async def test_approve_candidate_vanished_number_points_to_refresh(monkeypatch):
    c, ev = _Cockpit(), _Ev()
    c._set_decision_inbox_context(ev, "snap-1", inbox_index=[{"n": 1, "candidate_id": "cand-a"}])
    called = {"n": 0}
    monkeypatch.setattr(approval_bridge, "request_promotion_approval",
                        lambda **kw: called.__setitem__("n", called["n"] + 1))
    out = await c.handle_decision_inbox_reply(ev, InboxReply("approve_candidate", 9, "preview"))
    assert called["n"] == 0 and "no item 9" in out.lower() and "refresh" in out.lower()


@pytest.mark.asyncio
async def test_approve_candidate_blocked_renders_no_write(monkeypatch):
    c, ev = _Cockpit(), _Ev()
    _ctx_with_index(c, ev)

    def fake_request(**kw):
        return {"status": "blocked", "requested_action": "approve_promotion_candidate",
                "message": "Promotion approval is disabled.", "mutation_performed": False}

    monkeypatch.setattr(approval_bridge, "request_promotion_approval", fake_request)
    out = await c.handle_decision_inbox_reply(ev, InboxReply("approve_candidate", 3, "confirm"))
    assert "Blocked:" in out and "No record written." in out

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

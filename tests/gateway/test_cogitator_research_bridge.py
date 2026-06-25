"""Unit tests for the Cogitator research-action bridge helper and the Decision
Inbox reply parser (research an inbox item from the Virgil cockpit).

Cover: the deterministic reply parser (research/show/refresh/skip, strict shape),
the request builder, the POST transport (injected fake ``urlopen``), fail-closed
response validation (promotion must be False; ok/rejected/disabled accepted), and
compact rendering of result / failure / rejection. No real endpoint, no provider
call; the bearer token stays in the Authorization header and never reaches output.
"""

import json

import pytest

from gateway.cogitator_research_bridge import (
    BRIDGE_PATH,
    ResearchBridgeError,
    build_research_request,
    render_research_message,
    request_research_decision_item,
    validate_research_response,
)
from gateway.decision_inbox_cockpit import parse_inbox_reply


def _ok_response(**overrides):
    base = {
        "status": "ok",
        "requested_action": "research_decision_item",
        "request_id": "ra-1",
        "item_id": "2",
        "title": "Hermes /learn workspace pattern",
        "research_status": "complete",
        "recommendation": "watchlist",
        "current_disposition": "watchlist",
        "confidence": "moderate",
        "evidence_for": ["useful workflow evidence"],
        "evidence_against": [],
        "contradictions": [],
        "missing_evidence": ["independent primary-source confirmation"],
        "risk_if_wrong": "low",
        "sources_checked": ["https://docs.example/x"],
        "mutation_performed": False,
        "promotion_performed": False,
    }
    base.update(overrides)
    return base


class _FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._raw


def _fake_urlopen(payload, captured):
    def opener(request, timeout=None):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(payload)

    return opener


class TestParseInboxReply:
    def test_research_number(self):
        r = parse_inbox_reply("research 3")
        assert r.verb == "research" and r.number == 3

    def test_research_hash_number(self):
        r = parse_inbox_reply("show #2")
        assert r.verb == "show" and r.number == 2

    def test_refresh_bare(self):
        r = parse_inbox_reply("refresh")
        assert r.verb == "refresh" and r.number is None

    def test_skip_number(self):
        assert parse_inbox_reply("skip 1").verb == "skip"

    def test_case_insensitive(self):
        assert parse_inbox_reply("RESEARCH 4").number == 4

    def test_prose_is_not_a_reply(self):
        assert parse_inbox_reply("research three papers on retrieval") is None
        assert parse_inbox_reply("can you research 3 things") is None
        assert parse_inbox_reply("research") is None  # verb needs a number

    def test_slash_command_is_not_a_reply(self):
        assert parse_inbox_reply("/decision_batch") is None
        assert parse_inbox_reply("/research 3") is None

    def test_empty(self):
        assert parse_inbox_reply("") is None
        assert parse_inbox_reply("   ") is None


class TestBuildRequest:
    def test_packet_targets_research_action_with_confirm(self):
        p = build_research_request(item_id="2", expected_snapshot_id="abc")
        assert p["source_agent"] == "hermes"
        assert p["requested_action"] == "research_decision_item"
        assert p["approval_status"] == "draft_only"
        assert p["context"] == {"item_id": "2", "confirm": True, "expected_snapshot_id": "abc"}

    def test_snapshot_omitted_when_empty(self):
        p = build_research_request(item_id="2")
        assert "expected_snapshot_id" not in p["context"]
        assert p["context"]["confirm"] is True


class TestTransport:
    def test_posts_with_bearer_and_returns_validated(self):
        captured = {}
        out = request_research_decision_item(
            base_url="https://cog.example", token="secret-token",
            item_id="2", expected_snapshot_id="abc",
            urlopen=_fake_urlopen(_ok_response(), captured),
        )
        assert out["status"] == "ok"
        assert captured["url"].endswith(BRIDGE_PATH)
        assert captured["authorization"] == "Bearer secret-token"
        assert captured["body"]["context"]["item_id"] == "2"

    def test_missing_config_fails_closed(self):
        with pytest.raises(ResearchBridgeError):
            request_research_decision_item(base_url="", token="t", item_id="2")
        with pytest.raises(ResearchBridgeError):
            request_research_decision_item(base_url="u", token="", item_id="2")
        with pytest.raises(ResearchBridgeError):
            request_research_decision_item(base_url="u", token="t", item_id="")


class TestValidate:
    def test_rejected_and_disabled_are_valid_outcomes(self):
        assert validate_research_response(
            {"status": "rejected", "requested_action": "research_decision_item",
             "reason_code": "stale_snapshot", "promotion_performed": False})["status"] == "rejected"
        assert validate_research_response(
            {"status": "disabled", "requested_action": "research_decision_item",
             "promotion_performed": False})["status"] == "disabled"

    def test_promotion_reported_rejected(self):
        with pytest.raises(ResearchBridgeError):
            validate_research_response(_ok_response(promotion_performed=True))

    def test_execution_field_rejected(self):
        with pytest.raises(ResearchBridgeError):
            validate_research_response(_ok_response(promoted=1))

    def test_action_mismatch_rejected(self):
        with pytest.raises(ResearchBridgeError):
            validate_research_response(_ok_response(requested_action="something_else"))


class TestRender:
    def test_result_shows_title_and_recommendation(self):
        msg = render_research_message(_ok_response())
        assert "Research started for:" in msg
        assert "Hermes /learn workspace pattern" in msg
        assert "recommendation: watchlist" in msg
        assert "useful workflow evidence" in msg
        assert "secret-token" not in msg  # never leak a token

    def test_failed_research_stays_needs_research(self):
        msg = render_research_message(_ok_response(
            research_status="failed", failure_reason="provider unavailable"))
        assert "could not complete" in msg
        assert "Needs research" in msg

    def test_stale_rejection_points_to_refresh(self):
        msg = render_research_message({
            "status": "rejected", "requested_action": "research_decision_item",
            "reason_code": "stale_snapshot",
            "message": "The decision inbox changed since this number was shown.",
            "promotion_performed": False})
        assert "refresh" in msg.lower()

    def test_disabled_message(self):
        msg = render_research_message({
            "status": "disabled", "requested_action": "research_decision_item",
            "promotion_performed": False})
        assert "disabled" in msg.lower()

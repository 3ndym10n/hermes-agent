"""Tests for the plain-text ``intake`` command: strict parser, draft-only bridge
request/validation (fail-closed), rendering, and the mixin handler dispatch.
No real HTTP — the bridge transport is stubbed everywhere.
"""

import asyncio
import json

import pytest

import gateway.cogitator_intake_bridge as ib
from gateway.slash_commands import GatewaySlashCommandsMixin


# --- parser -----------------------------------------------------------------

def test_parse_no_lens_intake():
    cmd = ib.parse_intake_message("intake\nSome messy pasted material.\nMore lines.")
    assert cmd is not None and cmd.error == ""
    assert cmd.lens == ""
    assert cmd.raw_text == "Some messy pasted material.\nMore lines."


def test_parse_lens_intake():
    cmd = ib.parse_intake_message("intake lens gpu-store\nbody text here")
    assert cmd is not None and cmd.error == ""
    assert cmd.lens == "gpu-store"
    assert cmd.raw_text == "body text here"


def test_parse_missing_body_rejected():
    cmd = ib.parse_intake_message("intake\n   \n")
    assert cmd is not None and cmd.error == "missing_body"
    cmd = ib.parse_intake_message("intake")
    assert cmd is not None and cmd.error == "missing_body"


def test_parse_oversized_body_rejected():
    cmd = ib.parse_intake_message("intake\n" + "x" * (ib.MAX_BODY_CHARS + 1))
    assert cmd is not None and cmd.error == "oversized_body"


def test_parse_invalid_lens_rejected():
    cmd = ib.parse_intake_message("intake lens ../../etc\nbody")
    assert cmd is not None and cmd.error == "invalid_lens"
    cmd = ib.parse_intake_message("intake lens " + "a" * 61 + "\nbody")
    assert cmd is not None and cmd.error == "invalid_lens"


def test_parse_inline_url():
    # One-line ``intake <url>`` is the natural phone form — must parse.
    url = "https://x.com/femke_plantinga/status/2071909327808483360?s=20"
    cmd = ib.parse_intake_message(f"intake {url}")
    assert cmd is not None and cmd.error == "" and cmd.lens == ""
    assert cmd.raw_text == url
    # Extra body lines after the inline URL stay part of the dump.
    cmd = ib.parse_intake_message(f"intake {url}\nsecond line")
    assert cmd is not None and cmd.error == ""
    assert cmd.raw_text == f"{url}\nsecond line"
    # Non-URL words after the verb are still prose, never hijacked.
    assert ib.parse_intake_message("intake https broke again") is None


def test_parse_ordinary_prose_falls_through():
    # Prose containing/starting with the word must NOT be hijacked.
    assert ib.parse_intake_message("intake of protein matters a lot") is None
    assert ib.parse_intake_message("the intake\nprocess is fine") is None
    assert ib.parse_intake_message("/intake something") is None
    assert ib.parse_intake_message("") is None


def test_parse_research_verb():
    cmd = ib.parse_intake_message("intake research 2")
    assert cmd is not None and cmd.error == "" and cmd.research_number == 2
    assert cmd.packet_path == ""
    cmd = ib.parse_intake_message(
        "intake research storage/intake/packets/2026-07-03_12-00-00-intake-packet.md 3")
    assert cmd is not None and cmd.research_number == 3
    assert cmd.packet_path.endswith("-packet.md")
    cmd = ib.parse_intake_message("intake research two")
    assert cmd is not None and cmd.error == "invalid_research"
    cmd = ib.parse_intake_message("intake research 2\nplus body")
    assert cmd is not None and cmd.error == "invalid_research"


# --- request/response validation ---------------------------------------------

def _ok_response(**overrides):
    resp = {
        "status": "ok",
        "requested_action": "intake_review_packet",
        "dry_run": False,
        "raw_path": "storage/intake/raw/2026-07-03_12-00-00-intake.md",
        "packet_path": "storage/intake/packets/2026-07-03_12-00-00-intake-packet.md",
        "detected_domains": ["business_validation", "marketing_presale"],
        "counts": {
            "high_value_ideas": 1, "claims_to_verify": 2, "opportunities": 1,
            "playbook_candidates": 1, "retrieval_candidates": 1, "ignored": 3,
        },
        "top_outputs": ["opportunity: run a preorder test"],
        "next_action": "Run the smallest preorder test.",
        "mutation_performed": True,
        "research_performed": False,
        "promotion_performed": False,
    }
    resp.update(overrides)
    return resp


class _FakeHTTP:
    def __init__(self, payload):
        self._payload = payload
        self.request = None

    def __call__(self, request, timeout=None):
        self.request = request
        payload = self._payload

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        return _Resp()


def test_request_maps_lens_to_context_label():
    fake = _FakeHTTP(_ok_response())
    out = ib.request_intake_review(
        base_url="https://cog.example", token="tkn",
        raw_text="body", context_label="gpu-store", urlopen=fake,
    )
    sent = json.loads(fake.request.data.decode("utf-8"))
    assert sent["requested_action"] == "intake_review_packet"
    assert sent["approval_status"] == "draft_only"
    assert sent["context"] == {"raw_text": "body", "context_label": "gpu-store"}
    assert out["status"] == "ok"


def test_request_no_lens_omits_context_label():
    fake = _FakeHTTP(_ok_response())
    ib.request_intake_review(
        base_url="https://cog.example", token="tkn", raw_text="body", urlopen=fake,
    )
    sent = json.loads(fake.request.data.decode("utf-8"))
    assert "context_label" not in sent["context"]


def test_response_research_or_promotion_fails_closed():
    with pytest.raises(ib.IntakeBridgeError) as exc:
        ib.validate_intake_response(_ok_response(research_performed=True))
    assert exc.value.code == "BRIDGE_STATEFUL_RESPONSE"
    with pytest.raises(ib.IntakeBridgeError):
        ib.validate_intake_response(_ok_response(promotion_performed=True))
    with pytest.raises(ib.IntakeBridgeError):
        ib.validate_intake_response(_ok_response(approved=True))
    with pytest.raises(ib.IntakeBridgeError):
        ib.validate_intake_response(_ok_response(status="weird"))
    with pytest.raises(ib.IntakeBridgeError):
        ib.validate_intake_response(_ok_response(requested_action="save_note"))


def test_render_summary():
    out = ib.render_intake_message(_ok_response())
    assert "Intake packet created" in out
    assert "high-value ideas: 1" in out
    assert "claims to verify: 2" in out
    assert "business/action opportunities: 1" in out
    assert "playbook candidates: 1" in out
    assert "retrieval candidates: 1" in out
    assert "ignored/low-value: 3" in out
    assert "business_validation" in out
    assert "opportunity: run a preorder test" in out
    assert "Next action:" in out
    assert "storage/intake/raw/2026-07-03_12-00-00-intake.md" in out
    assert "storage/intake/packets/2026-07-03_12-00-00-intake-packet.md" in out


def test_render_rejected():
    out = ib.render_intake_message({
        "status": "rejected", "requested_action": "intake_review_packet",
        "message": "raw_text is required",
    })
    assert "Intake rejected" in out and "raw_text is required" in out


_AUTO_RESEARCH_OK = {
    "status": "ok",
    "claim_number": 1,
    "claim": "AI dynamic pricing lifts margins by 25%",
    "verdict": "verified_enough",
    "evidence_quality": "moderate",
    "engine": "adaptive",
    "recommended_action": "Direct sources support the claim.",
    "note_path": "storage/research_notes/intake-research-x.md",
}


def test_auto_research_response_accepted_only_with_result():
    # research_performed=True is legitimate ONLY alongside an auto_research block
    ok = ib.validate_intake_response(
        _ok_response(research_performed=True, auto_research=dict(_AUTO_RESEARCH_OK)))
    assert ok["auto_research"]["verdict"] == "verified_enough"
    with pytest.raises(ib.IntakeBridgeError) as exc:
        ib.validate_intake_response(_ok_response(research_performed=True))
    assert exc.value.code == "BRIDGE_STATEFUL_RESPONSE"
    with pytest.raises(ib.IntakeBridgeError):
        ib.validate_intake_response(
            _ok_response(research_performed=True, auto_research="yes"))
    with pytest.raises(ib.IntakeBridgeError):  # promotion stays forbidden regardless
        ib.validate_intake_response(
            _ok_response(promotion_performed=True, auto_research=dict(_AUTO_RESEARCH_OK)))


def test_render_summary_shows_auto_research_verdict():
    out = ib.render_intake_message(
        _ok_response(research_performed=True, auto_research=dict(_AUTO_RESEARCH_OK)))
    assert "✅ Auto-research (claim 1): verified_enough" in out
    assert "AI dynamic pricing lifts margins by 25%" in out
    assert "engine: adaptive" in out
    assert "storage/research_notes/intake-research-x.md" in out


def test_render_summary_shows_auto_research_failure_softly():
    out = ib.render_intake_message(_ok_response(
        auto_research={"status": "failed", "reason": "auto-research error: RuntimeError"}))
    assert "Auto-research failed" in out
    assert "intake itself succeeded" in out


# --- mixin handler dispatch ----------------------------------------------------

def _handler_with_config(enabled, base_url, local_dir=""):
    mixin = GatewaySlashCommandsMixin()
    mixin._intake_config = lambda: (enabled, base_url, local_dir)  # type: ignore[attr-defined]
    return mixin


def test_save_local_copies_writes_only_present_markdown(tmp_path):
    saved = ib.save_local_copies({
        "packet_path": "storage/intake/packets/2026-07-07_10-00-00-intake-packet.md",
        "packet_markdown": "# Intake Review Packet\ncontent",
        "note_path": "storage/research_notes/x.md",
        "note_markdown": "",  # empty → skipped
        "bundle_path": "../../evil.md",  # traversal → basename only
        "bundle_markdown": "sources",
    }, str(tmp_path))
    assert sorted(p.replace(str(tmp_path), "") for p in saved) == [
        "/intake/extracted/evil.md",
        "/intake/packets/2026-07-07_10-00-00-intake-packet.md",
    ]
    packet = tmp_path / "intake/packets/2026-07-07_10-00-00-intake-packet.md"
    assert packet.read_text() == "# Intake Review Packet\ncontent"
    assert not (tmp_path / "research_notes").exists()
    assert (tmp_path / "intake/extracted/evil.md").read_text() == "sources"


class _Ev:
    """Minimal event with a session source for context-key building."""

    def __init__(self):
        from gateway.config import Platform
        from gateway.session import SessionSource

        self.source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1")
        self.text = ""


def _run(coro):
    return asyncio.run(coro)


def test_handler_missing_body_returns_help_without_network(monkeypatch):
    monkeypatch.setattr(ib, "request_intake_review",
                        lambda **k: (_ for _ in ()).throw(AssertionError("should not POST")))
    mixin = _handler_with_config(True, "https://cog.example")
    out = _run(mixin.handle_intake_message(_Ev(), ib.IntakeCommand(error="missing_body")))
    assert "intake" in out and "verbatim" in out


def test_handler_disabled_gate_no_network(monkeypatch):
    monkeypatch.setattr(ib, "request_intake_review",
                        lambda **k: (_ for _ in ()).throw(AssertionError("should not POST")))
    mixin = _handler_with_config(False, "https://cog.example")
    out = _run(mixin.handle_intake_message(_Ev(), ib.IntakeCommand(raw_text="body")))
    assert "disabled" in out.lower()


def test_handler_not_configured_when_token_missing(monkeypatch):
    monkeypatch.delenv(ib.TOKEN_ENV, raising=False)
    mixin = _handler_with_config(True, "https://cog.example")
    out = _run(mixin.handle_intake_message(_Ev(), ib.IntakeCommand(raw_text="body")))
    assert "not configured" in out.lower()


def test_handler_enabled_path_calls_bridge_and_stores_context(monkeypatch):
    calls = {}

    def fake_request(*, base_url, token, raw_text, context_label="", **kw):
        calls.update(base_url=base_url, raw_text=raw_text, context_label=context_label)
        return _ok_response()

    monkeypatch.setattr(ib, "request_intake_review", fake_request)
    monkeypatch.setenv(ib.TOKEN_ENV, "tkn")
    mixin = _handler_with_config(True, "https://cog.example")
    ev = _Ev()
    out = _run(mixin.handle_intake_message(
        ev, ib.IntakeCommand(raw_text="messy dump", lens="gpu-store")))
    assert calls == {
        "base_url": "https://cog.example",
        "raw_text": "messy dump",
        "context_label": "gpu-store",
    }
    assert "Intake packet created" in out
    ctx = mixin._active_intake_context(ev)
    assert ctx and ctx["packet_path"].endswith("-intake-packet.md")


def test_is_url_only_body():
    assert ib.is_url_only_body("https://a.example\nhttps://b.example")
    assert ib.is_url_only_body("  https://a.example  \n\n")
    assert not ib.is_url_only_body("check https://a.example out")
    assert not ib.is_url_only_body("https://a.example\nplus a comment line")
    assert not ib.is_url_only_body("")


def _ok_link_response(**overrides):
    resp = {
        "status": "ok",
        "requested_action": "source_access_intake_packet",
        "dry_run": False,
        "raw_path": "storage/intake/raw/2026-07-03_13-00-00-intake-links.md",
        "bundle_path": "storage/intake/extracted/2026-07-03_13-00-00-intake-sources.md",
        "packet_path": "storage/intake/packets/2026-07-03_13-00-00-intake-packet.md",
        "source_status_counts": {"fetched_full": 1, "needs_full_source": 1},
        "mined_sources": 1,
        "counts": {"high_value_ideas": 0, "claims_to_verify": 1, "opportunities": 0,
                   "playbook_candidates": 1, "retrieval_candidates": 0, "ignored": 2},
        "top_outputs": ["playbook candidate: Example Tool"],
        "next_action": "Verify the claim.",
        "detected_domains": ["agent_building"],
        "mutation_performed": True,
        "research_performed": False,
        "promotion_performed": False,
    }
    resp.update(overrides)
    return resp


def test_link_request_maps_urls_and_validates():
    fake = _FakeHTTP(_ok_link_response())
    out = ib.request_source_access_intake(
        base_url="https://cog.example", token="tkn",
        urls=["https://github.com/example/tool", "https://x.com/a/status/1"],
        urlopen=fake,
    )
    sent = json.loads(fake.request.data.decode("utf-8"))
    assert sent["requested_action"] == "source_access_intake_packet"
    assert sent["context"]["urls"] == [
        "https://github.com/example/tool", "https://x.com/a/status/1"]
    assert out["mined_sources"] == 1


def test_link_request_rejects_too_many():
    with pytest.raises(ib.IntakeBridgeError) as exc:
        ib.request_source_access_intake(
            base_url="https://cog.example", token="tkn",
            urls=[f"https://e.example/{i}" for i in range(26)],
        )
    assert exc.value.code == "TOO_MANY_LINKS"


def test_link_response_promotion_fails_closed():
    with pytest.raises(ib.IntakeBridgeError):
        ib._validate_response(_ok_link_response(promotion_performed=True),
                              expected_action="source_access_intake_packet")


def test_render_link_intake_message_shows_honest_statuses():
    out = ib.render_link_intake_message(_ok_link_response())
    assert "fetched_full: 1" in out
    assert "needs_full_source: 1" in out
    assert "mined into packet: 1" in out
    assert "playbook candidate: Example Tool" in out
    assert "intake-sources.md" in out
    assert "Auto-research" not in out  # no block when Cogitator sent none


def test_render_link_intake_message_shows_auto_research_verdict():
    out = ib.render_link_intake_message(_ok_link_response(
        research_performed=True, auto_research=dict(_AUTO_RESEARCH_OK)))
    assert "Auto-research (claim 1): verified_enough" in out
    assert "engine: adaptive" in out
    assert "storage/research_notes/intake-research-x.md" in out


def test_render_link_intake_message_shows_auto_research_failure_softly():
    out = ib.render_link_intake_message(_ok_link_response(
        auto_research={"status": "failed", "reason": "auto-research error: RuntimeError"}))
    assert "Auto-research failed" in out
    assert "intake itself succeeded" in out


def test_handler_url_only_body_routes_to_source_access(monkeypatch):
    calls = {}

    def fake_link_request(*, base_url, token, urls, context_label="", **kw):
        calls["urls"] = urls
        return _ok_link_response()

    monkeypatch.setattr(ib, "request_source_access_intake", fake_link_request)
    monkeypatch.setattr(ib, "request_intake_review",
                        lambda **k: (_ for _ in ()).throw(AssertionError("wrong route")))
    monkeypatch.setenv(ib.TOKEN_ENV, "tkn")
    mixin = _handler_with_config(True, "https://cog.example")
    out = _run(mixin.handle_intake_message(
        _Ev(), ib.IntakeCommand(raw_text="https://github.com/example/tool\nhttps://x.com/a/status/1")))
    assert calls["urls"] == ["https://github.com/example/tool", "https://x.com/a/status/1"]
    assert "Link intake complete" in out


def test_handler_mixed_body_routes_to_text_intake(monkeypatch):
    monkeypatch.setattr(ib, "request_source_access_intake",
                        lambda **k: (_ for _ in ()).throw(AssertionError("wrong route")))
    monkeypatch.setattr(ib, "request_intake_review", lambda **k: _ok_response())
    monkeypatch.setenv(ib.TOKEN_ENV, "tkn")
    mixin = _handler_with_config(True, "https://cog.example")
    out = _run(mixin.handle_intake_message(
        _Ev(), ib.IntakeCommand(raw_text="notes about https://github.com/example/tool")))
    assert "Intake packet created" in out


def test_handler_bridge_error_is_sanitized(monkeypatch):
    def boom(**k):
        raise ib.IntakeBridgeError("BRIDGE_UNREACHABLE", "URLError")

    monkeypatch.setattr(ib, "request_intake_review", boom)
    monkeypatch.setenv(ib.TOKEN_ENV, "tkn")
    mixin = _handler_with_config(True, "https://cog.example")
    out = _run(mixin.handle_intake_message(_Ev(), ib.IntakeCommand(raw_text="body")))
    assert "BRIDGE_UNREACHABLE" in out
    assert "tkn" not in out


def _ok_research_response(**overrides):
    resp = {
        "status": "ok",
        "requested_action": "research_intake_item",
        "packet_path": "storage/intake/packets/2026-07-03_12-00-00-intake-packet.md",
        "claim_number": 1,
        "claim": "AI dynamic pricing lifts margins by 25%",
        "verdict": "needs_more_evidence",
        "evidence_quality": "none",
        "sources_used": [
            {"url": "https://x.com/v/status/1", "stance": "failed",
             "evidence_type": "none", "note": "status=needs_full_source"},
        ],
        "missing_evidence": ["https://x.com/v/status/1 — full source unavailable"],
        "recommended_action": "Verify manually before relying on the claim.",
        "note_path": "storage/research_notes/ai-dynamic-pricing_x-claim-1.md",
        "mutation_performed": True,
        "research_performed": True,
        "promotion_performed": False,
    }
    resp.update(overrides)
    return resp


def test_research_response_promotion_fails_closed():
    with pytest.raises(ib.IntakeBridgeError):
        ib.validate_intake_research_response(_ok_research_response(promotion_performed=True))
    # research_performed=True is expected and allowed here
    assert ib.validate_intake_research_response(_ok_research_response())["verdict"]


def test_render_research_message():
    out = ib.render_intake_research_message(_ok_research_response())
    assert "needs_more_evidence" in out
    assert "AI dynamic pricing" in out
    assert "Sources consulted:" in out
    assert "Research note: storage/research_notes/" in out


def test_render_intake_message_lists_research_targets():
    out = ib.render_intake_message(_ok_response(research_targets=[
        {"n": 1, "claim": "AI dynamic pricing lifts margins by 25%"},
        {"n": 2, "claim": "Chatbots handle 70-90% of queries"},
    ]))
    assert "intake research <n>" in out
    assert "1. AI dynamic pricing" in out


def test_handler_research_uses_session_context(monkeypatch):
    calls = {}

    def fake_research(*, base_url, token, packet_path, item_number, **kw):
        calls.update(packet_path=packet_path, item_number=item_number)
        return _ok_research_response()

    monkeypatch.setattr(ib, "request_intake_research", fake_research)
    monkeypatch.setattr(ib, "request_intake_review", lambda **k: _ok_response())
    monkeypatch.setenv(ib.TOKEN_ENV, "tkn")
    mixin = _handler_with_config(True, "https://cog.example")
    ev = _Ev()
    # no context yet → nudge
    out = _run(mixin.handle_intake_message(ev, ib.IntakeCommand(research_number=1)))
    assert "Run intake first" in out
    # after an intake, the latest packet is addressed automatically
    _run(mixin.handle_intake_message(ev, ib.IntakeCommand(raw_text="some dump")))
    out = _run(mixin.handle_intake_message(ev, ib.IntakeCommand(research_number=2)))
    assert calls["item_number"] == 2
    assert calls["packet_path"].endswith("-intake-packet.md")
    assert "Research verdict" in out


def test_handler_research_explicit_path(monkeypatch):
    calls = {}

    def fake_research(*, packet_path, item_number, **kw):
        calls.update(packet_path=packet_path, item_number=item_number)
        return _ok_research_response()

    monkeypatch.setattr(ib, "request_intake_research", fake_research)
    monkeypatch.setenv(ib.TOKEN_ENV, "tkn")
    mixin = _handler_with_config(True, "https://cog.example")
    out = _run(mixin.handle_intake_message(_Ev(), ib.IntakeCommand(
        research_number=3, packet_path="storage/intake/packets/x-packet.md")))
    assert calls == {"packet_path": "storage/intake/packets/x-packet.md", "item_number": 3}
    assert "Research verdict" in out

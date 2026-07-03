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


def test_parse_ordinary_prose_falls_through():
    # Prose containing/starting with the word must NOT be hijacked.
    assert ib.parse_intake_message("intake of protein matters a lot") is None
    assert ib.parse_intake_message("the intake\nprocess is fine") is None
    assert ib.parse_intake_message("/intake something") is None
    assert ib.parse_intake_message("") is None
    # research verb belongs to the (later) research flow, not this parser
    assert ib.parse_intake_message("intake research 2") is None


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


# --- mixin handler dispatch ----------------------------------------------------

def _handler_with_config(enabled, base_url):
    mixin = GatewaySlashCommandsMixin()
    mixin._intake_config = lambda: (enabled, base_url)  # type: ignore[attr-defined]
    return mixin


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


def test_handler_bridge_error_is_sanitized(monkeypatch):
    def boom(**k):
        raise ib.IntakeBridgeError("BRIDGE_UNREACHABLE", "URLError")

    monkeypatch.setattr(ib, "request_intake_review", boom)
    monkeypatch.setenv(ib.TOKEN_ENV, "tkn")
    mixin = _handler_with_config(True, "https://cog.example")
    out = _run(mixin.handle_intake_message(_Ev(), ib.IntakeCommand(raw_text="body")))
    assert "BRIDGE_UNREACHABLE" in out
    assert "tkn" not in out

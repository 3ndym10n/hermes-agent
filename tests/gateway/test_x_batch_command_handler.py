"""Dispatch tests for the in-session /x_batch handler.

Exercise the registration→handler wiring without a live Cogitator endpoint:
empty command → compact help, disabled gate → notice (no network), and the
enabled path → calls only the ``x_link_batch_intake`` bridge helper and renders
a compact summary. The bridge call is stubbed so no HTTP/provider call happens.
"""

import asyncio

import gateway.cogitator_x_batch_bridge as xbb
from gateway.slash_commands import GatewaySlashCommandsMixin


class _Event:
    def __init__(self, args: str):
        self._args = args

    def get_command_args(self) -> str:
        return self._args


def _handler_with_config(enabled, base_url):
    mixin = GatewaySlashCommandsMixin()
    mixin._x_batch_config = lambda: (enabled, base_url)  # type: ignore[attr-defined]
    return mixin


def _run(coro):
    return asyncio.run(coro)


def test_empty_command_returns_help():
    mixin = _handler_with_config(True, "https://cog.example")
    out = _run(mixin._handle_x_batch_command(_Event("")))
    assert "/x_batch" in out
    assert "one per line" in out


def test_disabled_gate_returns_notice_without_network(monkeypatch):
    # If the handler tried to contact Cogitator this would raise.
    monkeypatch.setattr(xbb, "request_x_batch_intake", lambda **k: (_ for _ in ()).throw(AssertionError("should not POST")))
    mixin = _handler_with_config(False, "https://cog.example")
    out = _run(mixin._handle_x_batch_command(_Event("https://x.com/a/status/1")))
    assert "disabled" in out.lower()


def test_enabled_path_calls_only_x_batch_action_and_renders(monkeypatch):
    calls = {}

    def fake_request(*, base_url, token, urls, dry_run, **kw):
        calls["base_url"] = base_url
        calls["urls"] = urls
        calls["dry_run"] = dry_run
        return {
            "status": "ok",
            "requested_action": "x_link_batch_intake",
            "verified_captured_count": 1,
            "manual_text_count": 0,
            "bookmark_export_count": 0,
            "duplicate_in_batch_count": 0,
            "already_captured_count": 0,
            "source_needed_count": 0,
            "failed_count": 0,
            "research_candidate_count": 0,
            "promotion_performed": False,
        }

    monkeypatch.setattr(xbb, "request_x_batch_intake", fake_request)
    monkeypatch.setenv("COGITATOR_BRIDGE_TOKEN", "tkn")
    mixin = _handler_with_config(True, "https://cog.example")
    out = _run(mixin._handle_x_batch_command(_Event("dry_run\nhttps://x.com/a/status/1")))
    assert calls["urls"] == "https://x.com/a/status/1"
    assert calls["dry_run"] is True
    assert "X batch" in out
    assert "verified and captured: 1" in out


def test_not_configured_when_token_missing(monkeypatch):
    monkeypatch.delenv("COGITATOR_BRIDGE_TOKEN", raising=False)
    mixin = _handler_with_config(True, "https://cog.example")
    out = _run(mixin._handle_x_batch_command(_Event("https://x.com/a/status/1")))
    assert "not configured" in out.lower()

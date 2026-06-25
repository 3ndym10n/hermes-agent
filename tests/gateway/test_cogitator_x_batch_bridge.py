"""Unit tests for the Cogitator X-link batch-intake bridge helper (/x_batch).

Cover command parsing (dry_run + newline links), the request builder, the HTTP
POST transport (injected fake ``urlopen``), fail-closed response validation
(promotion must be False), and compact rendering. No real endpoint, no
model/provider call, no secrets on disk; the bearer token is asserted to stay in
the Authorization header and never reach rendered output.
"""

import json

import pytest

from gateway.cogitator_x_batch_bridge import (
    BRIDGE_PATH,
    MAX_BATCH_LINKS,
    XBatchBridgeError,
    build_x_batch_request,
    count_candidate_links,
    parse_x_batch_command,
    render_x_batch_message,
    request_x_batch_intake,
    validate_x_batch_response,
    x_batch_help_text,
)


def _ok_response(**overrides):
    base = {
        "status": "ok",
        "requested_action": "x_link_batch_intake",
        "dry_run": False,
        "verified_captured_count": 2,
        "manual_text_count": 1,
        "bookmark_export_count": 0,
        "duplicate_in_batch_count": 1,
        "already_captured_count": 0,
        "source_needed_count": 1,
        "failed_count": 0,
        "research_candidate_count": 2,
        "mutation_performed": True,
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
        captured["method"] = request.get_method()
        captured["authorization"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(payload)

    return opener


class TestParseCommand:
    def test_plain_links(self):
        urls, dry = parse_x_batch_command("https://x.com/a/status/1\nhttps://x.com/b/status/2")
        assert dry is False
        assert urls == "https://x.com/a/status/1\nhttps://x.com/b/status/2"

    def test_dry_run_leading_line(self):
        urls, dry = parse_x_batch_command("dry_run\nhttps://x.com/a/status/1")
        assert dry is True
        assert urls == "https://x.com/a/status/1"

    def test_dry_run_first_token_same_line(self):
        urls, dry = parse_x_batch_command("dry-run https://x.com/a/status/1\nhttps://x.com/b/status/2")
        assert dry is True
        assert urls == "https://x.com/a/status/1\nhttps://x.com/b/status/2"

    def test_empty(self):
        urls, dry = parse_x_batch_command("")
        assert urls == "" and dry is False

    def test_count_candidate_links_ignores_blanks(self):
        assert count_candidate_links("a\n\n b \n") == 2


class TestBuildRequest:
    def test_packet_shape_targets_only_the_x_batch_action(self):
        packet = build_x_batch_request(urls="https://x.com/a/status/1", dry_run=True)
        assert packet["source_agent"] == "hermes"
        assert packet["requested_action"] == "x_link_batch_intake"
        assert packet["approval_status"] == "draft_only"
        assert packet["risk_level"] == "low"
        assert packet["context"] == {"urls": "https://x.com/a/status/1", "dry_run": True}


class TestTransport:
    def test_posts_with_bearer_and_returns_validated(self):
        captured = {}
        result = request_x_batch_intake(
            base_url="https://cogitator.example",
            token="s3cr3t-token",
            urls="https://x.com/a/status/1",
            urlopen=_fake_urlopen(_ok_response(), captured),
        )
        assert captured["url"] == "https://cogitator.example" + BRIDGE_PATH
        assert captured["method"] == "POST"
        assert captured["authorization"] == "Bearer s3cr3t-token"
        assert captured["body"]["requested_action"] == "x_link_batch_intake"
        assert result["status"] == "ok"

    def test_missing_base_url_fails_closed_without_posting(self):
        called = {"n": 0}

        def opener(*a, **k):
            called["n"] += 1

        with pytest.raises(XBatchBridgeError) as exc:
            request_x_batch_intake(base_url="", token="t", urls="https://x.com/a/status/1", urlopen=opener)
        assert exc.value.code == "BRIDGE_NOT_CONFIGURED"
        assert called["n"] == 0

    def test_missing_token_fails_closed(self):
        with pytest.raises(XBatchBridgeError) as exc:
            request_x_batch_intake(base_url="https://x", token="", urls="u")
        assert exc.value.code == "BRIDGE_TOKEN_MISSING"

    def test_no_links_fails_closed(self):
        with pytest.raises(XBatchBridgeError) as exc:
            request_x_batch_intake(base_url="https://x", token="t", urls="   ")
        assert exc.value.code == "NO_LINKS"

    def test_too_many_links_refused_without_posting(self):
        called = {"n": 0}

        def opener(*a, **k):
            called["n"] += 1

        body = "\n".join(f"https://x.com/a/status/{n}" for n in range(MAX_BATCH_LINKS + 1))
        with pytest.raises(XBatchBridgeError) as exc:
            request_x_batch_intake(base_url="https://x", token="t", urls=body, urlopen=opener)
        assert exc.value.code == "TOO_MANY_LINKS"
        assert called["n"] == 0

    def test_token_never_in_rendered_output(self):
        captured = {}
        result = request_x_batch_intake(
            base_url="https://cogitator.example",
            token="super-secret",
            urls="https://x.com/a/status/1",
            urlopen=_fake_urlopen(_ok_response(), captured),
        )
        assert "super-secret" not in render_x_batch_message(result)


class TestValidation:
    def test_promotion_reported_is_rejected(self):
        with pytest.raises(XBatchBridgeError) as exc:
            validate_x_batch_response(_ok_response(promotion_performed=True))
        assert exc.value.code == "BRIDGE_PROMOTION_REPORTED"

    def test_approval_execution_field_rejected(self):
        with pytest.raises(XBatchBridgeError) as exc:
            validate_x_batch_response(_ok_response(approved=True))
        assert exc.value.code == "BRIDGE_STATEFUL_RESPONSE"

    def test_action_mismatch_rejected(self):
        with pytest.raises(XBatchBridgeError) as exc:
            validate_x_batch_response(_ok_response(requested_action="save_note"))
        assert exc.value.code == "BRIDGE_ACTION_MISMATCH"

    def test_disabled_status_is_accepted(self):
        out = validate_x_batch_response({
            "status": "disabled",
            "requested_action": "x_link_batch_intake",
            "promotion_performed": False,
        })
        assert out["status"] == "disabled"


class TestRender:
    def test_compact_summary_has_counts_no_raw_json(self):
        msg = render_x_batch_message(_ok_response())
        assert "X batch complete:" in msg
        assert "verified and captured: 2" in msg
        assert "duplicates skipped: 1" in msg  # in-batch + already-captured
        assert "research candidates: 2" in msg
        assert "{" not in msg  # never dumps raw json

    def test_dry_run_header(self):
        msg = render_x_batch_message(_ok_response(dry_run=True))
        assert "preview" in msg.lower()

    def test_disabled_message(self):
        msg = render_x_batch_message({"status": "disabled", "requested_action": "x_link_batch_intake", "promotion_performed": False})
        assert "disabled" in msg.lower()

    def test_help_text_lists_usage(self):
        assert "/x_batch" in x_batch_help_text()

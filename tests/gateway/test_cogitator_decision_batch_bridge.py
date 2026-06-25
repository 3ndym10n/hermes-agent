"""Unit tests for the manual Cogitator decision-batch bridge helper.

Cover the pure request builder, the HTTP POST transport (with an injected fake
``urlopen``), fail-closed response validation (read-only + no approval
execution), and message rendering. No real Cogitator endpoint, no model/provider
call, no secrets on disk. The bearer token is asserted to stay in the
Authorization header and never reach the rendered output.
"""

import json

import pytest

from gateway.cogitator_decision_batch_bridge import (
    BRIDGE_PATH,
    DecisionBatchBridgeError,
    build_decision_batch_request,
    render_decision_batch_message,
    request_decision_batch,
    validate_decision_batch_response,
)

_EXPECTED_USER_INTENT = "Show the current Cogitator decision batch for review (read-only)."

_SAMPLE_RENDER = (
    "Decision Inbox\n\n"
    "Needs your decision:\n1. skills\n   Why: strong evidence\n"
    "   Action needed: approve later when approval execution is enabled.\n\n"
    "Needs research:\nNone\n\nDeferred:\nNone\n\nSkipped:\n2. caching\n   Why: already covered\n\n"
    "Status:\nRead-only display. Approval not enabled yet — I can show details, "
    "but I cannot approve or promote from Telegram yet.\n\nTry:\n- show <n>\n- refresh"
)


def _ok_response(**overrides):
    base = {
        "status": "ok",
        "requested_action": "render_decision_batch",
        "mutated": False,
        "proposal_only": True,
        "execution_authorized": False,
        "approval_required": False,
        "sample": True,
        "needs_cal_count": 1,
        "rendered_batch": _SAMPLE_RENDER,
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


class TestBuildRequest:
    def test_sample_packet_shape(self):
        packet = build_decision_batch_request()
        assert packet["source_agent"] == "hermes"
        assert packet["requested_action"] == "render_decision_batch"
        assert packet["user_intent"] == _EXPECTED_USER_INTENT
        assert packet["content"] == ""
        assert packet["approval_status"] == "draft_only"
        assert packet["risk_level"] == "low"
        # no items / detail_id supplied → empty context (sample mode).
        assert packet["context"] == {}

    def test_detail_id_forwarded(self):
        packet = build_decision_batch_request(detail_id="sample-approve")
        assert packet["context"] == {"detail_id": "sample-approve"}

    def test_items_coerced_to_plain_dicts(self):
        packet = build_decision_batch_request(
            items=[{"candidate_id": "1", "disposition": "reject", "title": "x", "reason": "y"}]
        )
        assert packet["context"]["items"] == [
            {"candidate_id": "1", "disposition": "reject", "title": "x", "reason": "y"}
        ]


class TestTransport:
    def test_posts_with_bearer_and_returns_validated(self):
        captured = {}
        result = request_decision_batch(
            base_url="https://cogitator.example",
            token="s3cr3t-token",
            detail_id="sample-approve",
            urlopen=_fake_urlopen(_ok_response(), captured),
        )
        assert captured["url"] == "https://cogitator.example" + BRIDGE_PATH
        assert captured["method"] == "POST"
        assert captured["authorization"] == "Bearer s3cr3t-token"
        assert captured["body"]["requested_action"] == "render_decision_batch"
        assert captured["body"]["context"]["detail_id"] == "sample-approve"
        assert result["status"] == "ok"

    def test_missing_base_url_fails_closed_without_posting(self):
        called = {"n": 0}

        def opener(*a, **k):
            called["n"] += 1

        with pytest.raises(DecisionBatchBridgeError) as exc:
            request_decision_batch(base_url="", token="t", urlopen=opener)
        assert exc.value.code == "BRIDGE_NOT_CONFIGURED"
        assert called["n"] == 0

    def test_missing_token_fails_closed_without_posting(self):
        called = {"n": 0}

        def opener(*a, **k):
            called["n"] += 1

        with pytest.raises(DecisionBatchBridgeError) as exc:
            request_decision_batch(base_url="https://x", token="", urlopen=opener)
        assert exc.value.code == "BRIDGE_TOKEN_MISSING"
        assert called["n"] == 0

    def test_transport_failure_fails_closed(self):
        def opener(request, timeout=None):
            raise OSError("connection refused")

        with pytest.raises(DecisionBatchBridgeError) as exc:
            request_decision_batch(base_url="https://x", token="t", urlopen=opener)
        assert exc.value.code == "BRIDGE_UNREACHABLE"

    def test_non_json_response_fails_closed(self):
        class _Bad:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"<html>not json</html>"

        with pytest.raises(DecisionBatchBridgeError) as exc:
            request_decision_batch(
                base_url="https://x", token="t",
                urlopen=lambda request, timeout=None: _Bad(),
            )
        assert exc.value.code == "BRIDGE_RESPONSE_INVALID"

    def test_token_never_appears_in_error_detail(self):
        def opener(request, timeout=None):
            raise OSError("boom")

        with pytest.raises(DecisionBatchBridgeError) as exc:
            request_decision_batch(
                base_url="https://x", token="super-secret-token", urlopen=opener
            )
        assert "super-secret-token" not in str(exc.value)
        assert "super-secret-token" not in (exc.value.detail or "")


class TestValidateResponse:
    def test_accepts_well_formed_readonly_response(self):
        assert validate_decision_batch_response(_ok_response())["status"] == "ok"

    def test_rejects_mutated_true(self):
        with pytest.raises(DecisionBatchBridgeError) as exc:
            validate_decision_batch_response(_ok_response(mutated=True))
        assert exc.value.code == "BRIDGE_MUTATION_REPORTED"

    def test_rejects_execution_authorized_true(self):
        with pytest.raises(DecisionBatchBridgeError) as exc:
            validate_decision_batch_response(_ok_response(execution_authorized=True))
        assert exc.value.code == "BRIDGE_EXECUTION_REPORTED"

    def test_rejects_status_not_ok(self):
        with pytest.raises(DecisionBatchBridgeError) as exc:
            validate_decision_batch_response(_ok_response(status="error"))
        assert exc.value.code == "BRIDGE_STATUS_NOT_OK"

    def test_rejects_action_mismatch(self):
        with pytest.raises(DecisionBatchBridgeError) as exc:
            validate_decision_batch_response(_ok_response(requested_action="save_note"))
        assert exc.value.code == "BRIDGE_ACTION_MISMATCH"

    def test_rejects_missing_rendered_batch(self):
        resp = _ok_response()
        resp.pop("rendered_batch")
        with pytest.raises(DecisionBatchBridgeError) as exc:
            validate_decision_batch_response(resp)
        assert exc.value.code == "BRIDGE_BATCH_MISSING"

    @pytest.mark.parametrize("field", ["storage_path", "promoted", "approved", "executed"])
    def test_rejects_stateful_response_fields(self, field):
        with pytest.raises(DecisionBatchBridgeError) as exc:
            validate_decision_batch_response(_ok_response(**{field: "anything"}))
        assert exc.value.code == "BRIDGE_STATEFUL_RESPONSE"

    @pytest.mark.parametrize("malformed", [None, "not-a-dict", 42, []])
    def test_rejects_malformed_response_safely(self, malformed):
        with pytest.raises(DecisionBatchBridgeError) as exc:
            validate_decision_batch_response(malformed)
        assert exc.value.code == "BRIDGE_RESPONSE_INVALID"


class TestRender:
    def test_passes_through_inbox_without_internal_bridge_text(self):
        msg = render_decision_batch_message(_ok_response(sample=False))
        # the Cal-facing inbox is passed through verbatim
        assert "Decision Inbox" in msg
        assert "Needs your decision:" in msg
        # no internal bridge/action wrapper text, no approve footer
        assert "bridge action" not in msg
        assert "draft only" not in msg
        assert "approve #" not in msg
        # the read-only assurance comes from the inbox's own Status line
        assert "Read-only display." in msg

    def test_sample_data_note_when_sample(self):
        msg = render_decision_batch_message(_ok_response(sample=True))
        assert "Sample data" in msg

    def test_no_sample_note_for_real_batch(self):
        msg = render_decision_batch_message(_ok_response(sample=False))
        assert "Sample data" not in msg

    def test_item_detail_rendered_when_present(self):
        msg = render_decision_batch_message(
            _ok_response(rendered_item="Detail #1 — skills\n\nSection: Needs your decision\nRecord id: sample-approve")
        )
        assert "Detail #1 — skills" in msg
        assert "Record id: sample-approve" in msg

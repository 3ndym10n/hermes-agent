"""Unit tests for the manual Cogitator context-checkpoint bridge helper (V0-D).

Cover the pure request builder, the HTTP POST transport (with an injected fake
``urlopen``), fail-closed response validation, and section rendering. No real
Cogitator endpoint, no model/provider call, no secrets on disk, and no
auto-rotation are involved. The bearer token is asserted to stay in the
Authorization header and never reach the rendered output.
"""

import json

import pytest

from gateway.cogitator_checkpoint_bridge import (
    BRIDGE_PATH,
    CHECKPOINT_CONTEXT_FIELDS,
    CheckpointBridgeError,
    build_checkpoint_request,
    render_checkpoint_message,
    request_context_checkpoint,
    validate_checkpoint_response,
)

_EXPECTED_USER_INTENT = (
    "Build a read-only checkpoint for the current Hermes conversation; "
    "do not rotate or inject."
)


def _checkpoint(**overrides):
    base = {
        "purpose": "demo",
        "current_state": "writing the helper",
        "active_constraints": ["read-only"],
        "decisions_made": [],
        "open_questions": [],
        "artifact_paths": [],
        "verification": [],
        "next_recommended_action": "ship it",
        "safety": {"mutation_allowed": False, "mutation_performed": False},
    }
    base.update(overrides)
    return base


def _ok_response(checkpoint=None):
    return {
        "status": "ok",
        "requested_action": "build_context_checkpoint",
        "mutated": False,
        "proposal_only": True,
        "execution_authorized": False,
        "checkpoint": checkpoint if checkpoint is not None else _checkpoint(),
    }


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
    def test_exact_packet_shape(self):
        packet = build_checkpoint_request({"current_state": "doing work"})
        assert packet["source_agent"] == "hermes"
        assert packet["requested_action"] == "build_context_checkpoint"
        assert packet["user_intent"] == _EXPECTED_USER_INTENT
        assert packet["content"] == ""
        assert packet["approval_status"] == "draft_only"
        assert packet["risk_level"] == "low"
        # context contains exactly the eight checkpoint fields, nothing else.
        assert set(packet["context"]) == set(CHECKPOINT_CONTEXT_FIELDS)
        assert packet["context"]["current_state"] == "doing work"

    def test_unknown_fields_are_dropped(self):
        packet = build_checkpoint_request(
            {"current_state": "x", "token": "leak", "secrets": "nope"}
        )
        assert set(packet["context"]) == set(CHECKPOINT_CONTEXT_FIELDS)
        assert "token" not in packet["context"]
        assert "secrets" not in packet["context"]


class TestRequestTransport:
    def test_posts_bearer_token_to_bridge_path_and_validates(self):
        captured = {}
        result = request_context_checkpoint(
            {"current_state": "writing"},
            base_url="https://cogitator.example",
            token="s3cr3t-token",
            urlopen=_fake_urlopen(_ok_response(), captured),
        )
        assert captured["url"] == "https://cogitator.example" + BRIDGE_PATH
        assert captured["method"] == "POST"
        assert captured["authorization"] == "Bearer s3cr3t-token"
        assert captured["body"]["requested_action"] == "build_context_checkpoint"
        assert captured["body"]["context"]["current_state"] == "writing"
        assert result["status"] == "ok"

    def test_missing_base_url_fails_closed_without_posting(self):
        called = {"n": 0}

        def opener(*a, **k):
            called["n"] += 1

        with pytest.raises(CheckpointBridgeError) as exc:
            request_context_checkpoint(
                {"current_state": "x"}, base_url="", token="t", urlopen=opener
            )
        assert exc.value.code == "BRIDGE_NOT_CONFIGURED"
        assert called["n"] == 0

    def test_missing_token_fails_closed_without_posting(self):
        called = {"n": 0}

        def opener(*a, **k):
            called["n"] += 1

        with pytest.raises(CheckpointBridgeError) as exc:
            request_context_checkpoint(
                {"current_state": "x"}, base_url="https://x", token="", urlopen=opener
            )
        assert exc.value.code == "BRIDGE_TOKEN_MISSING"
        assert called["n"] == 0

    def test_transport_failure_fails_closed(self):
        def opener(request, timeout=None):
            raise OSError("connection refused")

        with pytest.raises(CheckpointBridgeError) as exc:
            request_context_checkpoint(
                {"current_state": "x"},
                base_url="https://x",
                token="t",
                urlopen=opener,
            )
        assert exc.value.code == "BRIDGE_UNREACHABLE"

    def test_non_json_response_fails_closed(self):
        class _Bad:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"<html>not json</html>"

        with pytest.raises(CheckpointBridgeError) as exc:
            request_context_checkpoint(
                {"current_state": "x"},
                base_url="https://x",
                token="t",
                urlopen=lambda request, timeout=None: _Bad(),
            )
        assert exc.value.code == "BRIDGE_RESPONSE_INVALID"

    def test_token_never_appears_in_error_detail(self):
        def opener(request, timeout=None):
            raise OSError("boom")

        with pytest.raises(CheckpointBridgeError) as exc:
            request_context_checkpoint(
                {"current_state": "x"},
                base_url="https://x",
                token="super-secret-token",
                urlopen=opener,
            )
        assert "super-secret-token" not in str(exc.value)
        assert "super-secret-token" not in (exc.value.detail or "")


class TestValidateResponse:
    def test_accepts_well_formed_readonly_response(self):
        assert validate_checkpoint_response(_ok_response())["status"] == "ok"

    def test_rejects_mutated_true(self):
        resp = _ok_response()
        resp["mutated"] = True
        with pytest.raises(CheckpointBridgeError) as exc:
            validate_checkpoint_response(resp)
        assert exc.value.code == "BRIDGE_MUTATION_REPORTED"

    def test_rejects_missing_checkpoint(self):
        resp = _ok_response()
        resp.pop("checkpoint")
        with pytest.raises(CheckpointBridgeError) as exc:
            validate_checkpoint_response(resp)
        assert exc.value.code == "BRIDGE_CHECKPOINT_MISSING"

    def test_rejects_status_not_ok(self):
        resp = _ok_response()
        resp["status"] = "error"
        with pytest.raises(CheckpointBridgeError) as exc:
            validate_checkpoint_response(resp)
        assert exc.value.code == "BRIDGE_STATUS_NOT_OK"

    def test_rejects_action_mismatch(self):
        resp = _ok_response()
        resp["requested_action"] = "save_note"
        with pytest.raises(CheckpointBridgeError) as exc:
            validate_checkpoint_response(resp)
        assert exc.value.code == "BRIDGE_ACTION_MISMATCH"

    def test_rejects_checkpoint_safety_reporting_mutation(self):
        resp = _ok_response(
            checkpoint=_checkpoint(safety={"mutation_allowed": False, "mutation_performed": True})
        )
        with pytest.raises(CheckpointBridgeError) as exc:
            validate_checkpoint_response(resp)
        assert exc.value.code == "BRIDGE_CHECKPOINT_UNSAFE"

    @pytest.mark.parametrize("field", ["storage_path", "session_id", "rotated", "injected"])
    def test_rejects_stateful_response_fields(self, field):
        resp = _ok_response()
        resp[field] = "anything"
        with pytest.raises(CheckpointBridgeError) as exc:
            validate_checkpoint_response(resp)
        assert exc.value.code == "BRIDGE_STATEFUL_RESPONSE"

    @pytest.mark.parametrize("malformed", [None, "not-a-dict", 42, []])
    def test_rejects_malformed_response_safely(self, malformed):
        with pytest.raises(CheckpointBridgeError) as exc:
            validate_checkpoint_response(malformed)
        assert exc.value.code == "BRIDGE_RESPONSE_INVALID"


class TestRender:
    def test_renders_all_sections(self):
        msg = render_checkpoint_message(_ok_response())
        assert "Context Checkpoint" in msg
        assert "read-only" in msg
        assert "proposal only" in msg
        for label in (
            "Purpose:",
            "Current state:",
            "Active constraints:",
            "Decisions made:",
            "Open questions:",
            "Artifact paths:",
            "Verification:",
            "Next recommended action:",
        ):
            assert label in msg
        assert "writing the helper" in msg
        assert "- read-only" in msg  # list section rendered

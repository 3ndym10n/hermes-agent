from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway import cogitator_intake_bridge as ib


def test_bare_url_routes_to_intake_but_url_question_stays_conversational():
    bare = ib.parse_intelligent_intake(
        "https://x.com/i/status/2076300807189024873"
    )
    assert bare == ib.IntelligentIntake(
        "https://x.com/i/status/2076300807189024873",
        "bare_url",
    )
    assert ib.parse_intelligent_intake(
        "What do you think of https://x.com/i/status/2076300807189024873?"
    ) is None
    assert ib.parse_intelligent_intake(
        "https://example.com/article should we use this?"
    ) is None


def test_explicit_pasted_forwarded_and_transcript_inputs_route():
    explicit = ib.parse_intelligent_intake("intake\nA reusable workflow")
    assert explicit == ib.IntelligentIntake(
        "A reusable workflow", "pasted_text", explicit=True
    )
    forwarded = ib.parse_intelligent_intake(
        "Forwarded post content", forwarded=True
    )
    assert forwarded.input_kind == "forwarded_post"
    transcript = ib.parse_intelligent_intake(
        "intake\nTranscript body", transcript=True
    )
    assert transcript.input_kind == "transcript"
    assert ib.parse_intelligent_intake("ordinary conversation") is None


def test_event_metadata_routes_forwarded_posts_and_transcripts():
    forwarded = SimpleNamespace(
        raw_message=SimpleNamespace(forward_origin=object())
    )
    transcript = SimpleNamespace(
        raw_message=SimpleNamespace(transcript="voice transcript")
    )
    assert ib.intelligent_intake_event_flags(forwarded) == {
        "forwarded": True,
        "transcript": False,
    }
    assert ib.intelligent_intake_event_flags(transcript) == {
        "forwarded": False,
        "transcript": True,
    }


def test_dynamic_mock_attributes_are_not_trusted_as_forward_metadata():
    assert ib.intelligent_intake_event_flags(MagicMock()) == {
        "forwarded": False, "transcript": False,
    }


def test_manual_synthesis_phrase_and_packet_are_exact():
    assert ib.is_intelligent_synthesis_request("  Synthesize   brain ")
    assert ib.is_intelligent_synthesis_request("brain synthesis")
    assert not ib.is_intelligent_synthesis_request("please synthesize my brain")
    packet = ib.build_intelligent_synthesis_request()
    assert packet["requested_action"] == "build_intelligent_synthesis"
    assert packet["approval_status"] == "draft_only"


def test_prepare_and_finalize_packets_are_draft_only_and_bounded():
    intake = ib.IntelligentIntake(
        "Cal prefers one recommendation.", "cal_authored", explicit=True
    )
    prepare = ib.build_intelligent_prepare_request(
        intake,
        origin={
            "platform": "telegram",
            "chat_id": "123",
            "chat_type": "dm",
        },
    )
    assert prepare["requested_action"] == "prepare_intelligent_intake"
    assert prepare["approval_status"] == "draft_only"
    assert prepare["context"]["input_kind"] == "cal_authored"

    finalize = ib.build_intelligent_finalize_request(
        preparation_id="isbp_123",
        model_response='{"schema_version":"x"}',
        provider_path="hermes:openai-codex:oauth",
        latency_ms=12,
        provider_invoked=True,
    )
    assert finalize["requested_action"] == "finalize_intelligent_intake"
    assert finalize["approval_status"] == "draft_only"
    assert "promoted" not in finalize


@pytest.mark.parametrize(
    "provider,api_mode",
    [
        ("openrouter", "openai_chat"),
        ("openai", "openai_chat"),
        ("anthropic", "anthropic"),
        ("openai-codex", "openai_chat"),
        ("xai-oauth", "openai_chat"),
        ("gptr", "openai_chat"),
        ("research", "openai_chat"),
        ("openrouter", "codex_responses"),
    ],
)
def test_paid_or_ambiguous_routes_fail_before_invocation(provider, api_mode):
    calls = []

    result = ib.run_subscription_assessment(
        "{}",
        provider=provider,
        api_mode=api_mode,
        run_turn=lambda *_args: calls.append(True),
    )
    assert result == {
        "provider_invoked": False,
        "reason": "no_eligible_oauth_route",
    }
    assert calls == []


@pytest.mark.parametrize("provider", ["openai-codex", "xai-oauth"])
def test_oauth_route_is_single_provider_tool_free_and_has_no_fallback(provider):
    seen = {}

    def run_turn(prompt, route):
        seen["prompt"] = prompt
        seen["route"] = route
        return {"final_response": '{"ok":true}', "tools": []}

    result = ib.run_subscription_assessment(
        "assessment prompt",
        provider=provider,
        api_mode="codex_responses",
        run_turn=run_turn,
    )
    route = seen["route"]
    assert route["providers_allowed"] == [provider]
    assert route["providers_order"] == [provider]
    assert route["providers_ignored"] == ["openrouter"]
    assert route["fallback_model"] is None
    assert route["enabled_toolsets"] == []
    assert route["max_iterations"] == 1
    assert result["provider_invoked"] is True
    assert result["provider_path"] == f"hermes:{provider}:oauth"
    assert result["model_response"] == '{"ok":true}'


def test_tool_use_fails_assessment_instead_of_running_research():
    result = ib.run_subscription_assessment(
        "assessment prompt",
        provider="xai-oauth",
        api_mode="codex_responses",
        run_turn=lambda *_args: {
            "final_response": "should be rejected",
            "tools": ["web_search"],
        },
    )
    assert result["provider_invoked"] is True
    assert result["model_response"] is None


def test_provider_failure_is_sanitized_for_safe_finalization():
    def fail(*_args):
        raise RuntimeError("upstream secret-bearing error")

    result = ib.run_subscription_assessment(
        "assessment prompt",
        provider="xai-oauth",
        api_mode="codex_responses",
        run_turn=fail,
    )
    assert result["provider_invoked"] is True
    assert result["model_response"] is None
    assert "error" not in result


def test_finalize_requires_preparation_but_allows_safe_no_provider_fallback():
    with pytest.raises(ib.IntakeBridgeError) as missing_id:
        ib.build_intelligent_finalize_request(
            preparation_id="",
            model_response="{}",
            provider_path="hermes:xai-oauth:oauth",
            latency_ms=1,
            provider_invoked=True,
        )
    assert missing_id.value.code == "PREPARATION_ID_MISSING"
    fallback = ib.build_intelligent_finalize_request(
        preparation_id="isbp_1",
        model_response=None,
        provider_path="",
        latency_ms=0,
        provider_invoked=False,
    )
    assert fallback["context"]["model_response"] is None
    assert fallback["context"]["provider_invoked"] is False


def test_intelligent_response_validation_is_fail_closed():
    ready = ib._validate_intelligent_response(
        {
            "status": "ready",
            "requested_action": "prepare_intelligent_intake",
            "preparation_id": "isbp_1",
            "prompt": "{}",
            "research_performed": False,
            "promotion_performed": False,
        },
        expected_action="prepare_intelligent_intake",
    )
    assert ready["preparation_id"] == "isbp_1"
    final = ib._validate_intelligent_response(
        {
            "status": "ok",
            "requested_action": "finalize_intelligent_intake",
            "item_id": "ki_1",
            "review_id": "inote-1",
            "paid_api_used": False,
            "estimated_paid_cost": 0,
            "research_performed": False,
            "promotion_performed": False,
            "telegram_message": (
                "VALUE: high\nREVIEW ID: inote-1\n"
                "PROVIDER: hermes:openai-codex:oauth\nPAID API: no"
            ),
        },
        expected_action="finalize_intelligent_intake",
    )
    assert final["paid_api_used"] is False
    with pytest.raises(ib.IntakeBridgeError):
        ib._validate_intelligent_response(
            {**final, "research_performed": True},
            expected_action="finalize_intelligent_intake",
        )


def test_synthesis_response_is_fail_closed_and_has_cost_receipt():
    response = {
        "requested_action": "build_intelligent_synthesis",
        "status": "ok",
        "synthesis": "WHAT THE BRAIN CURRENTLY BELIEVES\n- Evidence [ki_1]",
        "provider_path": "deterministic:no-provider",
        "paid_api_used": False,
        "estimated_paid_cost": 0,
        "research_performed": False,
        "promotion_performed": False,
    }

    class FakeHTTPResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return __import__("json").dumps(response).encode()

    result = ib.request_intelligent_synthesis(
        base_url="http://127.0.0.1:9999",
        token="secret",
        urlopen=lambda *_args, **_kwargs: FakeHTTPResponse(),
    )
    rendered = ib.render_intelligent_synthesis_message(result)
    assert "PROVIDER: deterministic:no-provider" in rendered
    assert "PAID API: no (estimated cost: 0.0000)" in rendered

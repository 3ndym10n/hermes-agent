from pathlib import Path
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


@pytest.mark.parametrize(
    "fixture_name",
    [
        "unauthorized_research_reference_paste.txt",
        "unauthorized_research_reference_followup.txt",
    ],
)
def test_exact_incident_reference_dumps_route_to_bounded_intake_not_open_research(
    fixture_name,
):
    fixture = Path(__file__).parents[1] / "fixtures/gateway" / fixture_name
    pasted = fixture.read_text(encoding="utf-8")
    intake = ib.parse_intelligent_intake(pasted)
    assert intake == ib.IntelligentIntake(pasted, "pasted_text")
    assert ib.is_passive_reference_content(pasted) is True


def test_explicit_research_and_long_task_instructions_do_not_become_passive_intake():
    reference = "Mem0 graph memory notes https://example.com/source " + ("detail " * 200)
    assert ib.parse_intelligent_intake("Research this: " + reference) is None
    assert ib.parse_intelligent_intake(reference + "\nResearch this.") is None
    embedded = (
        reference
        + "\nPlease research this and compare it with our current approach.\n"
        + ("more supplied detail " * 100)
    )
    assert ib.parse_intelligent_intake(embedded) is None
    bare_embedded = (
        reference
        + "\nResearch this and compare it with our current approach.\n"
        + ("more supplied detail " * 100)
    )
    assert ib.parse_intelligent_intake(bare_embedded) is None
    task = "TASK\nImplement the approved routing fix.\n" + ("constraint " * 200)
    assert ib.parse_intelligent_intake(task) is None


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
    assert finalize["context"]["retry_count"] == 0
    assert finalize["context"]["repair_attempt"] is False


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


@pytest.mark.parametrize(
    ("provider_result", "category", "retries", "status"),
    [
        (
            {
                "failed": True,
                "error": (
                    "API call failed after 3 retries: HTTP 520 — "
                    "HTML error page (title not found)"
                ),
                "final_response": (
                    "API call failed after 3 retries: HTTP 520 — "
                    "HTML error page (title not found)"
                ),
                "tools": [],
            },
            "provider_http_error",
            3,
            520,
        ),
        ({"final_response": "<html>upstream failure</html>", "tools": []},
         "provider_error", 0, 0),
        ({"final_response": "", "tools": []}, "empty_response", 0, 0),
    ],
)
def test_provider_failure_results_never_become_assessment_text(
    provider_result, category, retries, status
):
    result = ib.run_subscription_assessment(
        "assessment prompt",
        provider="openai-codex",
        api_mode="codex_responses",
        run_turn=lambda *_args: provider_result,
    )

    assert result["processing_status"] == "failed_reasoning"
    assert result["model_response"] is None
    assert result["failure_category"] == category
    assert result["retry_count"] == retries
    assert result["http_status"] == status
    assert "html" not in str(result).lower()


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
            "final_state": "complete_assessment",
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
    with pytest.raises(ib.IntakeBridgeError) as state_error:
        ib._validate_intelligent_response(
            {**final, "final_state": "failed_reasoning"},
            expected_action="finalize_intelligent_intake",
        )
    assert state_error.value.code == "BRIDGE_RESPONSE_INVALID"
    rejected = ib._validate_intelligent_response(
        {
            "status": "rejected",
            "final_state": "failed_reasoning",
            "requested_action": "finalize_intelligent_intake",
            "research_performed": False,
            "promotion_performed": False,
        },
        expected_action="finalize_intelligent_intake",
    )
    assert rejected["final_state"] == "failed_reasoning"
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


def test_review_and_outcome_commands_are_explicit_and_strict():
    approved = ib.parse_intelligent_review_command(
        "approve knowledge inote-12"
    )
    assert approved == ib.IntelligentReviewCommand("inote-12", "approve", "")
    related = ib.parse_intelligent_review_command("knowledge related inote-12")
    assert related.action == "view_related"
    for text in (
        "show assessment for inote-384",
        "view inote-384",
        "review details inote-384",
        "Show me the full assessment for inote-384.",
    ):
        detail = ib.parse_intelligent_review_command(text)
        assert detail == ib.IntelligentReviewCommand(
            "inote-384", "view_related", "assessment"
        )
    assert ib.parse_intelligent_review_command("view inote-0").error == (
        "invalid_review_command"
    )
    lifecycle = ib.parse_intelligent_review_command(
        "show lifecycle inote-384"
    )
    assert lifecycle == ib.IntelligentReviewCommand(
        "inote-384", "view_related", ""
    )
    candidates = ib.parse_intelligent_review_command(
        "List review candidates."
    )
    assert candidates == ib.IntelligentReviewCommand(
        "", "list_candidates", ""
    )
    merged = ib.parse_intelligent_review_command(
        "knowledge merge inote-12 Merge the cost lesson"
    )
    assert merged.action == "update_or_merge"
    assert merged.payload == "Merge the cost lesson"
    assert ib.parse_intelligent_review_command(
        "knowledge merge inote-12"
    ).error == "payload_required"
    assert ib.parse_intelligent_review_command("approve this idea") is None
    assert ib.parse_intelligent_review_command("ordinary approval discussion") is None

    useful = ib.parse_intelligent_outcome_command(
        "knowledge outcome useful Reduced provider cost"
    )
    assert useful == ib.IntelligentOutcomeCommand(
        "useful", "", "Reduced provider cost"
    )
    corrected = ib.parse_intelligent_outcome_command(
        "knowledge outcome corrected ki_0123456789abcdef01234567 "
        "Use premium reasoning only for irreversible gates"
    )
    assert corrected.outcome == "corrected"
    assert corrected.item_id == "ki_0123456789abcdef01234567"
    assert ib.parse_intelligent_outcome_command(
        "knowledge outcome corrected"
    ).error == "details_required"
    assert ib.parse_intelligent_outcome_command(
        "knowledge outcome useful ki_not-a-real-item Saved labour"
    ).error == "invalid_item_id"
    assert ib.parse_intelligent_outcome_command("the outcome was useful") is None


def test_persisted_assessment_detail_is_validated_and_rendered_without_model():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return __import__("json").dumps(payload).encode()

    payload = {
        "requested_action": "review_intelligent_knowledge",
        "status": "ok",
        "action": "view_related",
        "item_id": "ki_20b43d376a74f9ece633d71f",
        "review_id": "inote-384",
        "lifecycle_state": "review_candidate",
        "markdown_path": (
            "storage/notes/knowledge/ki_20b43d376a74f9ece633d71f.md"
        ),
        "relations": [],
        "mutation_performed": False,
        "paid_api_used": False,
        "research_performed": False,
        "assessment_detail": {
            "value": "medium",
            "content_type": "operating_playbook",
            "core_idea": "Automate bounded workflows with deterministic guardrails.",
            "evidence_quality": "weak",
            "confidence": 0.74,
            "why_it_matters": "It identifies practical high-ROI agent work.",
            "related_memory_ids": [],
            "relation_types": [],
            "duplication_status": "new",
            "reinforcement": [],
            "refinement": [],
            "contradictions": [],
            "what_this_changes": "Use deterministic code around model judgment.",
            "recommended_action": "Keep as latent operating context.",
            "smallest_test": "Map one workflow and measure it.",
            "do_not_do": None,
            "lifecycle": "review_candidate",
            "knowledge_item_id": "ki_20b43d376a74f9ece633d71f",
            "markdown_path": (
                "storage/notes/knowledge/ki_20b43d376a74f9ece633d71f.md"
            ),
            "provider_path": "hermes:openai-codex:oauth",
            "paid_api_used": False,
            "estimated_paid_cost": 0,
        },
    }
    command = ib.IntelligentReviewCommand(
        "inote-384", "view_related", "assessment"
    )
    result = ib.request_intelligent_review(
        base_url="http://bridge",
        token="secret",
        command=command,
        urlopen=lambda *_args, **_kwargs: Response(),
    )
    rendered = ib.render_intelligent_review_message(result)

    assert "VALUE: medium" in rendered
    assert "TYPE: operating_playbook" in rendered
    assert "KNOWLEDGE ITEM: ki_20b43d376a74f9ece633d71f" in rendered
    assert "PROVIDER: hermes:openai-codex:oauth" in rendered
    assert "PAID API: no (estimated cost: 0.0000)" in rendered
    assert "DO NOT: Not available in persisted assessment" in rendered
    assert "RELATED KNOWLEDGE: Not available in persisted assessment" in rendered

    payload["review_id"] = "inote-999"
    with pytest.raises(ib.IntakeBridgeError):
        ib.request_intelligent_review(
            base_url="http://bridge",
            token="secret",
            command=command,
            urlopen=lambda *_args, **_kwargs: Response(),
        )
    payload["review_id"] = "inote-384"
    payload["assessment_detail"]["knowledge_item_id"] = (
        "ki_ffffffffffffffffffffffff"
    )
    with pytest.raises(ib.IntakeBridgeError):
        ib.request_intelligent_review(
            base_url="http://bridge",
            token="secret",
            command=command,
            urlopen=lambda *_args, **_kwargs: Response(),
        )


def test_candidate_list_is_validated_and_rendered_without_provider():
    command = ib.IntelligentReviewCommand(action="list_candidates")
    packet = ib.build_intelligent_review_request(command)
    assert packet["approval_status"] == "draft_only"
    assert packet["context"] == {
        "review_id": "",
        "action": "list_candidates",
        "payload": "",
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return __import__("json").dumps(payload).encode()

    payload = {
        "requested_action": "review_intelligent_knowledge",
        "status": "ok",
        "action": "list_candidates",
        "candidates": [
            {
                "review_id": "inote-384",
                "title": "Stored assessment",
                "disposition": "review",
                "reason": "Awaiting Cal",
            }
        ],
        "mutation_performed": False,
        "paid_api_used": False,
        "research_performed": False,
    }
    result = ib.request_intelligent_review(
        base_url="http://bridge",
        token="secret",
        command=command,
        urlopen=lambda *_args, **_kwargs: Response(),
    )
    rendered = ib.render_intelligent_review_message(result)
    assert "inote-384 | review | Stored assessment" in rendered
    assert "PAID API: no" in rendered


def test_lifecycle_view_renders_canonical_stored_state():
    rendered = ib.render_intelligent_review_message(
        {
            "status": "ok",
            "item_id": "ki_20b43d376a74f9ece633d71f",
            "review_id": "inote-384",
            "lifecycle_state": "review_candidate",
            "markdown_path": (
                "storage/notes/knowledge/ki_20b43d376a74f9ece633d71f.md"
            ),
        }
    )

    assert "LIFECYCLE: review_candidate" in rendered
    assert "KNOWLEDGE ITEM: ki_20b43d376a74f9ece633d71f" in rendered
    assert "PAID API: no" in rendered


def test_action_success_receipts_must_echo_durable_results():
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return __import__("json").dumps(self.payload).encode()

    command = ib.IntelligentReviewCommand("inote-2", "approve")
    review = {
        "requested_action": "review_intelligent_knowledge",
        "status": "applied",
        "action": "approve",
        "item_id": "ki_0123456789abcdef01234567",
        "lifecycle_state": "promoted",
        "promoted_path": "storage/promoted/ai-cost.md",
        "paid_api_used": False,
        "research_performed": False,
    }
    accepted = ib.request_intelligent_review(
        base_url="http://bridge",
        token="secret",
        command=command,
        urlopen=lambda *_a, **_kw: Response(review),
    )
    assert "Promoted Markdown: storage/promoted/ai-cost.md" in (
        ib.render_intelligent_review_message(accepted)
    )
    with pytest.raises(ib.IntakeBridgeError) as wrong_review:
        ib.request_intelligent_review(
            base_url="http://bridge", token="secret", command=command,
            urlopen=lambda *_a, **_kw: Response(
                {**review, "status": "candidate_created"}
            ),
        )
    assert wrong_review.value.code == "BRIDGE_RESPONSE_INVALID"
    with pytest.raises(ib.IntakeBridgeError) as unsafe_blocked:
        ib.request_intelligent_review(
            base_url="http://bridge",
            token="secret",
            command=command,
            urlopen=lambda *_a, **_kw: Response(
                {
                    **review, "status": "blocked", "reason": "no",
                    "mutation_performed": True,
                }
            ),
        )
    assert unsafe_blocked.value.code == "BRIDGE_RESPONSE_INVALID"

    outcome = {
        "requested_action": "record_intelligent_knowledge_outcome",
        "status": "recorded",
        "item_id": "ki_0123456789abcdef01234567",
        "outcome": "useful",
        "markdown_path": "",
        "original_preserved": True,
        "paid_api_used": False,
        "research_performed": False,
    }
    with pytest.raises(ib.IntakeBridgeError) as missing_durable_path:
        ib.request_intelligent_outcome(
            base_url="http://bridge",
            token="secret",
            command=ib.IntelligentOutcomeCommand("useful"),
            retrieval_receipt_id="isbr_12345678901234567890",
            item_id="ki_0123456789abcdef01234567",
            urlopen=lambda *_a, **_kw: Response(outcome),
        )
    assert missing_durable_path.value.code == "BRIDGE_RESPONSE_INVALID"
    with pytest.raises(ib.IntakeBridgeError) as unsafe_outcome_block:
        ib.request_intelligent_outcome(
            base_url="http://bridge",
            token="secret",
            command=ib.IntelligentOutcomeCommand("useful"),
            retrieval_receipt_id="isbr_12345678901234567890",
            item_id="ki_0123456789abcdef01234567",
            urlopen=lambda *_a, **_kw: Response(
                {
                    **outcome,
                    "status": "blocked",
                    "reason": "no",
                    "mutation_performed": True,
                }
            ),
        )
    assert unsafe_outcome_block.value.code == "BRIDGE_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "text,platform,proxy,internal,expected",
    [
        ("Should we use premium reasoning for this decision?", "telegram", False, False, True),
        (
            "What is our policy for keeping our Hermes fork updated without "
            "breaking Cogitator or wasting development tokens?",
            "telegram", False, False, True,
        ),
        ("Plan five real provider-cost experiments for this workflow", "telegram", False, False, True),
        ("hello there friend", "telegram", False, False, False),
        ("how are you", "telegram", False, False, False),
        ("How are you doing today?", "telegram", False, False, False),
        ("What now?", "telegram", False, False, False),
        ("/status please show the current runtime", "telegram", False, False, False),
        ("intake\nA useful operating method", "telegram", False, False, False),
        ("synthesize brain", "telegram", False, False, False),
        ("approve knowledge inote-1", "telegram", False, False, False),
        ("knowledge outcome useful", "telegram", False, False, False),
        ("https://example.com/article", "telegram", False, False, False),
        ("Should we use premium reasoning for this decision?", "discord", False, False, False),
        ("Should we use premium reasoning for this decision?", "telegram", True, False, False),
        ("Should we use premium reasoning for this decision?", "telegram", False, True, False),
    ],
)
def test_targeted_retrieval_eligibility_is_narrow(
    text, platform, proxy, internal, expected
):
    assert ib.is_intelligent_retrieval_eligible(
        text, platform=platform, proxy=proxy, internal=internal
    ) is expected


def test_review_retrieval_and_outcome_packets_preserve_authority_boundary():
    review = ib.build_intelligent_review_request(
        ib.IntelligentReviewCommand("inote-2", "approve")
    )
    assert review["approval_status"] == "approved"
    assert set(review["context"]) == {"review_id", "action", "payload"}
    assert "actor" not in review["context"]
    assert "confirm" not in review["context"]
    related = ib.build_intelligent_review_request(
        ib.IntelligentReviewCommand("inote-2", "view_related")
    )
    assert related["approval_status"] == "draft_only"

    retrieval = ib.build_intelligent_retrieval_request(
        "Choose provider cost for this task"
    )
    assert retrieval["approval_status"] == "draft_only"
    assert set(retrieval["context"]) == {"task_description"}

    usage = ib.build_intelligent_usage_request(
        retrieval_receipt_id="isbr_12345678901234567890",
        response_task_id="isbt_0123456789abcdef01234567",
        response_text=(
            "Use [ki_0123456789abcdef01234567]"
            "(storage/knowledge/ai-cost.md)."
        ),
        used_item_ids=["ki_0123456789abcdef01234567"],
        other_context_sources=["tool:session_search"],
        provider_path="openai-codex:gpt-5",
        paid_web_research_api_used=False,
    )
    assert usage["approval_status"] == "draft_only"
    assert usage["requested_action"] == "record_intelligent_response_usage"
    assert "approval" not in usage["context"]
    outcome = ib.build_intelligent_outcome_request(
        ib.IntelligentOutcomeCommand("useful", details="Saved labour"),
        retrieval_receipt_id="isbr_12345678901234567890",
        item_id="ki_0123456789abcdef01234567",
    )
    assert outcome["approval_status"] == "approved"
    assert set(outcome["context"]) == {
        "retrieval_receipt_id", "item_id", "outcome", "details",
    }
    assert "citations" not in outcome["context"]


def test_retrieval_response_requires_opaque_receipt_and_never_accepts_server_proof():
    base = {
        "requested_action": "retrieve_intelligent_knowledge",
        "status": "ok",
        "records": [
            {
                "item_id": "ki_0123456789abcdef01234567",
                "label": "KNOWLEDGE",
                "title": "AI cost discipline",
                "core_idea": "Use premium reasoning at decision gates.",
                "why_relevant": "It affects provider choice.",
                "plan_delta": "Measure five tasks.",
                "citation": "storage/knowledge/ai-cost.md",
                "lifecycle_state": "promoted",
            }
        ],
        "citations": ["storage/knowledge/ai-cost.md"],
        "retrieval_receipt_id": "isbr_12345678901234567890",
        "paid_api_used": False,
        "research_performed": False,
    }

    class FakeHTTPResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return __import__("json").dumps(self.payload).encode()

    result = ib.request_intelligent_retrieval(
        base_url="http://127.0.0.1:9999",
        token="secret",
        task_description="Choose provider cost for this task",
        urlopen=lambda *_args, **_kwargs: FakeHTTPResponse(base),
    )
    assert "receipt" not in result
    rendered = ib.render_intelligent_retrieval_context(result)
    assert "ki_0123456789abcdef01234567" in rendered
    assert "storage/knowledge/ai-cost.md" in rendered
    assert "COGITATOR USED: <item_id>" in rendered
    assert "PROPOSED REFINEMENT:" in rendered

    with pytest.raises(ib.IntakeBridgeError) as leaked:
        ib.request_intelligent_retrieval(
            base_url="http://127.0.0.1:9999",
            token="secret",
            task_description="Choose provider cost for this task",
            urlopen=lambda *_args, **_kwargs: FakeHTTPResponse(
                {**base, "receipt": {"used_item_ids": ["forged"]}}
            ),
        )
    assert leaked.value.code == "BRIDGE_RESPONSE_INVALID"

    with pytest.raises(ib.IntakeBridgeError) as paid:
        ib.request_intelligent_retrieval(
            base_url="http://127.0.0.1:9999",
            token="secret",
            task_description="Choose provider cost for this task",
            urlopen=lambda *_args, **_kwargs: FakeHTTPResponse(
                {**base, "paid_api_used": True}
            ),
        )
    assert paid.value.code == "BRIDGE_STATEFUL_RESPONSE"



def test_response_usage_client_validates_provenance_and_no_promotion():
    payload = {
        "requested_action": "record_intelligent_response_usage",
        "status": "recorded",
        "response_task_id": "isbt_0123456789abcdef01234567",
        "provenance_markdown_path": "Outcomes/provenance.md",
        "retrieved_item_ids": ["ki_0123456789abcdef01234567"],
        "used_item_ids": ["ki_0123456789abcdef01234567"],
        "citations": ["storage/knowledge/ai-cost.md"],
        "candidate_review_id": "inote-8",
        "promotion_performed": False,
        "paid_api_used": False,
        "research_performed": False,
    }

    class FakeHTTPResponse:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return __import__("json").dumps(self.value).encode()

    kwargs = {
        "base_url": "http://127.0.0.1:9999",
        "token": "secret",
        "retrieval_receipt_id": "isbr_12345678901234567890",
        "response_task_id": "isbt_0123456789abcdef01234567",
        "response_text": (
            "Use [ki_0123456789abcdef01234567]"
            "(storage/knowledge/ai-cost.md)."
        ),
        "used_item_ids": [],
        "retrieved_item_ids": ["ki_0123456789abcdef01234567"],
        "other_context_sources": ["tool:session_search"],
        "provider_path": "openai-codex:gpt-5",
        "paid_web_research_api_used": False,
    }
    result = ib.request_intelligent_response_usage(
        **kwargs,
        urlopen=lambda *_args, **_kwargs: FakeHTTPResponse(payload),
    )
    assert result["candidate_review_id"] == "inote-8"

    with pytest.raises(ib.IntakeBridgeError) as forged:
        ib.request_intelligent_response_usage(
            **kwargs,
            urlopen=lambda *_args, **_kwargs: FakeHTTPResponse(
                {
                    **payload,
                    "used_item_ids": ["ki_ffffffffffffffffffffffff"],
                }
            ),
        )
    assert forged.value.code == "BRIDGE_RESPONSE_INVALID"

    with pytest.raises(ib.IntakeBridgeError) as promoted:
        ib.request_intelligent_response_usage(
            **kwargs,
            urlopen=lambda *_args, **_kwargs: FakeHTTPResponse(
                {**payload, "promotion_performed": True}
            ),
        )
    assert promoted.value.code == "BRIDGE_RESPONSE_INVALID"

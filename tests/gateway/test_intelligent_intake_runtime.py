import pytest

from gateway import cogitator_intake_bridge as ib
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.slash_commands import GatewaySlashCommandsMixin


def _event(text="intake\nCal-authored workflow"):
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="u1",
            chat_id="c1",
            user_name="Cal",
            chat_type="dm",
        ),
        message_id="m1",
    )


class RuntimeHarness(GatewaySlashCommandsMixin):
    def __init__(self):
        self.cleaned = []
        self.agent_cache = {"normal": object()}
        self.session_history = ["prior normal turn"]

    def _resolve_session_agent_runtime(self, **kwargs):
        self.runtime_resolution_kwargs = kwargs
        return "gpt-5", {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
        }

    def _resolve_turn_agent_config(self, _prompt, model, runtime):
        return {"model": model, "runtime": dict(runtime)}

    def _resolve_session_reasoning_config(self, **_kwargs):
        return {"effort": "medium"}

    def _cleanup_agent_resources(self, agent):
        self.cleaned.append(agent)

    async def _run_in_executor_with_context(self, func, *args):
        return func(*args)


@pytest.mark.asyncio
async def test_isolated_assessment_uses_only_pinned_oauth_and_no_session_state(
    monkeypatch,
):
    import gateway.run as gateway_run
    import run_agent

    created = []

    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)

        def run_conversation(self, **kwargs):
            self.run_kwargs = kwargs
            return {"final_response": '{"schema_version":"v1"}', "tools": []}

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    harness = RuntimeHarness()
    cache_before = dict(harness.agent_cache)
    history_before = list(harness.session_history)

    result = await harness._run_isolated_intelligent_assessment(
        "strict assessment prompt", _event()
    )

    assert result["provider_invoked"] is True
    assert result["provider_path"] == "hermes:openai-codex:oauth"
    assert harness.runtime_resolution_kwargs["cache_result"] is False
    assert len(created) == 1
    agent = created[0]
    assert agent.kwargs["max_iterations"] == 1
    assert agent.kwargs["enabled_toolsets"] == []
    assert set(agent.kwargs["disabled_toolsets"]) >= {
        "web", "research", "delegate", "browser",
    }
    assert agent.kwargs["providers_allowed"] == ["openai-codex"]
    assert agent.kwargs["providers_order"] == ["openai-codex"]
    assert agent.kwargs["providers_ignored"] == ["openrouter"]
    assert agent.kwargs["fallback_model"] is None
    assert agent.kwargs["session_db"] is None
    assert agent.kwargs["skip_context_files"] is True
    assert agent.kwargs["skip_memory"] is True
    assert agent.run_kwargs["conversation_history"] == []
    assert harness.cleaned == [agent]
    assert harness.agent_cache == cache_before
    assert harness.session_history == history_before


@pytest.mark.asyncio
async def test_ineligible_runtime_abstains_before_agent_construction(monkeypatch):
    import gateway.run as gateway_run
    import run_agent

    class ForbiddenAgent:
        def __init__(self, **_kwargs):
            raise AssertionError("paid or ambiguous provider must not run")

    harness = RuntimeHarness()
    harness._resolve_session_agent_runtime = lambda **_kwargs: (
        "paid-model",
        {"provider": "openrouter", "api_mode": "openai_chat"},
    )
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(run_agent, "AIAgent", ForbiddenAgent)

    result = await harness._run_isolated_intelligent_assessment(
        "strict assessment prompt", _event()
    )

    assert result == {
        "provider_invoked": False,
        "reason": "no_eligible_oauth_route",
    }
    assert harness.cleaned == []


@pytest.mark.asyncio
async def test_intake_handler_orders_prepare_reason_finalize_and_renders_receipt(
    monkeypatch,
):
    import gateway.cogitator_intake_bridge as bridge

    sequence = []
    final = {
        "status": "ok",
        "telegram_message": (
            "VALUE: high\nTYPE: operating_playbook\nREVIEW ID: inote-1\n"
            "PROVIDER: hermes:openai-codex:oauth\nPAID API: no"
        ),
    }
    harness = RuntimeHarness()
    harness._intake_config = lambda: (True, "http://bridge", "")
    harness._intake_origin = lambda _event: {
        "platform": "telegram",
        "chat_id": "c1",
        "chat_type": "dm",
    }

    def prepare(**_kwargs):
        sequence.append("prepare")
        return {"status": "ready", "preparation_id": "isbp_1", "prompt": "{}"}

    async def reason(_prompt, _event):
        sequence.append("reason")
        return {
            "provider_invoked": True,
            "provider_path": "hermes:openai-codex:oauth",
            "latency_ms": 1,
            "model_response": "{}",
        }

    def finalize(**kwargs):
        sequence.append("finalize")
        assert kwargs["preparation_id"] == "isbp_1"
        return final

    monkeypatch.setenv(bridge.TOKEN_ENV, "secret")
    monkeypatch.setattr(bridge, "request_intelligent_prepare", prepare)
    monkeypatch.setattr(bridge, "request_intelligent_finalize", finalize)
    harness._run_isolated_intelligent_assessment = reason
    intake = ib.IntelligentIntake("Cal prefers one action", "cal_authored", True)

    result = await harness.handle_intelligent_intake(_event(), intake)

    assert sequence == ["prepare", "reason", "finalize"]
    assert result == final["telegram_message"]


def _retrieval_response():
    return {
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
            }
        ],
        "citations": ["storage/knowledge/ai-cost.md"],
        "retrieval_receipt_id": "isbr_12345678901234567890",
        "paid_api_used": False,
        "research_performed": False,
    }


@pytest.mark.asyncio
async def test_targeted_retrieval_stores_only_opaque_session_handle(monkeypatch):
    import gateway.cogitator_intake_bridge as bridge

    harness = RuntimeHarness()
    harness._intake_config = lambda: (True, "http://bridge", "")
    monkeypatch.setenv(bridge.TOKEN_ENV, "secret")
    monkeypatch.setattr(
        bridge,
        "request_intelligent_retrieval",
        lambda **_kwargs: _retrieval_response(),
    )

    rendered = await harness.retrieve_intelligent_task_context(
        _event("Should we use premium reasoning for this decision?"),
        "Should we use premium reasoning for this decision?",
    )

    assert "storage/knowledge/ai-cost.md" in rendered
    state = harness._active_intelligent_retrieval_context(
        _event("Should we use premium reasoning for this decision?")
    )
    assert state == {
        "retrieval_receipt_id": "isbr_12345678901234567890",
        "item_ids": ["ki_0123456789abcdef01234567"],
        "ts": state["ts"],
    }
    assert "citations" not in state
    assert "records" not in state


@pytest.mark.asyncio
async def test_review_and_outcome_handlers_use_explicit_authority(monkeypatch):
    import gateway.cogitator_intake_bridge as bridge

    harness = RuntimeHarness()
    harness._intake_config = lambda: (True, "http://bridge", "")
    monkeypatch.setenv(bridge.TOKEN_ENV, "secret")
    event = _event("approve knowledge inote-7")
    seen = {}

    def review(**kwargs):
        seen["review"] = kwargs
        return {
            "requested_action": "review_intelligent_knowledge",
            "status": "applied",
            "promoted_path": "storage/promoted/ai-cost.md",
            "paid_api_used": False,
            "research_performed": False,
        }

    monkeypatch.setattr(bridge, "request_intelligent_review", review)
    review_result = await harness.handle_intelligent_review(
        event, bridge.parse_intelligent_review_command(event.text)
    )
    assert "Promoted Markdown" in review_result
    assert seen["review"]["command"].action == "approve"

    harness._set_intelligent_retrieval_context(event, _retrieval_response())

    def outcome(**kwargs):
        seen["outcome"] = kwargs
        return {
            "requested_action": "record_intelligent_knowledge_outcome",
            "status": "recorded",
            "markdown_path": "storage/notes/outcome.md",
            "paid_api_used": False,
            "research_performed": False,
        }

    monkeypatch.setattr(bridge, "request_intelligent_outcome", outcome)
    command = bridge.parse_intelligent_outcome_command(
        "knowledge outcome useful Reduced cost"
    )
    outcome_result = await harness.handle_intelligent_outcome(event, command)
    assert "Knowledge outcome recorded" in outcome_result
    assert seen["outcome"]["retrieval_receipt_id"] == (
        "isbr_12345678901234567890"
    )
    assert seen["outcome"]["item_id"] == "ki_0123456789abcdef01234567"


@pytest.mark.asyncio
async def test_retrieval_handle_expires_and_wrong_item_fails_before_bridge(monkeypatch):
    import gateway.cogitator_intake_bridge as bridge
    import gateway.slash_commands as slash_commands

    now = [100.0]
    monkeypatch.setattr(slash_commands.time, "time", lambda: now[0])
    harness = RuntimeHarness()
    event = _event("knowledge outcome useful")
    harness._set_intelligent_retrieval_context(event, _retrieval_response())
    assert harness._active_intelligent_retrieval_context(event)
    now[0] += harness._INTELLIGENT_RETRIEVAL_TTL_SECONDS
    assert harness._active_intelligent_retrieval_context(event) is None

    harness._set_intelligent_retrieval_context(event, _retrieval_response())
    command = bridge.parse_intelligent_outcome_command(
        "knowledge outcome useful ki_ffffffffffffffffffffffff"
    )
    monkeypatch.setattr(
        bridge,
        "request_intelligent_outcome",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("wrong item must fail before bridge")
        ),
    )
    result = await harness.handle_intelligent_outcome(event, command)
    assert result == "That item was not cited in the latest targeted retrieval."

    malformed = bridge.parse_intelligent_outcome_command(
        "knowledge outcome useful ki_not-a-real-item Saved labour"
    )
    result = await harness.handle_intelligent_outcome(event, malformed)
    assert result.startswith("Knowledge outcome format:")

"""Regression tests: /yolo and /verbose dispatch mid-agent-run.

When an agent is running, the gateway's running-agent guard rejects most
slash commands with "⏳ Agent is running — /{cmd} can't run mid-turn"
(PR #12334). A small allowlist bypasses that and actually dispatches:

  * /yolo — toggles the session yolo flag; useful to pre-approve a
    pending approval prompt without waiting for the agent to finish.
  * /verbose — cycles the per-platform tool-progress display mode;
    affects the ongoing stream.

Commands whose handlers say "takes effect on next message" stay on the
catch-all by design:

  * /fast — writes config.yaml only
  * /reasoning — writes config.yaml only

These tests lock in both behaviors so the allowlist doesn't silently
grow or shrink.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    """Minimal GatewayRunner with an active running agent for this session."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._service_tier = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()

    # Simulate agent actively running for this session so the guard fires.
    # Note: the stale-eviction branch calls agent.get_activity_summary() and
    # compares seconds_since_activity against HERMES_AGENT_TIMEOUT. Return a
    # dict with recent activity so the eviction path doesn't clear our
    # fake running agent before the toggle guard runs.
    import time
    sk = build_session_key(_make_source())
    agent_mock = MagicMock()
    agent_mock.get_activity_summary.return_value = {
        "seconds_since_activity": 0.0,
        "last_activity_desc": "api_call",
        "api_call_count": 1,
        "max_iterations": 60,
    }
    runner._running_agents[sk] = agent_mock
    runner._running_agents_ts[sk] = time.time()
    return runner


@pytest.mark.asyncio
async def test_yolo_dispatches_mid_run(monkeypatch):
    """/yolo mid-run must dispatch to its handler, not hit the catch-all."""
    runner = _make_runner()
    runner._handle_yolo_command = AsyncMock(return_value="⚡ YOLO mode **ON** for this session")

    result = await runner._handle_message(_make_event("/yolo"))

    runner._handle_yolo_command.assert_awaited_once()
    assert result == "⚡ YOLO mode **ON** for this session"
    assert "can't run mid-turn" not in (result or "")


@pytest.mark.asyncio
async def test_verbose_dispatches_mid_run(monkeypatch):
    """/verbose mid-run must dispatch to its handler, not hit the catch-all."""
    runner = _make_runner()
    runner._handle_verbose_command = AsyncMock(return_value="tool progress: new")

    result = await runner._handle_message(_make_event("/verbose"))

    runner._handle_verbose_command.assert_awaited_once()
    assert result == "tool progress: new"
    assert "can't run mid-turn" not in (result or "")


@pytest.mark.asyncio
async def test_fast_rejected_mid_run():
    """/fast mid-run must hit the busy catch-all — config-only, next message."""
    runner = _make_runner()
    runner._handle_fast_command = AsyncMock(
        side_effect=AssertionError("/fast should not dispatch mid-run")
    )

    result = await runner._handle_message(_make_event("/fast"))

    runner._handle_fast_command.assert_not_awaited()
    assert result is not None
    assert "can't run mid-turn" in result
    assert "/fast" in result


@pytest.mark.asyncio
async def test_reasoning_rejected_mid_run():
    """/reasoning mid-run must hit the busy catch-all — config-only, next message."""
    runner = _make_runner()
    runner._handle_reasoning_command = AsyncMock(
        side_effect=AssertionError("/reasoning should not dispatch mid-run")
    )

    result = await runner._handle_message(_make_event("/reasoning high"))

    runner._handle_reasoning_command.assert_not_awaited()
    assert result is not None
    assert "can't run mid-turn" in result
    assert "/reasoning" in result


@pytest.mark.asyncio
async def test_btw_dispatches_mid_run():
    """/btw mid-run must dispatch to /background's handler, not hit the catch-all.

    /btw is an alias of /background (see hermes_cli/commands.py). Typing
    /btw mid-turn must spawn a parallel background task — that's the whole
    point of the command. Before the mid-turn bypass was added for
    /background, /btw fell through to the "Agent is running — wait or
    /stop first" catch-all, making it useless in exactly the scenario it
    was designed for. The alias and the bypass together make it work.
    """
    runner = _make_runner()
    runner._handle_background_command = AsyncMock(
        return_value='🚀 Background task started: "what module owns titles?"'
    )

    result = await runner._handle_message(_make_event("/btw what module owns titles?"))

    runner._handle_background_command.assert_awaited_once()
    assert result is not None
    assert "can't run mid-turn" not in result


@pytest.mark.asyncio
async def test_intelligent_bare_url_dispatches_at_gateway_boundary():
    runner = _make_runner()
    runner._running_agents.clear()
    runner._running_agents_ts.clear()
    runner.handle_intelligent_intake = AsyncMock(return_value="intelligent receipt")

    result = await runner._handle_message(
        _make_event("https://x.com/i/status/2076300807189024873")
    )

    runner.handle_intelligent_intake.assert_awaited_once()
    intake = runner.handle_intelligent_intake.await_args.args[1]
    assert intake.input_kind == "bare_url"
    assert result == "intelligent receipt"


@pytest.mark.asyncio
async def test_passive_reference_dispatches_to_intelligent_intake_without_agent_tools():
    runner = _make_runner()
    runner._running_agents.clear()
    runner._running_agents_ts.clear()
    runner.handle_intelligent_intake = AsyncMock(return_value="reference assessment")
    pasted = (
        "Mem0 graph memory and semantic triplets. Source: https://example.com/mem0\n"
        + "Reference excerpt about retrieval, provenance, and graph edges. " * 40
    )

    result = await runner._handle_message(_make_event(pasted))

    runner.handle_intelligent_intake.assert_awaited_once()
    intake = runner.handle_intelligent_intake.await_args.args[1]
    assert intake.input_kind == "pasted_text"
    assert result == "reference assessment"


@pytest.mark.asyncio
async def test_active_task_queues_intelligent_intake_instead_of_dispatching_it():
    runner = _make_runner()
    runner.handle_intelligent_intake = AsyncMock(
        side_effect=AssertionError("active intake must wait for the next turn")
    )
    runner._handle_active_session_busy_message = AsyncMock(return_value=True)

    result = await runner._handle_message(
        _make_event("https://x.com/i/status/2076300807189024873")
    )

    assert result == ""
    runner.handle_intelligent_intake.assert_not_awaited()
    runner._handle_active_session_busy_message.assert_awaited_once()
    assert runner._handle_active_session_busy_message.await_args.kwargs == {
        "force_queue": True
    }


@pytest.mark.asyncio
async def test_intelligent_forwarded_post_dispatches_at_gateway_boundary():
    runner = _make_runner()
    runner._running_agents.clear()
    runner._running_agents_ts.clear()
    runner.handle_intelligent_intake = AsyncMock(return_value="forwarded receipt")
    event = _make_event("Forwarded operating playbook")
    event.raw_message = SimpleNamespace(forward_origin=object())

    result = await runner._handle_message(event)

    intake = runner.handle_intelligent_intake.await_args.args[1]
    assert intake.input_kind == "forwarded_post"
    assert result == "forwarded receipt"


@pytest.mark.asyncio
async def test_url_plus_question_does_not_dispatch_intelligent_intake():
    runner = _make_runner()
    runner.handle_intelligent_intake = AsyncMock(
        side_effect=AssertionError("question must stay conversational")
    )

    await runner._handle_message(
        _make_event(
            "What do you think of "
            "https://x.com/i/status/2076300807189024873?"
        )
    )

    runner.handle_intelligent_intake.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_synthesis_dispatches_at_gateway_boundary():
    runner = _make_runner()
    runner.handle_intelligent_synthesis = AsyncMock(return_value="synthesis receipt")

    result = await runner._handle_message(_make_event("synthesize brain"))

    runner.handle_intelligent_synthesis.assert_awaited_once()
    assert result == "synthesis receipt"


@pytest.mark.asyncio
async def test_intelligent_review_dispatches_at_gateway_boundary():
    runner = _make_runner()
    runner.handle_intelligent_review = AsyncMock(return_value="review receipt")

    result = await runner._handle_message(
        _make_event("approve knowledge inote-17")
    )

    runner.handle_intelligent_review.assert_awaited_once()
    command = runner.handle_intelligent_review.await_args.args[1]
    assert command.review_id == "inote-17"
    assert command.action == "approve"
    assert result == "review receipt"


@pytest.mark.asyncio
async def test_assessment_detail_bypasses_busy_or_failed_provider_boundary():
    runner = _make_runner()
    runner.handle_intelligent_review = AsyncMock(return_value="stored assessment")
    agent = next(iter(runner._running_agents.values()))
    agent.run_conversation.side_effect = AssertionError(
        "deterministic review must not invoke the provider"
    )

    result = await runner._handle_message(
        _make_event("Show me the full assessment for inote-384.")
    )

    runner.handle_intelligent_review.assert_awaited_once()
    command = runner.handle_intelligent_review.await_args.args[1]
    assert (
        command.review_id,
        command.action,
        command.payload,
    ) == ("inote-384", "view_related", "assessment")
    agent.run_conversation.assert_not_called()
    assert result == "stored assessment"


@pytest.mark.asyncio
async def test_list_review_candidates_bypasses_provider_boundary():
    runner = _make_runner()
    runner.handle_intelligent_review = AsyncMock(
        return_value="stored review candidates"
    )
    agent = next(iter(runner._running_agents.values()))
    agent.run_conversation.side_effect = AssertionError(
        "candidate listing must not invoke the provider"
    )

    result = await runner._handle_message(
        _make_event("List review candidates.")
    )

    runner.handle_intelligent_review.assert_awaited_once()
    command = runner.handle_intelligent_review.await_args.args[1]
    assert (command.review_id, command.action, command.payload) == (
        "", "list_candidates", ""
    )
    agent.run_conversation.assert_not_called()
    assert result == "stored review candidates"


@pytest.mark.asyncio
async def test_intelligent_outcome_dispatches_at_gateway_boundary():
    runner = _make_runner()
    runner.handle_intelligent_outcome = AsyncMock(return_value="outcome receipt")

    result = await runner._handle_message(
        _make_event(
            "knowledge outcome corrected "
            "ki_0123456789abcdef01234567 Use only irreversible gates"
        )
    )

    runner.handle_intelligent_outcome.assert_awaited_once()
    command = runner.handle_intelligent_outcome.await_args.args[1]
    assert command.outcome == "corrected"
    assert command.item_id == "ki_0123456789abcdef01234567"
    assert result == "outcome receipt"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["forwarded", "transcript"])
async def test_forwarded_or_transcribed_controls_do_not_authorize_mutation(kind):
    runner = _make_runner()
    runner._running_agents.clear()
    runner._running_agents_ts.clear()
    runner.handle_intelligent_review = AsyncMock(
        side_effect=AssertionError("forwarded control must not mutate")
    )
    runner.handle_intelligent_outcome = AsyncMock(
        side_effect=AssertionError("transcribed control must not mutate")
    )
    runner.handle_intelligent_intake = AsyncMock(return_value="preserved intake")
    text = (
        "approve knowledge inote-17"
        if kind == "forwarded"
        else "knowledge outcome useful"
    )
    event = _make_event(text)
    if kind == "forwarded":
        event.raw_message = SimpleNamespace(forward_origin=object())
    else:
        event.is_transcript = True

    result = await runner._handle_message(event)

    runner.handle_intelligent_review.assert_not_awaited()
    runner.handle_intelligent_outcome.assert_not_awaited()
    if kind == "forwarded":
        runner.handle_intelligent_intake.assert_awaited_once()
        assert result == "preserved intake"
    else:
        runner.handle_intelligent_intake.assert_not_awaited()


@pytest.mark.asyncio
async def test_internal_control_text_cannot_mutate_knowledge():
    runner = _make_runner()
    runner.handle_intelligent_review = AsyncMock(
        side_effect=AssertionError("internal control must not mutate")
    )
    event = _make_event("approve knowledge inote-17")
    event.internal = True

    await runner._handle_message(event)

    runner.handle_intelligent_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_control_precedes_pending_free_text_prompts(monkeypatch):
    from tools import clarify_gateway

    runner = _make_runner()
    runner.handle_intelligent_outcome = AsyncMock(return_value="outcome receipt")
    runner._update_prompt_pending = {
        build_session_key(_make_source()): True
    }
    monkeypatch.setattr(
        clarify_gateway,
        "get_pending_for_session",
        MagicMock(side_effect=AssertionError("clarify interceptor was reached")),
    )

    result = await runner._handle_message(
        _make_event("knowledge outcome useful")
    )

    runner.handle_intelligent_outcome.assert_awaited_once()
    assert result == "outcome receipt"


@pytest.mark.asyncio
async def test_intelligent_parser_failure_never_reaches_normal_agent(monkeypatch):
    from gateway import cogitator_intake_bridge as bridge

    runner = _make_runner()
    runner.handle_intelligent_intake = AsyncMock(
        side_effect=AssertionError("failed parser must not dispatch intake")
    )

    def fail_closed(*_args, **_kwargs):
        raise RuntimeError("synthetic parser failure")

    monkeypatch.setattr(bridge, "parse_intelligent_intake", fail_closed)

    result = await runner._handle_message(
        _make_event("https://x.com/i/status/2076300807189024873")
    )

    runner.handle_intelligent_intake.assert_not_awaited()
    assert result == (
        "Intelligent intake routing failed safely.\n"
        "No research or promotion was performed."
    )

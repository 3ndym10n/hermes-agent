"""E2E tests for gateway slash commands (Telegram, Discord).

Each test drives a message through the full async pipeline:
    adapter.handle_message(event)
        → BasePlatformAdapter._process_message_background()
        → GatewayRunner._handle_message() (command dispatch)
        → adapter.send() (captured for assertions)

No LLM involved — only gateway-level commands are tested.
Tests are parametrized over platforms via the ``platform`` fixture in conftest.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from tests.e2e.conftest import make_event, send_and_capture


class TestSlashCommands:
    """Gateway slash commands dispatched through the full adapter pipeline."""

    @pytest.mark.asyncio
    async def test_help_returns_command_list(self, adapter, platform):
        send = await send_and_capture(adapter, "/help", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "/new" in response_text
        assert "/status" in response_text

    @pytest.mark.asyncio
    async def test_status_shows_session_info(self, adapter, platform):
        send = await send_and_capture(adapter, "/status", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "session" in response_text.lower() or "Session" in response_text

    @pytest.mark.asyncio
    async def test_new_resets_session(self, adapter, runner, platform):
        send = await send_and_capture(adapter, "/new", platform)

        send.assert_called_once()
        runner.session_store.reset_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_when_no_agent_running(self, adapter, platform):
        send = await send_and_capture(adapter, "/stop", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        response_lower = response_text.lower()
        assert "no" in response_lower or "stop" in response_lower or "not running" in response_lower

    @pytest.mark.asyncio
    async def test_commands_shows_listing(self, adapter, platform):
        send = await send_and_capture(adapter, "/commands", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        # Should list at least some commands
        assert "/" in response_text

    @pytest.mark.asyncio
    async def test_loop_health_returns_cogitator_report_without_agent(self, adapter, runner, platform, monkeypatch, tmp_path):
        cogitator_root = tmp_path / "Cogitator_clean"
        cogitator_root.mkdir()
        (cogitator_root / "cogitator_loop_health.py").write_text(
            "def build_loop_health_report(*, log_path=None, limit=50):\n"
            "    return 'Cogitator Loop Health\\nTotal preflight attempts: 0\\nNext action: Run /repo smoke.'\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("COGITATOR_REPO_ROOT", str(cogitator_root))
        runner._handle_message_with_agent = AsyncMock(
            side_effect=AssertionError("/loop_health must not call the model/agent")
        )

        send = await send_and_capture(adapter, "/loop_health", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "Cogitator Loop Health" in response_text
        assert "Total preflight attempts: 0" in response_text
        assert "inspect the current repository status" not in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_loop_health_hyphenated_alias_returns_report(self, adapter, runner, platform, monkeypatch, tmp_path):
        cogitator_root = tmp_path / "Cogitator_clean"
        cogitator_root.mkdir()
        (cogitator_root / "cogitator_loop_health.py").write_text(
            "def build_loop_health_report(*, log_path=None, limit=50):\n"
            "    return 'Cogitator Loop Health\\nTotal preflight attempts: 0\\nNext action: Run /repo smoke.'\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("COGITATOR_REPO_ROOT", str(cogitator_root))
        runner._handle_message_with_agent = AsyncMock(
            side_effect=AssertionError("/loop-health must not call the model/agent")
        )

        send = await send_and_capture(adapter, "/loop-health", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "Cogitator Loop Health" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_slash_command_still_rejected(self, adapter, runner, platform):
        runner._handle_message_with_agent = AsyncMock(
            side_effect=AssertionError("unknown slash command must not call the model/agent")
        )

        send = await send_and_capture(adapter, "/not_loop_health", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "Unknown command" in response_text
        assert "/not_loop_health" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cogitator_review_returns_queue_without_agent(self, adapter, runner, platform):
        rows = [(42, "Issue 4 proof note", "summary", 5, 0)]
        cog = SimpleNamespace(
            get_review_notes=MagicMock(return_value=rows),
            get_enrichment_review_summaries=MagicMock(return_value={}),
            render_review_rows=MagicMock(return_value="1. Issue 4 proof note (Score: 5)\n"),
        )
        runner._load_cogitator_app_module = MagicMock(return_value=cog)
        runner._handle_message_with_agent = AsyncMock(
            side_effect=AssertionError("/review must not call the model/agent")
        )

        send = await send_and_capture(adapter, "/review", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "Review Queue (page 1)" in response_text
        assert "Issue 4 proof note" in response_text
        assert runner._cogitator_review_pages
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cogitator_review_page_returns_queue_without_agent(self, adapter, runner, platform):
        rows = [(202, "Page 2 proof note", "summary", 4, 0)]
        cog = SimpleNamespace(
            REVIEW_PAGE_SIZE=10,
            get_review_notes_count=MagicMock(return_value=11),
            get_review_notes_page=MagicMock(return_value=rows),
            get_enrichment_review_summaries=MagicMock(return_value={}),
            render_review_rows=MagicMock(return_value="1. Page 2 proof note (Score: 4)\n"),
        )
        runner._load_cogitator_app_module = MagicMock(return_value=cog)
        runner._handle_message_with_agent = AsyncMock(
            side_effect=AssertionError("/review_page must not call the model/agent")
        )

        send = await send_and_capture(adapter, "/review_page 2", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "Review Queue — Page 2/2" in response_text
        assert "Page 2 proof note" in response_text
        cog.get_review_notes_page.assert_called_once_with(2)
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cogitator_promote_requires_current_review_page(self, adapter, runner, platform):
        runner._load_cogitator_app_module = MagicMock(
            side_effect=AssertionError("/promote without review page must not load Cogitator")
        )
        runner._handle_message_with_agent = AsyncMock(
            side_effect=AssertionError("/promote must not call the model/agent")
        )

        send = await send_and_capture(adapter, "/promote 1", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "Run /review or /review_page" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cogitator_promote_uses_selected_visible_review_note_id(self, adapter, runner, platform):
        rows = [
            (101, "First visible note", "summary", 5, 0),
            (202, "Second visible note", "summary", 5, 0),
        ]
        cog = SimpleNamespace(
            get_review_notes=MagicMock(return_value=rows),
            get_enrichment_review_summaries=MagicMock(return_value={}),
            render_review_rows=MagicMock(return_value="1. First visible note\n2. Second visible note\n"),
            get_note_by_id=MagicMock(return_value=(202, "Second visible note")),
            promote_note_by_id=MagicMock(return_value={
                "promotion_type": "cogitator_design_principle",
                "title": "Second visible note",
                "promoted_path": "storage/promoted/second.md",
                "retrieval_record_path": "storage/promoted/second.retrieval.md",
            }),
        )
        runner._load_cogitator_app_module = MagicMock(return_value=cog)
        runner._handle_message_with_agent = AsyncMock(
            side_effect=AssertionError("/promote must not call the model/agent")
        )

        await send_and_capture(adapter, "/review", platform)
        send = await send_and_capture(adapter, "/promote 2", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "Promoted to cogitator_design_principle" in response_text
        assert "Saved to:\nstorage/promoted/second.md" in response_text
        assert "Retrieval record:\nstorage/promoted/second.retrieval.md" in response_text
        cog.get_note_by_id.assert_called_once_with(202)
        cog.promote_note_by_id.assert_called_once_with(202)
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_context_checkpoint_disabled_by_default_returns_notice(
        self, adapter, runner, platform, monkeypatch
    ):
        """Default-off: returns a disabled notice and never contacts Cogitator."""
        import gateway.cogitator_checkpoint_bridge as bridge_mod
        from gateway import run as gateway_run

        # Real default-off gate: no context_checkpoint config present.
        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
        monkeypatch.setenv("COGITATOR_BRIDGE_TOKEN", "should-not-be-used")
        monkeypatch.setattr(
            bridge_mod,
            "request_context_checkpoint",
            MagicMock(side_effect=AssertionError("disabled command must not contact the bridge")),
        )
        monkeypatch.setattr(
            bridge_mod,
            "_post_bridge",
            MagicMock(side_effect=AssertionError("disabled command must not POST to the bridge")),
        )
        runner._handle_message_with_agent = AsyncMock(
            side_effect=AssertionError("/context_checkpoint must not call the model/agent")
        )

        send = await send_and_capture(adapter, "/context_checkpoint working on rotation", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "disabled" in response_text.lower()
        assert "Auto-rotation is not implemented" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_context_checkpoint_enabled_missing_token_fails_closed(
        self, adapter, runner, platform, monkeypatch
    ):
        """Enabled but no COGITATOR_BRIDGE_TOKEN env → fail closed, never POSTs."""
        import gateway.cogitator_checkpoint_bridge as bridge_mod
        from gateway import run as gateway_run

        monkeypatch.setattr(
            gateway_run,
            "_load_gateway_config",
            lambda: {"context_checkpoint": {"enabled": True, "base_url": "https://cog.example"}},
        )
        monkeypatch.delenv("COGITATOR_BRIDGE_TOKEN", raising=False)
        monkeypatch.setattr(
            bridge_mod,
            "_post_bridge",
            MagicMock(side_effect=AssertionError("must not POST without a token")),
        )
        runner._handle_message_with_agent = AsyncMock(
            side_effect=AssertionError("/context_checkpoint must not call the model/agent")
        )

        send = await send_and_capture(adapter, "/context_checkpoint state", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "not configured" in response_text.lower()
        assert "COGITATOR_BRIDGE_TOKEN" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_context_checkpoint_enabled_builds_packet_and_renders(
        self, adapter, runner, platform, monkeypatch, caplog
    ):
        """Enabled: POSTs the exact draft-only packet and renders the checkpoint."""
        import gateway.cogitator_checkpoint_bridge as bridge_mod
        from gateway import run as gateway_run

        monkeypatch.setattr(
            gateway_run,
            "_load_gateway_config",
            lambda: {"context_checkpoint": {"enabled": True, "base_url": "https://cog.example"}},
        )
        secret_token = "tok-DO-NOT-LEAK-12345"
        monkeypatch.setenv("COGITATOR_BRIDGE_TOKEN", secret_token)

        captured = {}

        def fake_post(packet, *, base_url, token, urlopen=None):
            captured["packet"] = packet
            captured["base_url"] = base_url
            captured["token"] = token
            return {
                "status": "ok",
                "requested_action": "build_context_checkpoint",
                "mutated": False,
                "proposal_only": True,
                "checkpoint": {
                    "purpose": "",
                    "current_state": packet["context"].get("current_state", ""),
                    "active_constraints": ["read-only"],
                    "decisions_made": [],
                    "open_questions": [],
                    "artifact_paths": [],
                    "verification": [],
                    "next_recommended_action": "",
                    "safety": {"mutation_allowed": False, "mutation_performed": False},
                },
            }

        monkeypatch.setattr(bridge_mod, "_post_bridge", fake_post)
        runner._handle_message_with_agent = AsyncMock(
            side_effect=AssertionError("/context_checkpoint must not call the model/agent")
        )

        with caplog.at_level("DEBUG"):
            send = await send_and_capture(
                adapter, "/context_checkpoint rotating the context window", platform
            )

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]

        packet = captured["packet"]
        assert packet["source_agent"] == "hermes"
        assert packet["requested_action"] == "build_context_checkpoint"
        assert packet["user_intent"] == (
            "Build a read-only checkpoint for the current Hermes conversation; "
            "do not rotate or inject."
        )
        assert packet["content"] == ""
        assert packet["approval_status"] == "draft_only"
        assert packet["risk_level"] == "low"
        assert set(packet["context"]) == {
            "purpose",
            "current_state",
            "active_constraints",
            "decisions_made",
            "open_questions",
            "artifact_paths",
            "verification",
            "next_recommended_action",
        }
        assert packet["context"]["current_state"] == "rotating the context window"
        assert captured["base_url"] == "https://cog.example"
        assert captured["token"] == secret_token

        assert "Context Checkpoint" in response_text
        assert "Current state: rotating the context window" in response_text
        assert "Active constraints:" in response_text
        # Token must never reach the chat or the logs.
        assert secret_token not in response_text
        assert secret_token not in caplog.text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_context_checkpoint_enabled_rejects_mutated_response(
        self, adapter, runner, platform, monkeypatch
    ):
        """A mutated/unsafe bridge response is rejected, never rendered as a checkpoint."""
        import gateway.cogitator_checkpoint_bridge as bridge_mod
        from gateway import run as gateway_run

        monkeypatch.setattr(
            gateway_run,
            "_load_gateway_config",
            lambda: {"context_checkpoint": {"enabled": True, "base_url": "https://cog.example"}},
        )
        monkeypatch.setenv("COGITATOR_BRIDGE_TOKEN", "tok")
        monkeypatch.setattr(
            bridge_mod,
            "_post_bridge",
            lambda packet, *, base_url, token, urlopen=None: {
                "status": "ok",
                "requested_action": "build_context_checkpoint",
                "mutated": True,  # contract violation
                "checkpoint": {"safety": {"mutation_allowed": True, "mutation_performed": True}},
            },
        )
        runner._handle_message_with_agent = AsyncMock(
            side_effect=AssertionError("/context_checkpoint must not call the model/agent")
        )

        send = await send_and_capture(adapter, "/context_checkpoint state", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "unavailable" in response_text.lower()
        assert "BRIDGE_MUTATION_REPORTED" in response_text
        assert "Context Checkpoint (read-only" not in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_context_checkpoint_enabled_rejects_missing_checkpoint(
        self, adapter, runner, platform, monkeypatch
    ):
        """A response with no checkpoint object is rejected and not rendered."""
        import gateway.cogitator_checkpoint_bridge as bridge_mod
        from gateway import run as gateway_run

        monkeypatch.setattr(
            gateway_run,
            "_load_gateway_config",
            lambda: {"context_checkpoint": {"enabled": True, "base_url": "https://cog.example"}},
        )
        monkeypatch.setenv("COGITATOR_BRIDGE_TOKEN", "tok")
        monkeypatch.setattr(
            bridge_mod,
            "_post_bridge",
            lambda packet, *, base_url, token, urlopen=None: {
                "status": "ok",
                "requested_action": "build_context_checkpoint",
                "mutated": False,
            },
        )
        runner._handle_message_with_agent = AsyncMock(
            side_effect=AssertionError("/context_checkpoint must not call the model/agent")
        )

        send = await send_and_capture(adapter, "/context_checkpoint state", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "unavailable" in response_text.lower()
        assert "BRIDGE_CHECKPOINT_MISSING" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_context_checkpoint_default_off_does_not_contact_bridge(
        self, runner, platform, monkeypatch
    ):
        """V0-E default-off: normal turns do not contact Cogitator."""
        import gateway.cogitator_checkpoint_bridge as bridge_mod
        from gateway import run as gateway_run

        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
        monkeypatch.setenv("COGITATOR_BRIDGE_TOKEN", "should-not-be-used")
        monkeypatch.setattr(
            bridge_mod,
            "request_context_checkpoint",
            MagicMock(side_effect=AssertionError("auto checkpoint is default-off")),
        )
        history = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "reply"},
        ]
        event = make_event(platform, text="continue")

        message = runner._maybe_render_auto_context_checkpoint(
            event=event,
            response="normal response",
            agent_result={
                "final_response": "normal response",
                "last_prompt_tokens": 900,
                "context_length": 1000,
            },
            history=history,
            session_id="sess-1",
            session_key="e2e-session",
        )

        assert message == ""
        bridge_mod.request_context_checkpoint.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_context_checkpoint_threshold_returns_handoff_packet(
        self, runner, platform, monkeypatch, caplog
    ):
        """V0-E threshold hit: calls build_context_checkpoint and returns handoff."""
        import gateway.cogitator_checkpoint_bridge as bridge_mod
        from gateway import run as gateway_run

        monkeypatch.setattr(
            gateway_run,
            "_load_gateway_config",
            lambda: {
                "context_checkpoint": {
                    "enabled": True,
                    "base_url": "https://cog.example",
                    "auto_trigger": {"enabled": True, "threshold": 0.75},
                }
            },
        )
        secret_token = "tok-DO-NOT-LEAK-AUTO-12345"
        monkeypatch.setenv("COGITATOR_BRIDGE_TOKEN", secret_token)
        captured = {}

        def fake_request(context_fields, *, base_url, token, urlopen=None):
            captured["context"] = context_fields
            captured["base_url"] = base_url
            captured["token"] = token
            return {
                "status": "ok",
                "requested_action": "build_context_checkpoint",
                "mutated": False,
                "proposal_only": True,
                "checkpoint": {
                    "purpose": context_fields["purpose"],
                    "current_state": context_fields["current_state"],
                    "active_constraints": context_fields["active_constraints"],
                    "decisions_made": context_fields["decisions_made"],
                    "open_questions": context_fields["open_questions"],
                    "artifact_paths": context_fields["artifact_paths"],
                    "verification": context_fields["verification"],
                    "next_recommended_action": context_fields["next_recommended_action"],
                    "safety": {"mutation_allowed": False, "mutation_performed": False},
                },
            }

        monkeypatch.setattr(bridge_mod, "request_context_checkpoint", fake_request)
        history = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "reply"},
        ]
        event = make_event(platform, text="continue the task")

        with caplog.at_level("DEBUG"):
            message = runner._maybe_render_auto_context_checkpoint(
                event=event,
                response="normal response",
                agent_result={
                    "final_response": "normal response",
                    "last_prompt_tokens": 800,
                    "context_length": 1000,
                },
                history=history,
                session_id="sess-1",
                session_key="e2e-session",
            )

        assert "Automatic Context Protection" in message
        assert "Clean continuation action:" in message
        assert "/new" in message
        assert captured["base_url"] == "https://cog.example"
        assert captured["token"] == secret_token
        assert "continue the task" in captured["context"]["current_state"]
        assert captured["context"]["next_recommended_action"] == (
            "Start a clean continuation with /new, then paste this checkpoint/handoff packet as the first message."
        )
        assert secret_token not in message
        assert secret_token not in caplog.text

    @pytest.mark.asyncio
    async def test_auto_context_checkpoint_output_is_not_persisted_to_transcript(
        self, runner, adapter, platform, session_entry, monkeypatch
    ):
        """V0-E automatic checkpoint is a post-persistence notice, not transcript content."""
        import gateway.cogitator_checkpoint_bridge as bridge_mod
        from gateway import run as gateway_run
        from gateway.run import GatewayRunner

        monkeypatch.setattr(
            gateway_run,
            "_load_gateway_config",
            lambda: {
                "context_checkpoint": {
                    "enabled": True,
                    "base_url": "https://cog.example",
                    "auto_trigger": {"enabled": True, "threshold": 0.75},
                }
            },
        )
        monkeypatch.setenv("COGITATOR_BRIDGE_TOKEN", "tok-not-persisted")

        def fake_request(context_fields, *, base_url, token, urlopen=None):
            return {
                "status": "ok",
                "requested_action": "build_context_checkpoint",
                "mutated": False,
                "proposal_only": True,
                "checkpoint": {
                    "purpose": context_fields["purpose"],
                    "current_state": context_fields["current_state"],
                    "active_constraints": context_fields["active_constraints"],
                    "decisions_made": context_fields["decisions_made"],
                    "open_questions": context_fields["open_questions"],
                    "artifact_paths": context_fields["artifact_paths"],
                    "verification": context_fields["verification"],
                    "next_recommended_action": context_fields["next_recommended_action"],
                    "safety": {"mutation_allowed": False, "mutation_performed": False},
                },
            }

        monkeypatch.setattr(bridge_mod, "request_context_checkpoint", fake_request)
        runner.session_store.load_transcript.return_value = []
        runner.session_store.has_any_sessions.return_value = True
        runner._run_agent = AsyncMock(
            return_value={
                "final_response": "normal response",
                "messages": [],
                "tools": [],
                "last_prompt_tokens": 800,
                "context_length": 1000,
            }
        )
        runner._session_run_generation = {session_entry.session_key: 1}

        event = make_event(platform, text="continue the task")
        returned = await GatewayRunner._handle_message_with_agent(
            runner,
            event,
            event.source,
            session_entry.session_key,
            1,
        )

        assert returned == "normal response"
        persisted_entries = [call.args[1] for call in runner.session_store.append_to_transcript.call_args_list]
        assistant_entries = [entry for entry in persisted_entries if entry.get("role") == "assistant"]
        assert len(assistant_entries) == 1
        assert assistant_entries[0]["content"] == "normal response"
        assert all("Automatic Context Protection" not in str(entry) for entry in persisted_entries)
        assert all("Clean continuation action" not in str(entry) for entry in persisted_entries)

        callback = adapter.pop_post_delivery_callback(session_entry.session_key, generation=1)
        assert callback is not None
        await callback()
        notice_text = adapter.send.call_args[1].get("content") or adapter.send.call_args[0][1]
        assert "Automatic Context Protection" in notice_text
        assert "Clean continuation action:" in notice_text

    @pytest.mark.asyncio
    async def test_auto_context_checkpoint_threshold_missing_token_fails_closed_without_post(
        self, runner, platform, monkeypatch
    ):
        """V0-E threshold hit without token: visible fail-closed notice, no POST."""
        import gateway.cogitator_checkpoint_bridge as bridge_mod
        from gateway import run as gateway_run

        monkeypatch.setattr(
            gateway_run,
            "_load_gateway_config",
            lambda: {
                "context_checkpoint": {
                    "enabled": True,
                    "base_url": "https://cog.example",
                    "auto_trigger": {"enabled": True, "threshold": 0.75},
                }
            },
        )
        monkeypatch.delenv("COGITATOR_BRIDGE_TOKEN", raising=False)
        post = MagicMock(side_effect=AssertionError("must not POST without token"))
        monkeypatch.setattr(bridge_mod, "request_context_checkpoint", post)

        message = runner._maybe_render_auto_context_checkpoint(
            event=make_event(platform, text="continue the task"),
            response="normal response",
            agent_result={
                "last_prompt_tokens": 800,
                "context_length": 1000,
            },
            history=[{"role": "user", "content": "old"}],
            session_id="sess-1",
            session_key="e2e-session",
        )

        assert "Automatic Context Protection triggered" in message
        assert "COGITATOR_BRIDGE_TOKEN is missing" in message
        assert "No automatic injection or rotation has happened" in message
        post.assert_not_called()

    @pytest.mark.asyncio
    async def test_sequential_commands_share_session(self, adapter, platform):
        """Two commands from the same chat_id should both succeed."""
        send_help = await send_and_capture(adapter, "/help", platform)
        send_help.assert_called_once()

        send_status = await send_and_capture(adapter, "/status", platform)
        send_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_verbose_responds(self, adapter, platform):
        send = await send_and_capture(adapter, "/verbose", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        # Either shows the mode cycle or tells user to enable it in config
        assert "verbose" in response_text.lower() or "tool_progress" in response_text

    @pytest.mark.asyncio
    async def test_plaintext_restart_gateway_routes_to_safe_restart_command(self, adapter, runner, platform, monkeypatch):
        if platform != Platform.TELEGRAM:
            pytest.skip("Plaintext restart shortcut is intentionally DM/Telegram-focused")

        monkeypatch.setenv("INVOCATION_ID", "e2e-systemd")
        runner.request_restart = MagicMock(return_value=True)

        send = await send_and_capture(adapter, "restart gateway", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "restart" in response_text.lower() or "draining" in response_text.lower()
        runner.request_restart.assert_called_once_with(detached=False, via_service=True)

    @pytest.mark.asyncio
    async def test_plaintext_restart_gateway_in_group_stays_plain_text(self, adapter, runner, platform, monkeypatch):
        if platform != Platform.TELEGRAM:
            pytest.skip("Shortcut scope is only verified for Telegram here")

        monkeypatch.setenv("INVOCATION_ID", "e2e-systemd")
        runner.request_restart = MagicMock(return_value=True)
        runner._handle_message_with_agent = AsyncMock(return_value="agent-handled")

        send = await send_and_capture(adapter, "restart gateway", platform, chat_id="group-chat-1", user_id="u1", chat_type="group")

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert response_text == "agent-handled"
        runner.request_restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_personality_lists_options(self, adapter, platform):
        send = await send_and_capture(adapter, "/personality", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "personalit" in response_text.lower()  # matches "personality" or "personalities"

    @pytest.mark.asyncio
    async def test_yolo_toggles_mode(self, adapter, platform):
        send = await send_and_capture(adapter, "/yolo", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "yolo" in response_text.lower()

    @pytest.mark.asyncio
    async def test_compress_command(self, adapter, platform):
        send = await send_and_capture(adapter, "/compress", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "compress" in response_text.lower() or "context" in response_text.lower()

    @pytest.mark.asyncio
    async def test_quick_command_alias_targets_builtin_command_with_args(
        self, adapter, runner, platform
    ):
        """Alias targets with args must reach the built-in command handler."""
        runner.config.quick_commands = {
            "s": {"type": "alias", "target": "/status extra-arg"}
        }
        async def _handle_status(event):
            assert event.get_command_args() == "extra-arg"
            return "status via alias"

        runner._handle_status_command = AsyncMock(side_effect=_handle_status)

        send = await send_and_capture(adapter, "/s", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert response_text == "status via alias"
        runner._handle_status_command.assert_awaited_once()
        runner._handle_message_with_agent.assert_not_awaited()



class TestSessionLifecycle:
    """Verify session state changes across command sequences."""

    @pytest.mark.asyncio
    async def test_new_then_status_reflects_reset(self, adapter, runner, session_entry, platform):
        """After /new, /status should report the fresh session."""
        await send_and_capture(adapter, "/new", platform)
        runner.session_store.reset_session.assert_called_once()

        send = await send_and_capture(adapter, "/status", platform)
        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        # Session ID from the entry should appear in the status output
        assert session_entry.session_id[:8] in response_text

    @pytest.mark.asyncio
    async def test_new_is_idempotent(self, adapter, runner, platform):
        """/new called twice should not crash."""
        await send_and_capture(adapter, "/new", platform)
        await send_and_capture(adapter, "/new", platform)
        assert runner.session_store.reset_session.call_count == 2


class TestAuthorization:
    """Verify the pipeline handles unauthorized users."""

    @pytest.mark.asyncio
    async def test_unauthorized_user_gets_pairing_response(self, adapter, runner, platform):
        """Unauthorized DM should trigger pairing code, not a command response."""
        runner._is_user_authorized = lambda _source: False

        event = make_event(platform, "/help")
        adapter.send.reset_mock()
        await adapter.handle_message(event)
        await asyncio.sleep(0.3)

        # The adapter.send is called directly by the authorization path
        # (not via _send_with_retry), so check it was called with a pairing message
        adapter.send.assert_called()
        response_text = adapter.send.call_args[0][1] if len(adapter.send.call_args[0]) > 1 else ""
        assert "recognize" in response_text.lower() or "pair" in response_text.lower() or "ABC123" in response_text

    @pytest.mark.asyncio
    async def test_unauthorized_user_does_not_get_help(self, adapter, runner, platform):
        """Unauthorized user should NOT see the help command output."""
        runner._is_user_authorized = lambda _source: False

        event = make_event(platform, "/help")
        adapter.send.reset_mock()
        await adapter.handle_message(event)
        await asyncio.sleep(0.3)

        # If send was called, it should NOT contain the help text
        if adapter.send.called:
            response_text = adapter.send.call_args[0][1] if len(adapter.send.call_args[0]) > 1 else ""
            assert "/new" not in response_text


class TestSendFailureResilience:
    """Verify the pipeline handles send failures gracefully."""

    @pytest.mark.asyncio
    async def test_send_failure_does_not_crash_pipeline(self, adapter, platform):
        """If send() returns failure, the pipeline should not raise."""
        adapter.send = AsyncMock(return_value=SendResult(success=False, error="network timeout"))
        adapter.set_message_handler(adapter._message_handler) # re-wire with same handler

        event = make_event(platform, "/help")
        # Should not raise — pipeline handles send failures internally
        await adapter.handle_message(event)
        await asyncio.sleep(0.3)

        adapter.send.assert_called()

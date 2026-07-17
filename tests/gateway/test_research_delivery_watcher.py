import inspect
from unittest.mock import AsyncMock

import pytest

import gateway.cogitator_intake_bridge as intake_bridge
import gateway.run as run_module
from gateway import intelligent_review_buttons as irb
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_research_delivery_watcher_acks_before_send_and_preserves_topic(monkeypatch):
    delivery = {
        "job_id": "job-1", "message": "Research complete: job-1",
        "origin": {
            "platform": "telegram", "chat_id": "123", "chat_type": "dm",
            "thread_id": "456", "message_id": "789",
        },
        "lease_token": "a" * 32, "version": 1, "attempts": 1,
    }
    claims = [delivery]
    acks = []
    thread_calls = []

    def claim(**kwargs):
        return claims.pop(0) if claims else None

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._intake_config = lambda: (True, "https://cogitator.example", "")

    def ack(**kwargs):
        acks.append(kwargs)
        if kwargs["outcome"] == "send_started":
            return {"delivery": {"version": 2}}
        runner._running = False
        return {"delivery": {"version": 3}}

    adapter = type("Adapter", (), {})()
    adapter._send_with_retry = AsyncMock(
        return_value=SendResult(success=True, message_id="telegram-1"))
    runner.adapters = {Platform.TELEGRAM: adapter}

    async def direct_thread(function, *args, **kwargs):
        thread_calls.append(function.__name__)
        return function(*args, **kwargs)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setenv(intake_bridge.TOKEN_ENV, "configured")
    monkeypatch.setattr(intake_bridge, "claim_research_delivery", claim)
    monkeypatch.setattr(intake_bridge, "ack_research_delivery", ack)
    monkeypatch.setattr(run_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(run_module.asyncio, "to_thread", direct_thread)

    await runner._research_delivery_watcher(interval=0)

    assert [item["outcome"] for item in acks] == ["send_started", "delivered"]
    assert thread_calls == ["claim", "ack", "ack"]
    adapter._send_with_retry.assert_awaited_once()
    kwargs = adapter._send_with_retry.await_args.kwargs
    assert kwargs["chat_id"] == "123" and kwargs["reply_to"] == "789"
    assert kwargs["metadata"]["thread_id"] == "456"
    assert kwargs["metadata"]["telegram_reply_to_message_id"] == "789"
    assert acks[-1]["remote_message_id"] == "telegram-1"


def test_research_delivery_failure_categories_are_sanitized():
    assert GatewayRunner._research_delivery_failure(
        SendResult(success=False, error="token detail", retryable=True)) == (
            "pending", "transient")
    assert GatewayRunner._research_delivery_failure(
        SendResult(success=False, error="403 Forbidden", retryable=True)) == (
            "failed", "forbidden")
    assert GatewayRunner._research_delivery_failure(
        SendResult(success=False, error="400 Bad Request: chat not found",
                   retryable=True)) == ("failed", "invalid_route")
    assert GatewayRunner._research_delivery_failure(
        SendResult(success=False, error="PoolTimeout", retryable=True)) == (
            "pending", "transient")
    assert GatewayRunner._research_delivery_failure(
        SendResult(success=False, error="401 Unauthorized")) == (
            "failed", "unauthorized")
    assert GatewayRunner._research_delivery_failure(
        SendResult(success=False, error="Timed out")) == (
        "failed", "timeout_unknown")


@pytest.mark.asyncio
async def test_repository_result_uses_existing_human_review_controls(monkeypatch):
    delivery = {
        "job_id": "inote-403-repository-review-v1",
        "message": "review\nRECOMMENDED DECISION: CREATE BOUNDED TEST",
        "origin": {
            "platform": "telegram", "chat_id": "123", "chat_type": "dm",
            "user_id": "123",
        },
        "lease_token": "c" * 32, "version": 1, "attempts": 1,
    }
    claims = [delivery]
    acks = []
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._intake_config = lambda: (True, "https://cogitator.example", "")
    runner._isb_button_store = irb.ReviewButtonStore()

    def claim(**_kwargs):
        return claims.pop(0) if claims else None

    def ack(**kwargs):
        acks.append(kwargs)
        if kwargs["outcome"] == "send_started":
            return {"delivery": {"version": 2}}
        runner._running = False
        return {"delivery": {"version": 3}}

    adapter = type("Adapter", (), {})()
    adapter.send_intelligent_review_message = AsyncMock(
        return_value=SendResult(success=True, message_id="telegram-2"))
    adapter._send_with_retry = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}

    async def direct_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setenv(intake_bridge.TOKEN_ENV, "configured")
    monkeypatch.setattr(intake_bridge, "claim_research_delivery", claim)
    monkeypatch.setattr(intake_bridge, "ack_research_delivery", ack)
    monkeypatch.setattr(run_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(run_module.asyncio, "to_thread", direct_thread)

    await runner._research_delivery_watcher(interval=0)

    adapter._send_with_retry.assert_not_awaited()
    sent = adapter.send_intelligent_review_message.await_args.kwargs
    labels = [label for row in sent["button_rows"] for label, _token in row]
    assert labels == [
        "✅ Keep Saved Review", "⭐ 📄 Review Details", "✖ Dismiss Buttons",
    ]
    assert [item["outcome"] for item in acks] == ["send_started", "delivered"]
    assert len(runner._intelligent_button_store) == 3

    entries = list(runner._intelligent_button_store._entries.values())
    assert {entry.artifact_kind for entry in entries} == {"repository_review_v1"}
    assert {entry.artifact_text for entry in entries} == {delivery["message"]}


def test_repository_button_actions_cannot_mutate_original_candidate():
    for action in irb.REPOSITORY_ACTIONS:
        assert irb.review_action_for(action) == ""


@pytest.mark.asyncio
async def test_repository_button_handler_is_artifact_local():
    runner = object.__new__(GatewayRunner)
    entry = type("Entry", (), {
        "action": irb.REPOSITORY_DETAILS,
        "artifact_text": "saved repository review",
    })()
    result = await runner.handle_intelligent_button_action(entry)
    assert result == {
        "text": "saved repository review",
        "remove_buttons": False,
        "status": "details",
    }

    entry.action = irb.REPOSITORY_SAVE
    result = await runner.handle_intelligent_button_action(entry)
    assert result["status"] == "saved"
    assert "not approved or promoted" in result["text"]



def test_research_delivery_config_and_startup_gate(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner._intake_config = lambda: (True, "https://cogitator.example", "")
    runner.adapters = {Platform.TELEGRAM: object()}
    monkeypatch.setenv(intake_bridge.TOKEN_ENV, "configured")
    assert runner._research_delivery_configured() is True
    monkeypatch.delenv(intake_bridge.TOKEN_ENV)
    assert runner._research_delivery_configured() is False

    source = inspect.getsource(GatewayRunner.start)
    assert "if self._research_delivery_configured():" in source
    assert "asyncio.create_task(self._research_delivery_watcher())" in source


@pytest.mark.asyncio
async def test_post_send_timeout_is_left_ambiguous_and_never_resent(
        monkeypatch, caplog):
    delivery = {
        "job_id": "job-timeout", "message": "sensitive research result",
        "origin": {
            "platform": "telegram", "chat_id": "987654321", "chat_type": "dm",
            "thread_id": "456", "message_id": "789",
        },
        "lease_token": "b" * 32, "version": 1, "attempts": 1,
    }
    claims = [delivery]
    acks = []
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._intake_config = lambda: (True, "https://cogitator.example", "")

    def claim(**kwargs):
        return claims.pop(0) if claims else None

    def ack(**kwargs):
        acks.append(kwargs)
        return {"delivery": {"version": 2}}

    adapter = type("Adapter", (), {})()
    adapter._send_with_retry = AsyncMock(
        side_effect=TimeoutError("post-send read timed out"))
    runner.adapters = {Platform.TELEGRAM: adapter}

    async def direct_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def bounded_sleep(seconds):
        if seconds == 0:
            runner._running = False

    monkeypatch.setenv(intake_bridge.TOKEN_ENV, "configured-token")
    monkeypatch.setattr(intake_bridge, "claim_research_delivery", claim)
    monkeypatch.setattr(intake_bridge, "ack_research_delivery", ack)
    monkeypatch.setattr(run_module.asyncio, "to_thread", direct_thread)
    monkeypatch.setattr(run_module.asyncio, "sleep", bounded_sleep)

    await runner._research_delivery_watcher(interval=0)

    assert [ack["outcome"] for ack in acks] == ["send_started"]
    adapter._send_with_retry.assert_awaited_once()
    log_text = caplog.text
    for secret in (
            "configured-token", delivery["message"],
            delivery["origin"]["chat_id"], delivery["lease_token"]):
        assert secret not in log_text

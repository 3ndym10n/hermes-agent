import json

import pytest

from commerce_jobs import CommerceJobStore
from tools import commerce_tool
from tools.registry import registry


ORIGIN = {
    "platform": "telegram",
    "chat_id": "-10042",
    "thread_id": "7",
    "user_id": "4242",
    "message_id": "99",
}
ENABLED = lambda: {"commerce": {"enabled": True}}


@pytest.fixture
def store(tmp_path):
    result = CommerceJobStore(tmp_path / "commerce.db")
    result.initialize()
    return result


def control(store, operation, **kwargs):
    return commerce_tool.commerce_control_from_origin(
        operation,
        origin=ORIGIN,
        store=store,
        config_loader=ENABLED,
        **kwargs,
    )


def test_schema_is_one_store_only_tool_without_routing_fields():
    schema = registry.get_schema("commerce_launch")

    assert registry.get_toolset_for_tool("commerce_launch") == "commerce"
    assert registry.get_entry("commerce_launch").check_fn is None
    assert schema["parameters"]["additionalProperties"] is False
    assert not (
        {"platform", "chat_id", "thread_id", "user_id", "message_id"}
        & schema["parameters"]["properties"].keys()
    )
    properties = schema["parameters"]["properties"]
    assert "gate_id" not in properties
    assert not {"approve", "deny"} & set(properties["operation"]["enum"])
    assert "purchase or publication" in schema["description"]


def test_model_handler_uses_only_trusted_session_context(monkeypatch, store):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "-10042",
        "HERMES_SESSION_THREAD_ID": "7",
        "HERMES_SESSION_USER_ID": "4242",
        "HERMES_SESSION_MESSAGE_ID": "99",
    }
    monkeypatch.setattr(
        commerce_tool,
        "get_session_env",
        lambda name, default="": values.get(name, default),
    )
    monkeypatch.setattr(commerce_tool, "load_config_readonly", ENABLED)
    monkeypatch.setattr(commerce_tool, "CommerceJobStore", lambda: store)

    result = json.loads(
        commerce_tool._handle_commerce_launch({"operation": "start_or_resume"})
    )
    rejected = json.loads(
        commerce_tool._handle_commerce_launch({
            "operation": "status",
            "user_id": "attacker",
            "chat_id": "elsewhere",
        })
    )

    assert result["ok"] is True
    job = store.get_job(result["job_id"])
    assert job["requester"] == "telegram:4242"
    assert store.list_events(job["job_id"])[0]["evidence"]["origin"] == ORIGIN
    assert rejected == {"error": "invalid_arguments", "ok": False}


def test_kill_switch_and_trusted_telegram_origin_are_fail_closed(store):
    assert commerce_tool.commerce_control_from_origin(
        "start_or_resume",
        origin=ORIGIN,
        store=store,
        config_loader=lambda: {"commerce": {"enabled": False}},
    ) == {"ok": False, "error": "commerce_disabled"}
    assert commerce_tool.commerce_control_from_origin(
        "start_or_resume",
        origin={**ORIGIN, "platform": "discord"},
        store=store,
        config_loader=ENABLED,
    ) == {"ok": False, "error": "telegram_only"}
    assert commerce_tool.commerce_control_from_origin(
        "start_or_resume",
        origin={**ORIGIN, "chat_id": ""},
        store=store,
        config_loader=ENABLED,
    ) == {"ok": False, "error": "missing_trusted_origin"}


def test_start_attaches_resumes_and_owner_controls_are_store_only(store):
    started = control(store, "start_or_resume")
    paused = control(store, "pause", job_id=started["job_id"])
    attached = control(store, "start_or_resume")
    facts = control(
        store,
        "answer_facts",
        job_id=started["job_id"],
        facts={"launch_region": "Australia"},
    )

    assert started["state"] == "requested"
    assert paused["state"] == "paused"
    assert attached["job_id"] == started["job_id"]
    assert attached["attached"] is True
    assert attached["resumed"] is True
    assert attached["state"] == "ready"
    assert facts["facts_recorded"] is True
    assert store.latest_facts(started["job_id"]) == {"launch_region": "Australia"}
    assert store.list_actions(started["job_id"]) == []

    other = commerce_tool.commerce_control_from_origin(
        "status",
        origin={**ORIGIN, "user_id": "9999"},
        job_id=started["job_id"],
        store=store,
        config_loader=ENABLED,
    )
    assert other == {"ok": False, "error": "job_not_owned"}


@pytest.mark.parametrize("operation", ["approve", "deny"])
def test_model_control_cannot_approve_or_deny(store, operation):
    started = control(store, "start_or_resume")

    assert control(store, operation, job_id=started["job_id"]) == {
        "ok": False,
        "error": "invalid_operation",
    }
    assert store.list_gates(started["job_id"]) == []


def test_receipt_is_pending_and_unexpected_failures_are_sanitized(store):
    started = control(store, "start_or_resume")
    receipt = control(store, "receipt", job_id=started["job_id"])

    class BrokenStore:
        def list_jobs(self):
            raise RuntimeError("provider-secret-must-not-leak")

    failed = commerce_tool.commerce_control_from_origin(
        "status",
        origin=ORIGIN,
        store=BrokenStore(),
        config_loader=ENABLED,
    )

    assert receipt["receipt_status"] == "pending"
    assert failed == {"ok": False, "error": "commerce_control_failed"}


def test_answer_facts_completes_only_the_active_facts_gate_and_worker_resumes(
    store, tmp_path
):
    from commerce_operator import CommerceOperator

    started = control(store, "start_or_resume")
    worker = CommerceOperator(
        store=store,
        enabled_fn=lambda: True,
        lock_path=tmp_path / "worker.lock",
        approved_facts_loader=lambda: {},
    )
    worker.tick()
    awaiting = store.get_job(started["job_id"])
    assert awaiting["current_state"] == "awaiting_cal"
    gate = store.get_gate(awaiting["current_gate_id"])
    assert gate["gate_type"] == "facts"

    result = control(
        store,
        "answer_facts",
        job_id=started["job_id"],
        facts={
            "contact_email": "ops@example.invalid",
            "business_identity_sentence": "Warp Supply test operator",
            "double_opt_in": True,
            "brand_signoff": True,
            "privacy_signoff": True,
        },
    )

    assert result["gate_completed"] is True
    assert "cgh_" not in json.dumps(result)
    assert store.get_gate(gate["gate_id"])["status"] == "completed"
    worker.tick()
    assert store.get_job(started["job_id"])["current_state"] == "ready"

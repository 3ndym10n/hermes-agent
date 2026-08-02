from __future__ import annotations

import json
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import commerce_operator as commerce

import commerce_browser as browser
from commerce_browser import BrowserLifecycleError
from commerce_jobs import CommerceActionError, CommerceJobStore
from commerce_operator import (
    CommerceOperator,
    WorkerAlreadyRunningError,
    WorkerLock,
    fake_planner,
)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def make_store(tmp_path: Path) -> CommerceJobStore:
    store = CommerceJobStore(tmp_path / "commerce.db")
    store.initialize()
    return store


def make_job(store: CommerceJobStore, objective: str = "Build waitlist") -> dict:
    return store.create_or_attach_job(
        requester="telegram:42", objective=objective, now=NOW
    )


def operator(
    store: CommerceJobStore,
    tmp_path: Path,
    *,
    planner=fake_planner,
    handlers=None,
    enabled=lambda: True,
    browser_ensure=None,
    approved_facts_loader=lambda: {},
    gate_verifiers=None,
    reconcilers=None,
    completion_handler=None,
) -> CommerceOperator:
    options = dict(
        store=store,
        planner=planner,
        step_handlers=handlers,
        enabled_fn=enabled,
        lock_path=tmp_path / "worker.lock",
        clock=lambda: NOW,
        approved_facts_loader=approved_facts_loader,
        gate_verifiers=gate_verifiers,
        reconcilers=reconcilers,
        completion_handler=completion_handler,
    )
    if browser_ensure is not None:
        options["browser_ensure"] = browser_ensure
    return CommerceOperator(**options)


def step(
    step_id: str,
    action_type: str,
    effect_class: str,
    *,
    approval_reference: str = "",
) -> dict:
    return {
        "step_id": step_id,
        "action_type": action_type,
        "provider": "fake-provider",
        "effect_class": effect_class,
        "idempotency_key": f"fake:{step_id}",
        "request": {"fixture": step_id},
        "approval_reference": approval_reference,
    }


def approved_record(fact_code: str, fact_value, **overrides) -> dict:
    record = {
        "lifecycle_state": "approved",
        "record_type": "commerce_launch_fact",
        "scope": "silicon_current_v1",
        "provenance": "approved packet fact-1",
        "fact_code": fact_code,
        "fact_value": fact_value,
    }
    record.update(overrides)
    return record


def test_fake_provider_job_reaches_ready_without_model_work(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)

    stats = operator(store, tmp_path).run(once=True)

    current = store.get_job(job["job_id"])
    assert current["current_state"] == "ready"
    assert current["plan"]["provider_account"] == "fake-provider"
    assert stats["recovery"] == {
        "recoverable": 0,
        "uncertain": 0,
        "inconsistent": 0,
    }
    assert [event["to_state"] for event in store.list_events(job["job_id"])] == [
        "requested",
        "planning",
        "planning",
        "ready",
    ]


def test_read_and_idempotent_steps_dispatch_in_order(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    plan = {
        "provider_account": "fake-provider",
        "steps": [
            step("catalog", "read_catalog", "read_only"),
            step("page", "upsert_page", "idempotent_write"),
        ],
    }
    calls: list[str] = []

    def handler(_job, action):
        calls.append(action["action_type"])
        return {"result_code": "provider_truth_verified"}

    operator(
        store,
        tmp_path,
        planner=lambda _job, _facts: plan,
        handlers={"read_catalog": handler, "upsert_page": handler},
    ).tick()

    assert calls == ["read_catalog", "upsert_page"]
    assert store.get_job(job["job_id"])["current_state"] == "ready"
    actions = store.list_actions(job["job_id"])
    assert {action["effect_class"] for action in actions} == {
        "read_only",
        "idempotent_write",
    }
    assert {action["action_status"] for action in actions} == {"succeeded"}
    assert all(
        action["request"]["provider_idempotency_key"].startswith("fake:")
        for action in actions
    )


def test_ready_dispatches_an_idempotent_write_without_dead_ending(tmp_path):
    """A resumed job whose next step is a write must not park as out-of-order."""
    store = make_store(tmp_path)
    job = make_job(store)
    plan = {
        "provider_account": "fake-provider",
        "steps": [step("page", "upsert_page", "idempotent_write")],
    }
    calls: list[str] = []

    def handler(_job, action):
        calls.append(action["action_type"])
        return {"result_code": "provider_truth_verified"}

    operator(
        store,
        tmp_path,
        planner=lambda _job, _facts: plan,
        handlers={"upsert_page": handler},
    ).tick()

    assert calls == ["upsert_page"]
    snapshot = store.get_job(job["job_id"])
    assert snapshot["current_state"] == "ready"
    assert [event["reason_code"] for event in store.list_events(job["job_id"])].count(
        "operator_pause"
    ) == 0


def test_ready_still_refuses_an_unapproved_consequential_action(tmp_path):
    """`ready -> executing` must not become a way around the approval gate."""
    store = make_store(tmp_path)
    job = make_job(store)
    store.transition(
        job["job_id"],
        "planning",
        expected_state="requested",
        expected_version=int(job["row_version"]),
        actor="test",
        reason_code="claimed",
        now=NOW,
    )
    snapshot = store.get_job(job["job_id"])
    store.transition(
        job["job_id"],
        "ready",
        expected_state="planning",
        expected_version=int(snapshot["row_version"]),
        actor="test",
        reason_code="planned",
        now=NOW,
    )
    snapshot = store.get_job(job["job_id"])
    action = store.record_action(
        job["job_id"],
        action_type="register_domain",
        provider="fake-provider",
        effect_class="consequential",
        idempotency_key="fake:register",
        request={"step_id": "buy", "input": {}},
        target_state="executing",
        now=NOW,
    )

    with pytest.raises(CommerceActionError) as raised:
        store.dispatch_and_transition(
            action["action_id"],
            expected_state="ready",
            expected_version=int(snapshot["row_version"]),
            actor="test",
            now=NOW,
        )

    assert raised.value.code == "action_approval_required"
    assert store.get_job(job["job_id"])["current_state"] == "ready"


def test_completed_exact_approval_is_consumed_but_worker_never_grants_it(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    plan = {
        "provider_account": "fake-provider",
        "steps": [
            step(
                "purchase",
                "register_fake_domain",
                "consequential",
                approval_reference="fake:approval:1",
            )
        ],
    }
    worker = operator(
        store,
        tmp_path,
        planner=lambda _job, _facts: plan,
        handlers={
            "register_fake_domain": lambda _job, _action: {
                "result_code": "fake_registered"
            }
        },
    )

    worker.tick()
    waiting = store.get_job(job["job_id"])
    action = store.list_actions(job["job_id"])[0]
    gate = store.list_gates(job["job_id"])[0]
    assert waiting["current_state"] == "awaiting_purchase_approval"
    assert action["approval_status"] == "unbound"
    assert gate["status"] == "open"

    store.complete_gate(
        gate["gate_id"],
        evidence={"provider_truth_verified": True, "approval_granted": True},
        actor="telegram:42",
        now=NOW,
    )
    worker.tick()

    finished = store.get_action(action["action_id"])
    assert finished["action_status"] == "succeeded"
    assert finished["approval_status"] == "live"
    assert store.get_gate(gate["gate_id"])["status"] == "consumed"
    assert store.get_job(job["job_id"])["current_state"] == "ready"


def test_kill_mid_idempotent_step_recovers_and_redispatches(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    marker = tmp_path / "handler-entered"
    lock = tmp_path / "worker.lock"
    script = r"""
import sys, time
from pathlib import Path
from commerce_jobs import CommerceJobStore
from commerce_operator import CommerceOperator

db, lock, marker = sys.argv[1:]
plan = {
    "provider_account": "fake-provider",
    "steps": [
        {"step_id": "read", "action_type": "read_catalog", "provider": "fake-provider",
         "effect_class": "read_only", "idempotency_key": "fake:read", "request": {}},
        {"step_id": "write", "action_type": "upsert_page", "provider": "fake-provider",
         "effect_class": "idempotent_write", "idempotency_key": "fake:write", "request": {}},
    ],
}
def wait_handler(_job, _action):
    Path(marker).write_text("entered", encoding="utf-8")
    time.sleep(60)
    return {"result_code": "unexpected"}
worker = CommerceOperator(
    store=CommerceJobStore(db),
    planner=lambda _job, _facts: plan,
    step_handlers={
        "read_catalog": lambda _job, _action: {"result_code": "read"},
        "upsert_page": wait_handler,
    },
    enabled_fn=lambda: True,
    lock_path=lock,
    approved_facts_loader=lambda: {},
)
worker.run(once=True)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(store.path), str(lock), str(marker)],
        cwd=ROOT,
    )
    deadline = time.monotonic() + 8
    while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not marker.exists():
        child.kill()
        child.wait(timeout=5)
        pytest.fail("child did not enter the idempotent handler")
    child.kill()
    child.wait(timeout=5)

    before = store.list_actions(job["job_id"])
    interrupted = next(
        action for action in before if action["effect_class"] == "idempotent_write"
    )
    assert interrupted["action_status"] == "dispatched"
    resumed = operator(
        store,
        tmp_path,
        handlers={
            "upsert_page": lambda _job, _action: {
                "result_code": "provider_truth_verified"
            }
        },
    ).run(once=True)

    assert resumed["recovery"] == {
        "recoverable": 1,
        "uncertain": 0,
        "inconsistent": 0,
    }
    recovered = store.get_action(interrupted["action_id"])
    assert recovered["action_id"] == interrupted["action_id"]
    assert recovered["action_status"] == "succeeded"
    assert store.get_job(job["job_id"])["current_state"] == "ready"


def test_duplicate_process_lock_fails_closed(tmp_path):
    path = tmp_path / "worker.lock"
    with WorkerLock(path):
        with pytest.raises(WorkerAlreadyRunningError, match="worker_already_running"):
            with WorkerLock(path):
                pass


def test_kill_switch_is_rechecked_and_fails_closed(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    enabled = False

    def is_enabled():
        return enabled

    worker = operator(store, tmp_path, enabled=is_enabled)
    assert worker.tick()["enabled"] is False
    assert store.get_job(job["job_id"])["current_state"] == "requested"

    enabled = True
    assert worker.tick()["enabled"] is True
    assert store.get_job(job["job_id"])["current_state"] == "ready"


def test_exception_text_is_never_persisted(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    plan = {
        "provider_account": "fake-provider",
        "steps": [step("read", "read_catalog", "read_only")],
    }

    def broken(_job, _action):
        raise RuntimeError("password=hunter2 provider said secret")

    operator(
        store,
        tmp_path,
        planner=lambda _job, _facts: plan,
        handlers={"read_catalog": broken},
    ).tick()

    actions = store.list_actions(job["job_id"])
    assert len(actions) == 3
    assert store.get_job(job["job_id"])["current_state"] == "paused"
    assert {action["result"]["error_code"] for action in actions} == {
        "provider_step_failed"
    }
    assert "hunter2" not in str(store.list_events(job["job_id"]))
    assert "hunter2" not in str(actions)


def test_worker_cannot_complete_without_receipt_persistence(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    unsafe_step = step("read", "read_catalog", "read_only")
    unsafe_step["next_state"] = "completed"

    operator(
        store,
        tmp_path,
        planner=lambda _job, _facts: {
            "provider_account": "fake-provider",
            "steps": [unsafe_step],
        },
    ).tick()

    assert store.get_job(job["job_id"])["current_state"] == "paused"


def test_only_typed_current_approved_launch_fact_metadata_is_accepted():
    records = [
        approved_record(
            "business_identity_sentence",
            "Silicon Current is operated by Example Trading.",
        ),
        approved_record("business_identity", "legacy alias must not satisfy the gate"),
        approved_record("privacy_signoff", True, lifecycle_state="promoted"),
        approved_record("contact_email", "launch@example.com"),
        approved_record(
            "contact_email",
            "other@example.com",
            conflicts_with=["fact-previous"],
        ),
        approved_record(
            "double_opt_in",
            True,
            record_type="unrelated_approved_record",
        ),
        approved_record("double_opt_in", ["yes"]),
        approved_record("brand_signoff", True, provenance=""),
        approved_record("brand_signoff", True, superseded_by="fact-replacement"),
        approved_record("not_required", "ignored"),
    ]

    assert commerce._approved_launch_facts(records) == {
        "business_identity_sentence": "Silicon Current is operated by Example Trading.",
        "privacy_signoff": True,
    }


def test_approved_fact_loader_uses_existing_intake_config_and_bridge_token(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        commerce,
        "load_config_readonly",
        lambda: {
            "intake": {
                "enabled": True,
                "base_url": "https://cogitator.example",
            }
        },
    )
    monkeypatch.setenv(commerce.TOKEN_ENV, "bridge-token")

    def retrieve(**kwargs):
        calls.append(kwargs)
        return {"records": [approved_record("contact_email", "launch@example.com")]}

    monkeypatch.setattr(commerce, "request_intelligent_retrieval", retrieve)

    assert commerce.load_approved_launch_facts() == {
        "contact_email": "launch@example.com"
    }
    assert calls == [
        {
            "base_url": "https://cogitator.example",
            "token": "bridge-token",
            "task_description": calls[0]["task_description"],
        }
    ]
    task_description = calls[0]["task_description"]
    assert "record_type commerce_launch_fact" in task_description
    assert "scope silicon_current_v1" in task_description
    assert "Do not infer from prose" in task_description

    def failed_retrieval(**_kwargs):
        raise RuntimeError("bridge response contained a secret")

    monkeypatch.setattr(commerce, "request_intelligent_retrieval", failed_retrieval)
    assert commerce.load_approved_launch_facts() == {}


def test_local_job_facts_override_approved_retrieved_facts(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    store.record_facts(
        job["job_id"],
        {"contact_email": "local@example.com"},
        expected_version=job["row_version"],
        actor="cal",
        now=NOW,
    )
    retrieved = {
        "contact_email": "retrieved@example.com",
        "business_identity_sentence": "Silicon Current is operated by Example Trading.",
        "double_opt_in": True,
        "brand_signoff": True,
        "privacy_signoff": True,
    }
    observed = {}

    def planner(current_job, facts):
        observed.update(facts)
        return fake_planner(current_job, facts)

    worker = operator(
        store,
        tmp_path,
        planner=planner,
        approved_facts_loader=lambda: retrieved,
    )

    assert worker.tick()["errors"] == 0
    assert observed == {
        **retrieved,
        "contact_email": "local@example.com",
    }
    assert store.get_job(job["job_id"])["current_state"] == "ready"


def test_approved_facts_bridge_failure_opens_the_normal_facts_gate(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)

    def broken_loader():
        raise RuntimeError("bridge response contained a secret")

    worker = operator(
        store,
        tmp_path,
        planner=commerce.default_planner,
        approved_facts_loader=broken_loader,
    )

    assert worker.tick()["errors"] == 0
    current = store.get_job(job["job_id"])
    gate = store.list_gates(job["job_id"])[0]
    assert current["current_state"] == "awaiting_cal"
    assert gate["gate_type"] == "facts"
    assert gate["human_action"] == "Provide the missing approved launch facts."
    assert gate["provider_truth_reference"] == "commerce.facts"
    assert gate["opening_evidence"] == {
        "missing_fact_codes": sorted(commerce.REQUIRED_FACTS)
    }
    persisted = str(store.list_events(job["job_id"])) + str(gate)
    assert "bridge response contained a secret" not in persisted


@pytest.mark.parametrize(
    "value",
    [
        "file:///tmp/provider",
        "javascript:alert(1)",
        "https://user:pass@example.com/login",
        "https://example.com/login?token=value",
        "https://example.com/login#step",
        "http://example.com/login",
        "https://localhost/login",
        "https://10.0.0.1/login",
        "https://intranet/login",
        " https://example.com/login",
        "https://example.com\\@internal/login",
    ],
)
def test_browser_entry_url_rejects_unsafe_or_non_reenterable_values(value):
    with pytest.raises(BrowserLifecycleError, match="invalid_browser_entry_url"):
        browser.validate_entry_url(value)


@pytest.mark.parametrize(
    "value",
    [
        "about:blank",
        "https://example.com/provider/login",
        "http://127.0.0.1:8765/provider/login",
        "http://[::1]:8765/provider/login",
    ],
)
def test_browser_entry_url_accepts_public_https_and_loopback_smokes(value):
    assert browser.validate_entry_url(value) == value


def test_browser_lifecycle_launches_exact_profile_and_enables_stream(
    tmp_path, monkeypatch
):
    job_id = "cj_12345678_1234_1234_1234_123456789abc"
    session = f"commerce_{job_id}"
    calls = []
    status_calls = 0

    def fake_run(arguments, **kwargs):
        nonlocal status_calls
        command = arguments[arguments.index("--json") + 1 :]
        calls.append((arguments, kwargs, command))
        if command == ["session", "list"]:
            data = {"sessions": []}
        elif command[:1] == ["open"]:
            data = {"url": command[1]}
        elif command == ["stream", "status"]:
            status_calls += 1
            data = (
                {"enabled": False}
                if status_calls == 1
                else {"enabled": True, "port": 9_321}
            )
        elif command == ["stream", "enable"]:
            data = {"enabled": True, "port": 9_321}
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps({"success": True, "data": data})
        )

    monkeypatch.setattr(browser, "browser_binary", lambda: "/safe/agent-browser")
    monkeypatch.setattr(subprocess, "run", fake_run)
    profile_root = tmp_path / "profiles"
    socket_dir = tmp_path / "socket"

    result = browser.ensure_browser_session(
        job_id,
        session,
        "https://example.com/provider/login",
        profile_root=profile_root,
        socket_dir=socket_dir,
    )

    profile = profile_root / job_id
    launch = next(call for call in calls if call[2][:1] == ["open"])
    assert launch[0][-2:] == ["open", "https://example.com/provider/login"]
    assert launch[0][launch[0].index("--profile") + 1] == str(profile)
    assert result == {
        "profile": str(profile),
        "reattached": False,
        "session": session,
    }
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700
    assert stat.S_IMODE(socket_dir.stat().st_mode) == 0o700
    assert [call[2] for call in calls].count(["stream", "enable"]) == 1
    assert all(call[1]["stderr"] is subprocess.DEVNULL for call in calls)
    assert all(call[1]["stdin"] is subprocess.DEVNULL for call in calls)


def test_browser_lifecycle_reattaches_without_worker_navigation(tmp_path, monkeypatch):
    job_id = "cj_12345678_1234_1234_1234_123456789abc"
    session = f"commerce_{job_id}"
    commands = []

    def fake_run(arguments, **_kwargs):
        command = arguments[arguments.index("--json") + 1 :]
        commands.append(command)
        if command == ["session", "list"]:
            data = {"sessions": [session]}
        elif command == ["get", "cdp-url"]:
            data = {"cdpUrl": "ws://127.0.0.1:9222/devtools/browser/id"}
        else:
            data = {"enabled": True, "port": 9_322}
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps({"success": True, "data": data})
        )

    monkeypatch.setattr(browser, "browser_binary", lambda: "/safe/agent-browser")
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = browser.ensure_browser_session(
        job_id,
        session,
        "https://example.com/provider/login",
        profile_root=tmp_path / "profiles",
        socket_dir=tmp_path / "socket",
    )

    assert result["reattached"] is True
    assert commands == [
        ["session", "list"],
        ["get", "cdp-url"],
        ["stream", "status"],
    ]
    assert not any(command[:1] == ["open"] for command in commands)


def test_browser_lifecycle_never_navigates_a_listed_session_on_probe_error(
    tmp_path, monkeypatch
):
    job_id = "cj_12345678_1234_1234_1234_123456789abc"
    session = f"commerce_{job_id}"
    commands = []

    def fake_run(arguments, **_kwargs):
        command = arguments[arguments.index("--json") + 1 :]
        commands.append(command)
        if command == ["session", "list"]:
            return subprocess.CompletedProcess(
                arguments,
                0,
                json.dumps({"success": True, "data": {"sessions": [session]}}),
            )
        return subprocess.CompletedProcess(arguments, 1, "suppressed detail")

    monkeypatch.setattr(browser, "browser_binary", lambda: "/safe/agent-browser")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(BrowserLifecycleError, match="agent_browser_command_failed"):
        browser.ensure_browser_session(
            job_id,
            session,
            "https://example.com/provider/login",
            profile_root=tmp_path / "profiles",
            socket_dir=tmp_path / "socket",
        )

    assert commands == [["session", "list"], ["get", "cdp-url"]]
    assert not any(command[:1] == ["open"] for command in commands)


def test_worker_maintains_only_viewer_gates_with_bound_entry_urls(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    planning = store.transition(
        job["job_id"],
        "planning",
        expected_state="requested",
        expected_version=job["row_version"],
        actor="worker",
        reason_code="prepare_gate",
        now=NOW,
    )
    gate = store.open_gate(
        job["job_id"],
        gate_type="merchant_login",
        human_action="Complete provider login.",
        provider_truth_reference="provider.account",
        opening_evidence={"entry_url": "https://example.com/provider/login"},
        now=NOW,
    )
    store.transition(
        job["job_id"],
        "awaiting_cal",
        expected_state="planning",
        expected_version=planning["row_version"],
        actor="worker",
        reason_code="human_gate",
        gate_id=gate["gate_id"],
        now=NOW,
    )
    calls = []
    worker = operator(
        store,
        tmp_path,
        browser_ensure=lambda *arguments: calls.append(arguments) or {},
    )

    assert worker.tick()["errors"] == 0
    assert calls == [
        (
            job["job_id"],
            job["browser_session"],
            "https://example.com/provider/login",
        )
    ]
    worker.tick()
    assert len(calls) == 1


def test_facts_gate_never_launches_a_viewer_browser(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    calls = []
    worker = operator(
        store,
        tmp_path,
        planner=lambda _job, _facts: {"missing_facts": ["contact_email"]},
        browser_ensure=lambda *arguments: calls.append(arguments) or {},
    )

    assert worker.tick()["errors"] == 0
    assert store.get_job(job["job_id"])["current_state"] == "awaiting_cal"
    assert store.list_gates(job["job_id"])[0]["gate_type"] == "facts"
    assert calls == []


def test_import_does_not_load_agent_or_model_tool_modules():
    code = (
        "import sys, commerce_operator; "
        "assert 'run_agent' not in sys.modules; "
        "assert 'model_tools' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)


def test_verified_fact_gate_auto_resumes_without_fabricated_done(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    facts = {}
    worker = operator(
        store,
        tmp_path,
        planner=commerce.default_planner,
        approved_facts_loader=lambda: dict(facts),
    )

    worker.tick()
    assert store.get_job(job["job_id"])["current_state"] == "awaiting_cal"

    facts.update({
        "contact_email": "launch@example.com",
        "business_identity_sentence": "Silicon Current is operated by Example Trading.",
        "double_opt_in": True,
        "brand_signoff": True,
        "privacy_signoff": True,
    })
    worker.tick()

    assert store.get_job(job["job_id"])["current_state"] == "ready"
    gate = store.list_gates(job["job_id"])[0]
    assert gate["status"] == "consumed"
    assert gate["done_requested_at"] is None
    assert gate["completion_evidence"] == {
        "provider_truth_verified": True,
        "fact_codes": sorted(commerce.REQUIRED_FACTS),
    }


@pytest.mark.parametrize("effect_class", ["read_only", "idempotent_write"])
def test_human_gate_requires_done_and_provider_truth_before_resume(
    tmp_path, effect_class
):
    store = make_store(tmp_path)
    job = make_job(store)
    plan = {
        "provider_account": "fake-provider",
        "steps": (
            [step("read", "read_catalog", "read_only")]
            if effect_class == "idempotent_write"
            else []
        )
        + [step("login", "request_provider_login", effect_class)],
    }
    worker = operator(
        store,
        tmp_path,
        planner=lambda _job, _facts: plan,
        handlers={
            "read_catalog": lambda _job, _action: {"result_code": "read"},
            "request_provider_login": lambda _job, _action: {
                "result_code": "gate_required",
                "_human_gate": {
                    "gate_type": "provider_login",
                    "human_action": "Complete provider login.",
                    "provider_truth_reference": "provider.account",
                    "opening_evidence": {
                        "entry_url": "https://example.com/provider/login"
                    },
                },
            },
        },
        browser_ensure=lambda *_arguments: {},
        gate_verifiers={
            "provider_login": lambda _job, _gate: {
                "provider_truth_verified": True,
                "account_ref": "provider-account",
            }
        },
    )

    worker.tick()
    waiting = store.get_job(job["job_id"])
    gate = store.get_gate(waiting["current_gate_id"])
    assert waiting["current_state"] == "awaiting_cal"
    assert gate["status"] == "open"
    worker.tick()
    assert store.get_job(job["job_id"])["current_state"] == "awaiting_cal"

    _, token = store.issue_gate_handoff(gate["gate_id"], now=NOW)
    store.request_gate_done(gate["gate_id"], token, actor="cal", now=NOW)
    worker.tick()

    assert store.get_job(job["job_id"])["current_state"] == "ready"
    assert store.get_gate(gate["gate_id"])["status"] == "consumed"


def _approved_consequential_job(store, tmp_path, handler, *, reconcilers=None):
    job = make_job(store)
    plan = {
        "provider_account": "fake-provider",
        "steps": [
            step(
                "purchase",
                "register_fake_domain",
                "consequential",
                approval_reference="fake:approval:1",
            )
        ],
    }
    worker = operator(
        store,
        tmp_path,
        planner=lambda _job, _facts: plan,
        handlers={"register_fake_domain": handler},
        reconcilers=reconcilers,
    )
    worker.tick()
    gate = store.list_gates(job["job_id"])[0]
    store.complete_gate(
        gate["gate_id"],
        evidence={"provider_truth_verified": True, "approval_granted": True},
        actor="cal",
        now=NOW,
    )
    return job, worker


def test_definitive_consequential_failure_is_not_marked_uncertain(tmp_path):
    store = make_store(tmp_path)

    def taken(_job, _action):
        raise commerce.ProviderStepError("domain_taken")

    job, worker = _approved_consequential_job(store, tmp_path, taken)
    worker.tick()

    action = store.list_actions(job["job_id"])[0]
    assert action["action_status"] == "failed"
    assert action["uncertainty"] is False
    assert store.get_job(job["job_id"])["current_state"] == "paused"
    assert "uncertain_external_state" not in {
        event["to_state"] for event in store.list_events(job["job_id"])
    }


def test_uncertain_consequential_result_reconciles_from_provider_truth(tmp_path):
    store = make_store(tmp_path)

    def unknown(_job, _action):
        raise commerce.ProviderStepError("provider_timeout", uncertain=True)

    job, worker = _approved_consequential_job(
        store,
        tmp_path,
        unknown,
        reconcilers={
            "register_fake_domain": lambda _job, _action: {
                "status": "succeeded",
                "evidence": {
                    "provider_truth_verified": True,
                    "domain_present": True,
                },
            }
        },
    )
    worker.tick()

    action = store.list_actions(job["job_id"])[0]
    assert action["action_status"] == "succeeded"
    assert action["result"]["reconciliation"]["domain_present"] is True
    assert store.get_job(job["job_id"])["current_state"] == "ready"
    states = [event["to_state"] for event in store.list_events(job["job_id"])]
    assert "uncertain_external_state" in states
    assert "reconciliation_required" in states


def test_plan_replacement_and_receipt_control_are_durable(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    discovery = step("discovery", "discover", "read_only")
    final = step("final", "final_verify", "read_only")
    initial = {"provider_account": "fake-provider", "steps": [discovery]}
    replacement = {
        "provider_account": "fake-provider",
        "domain": "siliconcurrent.com",
        "steps": [discovery, final],
    }
    receipts = []

    def complete(job_id, payload):
        receipts.append((job_id, dict(payload)))
        assert store.list_actions(job_id)[-1]["action_status"] == "succeeded"
        return {"receipt_ref": f"receipts/{job_id}.json"}

    worker = operator(
        store,
        tmp_path,
        planner=lambda _job, _facts: initial,
        handlers={
            "discover": lambda _job, _action: {
                "evidence_ref": f"evidence/{job['job_id']}/discovery.json",
                "_replace_plan": replacement,
            },
            "final_verify": lambda _job, _action: {
                "evidence_ref": f"evidence/{job['job_id']}/verification.json",
                "provider_truth_verified": True,
                "_complete": {"verification": "all_green"},
            },
        },
        completion_handler=complete,
    )

    worker.tick()

    current = store.get_job(job["job_id"])
    assert current["current_state"] == "completed"
    assert current["plan"]["domain"] == "siliconcurrent.com"
    assert receipts == [(job["job_id"], {"verification": "all_green"})]
    actions = store.list_actions(job["job_id"])
    discovery_action = next(
        item for item in actions if item["request"]["step_id"] == "discovery"
    )
    final_action = next(
        item for item in actions if item["request"]["step_id"] == "final"
    )
    assert "_replace_plan" not in discovery_action["result"]
    assert final_action["result"]["operator_control"]["completion"] == {
        "verification": "all_green"
    }

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest  # ty: ignore[unresolved-import]

import commerce_jobs as commerce

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts" / "commerce_job_cli.py"


def make_store(tmp_path: Path, name: str = "commerce.db") -> commerce.CommerceJobStore:
    store = commerce.CommerceJobStore(tmp_path / name)
    store.initialize()
    return store


def make_job(
    store: commerce.CommerceJobStore,
    *,
    objective: str = "Launch the governed GPU store",
    requester: str = "cal",
) -> dict:
    return store.create_or_attach_job(requester=requester, objective=objective, now=NOW)


def raw_connection(store: commerce.CommerceJobStore) -> sqlite3.Connection:
    connection = sqlite3.connect(store.path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def seed_state(
    store: commerce.CommerceJobStore,
    job_id: str,
    state: str,
    *,
    version: int = 0,
    deadline: datetime | None = None,
) -> None:
    with raw_connection(store) as connection:
        connection.execute(
            """UPDATE jobs SET current_state=?,row_version=?,active=?,
               deadline_at=?,state_entered_at=?,updated_at=?
               WHERE job_id=?""",
            (
                state,
                version,
                0 if state in commerce.TERMINAL_STATES else 1,
                commerce.iso_utc(deadline or (NOW + timedelta(hours=72))),
                commerce.iso_utc(NOW),
                commerce.iso_utc(NOW),
                job_id,
            ),
        )


def record_action(
    store: commerce.CommerceJobStore,
    job_id: str,
    *,
    effect_class: str = "read_only",
    target_state: str = "",
    action_type: str = "read_provider",
    key: str = "action-key",
    approve: bool = True,
    approval_reference: str = "",
) -> dict:
    if not target_state:
        target_state = (
            "executing"
            if effect_class in {"consequential", "idempotent_write"}
            else "executing_read_only"
        )
    action = store.record_action(
        job_id,
        action_type=action_type,
        provider="fake",
        effect_class=effect_class,
        idempotency_key=key,
        request={"operation": action_type, "target": "example.com"},
        target_state=target_state,
        now=NOW,
    )
    if effect_class != "consequential" or not approve:
        return action
    gate = store.open_gate(
        job_id,
        gate_type="action_approval",
        human_action=f"Approve {action_type}",
        provider_truth_reference="approval-verification-ref",
        opening_evidence={"action_id": action["action_id"]},
        approval_reference=approval_reference or f"approval-{key}",
        approval_fingerprint=action["action_fingerprint"],
        now=NOW,
    )
    store.complete_gate(
        gate["gate_id"],
        evidence={
            "approval_granted": True,
            "provider_truth_verified": True,
        },
        actor="operator",
        now=NOW,
    )
    return store.authorize_action(
        action["action_id"],
        gate_id=gate["gate_id"],
        actor="operator",
        now=NOW,
    )


def open_active_handoff_gate(
    store: commerce.CommerceJobStore,
    job: dict,
    *,
    expires_at: datetime | None = None,
) -> dict:
    seed_state(store, job["job_id"], "planning")
    gate = store.open_gate(
        job["job_id"],
        gate_type="merchant_login",
        human_action="Complete provider login",
        provider_truth_reference="provider-account-status-ref",
        opening_evidence={},
        expires_at=expires_at,
        now=NOW,
    )
    store.transition(
        job["job_id"],
        "awaiting_cal",
        expected_state="planning",
        expected_version=0,
        actor="worker",
        reason_code="human_action_required",
        gate_id=gate["gate_id"],
        now=NOW,
    )
    return gate


def run_cli(
    db_path: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    command_env.update(env or {})
    command_env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, str(CLI), "--db", str(db_path), *arguments],
        cwd=ROOT,
        env=command_env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def test_schema_initialization_is_idempotent_and_additive(tmp_path):
    path = tmp_path / "commerce.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE preserved_marker(value TEXT)")
        connection.execute("INSERT INTO preserved_marker VALUES ('kept')")
    store = commerce.CommerceJobStore(path)
    store.initialize()
    store.initialize()
    with raw_connection(store) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "jobs",
            "job_events",
            "job_actions",
            "provider_accounts",
            "gates",
            "preserved_marker",
        } <= tables
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        assert {"current_step", "current_gate_id", "browser_session"} <= columns
        action_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(job_actions)")
        }
        assert "plan_fingerprint" in action_columns
        gate_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(gates)")
        }
        assert {
            "browser_session",
            "handoff_token_hash",
            "handoff_expires_at",
            "done_requested_at",
        } <= gate_columns
        assert (
            connection.execute("SELECT value FROM preserved_marker").fetchone()[0]
            == "kept"
        )


def test_new_jobs_use_the_v2_state_machine_and_empty_current_step(tmp_path):
    job = make_job(make_store(tmp_path))
    assert job["state_machine_version"] == 2
    assert job["current_state"] == "requested"
    assert job["current_step"] == ""
    assert job["current_gate_id"] == ""
    assert job["browser_session"] == f"commerce_{job['job_id']}"


def test_job_origin_is_persisted_once_and_attach_retains_original(tmp_path):
    store = make_store(tmp_path)
    original_origin = {
        "platform": "telegram",
        "chat_id": "chat-123",
        "thread_id": "thread-7",
        "user_id": "user-42",
        "message_id": "message-9",
    }
    first = store.create_or_attach_job(
        requester="cal",
        objective="Launch the governed GPU store",
        origin=original_origin,
        now=NOW,
    )
    attached = store.create_or_attach_job(
        requester="cal",
        objective=" Launch   the governed GPU store ",
        origin={"platform": "discord", "chat_id": "different-chat"},
        now=NOW + timedelta(minutes=1),
    )

    assert first["attached"] is False
    assert attached["attached"] is True
    assert attached["job_id"] == first["job_id"]
    events = store.list_events(first["job_id"])
    assert len(events) == 1
    assert events[0]["event_type"] == "job_created"
    assert events[0]["evidence"] == {"origin": original_origin}


def test_job_origin_rejects_forbidden_data_before_persistence(tmp_path):
    store = make_store(tmp_path)

    with pytest.raises(
        commerce.CommerceForbiddenDataError, match="forbidden_sensitive_field"
    ):
        store.create_or_attach_job(
            requester="cal",
            objective="Launch the governed GPU store",
            origin={"platform": "telegram", "password": "do-not-store"},
            now=NOW,
        )

    assert store.list_jobs() == []


def test_facts_are_append_only_versioned_and_latest_is_readable(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    assert store.latest_facts(job["job_id"]) == {}

    first = store.record_facts(
        job["job_id"],
        {"brand_name": "Silicon Current", "currency": "GBP"},
        expected_version=0,
        actor="cal",
        now=NOW + timedelta(minutes=1),
    )
    second_facts = {"brand_name": "Silicon Current", "currency": "USD"}
    second = store.record_facts(
        job["job_id"],
        second_facts,
        expected_version=first["row_version"],
        actor="cal",
        now=NOW + timedelta(minutes=2),
    )

    assert first["row_version"] == 1
    assert second["row_version"] == 2
    assert store.latest_facts(job["job_id"]) == second_facts
    fact_events = [
        event
        for event in store.list_events(job["job_id"])
        if event["event_type"] == "facts_answered"
    ]
    assert [event["evidence"]["facts"] for event in fact_events] == [
        {"brand_name": "Silicon Current", "currency": "GBP"},
        second_facts,
    ]


def test_facts_reject_forbidden_data_and_stale_versions_without_changes(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    initial_events = store.list_events(job["job_id"])

    with pytest.raises(
        commerce.CommerceForbiddenDataError, match="forbidden_sensitive_field"
    ):
        store.record_facts(
            job["job_id"],
            {"password": "do-not-store"},
            expected_version=0,
            actor="cal",
            now=NOW,
        )
    with pytest.raises(commerce.CommerceStaleVersionError, match="stale_row_version"):
        store.record_facts(
            job["job_id"],
            {"brand_name": "Silicon Current"},
            expected_version=7,
            actor="cal",
            now=NOW,
        )

    assert store.get_job(job["job_id"])["row_version"] == 0
    assert store.latest_facts(job["job_id"]) == {}
    assert store.list_events(job["job_id"]) == initial_events


def test_connection_enables_foreign_keys_and_bounded_busy_timeout(tmp_path):
    store = make_store(tmp_path)
    connection = store._connect()
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert (
            connection.execute("PRAGMA busy_timeout").fetchone()[0]
            == commerce.BUSY_TIMEOUT_MS
        )
    finally:
        connection.close()


def test_future_schema_version_is_rejected(tmp_path):
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=3")
    with pytest.raises(
        commerce.CommerceConfigurationError, match="future_schema_version"
    ):
        commerce.CommerceJobStore(path).initialize()


def test_unshipped_v1_schema_is_rejected_without_partial_upgrade(tmp_path):
    path = tmp_path / "v1.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE jobs(job_id TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version=1")
    with pytest.raises(
        commerce.CommerceConfigurationError, match="unsupported_schema_version"
    ):
        commerce.CommerceJobStore(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("PRAGMA table_info(jobs)").fetchall() == [
            (0, "job_id", "TEXT", 0, None, 1)
        ]


def test_default_path_permissions_are_user_only(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    store = commerce.CommerceJobStore()
    store.initialize()
    assert store.path == hermes_home / "commerce" / "commerce_jobs.db"
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_job_events_are_append_only_at_database_level(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    with raw_connection(store) as connection:
        event_id = connection.execute(
            "SELECT event_id FROM job_events WHERE job_id=?", (job["job_id"],)
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            connection.execute(
                "UPDATE job_events SET actor='other' WHERE event_id=?", (event_id,)
            )
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            connection.execute("DELETE FROM job_events WHERE event_id=?", (event_id,))
    assert len(store.list_events(job["job_id"])) == 1


def test_job_events_reject_non_monotonic_insert(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    with raw_connection(store) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="invalid_sequence"):
            connection.execute(
                """INSERT INTO job_events
                   (event_id,job_id,sequence,event_type,from_state,to_state,
                    actor,reason_code,evidence_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    "invalid-event",
                    job["job_id"],
                    3,
                    "invalid",
                    "requested",
                    "requested",
                    "test",
                    "invalid",
                    "{}",
                    commerce.iso_utc(NOW),
                ),
            )


def test_v2_state_machine_contract():
    states = frozenset({
        "requested",
        "planning",
        "ready",
        "executing_read_only",
        "awaiting_purchase_approval",
        "awaiting_dns_approval",
        "awaiting_publication_approval",
        "awaiting_cal",
        "executing",
        "resuming",
        "verifying",
        "uncertain_external_state",
        "reconciliation_required",
        "timed_out",
        "paused",
        "completed",
        "failed",
        "cancelled",
    })
    core = {
        "requested": {"planning"},
        "planning": {"ready", "awaiting_cal"},
        "ready": {"awaiting_purchase_approval", "executing_read_only", "executing"},
        "executing_read_only": {
            "ready",
            "awaiting_dns_approval",
            "awaiting_cal",
            "executing",
            "verifying",
            "completed",
        },
        "awaiting_purchase_approval": {"executing", "cancelled"},
        "awaiting_dns_approval": {"executing", "cancelled"},
        "awaiting_publication_approval": {"executing", "paused"},
        "awaiting_cal": {"resuming", "timed_out"},
        "executing": {
            "executing_read_only",
            "ready",
            "uncertain_external_state",
            "awaiting_cal",
        },
        "resuming": {
            "planning",
            "ready",
            "executing_read_only",
            "awaiting_purchase_approval",
            "awaiting_dns_approval",
            "awaiting_publication_approval",
            "awaiting_cal",
            "executing",
            "verifying",
        },
        "verifying": {"awaiting_publication_approval", "ready"},
        "uncertain_external_state": {"reconciliation_required"},
        "reconciliation_required": {"ready", "failed"},
        "timed_out": {"ready", "cancelled"},
        "paused": {"ready", "cancelled"},
    }
    expected = {}
    controls_excluded = (
        commerce.TERMINAL_STATES
        | commerce.UNCERTAINTY_STATES
        | {"executing", "paused", "timed_out"}
    )
    for state in states:
        targets = set(core.get(state, set()))
        if state not in controls_excluded:
            targets.update({"paused", "cancelled", "timed_out"})
        expected[state] = frozenset(targets)

    assert commerce.STATE_MACHINE_VERSION == 2
    assert commerce.STATES == states
    assert commerce.TERMINAL_STATES == {"completed", "failed", "cancelled"}
    assert commerce.UNCERTAINTY_STATES == {
        "uncertain_external_state",
        "reconciliation_required",
    }
    assert commerce.EXECUTION_STATES == {"executing_read_only", "executing"}
    assert commerce.TIMEOUT_EXCLUDED_STATES == (
        commerce.TERMINAL_STATES
        | commerce.UNCERTAINTY_STATES
        | {"executing", "timed_out", "paused"}
    )
    assert commerce._CORE_TRANSITIONS == core
    assert commerce.ALLOWED_TRANSITIONS == expected


def test_every_allowed_transition_updates_atomically_with_one_event(tmp_path):
    store = make_store(tmp_path)
    index = 0
    for source, targets in commerce.ALLOWED_TRANSITIONS.items():
        for target in targets:
            index += 1
            job = make_job(store, objective=f"transition {index}")
            seed_state(
                store,
                job["job_id"],
                source,
                deadline=NOW if target == "timed_out" else None,
            )
            if source == "awaiting_cal":
                source_gate = store.open_gate(
                    job["job_id"],
                    gate_type="merchant_login",
                    human_action="Complete provider login",
                    provider_truth_reference="provider-account-status-ref",
                    opening_evidence={},
                    now=NOW,
                )
                with raw_connection(store) as connection:
                    connection.execute(
                        "UPDATE jobs SET current_gate_id=? WHERE job_id=?",
                        (source_gate["gate_id"], job["job_id"]),
                    )
                if target == "resuming":
                    _, token = store.issue_gate_handoff(source_gate["gate_id"], now=NOW)
                    store.request_gate_done(
                        source_gate["gate_id"], token, actor="cal", now=NOW
                    )
                    store.complete_gate(
                        source_gate["gate_id"],
                        evidence={"provider_truth_verified": True},
                        actor="worker",
                        now=NOW,
                    )
            gate_id = ""
            if target == "awaiting_cal":
                target_gate = store.open_gate(
                    job["job_id"],
                    gate_type="merchant_login",
                    human_action="Complete provider login",
                    provider_truth_reference="provider-account-status-ref",
                    opening_evidence={},
                    now=NOW,
                )
                gate_id = target_gate["gate_id"]
            action_id = ""
            if target in commerce.EXECUTION_STATES:
                consequential = target == "executing"
                action = record_action(
                    store,
                    job["job_id"],
                    effect_class="consequential" if consequential else "read_only",
                    target_state=target,
                    action_type=f"step_{index}",
                    key=f"dispatch-{index}",
                )
                action_id = action["action_id"]
                store.dispatch_action(action_id, now=NOW)
            before = len(store.list_events(job["job_id"]))
            if target == "timed_out":
                assert store.sweep_timeouts(now=NOW) == [job["job_id"]]
                updated = store.get_job(job["job_id"])
            else:
                updated = store.transition(
                    job["job_id"],
                    target,
                    expected_state=source,
                    expected_version=0,
                    actor="test",
                    reason_code="allowed_transition",
                    action_id=action_id,
                    gate_id=gate_id,
                    now=NOW,
                )
            assert updated["current_state"] == target
            assert updated["row_version"] == 1
            if action_id:
                assert updated["current_step"] == f"step_{index}"
            if target == "awaiting_cal":
                assert updated["current_gate_id"] == gate_id
            assert len(store.list_events(job["job_id"])) == before + 1


def test_disallowed_unknown_stale_and_duplicate_transitions_change_nothing(
    tmp_path,
):
    store = make_store(tmp_path)
    job = make_job(store)
    original_events = store.list_events(job["job_id"])
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="transition_not_allowed"
    ):
        store.transition(
            job["job_id"],
            "completed",
            expected_state="requested",
            expected_version=0,
            actor="test",
            reason_code="illegal",
        )
    with pytest.raises(commerce.CommerceInvalidTransitionError, match="unknown_state"):
        store.transition(
            job["job_id"],
            "invented",
            expected_state="requested",
            expected_version=0,
            actor="test",
            reason_code="illegal",
        )
    with pytest.raises(commerce.CommerceInvalidTransitionError, match="unknown_state"):
        store.transition(
            job["job_id"],
            "planning",
            expected_state="invented",
            expected_version=0,
            actor="test",
            reason_code="illegal",
        )
    with pytest.raises(commerce.CommerceStaleVersionError, match="stale_row_version"):
        store.transition(
            job["job_id"],
            "planning",
            expected_state="requested",
            expected_version=9,
            actor="test",
            reason_code="stale",
        )
    assert store.get_job(job["job_id"])["current_state"] == "requested"
    assert store.list_events(job["job_id"]) == original_events
    updated = store.transition(
        job["job_id"],
        "planning",
        expected_state="requested",
        expected_version=0,
        actor="test",
        reason_code="valid",
    )
    before_duplicate = store.list_events(job["job_id"])
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="unexpected_current_state"
    ):
        store.transition(
            job["job_id"],
            "planning",
            expected_state="requested",
            expected_version=0,
            actor="test",
            reason_code="duplicate",
        )
    assert store.get_job(job["job_id"]) == updated
    assert store.list_events(job["job_id"]) == before_duplicate


def test_state_update_rolls_back_when_event_insert_fails(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    with raw_connection(store) as connection:
        connection.execute(
            """CREATE TRIGGER fail_new_event BEFORE INSERT ON job_events
               BEGIN SELECT RAISE(ABORT, 'forced_event_failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="forced_event_failure"):
        store.transition(
            job["job_id"],
            "planning",
            expected_state="requested",
            expected_version=0,
            actor="test",
            reason_code="atomic",
        )
    current = store.get_job(job["job_id"])
    assert current["current_state"] == "requested"
    assert current["row_version"] == 0
    assert len(store.list_events(job["job_id"])) == 1


def test_read_action_dispatch_and_execution_binding_are_atomic(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "ready")
    action = record_action(
        store,
        job["job_id"],
        effect_class="read_only",
        action_type="read_inventory",
    )
    before_events = len(store.list_events(job["job_id"]))

    result = store.dispatch_and_transition(
        action["action_id"],
        expected_state="ready",
        expected_version=0,
        actor="worker",
        now=NOW,
    )

    assert result["redispatched"] is False
    assert result["action"]["action_status"] == "dispatched"
    assert result["job"]["current_state"] == "executing_read_only"
    assert result["job"]["current_step"] == "read_inventory"
    assert result["job"]["row_version"] == 1
    events = store.list_events(job["job_id"])
    assert len(events) == before_events + 1
    assert events[-1]["event_type"] == "state_transition"
    assert events[-1]["evidence"] == {"action_id": action["action_id"]}


def test_atomic_dispatch_rolls_back_action_and_job_when_event_insert_fails(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "ready")
    action = record_action(store, job["job_id"], effect_class="read_only")
    before_events = store.list_events(job["job_id"])
    with raw_connection(store) as connection:
        connection.execute(
            """CREATE TRIGGER fail_atomic_event BEFORE INSERT ON job_events
               BEGIN SELECT RAISE(ABORT, 'forced_atomic_event_failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced_atomic_event_failure"):
        store.dispatch_and_transition(
            action["action_id"],
            expected_state="ready",
            expected_version=0,
            actor="worker",
            now=NOW,
        )

    unchanged_action = store.get_action(action["action_id"])
    unchanged_job = store.get_job(job["job_id"])
    assert unchanged_action["action_status"] == "planned"
    assert unchanged_action["dispatched_at"] is None
    assert unchanged_job["current_state"] == "ready"
    assert unchanged_job["row_version"] == 0
    assert store.list_events(job["job_id"]) == before_events


def test_consequential_atomic_dispatch_requires_and_binds_exact_approval(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "awaiting_purchase_approval")
    unapproved = record_action(
        store,
        job["job_id"],
        effect_class="consequential",
        action_type="register_domain",
        key="unapproved-domain",
        approve=False,
    )

    with pytest.raises(commerce.CommerceActionError, match="action_approval_required"):
        store.dispatch_and_transition(
            unapproved["action_id"],
            expected_state="awaiting_purchase_approval",
            expected_version=0,
            actor="worker",
            now=NOW,
        )

    assert store.get_action(unapproved["action_id"])["action_status"] == "planned"
    assert store.get_job(job["job_id"])["current_state"] == (
        "awaiting_purchase_approval"
    )
    approved = record_action(
        store,
        job["job_id"],
        effect_class="consequential",
        action_type="register_domain",
        key="approved-domain",
    )
    result = store.dispatch_and_transition(
        approved["action_id"],
        expected_state="awaiting_purchase_approval",
        expected_version=0,
        actor="worker",
        now=NOW,
    )

    assert result["action"]["action_status"] == "dispatched"
    assert result["job"]["current_state"] == "executing"
    assert result["job"]["current_step"] == "register_domain"
    assert {action["action_id"] for action in store.list_actions(job["job_id"])} == {
        unapproved["action_id"],
        approved["action_id"],
    }
    gates = store.list_gates(job["job_id"])
    assert len(gates) == 1
    assert gates[0]["gate_type"] == "action_approval"
    assert gates[0]["status"] == "consumed"


def test_idempotent_write_dispatches_without_approval_and_recovers_for_redispatch(
    tmp_path,
):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "resuming")
    action = record_action(
        store,
        job["job_id"],
        effect_class="idempotent_write",
        action_type="ensure_draft_product",
        key="draft-product-1",
    )

    first = store.dispatch_and_transition(
        action["action_id"],
        expected_state="resuming",
        expected_version=0,
        actor="worker",
        now=NOW,
    )
    assert first["action"]["approval_status"] == "unbound"
    assert first["action"]["action_status"] == "dispatched"
    assert first["job"]["current_state"] == "executing"
    assert first["job"]["current_step"] == "ensure_draft_product"
    assert first["job"]["row_version"] == 1

    recovered = store.recover(now=NOW + timedelta(minutes=1))
    assert recovered == {"recoverable": 1, "uncertain": 0, "inconsistent": 0}
    assert store.get_action(action["action_id"])["action_status"] == "recoverable"
    event_count = len(store.list_events(job["job_id"]))
    second = store.dispatch_and_transition(
        action["action_id"],
        expected_state="executing",
        expected_version=1,
        actor="worker",
        now=NOW + timedelta(minutes=2),
    )

    assert second["redispatched"] is True
    assert second["action"]["action_status"] == "dispatched"
    assert second["job"]["current_state"] == "executing"
    assert second["job"]["row_version"] == 1
    assert len(store.list_events(job["job_id"])) == event_count
    finished = store.finish_action(
        action["action_id"],
        status="succeeded",
        result={"provider_truth_verified": True},
        now=NOW + timedelta(minutes=3),
    )
    assert finished["action_status"] == "succeeded"


@pytest.mark.parametrize("terminal", sorted(commerce.TERMINAL_STATES))
def test_terminal_states_cannot_transition(tmp_path, terminal):
    store = make_store(tmp_path)
    job = make_job(store, objective=f"terminal {terminal}")
    seed_state(store, job["job_id"], terminal)
    with pytest.raises(commerce.CommerceInvalidTransitionError):
        store.transition(
            job["job_id"],
            "paused",
            expected_state=terminal,
            expected_version=0,
            actor="test",
            reason_code="terminal",
        )


def test_generated_job_id_is_never_payload_redacted(tmp_path, monkeypatch):
    generated = "4242424242424242abcdefabcdefabcd"
    numeric_uuid = commerce.uuid.UUID(hex=generated)
    monkeypatch.setattr(commerce.uuid, "uuid4", lambda: numeric_uuid)
    store = make_store(tmp_path)
    job = make_job(store)
    expected = "cj_42424242_4242_4242_abcd_efabcdefabcd"
    assert job["job_id"] == expected
    assert store.get_job(job["job_id"])["job_id"] == expected
    assert len(store.list_events(job["job_id"])) == 1
    commerce.reject_forbidden_data({"job_id": expected})


def test_execution_states_require_bound_actions_and_persist_current_step(tmp_path):
    store = make_store(tmp_path)
    read_job = make_job(store)
    seed_state(store, read_job["job_id"], "ready")
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="action_required"
    ):
        store.transition(
            read_job["job_id"],
            "executing_read_only",
            expected_state="ready",
            expected_version=0,
            actor="worker",
            reason_code="dispatch",
        )
    wrong_effect = record_action(
        store,
        read_job["job_id"],
        effect_class="consequential",
        target_state="executing_read_only",
        action_type="read_inventory",
    )
    store.dispatch_action(wrong_effect["action_id"], now=NOW)
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="action_not_bound"
    ):
        store.transition(
            read_job["job_id"],
            "executing_read_only",
            expected_state="ready",
            expected_version=0,
            actor="worker",
            reason_code="dispatch",
            action_id=wrong_effect["action_id"],
        )
    store.finish_action(
        wrong_effect["action_id"],
        status="failed",
        result={"reason": "effect_class_mismatch"},
        now=NOW,
    )
    read_action = record_action(
        store,
        read_job["job_id"],
        effect_class="read_only",
        target_state="executing_read_only",
        action_type="read_inventory",
        key="read-action",
    )
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="action_not_bound"
    ):
        store.transition(
            read_job["job_id"],
            "executing_read_only",
            expected_state="ready",
            expected_version=0,
            actor="worker",
            reason_code="dispatch",
            action_id=read_action["action_id"],
        )
    assert store.recover(now=NOW) == {
        "recoverable": 0,
        "uncertain": 0,
        "inconsistent": 0,
    }
    store.dispatch_action(read_action["action_id"], now=NOW)
    reading = store.transition(
        read_job["job_id"],
        "executing_read_only",
        expected_state="ready",
        expected_version=0,
        actor="worker",
        reason_code="dispatch",
        action_id=read_action["action_id"],
    )
    assert reading["current_step"] == "read_inventory"

    write_job = make_job(store, objective="Execute approved mutation")
    seed_state(store, write_job["job_id"], "awaiting_purchase_approval")
    unbound = store.record_action(
        write_job["job_id"],
        action_type="register_domain",
        provider="fake",
        effect_class="consequential",
        idempotency_key="unbound-write",
        request={"operation": "register_domain"},
        target_state="executing",
        now=NOW,
    )
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="action_not_bound"
    ):
        store.transition(
            write_job["job_id"],
            "executing",
            expected_state="awaiting_purchase_approval",
            expected_version=0,
            actor="worker",
            reason_code="dispatch",
            action_id=unbound["action_id"],
        )
    with pytest.raises(commerce.CommerceActionError, match="action_approval_required"):
        store.dispatch_action(unbound["action_id"])

    approved = record_action(
        store,
        write_job["job_id"],
        effect_class="consequential",
        target_state="executing",
        action_type="register_domain",
        key="approved-write",
    )
    store.dispatch_action(approved["action_id"], now=NOW)
    executing = store.transition(
        write_job["job_id"],
        "executing",
        expected_state="awaiting_purchase_approval",
        expected_version=0,
        actor="worker",
        reason_code="dispatch",
        action_id=approved["action_id"],
    )
    assert executing["current_step"] == "register_domain"

    gate = store.open_gate(
        write_job["job_id"],
        gate_type="provider_challenge",
        human_action="Complete provider challenge",
        provider_truth_reference="provider-challenge-ref",
        opening_evidence={},
        now=NOW,
    )
    awaiting = store.transition(
        write_job["job_id"],
        "awaiting_cal",
        expected_state="executing",
        expected_version=1,
        actor="worker",
        reason_code="provider_challenge",
        gate_id=gate["gate_id"],
        now=NOW,
    )
    assert awaiting["current_step"] == "register_domain"
    assert awaiting["current_gate_id"] == gate["gate_id"]
    _, token = store.issue_gate_handoff(gate["gate_id"], now=NOW)
    store.request_gate_done(gate["gate_id"], token, actor="cal", now=NOW)
    store.complete_gate(
        gate["gate_id"],
        evidence={"provider_truth_verified": True},
        actor="worker",
        now=NOW,
    )
    resuming = store.transition(
        write_job["job_id"],
        "resuming",
        expected_state="awaiting_cal",
        expected_version=2,
        actor="worker",
        reason_code="gate_verified",
        now=NOW,
    )
    assert resuming["current_step"] == "register_domain"
    assert resuming["current_gate_id"] == ""
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="inflight_action_not_terminal"
    ):
        store.transition(
            write_job["job_id"],
            "ready",
            expected_state="resuming",
            expected_version=3,
            actor="worker",
            reason_code="abandon_active_action",
            now=NOW,
        )
    store.finish_action(
        approved["action_id"],
        status="succeeded",
        result={"provider_truth": "confirmed"},
        now=NOW,
    )
    next_action = record_action(
        store,
        write_job["job_id"],
        effect_class="read_only",
        target_state="executing_read_only",
        action_type="read_dns",
        key="read-dns",
    )
    store.dispatch_action(next_action["action_id"], now=NOW)
    reading = store.transition(
        write_job["job_id"],
        "executing_read_only",
        expected_state="resuming",
        expected_version=3,
        actor="worker",
        reason_code="next_step",
        action_id=next_action["action_id"],
        now=NOW,
    )
    assert reading["current_step"] == "read_dns"
    store.finish_action(
        next_action["action_id"],
        status="succeeded",
        result={"provider_truth": "confirmed"},
        now=NOW,
    )
    ready = store.transition(
        write_job["job_id"],
        "ready",
        expected_state="executing_read_only",
        expected_version=4,
        actor="worker",
        reason_code="step_complete",
        now=NOW,
    )
    assert ready["current_step"] == ""


def test_action_approval_requires_exact_fingerprint_and_is_single_use(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "awaiting_purchase_approval")
    action = record_action(
        store,
        job["job_id"],
        effect_class="consequential",
        key="first",
        approve=False,
    )
    mismatched = store.open_gate(
        job["job_id"],
        gate_type="action_approval",
        human_action="Approve exact action",
        provider_truth_reference="approval-verification-ref",
        opening_evidence={},
        approval_reference="fabricated",
        approval_fingerprint=job["plan_fingerprint"],
        now=NOW,
    )
    store.complete_gate(
        mismatched["gate_id"],
        evidence={"approval_granted": True, "provider_truth_verified": True},
        actor="operator",
        now=NOW,
    )
    with pytest.raises(
        commerce.CommerceActionError, match="approval_fingerprint_mismatch"
    ):
        store.authorize_action(
            action["action_id"],
            gate_id=mismatched["gate_id"],
            actor="operator",
            now=NOW,
        )

    job = make_job(store, objective="Correctly bound approval")
    seed_state(store, job["job_id"], "awaiting_purchase_approval")
    action = record_action(
        store,
        job["job_id"],
        effect_class="consequential",
        key="first",
        approve=False,
    )
    gate = store.open_gate(
        job["job_id"],
        gate_type="action_approval",
        human_action="Approve exact action",
        provider_truth_reference="approval-verification-ref",
        opening_evidence={},
        approval_reference="bound-approval",
        approval_fingerprint=action["action_fingerprint"],
        now=NOW,
    )
    store.complete_gate(
        gate["gate_id"],
        evidence={"approval_granted": True, "provider_truth_verified": True},
        actor="operator",
        now=NOW,
    )
    approved = store.authorize_action(
        action["action_id"],
        gate_id=gate["gate_id"],
        actor="operator",
        now=NOW,
    )
    assert approved["approval_fingerprint"] == action["action_fingerprint"]
    second = record_action(
        store,
        job["job_id"],
        effect_class="consequential",
        key="second",
        approve=False,
    )
    with pytest.raises(commerce.CommerceActionError, match="approval_gate_not_bound"):
        store.authorize_action(
            second["action_id"],
            gate_id=gate["gate_id"],
            actor="operator",
            now=NOW,
        )


def test_dispatch_requires_job_state_that_allows_action_target(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    action = record_action(
        store,
        job["job_id"],
        effect_class="read_only",
        target_state="executing_read_only",
    )
    with pytest.raises(commerce.CommerceActionError, match="job_not_ready_for_action"):
        store.dispatch_action(action["action_id"], now=NOW)


def test_pause_resume_and_cancel_preserve_pause_history(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    paused = store.pause(
        job["job_id"],
        expected_version=0,
        actor="operator",
        reason="waiting for reviewed facts",
        now=NOW,
    )
    assert paused["current_state"] == "paused"
    assert paused["paused_from_state"] == "requested"
    resumed = store.resume(
        job["job_id"],
        expected_version=1,
        actor="operator",
        now=NOW + timedelta(minutes=1),
    )
    assert resumed["current_state"] == "ready"
    assert resumed["paused_from_state"] == "requested"
    cancelled = store.cancel(
        job["job_id"],
        expected_version=2,
        actor="operator",
        reason="commercial objective withdrawn",
        now=NOW + timedelta(minutes=2),
    )
    assert cancelled["current_state"] == "cancelled"
    assert cancelled["active"] is False


def test_cancel_refuses_unresolved_or_uncertain_work(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "awaiting_purchase_approval")
    action = record_action(store, job["job_id"], effect_class="consequential")
    store.dispatch_action(action["action_id"], now=NOW)
    with pytest.raises(
        commerce.CommerceInvalidTransitionError,
        match="consequential_action_unresolved",
    ):
        store.cancel(
            job["job_id"],
            expected_version=0,
            actor="operator",
            reason="stop",
        )
    seed_state(store, job["job_id"], "uncertain_external_state")
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="reconciliation_required"
    ):
        store.cancel(
            job["job_id"],
            expected_version=0,
            actor="operator",
            reason="stop",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"card_number": "redacted"},
        {"outer": {"CVV": "redacted"}},
        {"items": [{"access-token": "redacted"}]},
        {"customerCardNumber": "redacted"},
        {"session_cookie": "redacted"},
        {"identity-document": {"number": "redacted"}},
        {"password": "redacted"},
        {"bank-token": "redacted"},
        {"privateKey": "redacted"},
        {"browser_form_value": "redacted"},
    ],
)
def test_forbidden_keys_are_rejected_recursively(payload):
    with pytest.raises(
        commerce.CommerceForbiddenDataError, match="forbidden_sensitive_field"
    ):
        commerce.canonical_json(payload)


def test_short_sensitive_key_matching_does_not_block_normal_commerce_fields():
    payload = {
        "company": "Linxio",
        "company_name": "Linxio Pty Ltd",
        "expansion_plan": "reviewed",
        "hotplate": "not an OTP field",
    }
    assert json.loads(commerce.canonical_json(payload)) == payload
    for key in ("pan", "pan-number", "cardCvv", "mfa_code", "3ds-value"):
        with pytest.raises(
            commerce.CommerceForbiddenDataError, match="forbidden_sensitive_field"
        ):
            commerce.canonical_json({key: "redacted"})


@pytest.mark.parametrize(
    "value",
    [
        "4242 4242 4242 4242",
        "expiry 12/29",
        "password=do-not-store",
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "sk_live_abcdefghijkl",
        "eyJabcdefghijk.eyJabcdefghijk.abcdefghijk",
        "sessionid=abcdef123456789",
    ],
)
def test_secret_shaped_values_are_rejected_without_echo(value):
    with pytest.raises(commerce.CommerceForbiddenDataError) as error:
        commerce.canonical_json({"note": value})
    assert value not in str(error.value)
    assert error.value.field == "payload.note"


def test_provider_account_storage_rejects_secrets_and_identity_conflicts(tmp_path):
    store = make_store(tmp_path)
    account = store.upsert_provider_account(
        account_reference="acct-porkbun-prod",
        provider="porkbun",
        environment_tag="production",
        identity={"account_id": "public-account-42"},
        status="unverified",
        metadata={"region": "global"},
        now=NOW,
    )
    assert account["identity"]["account_id"] == "public-account-42"
    with pytest.raises(commerce.CommerceForbiddenDataError):
        store.upsert_provider_account(
            account_reference="bad",
            provider="porkbun",
            environment_tag="production",
            identity={"api_key": "not-allowed"},
            status="unverified",
            metadata={},
        )
    with pytest.raises(
        commerce.CommerceActionError, match="provider_identity_conflict"
    ):
        store.upsert_provider_account(
            account_reference="acct-porkbun-prod",
            provider="porkbun",
            environment_tag="production",
            identity={"account_id": "substituted-account"},
            status="unverified",
            metadata={},
        )


def sample_plan() -> dict:
    return {
        "domain": "example.com",
        "tld": "com",
        "registrar": "porkbun",
        "provider_account": "acct-porkbun-prod",
        "prices": {"domain": {"amount": "12.00", "currency": "USD"}},
        "currencies": ["USD"],
        "term_length": "1y",
        "auto_renew": False,
        "subscription_recurrence": "monthly",
        "shopify_plan_tier": "basic",
        "product_price": {"amount": "99.00", "currency": "AUD"},
        "launch_date": "2026-09-01",
        "copy": "non-material headline",
        "page_order": ["home", "products"],
    }


def test_plan_fingerprint_is_canonical_and_material_only():
    plan = sample_plan()
    reordered = dict(reversed(tuple(plan.items())))
    assert commerce.plan_fingerprint(plan) == commerce.plan_fingerprint(reordered)
    non_material = {**plan, "copy": "different", "styling": "dark"}
    assert commerce.plan_fingerprint(plan) == commerce.plan_fingerprint(non_material)
    material = {**plan, "domain": "different.example"}
    assert commerce.plan_fingerprint(plan) != commerce.plan_fingerprint(material)


def test_action_fingerprint_excludes_runtime_metadata_but_binds_payload():
    base = commerce.action_fingerprint(
        action_type="register",
        provider="porkbun",
        effect_class="consequential",
        target_state="executing",
        request={"domain": "example.com", "timestamp": "first", "attempt": 1},
    )
    changed_runtime = commerce.action_fingerprint(
        action_type="register",
        provider="porkbun",
        effect_class="consequential",
        target_state="executing",
        request={"attempt": 99, "timestamp": "second", "domain": "example.com"},
    )
    changed_payload = commerce.action_fingerprint(
        action_type="register",
        provider="porkbun",
        effect_class="consequential",
        target_state="executing",
        request={"domain": "different.example"},
    )
    assert base == changed_runtime
    assert base != changed_payload


def test_material_plan_change_invalidates_matching_bindings(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    first = store.set_plan(
        job["job_id"], sample_plan(), expected_version=0, actor="planner", now=NOW
    )
    seed_state(store, job["job_id"], "awaiting_purchase_approval", version=1)
    matching = record_action(
        store,
        job["job_id"],
        effect_class="consequential",
        key="matching",
        approval_reference="approval-old",
    )
    matching_gate = store.open_gate(
        job["job_id"],
        gate_type="approval",
        human_action="Review exact terms",
        provider_truth_reference="provider-read-ref",
        opening_evidence={},
        approval_reference="approval-old",
        approval_fingerprint=first["plan_fingerprint"],
        now=NOW,
    )
    changed = {**sample_plan(), "domain": "new.example"}
    store.set_plan(job["job_id"], changed, expected_version=1, actor="planner", now=NOW)
    assert store.get_action(matching["action_id"])["approval_status"] == "stale"
    with raw_connection(store) as connection:
        statuses = {
            row["gate_id"]: row["status"]
            for row in connection.execute(
                "SELECT gate_id,status FROM gates WHERE job_id=?", (job["job_id"],)
            )
        }
    assert statuses[matching_gate["gate_id"]] == "invalidated"


@pytest.mark.parametrize(
    ("approval_reference", "approval_fingerprint"),
    [("approval-only", ""), ("", "0" * 64)],
)
def test_gate_approval_bindings_require_both_fields(
    tmp_path, approval_reference, approval_fingerprint
):
    store = make_store(tmp_path)
    job = make_job(store)
    with pytest.raises(commerce.CommerceGateError, match="approval_binding_incomplete"):
        store.open_gate(
            job["job_id"],
            gate_type="approval",
            human_action="Review exact terms",
            provider_truth_reference="provider-read-ref",
            opening_evidence={},
            approval_reference=approval_reference,
            approval_fingerprint=approval_fingerprint,
        )


def test_gate_approval_fingerprint_requires_sha256_shape(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    with pytest.raises(
        commerce.CommerceGateError, match="invalid_approval_fingerprint"
    ):
        store.open_gate(
            job["job_id"],
            gate_type="approval",
            human_action="Review exact terms",
            provider_truth_reference="provider-read-ref",
            opening_evidence={},
            approval_reference="approval",
            approval_fingerprint="fabricated",
        )


def test_stale_action_approval_cannot_transition_or_dispatch(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    store.set_plan(
        job["job_id"], sample_plan(), expected_version=0, actor="planner", now=NOW
    )
    seed_state(store, job["job_id"], "awaiting_purchase_approval", version=1)
    action = record_action(
        store,
        job["job_id"],
        effect_class="consequential",
        target_state="executing",
        action_type="register_domain",
        approval_reference="approval-old",
    )
    store.set_plan(
        job["job_id"],
        {**sample_plan(), "domain": "changed.example"},
        expected_version=1,
        actor="planner",
        now=NOW,
    )
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="stale_action_approval"
    ):
        store.transition(
            job["job_id"],
            "executing",
            expected_state="awaiting_purchase_approval",
            expected_version=2,
            actor="worker",
            reason_code="dispatch",
            action_id=action["action_id"],
        )
    with pytest.raises(commerce.CommerceActionError, match="stale_action_approval"):
        store.dispatch_action(action["action_id"])


def test_equivalent_objective_attaches_but_requesters_are_isolated(tmp_path):
    store = make_store(tmp_path)
    first = make_job(store, objective="  Launch   Café Store  ")
    attached = make_job(store, objective="launch cafe\u0301 store")
    other = make_job(
        store,
        objective="launch café store",
        requester="another-requester",
    )
    assert attached["job_id"] == first["job_id"]
    assert attached["attached"] is True
    assert other["job_id"] != first["job_id"]


def test_terminal_job_allows_new_equivalent_job(tmp_path):
    store = make_store(tmp_path)
    first = make_job(store)
    store.cancel(
        first["job_id"],
        expected_version=0,
        actor="operator",
        reason="stop",
        now=NOW,
    )
    second = make_job(store)
    assert second["job_id"] != first["job_id"]
    assert second["attached"] is False


def test_concurrent_duplicate_creation_produces_one_active_job(tmp_path):
    path = tmp_path / "concurrent.db"
    barrier = threading.Barrier(8)

    def create() -> dict:
        barrier.wait()
        return commerce.CommerceJobStore(path).create_or_attach_job(
            requester="cal",
            objective="Launch the same store",
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: create(), range(8)))
    assert len({result["job_id"] for result in results}) == 1
    assert sum(not result["attached"] for result in results) == 1
    assert len(commerce.CommerceJobStore(path).list_jobs(active_only=True)) == 1


def test_action_idempotency_replay_and_conflict(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    first = record_action(store, job["job_id"])
    replay = record_action(store, job["job_id"])
    assert replay["action_id"] == first["action_id"]
    assert replay["idempotent_replay"] is True
    with pytest.raises(commerce.CommerceActionError, match="idempotency_conflict"):
        store.record_action(
            job["job_id"],
            action_type="read_provider",
            provider="fake",
            effect_class="read_only",
            idempotency_key="action-key",
            request={"operation": "changed"},
        )


def test_action_classification_and_terminal_guards(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    with pytest.raises(commerce.CommerceActionError, match="invalid_effect_class"):
        record_action(store, job["job_id"], effect_class="maybe")
    seed_state(store, job["job_id"], "awaiting_purchase_approval")
    action = record_action(store, job["job_id"], effect_class="consequential")
    assert action["effect_class"] == "consequential"
    dispatched = store.dispatch_action(action["action_id"], now=NOW)
    assert dispatched["action_status"] == "dispatched"
    terminal = store.finish_action(
        action["action_id"], status="succeeded", result={"receipt_ref": "ref-1"}
    )
    assert terminal["result"] == {"receipt_ref": "ref-1"}
    with pytest.raises(commerce.CommerceActionError, match="action_not_terminalizable"):
        store.finish_action(
            action["action_id"], status="failed", result={"reason": "late"}
        )
    with pytest.raises(commerce.CommerceActionError, match="action_not_dispatchable"):
        store.dispatch_action(action["action_id"])


def test_uncertain_action_cannot_be_redispatched(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "awaiting_purchase_approval")
    action = record_action(store, job["job_id"], effect_class="consequential")
    store.dispatch_action(action["action_id"], now=NOW)
    uncertain = store.finish_action(
        action["action_id"], status="uncertain", result={"reason": "no_response"}
    )
    assert uncertain["uncertainty"] is True
    with pytest.raises(commerce.CommerceActionError, match="action_not_dispatchable"):
        store.dispatch_action(action["action_id"])


def test_gate_open_complete_expire_and_duplicate_guards(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    gate = store.open_gate(
        job["job_id"],
        gate_type="merchant_login",
        human_action="Log in to the approved merchant account",
        provider_truth_reference="provider-account-status-ref",
        opening_evidence={"account_reference": "merchant-account"},
        expires_at=NOW + timedelta(hours=1),
        now=NOW,
    )
    assert gate["status"] == "open"
    assert gate["provider_truth_reference"] == "provider-account-status-ref"
    with pytest.raises(commerce.CommerceGateError, match="active_gate_exists"):
        store.open_gate(
            job["job_id"],
            gate_type="duplicate",
            human_action="Duplicate",
            provider_truth_reference="duplicate-ref",
            opening_evidence={},
            now=NOW,
        )
    store.transition(
        job["job_id"],
        "planning",
        expected_state="requested",
        expected_version=0,
        actor="worker",
        reason_code="prepare_handoff",
        now=NOW,
    )
    current = store.get_job(job["job_id"])
    store.transition(
        job["job_id"],
        "awaiting_cal",
        expected_state="planning",
        expected_version=current["row_version"],
        actor="worker",
        reason_code="human_action_required",
        gate_id=gate["gate_id"],
        now=NOW,
    )
    with pytest.raises(commerce.CommerceGateError, match="gate_done_required"):
        store.complete_gate(
            gate["gate_id"],
            evidence={"provider_truth_verified": True},
            actor="operator",
            now=NOW,
        )
    _, token = store.issue_gate_handoff(gate["gate_id"], now=NOW)
    store.request_gate_done(
        gate["gate_id"], token, actor="cal", now=NOW + timedelta(minutes=1)
    )
    completed = store.complete_gate(
        gate["gate_id"],
        evidence={
            "provider_check": "complete",
            "provider_truth_verified": True,
        },
        actor="operator",
        now=NOW + timedelta(minutes=1),
    )
    assert completed["status"] == "completed"
    with pytest.raises(commerce.CommerceGateError, match="gate_not_open"):
        store.complete_gate(
            gate["gate_id"],
            evidence={"provider_truth_verified": True},
            actor="operator",
            now=NOW,
        )
    expiring_job = make_job(store, objective="Expiring gate")
    expiring = store.open_gate(
        expiring_job["job_id"],
        gate_type="approval",
        human_action="Approve exact launch packet",
        provider_truth_reference="launch-verification-ref",
        opening_evidence={},
        expires_at=NOW + timedelta(minutes=2),
        now=NOW,
    )
    with pytest.raises(commerce.CommerceGateError, match="gate_not_expired"):
        store.expire_gate(expiring["gate_id"], now=NOW)
    expired = store.expire_gate(expiring["gate_id"], now=NOW + timedelta(minutes=3))
    assert expired["status"] == "expired"


def test_gate_evidence_is_screened(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    with pytest.raises(commerce.CommerceForbiddenDataError):
        store.open_gate(
            job["job_id"],
            gate_type="bad",
            human_action="Review",
            provider_truth_reference="safe-reference",
            opening_evidence={"session_cookie": "no"},
        )


def test_gate_handoff_rotates_hashes_and_done_stays_provider_unverified(
    tmp_path, monkeypatch
):
    store = make_store(tmp_path)
    job = make_job(store)
    gate = open_active_handoff_gate(store, job, expires_at=NOW + timedelta(hours=2))
    public_gate = store.get_gate(gate["gate_id"])
    assert public_gate["browser_session"] == job["browser_session"]
    assert public_gate["handoff_expires_at"] is None
    assert "handoff_token_hash" not in public_gate

    issued_gate, token = store.issue_gate_handoff(gate["gate_id"], now=NOW)
    assert len(token) == 47
    assert issued_gate["handoff_expires_at"] == commerce.iso_utc(
        NOW + commerce.HANDOFF_TTL
    )
    assert "handoff_token_hash" not in issued_gate
    with raw_connection(store) as connection:
        stored = connection.execute(
            "SELECT * FROM gates WHERE gate_id=?", (gate["gate_id"],)
        ).fetchone()
        assert stored["browser_session"] == job["browser_session"]
        assert (
            stored["handoff_token_hash"]
            == hashlib.sha256(token.encode("utf-8")).hexdigest()
        )
        persisted = "\n".join(connection.iterdump())
    assert token not in persisted

    compare_calls = []
    real_compare = commerce.hmac.compare_digest

    def checked_compare(left, right):
        compare_calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(commerce.hmac, "compare_digest", checked_compare)
    wrong_token = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(commerce.CommerceGateError, match="invalid_handoff_token"):
        store.authorize_gate_handoff(gate["gate_id"], wrong_token, now=NOW)
    assert compare_calls
    authorized = store.authorize_gate_handoff(gate["gate_id"], token, now=NOW)
    assert authorized["gate_id"] == gate["gate_id"]

    done = store.request_gate_done(
        gate["gate_id"], token, actor="cal", now=NOW + timedelta(minutes=1)
    )
    assert done["status"] == "open"
    assert done["completed_at"] is None
    assert done["completion_evidence"] == {}
    assert done["done_requested_at"] == commerce.iso_utc(NOW + timedelta(minutes=1))
    event_count = len(store.list_events(job["job_id"]))
    replay = store.request_gate_done(
        gate["gate_id"], token, actor="cal", now=NOW + timedelta(minutes=2)
    )
    assert replay["done_requested_at"] == done["done_requested_at"]
    assert len(store.list_events(job["job_id"])) == event_count

    renewed_gate, renewed_token = store.renew_gate_handoff(
        gate["gate_id"], token, actor="cal", now=NOW + timedelta(minutes=3)
    )
    assert renewed_token != token
    assert renewed_gate["handoff_expires_at"] == commerce.iso_utc(
        NOW + timedelta(minutes=33)
    )
    with pytest.raises(commerce.CommerceGateError, match="invalid_handoff_token"):
        store.authorize_gate_handoff(gate["gate_id"], token, now=NOW)
    store.authorize_gate_handoff(gate["gate_id"], renewed_token, now=NOW)

    completed = store.complete_gate(
        gate["gate_id"],
        evidence={"provider_truth_verified": True},
        actor="worker",
        now=NOW + timedelta(minutes=4),
    )
    assert completed["status"] == "completed"
    assert completed["done_requested_at"] == done["done_requested_at"]
    assert completed["handoff_expires_at"] is None
    with pytest.raises(commerce.CommerceGateError, match="gate_not_open"):
        store.authorize_gate_handoff(gate["gate_id"], renewed_token, now=NOW)
    with raw_connection(store) as connection:
        stored = connection.execute(
            "SELECT * FROM gates WHERE gate_id=?", (gate["gate_id"],)
        ).fetchone()
        persisted = "\n".join(connection.iterdump())
    assert stored["handoff_token_hash"] == ""
    assert token not in persisted
    assert renewed_token not in persisted
    assert token not in str(store.list_events(job["job_id"]))
    assert renewed_token not in str(store.list_events(job["job_id"]))


def test_handoff_requires_current_human_gate_and_expiry_fails_closed(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "planning")
    gate = store.open_gate(
        job["job_id"],
        gate_type="merchant_login",
        human_action="Complete provider login",
        provider_truth_reference="provider-account-status-ref",
        opening_evidence={},
        expires_at=NOW + timedelta(minutes=40),
        now=NOW,
    )
    with pytest.raises(commerce.CommerceGateError, match="gate_not_active_handoff"):
        store.issue_gate_handoff(gate["gate_id"], now=NOW)
    store.transition(
        job["job_id"],
        "awaiting_cal",
        expected_state="planning",
        expected_version=0,
        actor="worker",
        reason_code="human_action_required",
        gate_id=gate["gate_id"],
        now=NOW,
    )
    _, token = store.issue_gate_handoff(gate["gate_id"], now=NOW)
    with pytest.raises(commerce.CommerceGateError, match="handoff_expired"):
        store.authorize_gate_handoff(
            gate["gate_id"], token, now=NOW + timedelta(minutes=30)
        )
    with pytest.raises(commerce.CommerceGateError, match="handoff_expired"):
        store.renew_gate_handoff(
            gate["gate_id"], token, now=NOW + timedelta(minutes=30)
        )
    fresh_gate, fresh_token = store.issue_gate_handoff(
        gate["gate_id"], now=NOW + timedelta(minutes=31)
    )
    assert fresh_gate["handoff_expires_at"] == commerce.iso_utc(
        NOW + timedelta(minutes=40)
    )
    store.authorize_gate_handoff(
        gate["gate_id"], fresh_token, now=NOW + timedelta(minutes=39)
    )
    with pytest.raises(commerce.CommerceGateError, match="gate_expired"):
        store.request_gate_done(
            gate["gate_id"],
            fresh_token,
            actor="cal",
            now=NOW + timedelta(minutes=40),
        )
    with raw_connection(store) as connection:
        assert (
            connection.execute(
                "SELECT status FROM gates WHERE gate_id=?", (gate["gate_id"],)
            ).fetchone()[0]
            == "open"
        )
    expired = store.expire_gate(gate["gate_id"], now=NOW + timedelta(minutes=40))
    assert expired["status"] == "expired"
    assert expired["handoff_expires_at"] is None

    approval_job = make_job(store, objective="Approval gate is not a viewer gate")
    seed_state(store, approval_job["job_id"], "planning")
    approval_gate = store.open_gate(
        approval_job["job_id"],
        gate_type="action_approval",
        human_action="Approve exact action",
        provider_truth_reference="approval-verification-ref",
        opening_evidence={},
        now=NOW,
    )
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="handoff_gate_required"
    ):
        store.transition(
            approval_job["job_id"],
            "awaiting_cal",
            expected_state="planning",
            expected_version=0,
            actor="worker",
            reason_code="human_action_required",
            gate_id=approval_gate["gate_id"],
            now=NOW,
        )


def test_awaiting_cal_rejects_expired_or_session_mismatched_gate(tmp_path):
    store = make_store(tmp_path)

    expired_job = make_job(store, objective="Expired viewer gate")
    seed_state(store, expired_job["job_id"], "planning")
    expired_gate = store.open_gate(
        expired_job["job_id"],
        gate_type="merchant_login",
        human_action="Complete provider login",
        provider_truth_reference="provider-account-status-ref",
        opening_evidence={},
        expires_at=NOW,
        now=NOW,
    )
    with pytest.raises(commerce.CommerceInvalidTransitionError, match="gate_expired"):
        store.transition(
            expired_job["job_id"],
            "awaiting_cal",
            expected_state="planning",
            expected_version=0,
            actor="worker",
            reason_code="human_action_required",
            gate_id=expired_gate["gate_id"],
            now=NOW,
        )

    session_job = make_job(store, objective="Mismatched viewer session")
    seed_state(store, session_job["job_id"], "planning")
    session_gate = store.open_gate(
        session_job["job_id"],
        gate_type="merchant_login",
        human_action="Complete provider login",
        provider_truth_reference="provider-account-status-ref",
        opening_evidence={},
        now=NOW,
    )
    with raw_connection(store) as connection:
        connection.execute(
            "UPDATE gates SET browser_session='commerce_other' WHERE gate_id=?",
            (session_gate["gate_id"],),
        )
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="gate_session_mismatch"
    ):
        store.transition(
            session_job["job_id"],
            "awaiting_cal",
            expected_state="planning",
            expected_version=0,
            actor="worker",
            reason_code="human_action_required",
            gate_id=session_gate["gate_id"],
            now=NOW,
        )


@pytest.mark.parametrize("target", ["paused", "cancelled"])
def test_handoff_is_invalidated_when_awaiting_cal_job_stops(target, tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    gate = open_active_handoff_gate(store, job)
    _, token = store.issue_gate_handoff(gate["gate_id"], now=NOW)
    store.transition(
        job["job_id"],
        target,
        expected_state="awaiting_cal",
        expected_version=1,
        actor="operator",
        reason_code=f"operator_{target}",
        now=NOW + timedelta(minutes=1),
    )
    invalidated = store.get_gate(gate["gate_id"])
    assert invalidated["status"] == "invalidated"
    assert invalidated["handoff_expires_at"] is None
    with raw_connection(store) as connection:
        assert (
            connection.execute(
                "SELECT handoff_token_hash FROM gates WHERE gate_id=?",
                (gate["gate_id"],),
            ).fetchone()[0]
            == ""
        )
    with pytest.raises(commerce.CommerceGateError, match="gate_not_open"):
        store.authorize_gate_handoff(gate["gate_id"], token, now=NOW)


def test_awaiting_cal_resumes_only_after_bound_gate_verification(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "planning")
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="open_gate_required"
    ):
        store.transition(
            job["job_id"],
            "awaiting_cal",
            expected_state="planning",
            expected_version=0,
            actor="worker",
            reason_code="missing_facts",
            now=NOW,
        )
    gate = store.open_gate(
        job["job_id"],
        gate_type="merchant_login",
        human_action="Complete provider login",
        provider_truth_reference="provider-account-status-ref",
        opening_evidence={},
        now=NOW,
    )
    awaiting = store.transition(
        job["job_id"],
        "awaiting_cal",
        expected_state="planning",
        expected_version=0,
        actor="worker",
        reason_code="missing_facts",
        gate_id=gate["gate_id"],
        now=NOW,
    )
    assert awaiting["current_gate_id"] == gate["gate_id"]
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="verified_gate_required"
    ):
        store.transition(
            job["job_id"],
            "resuming",
            expected_state="awaiting_cal",
            expected_version=1,
            actor="worker",
            reason_code="resume",
            now=NOW,
        )
    with pytest.raises(
        commerce.CommerceGateError, match="provider_truth_verification_required"
    ):
        store.complete_gate(
            gate["gate_id"],
            evidence={"done_requested": True},
            actor="worker",
            now=NOW,
        )
    with pytest.raises(commerce.CommerceGateError, match="gate_done_required"):
        store.complete_gate(
            gate["gate_id"],
            evidence={"provider_truth_verified": True},
            actor="worker",
            now=NOW,
        )
    _, token = store.issue_gate_handoff(gate["gate_id"], now=NOW)
    store.request_gate_done(gate["gate_id"], token, actor="cal", now=NOW)
    store.complete_gate(
        gate["gate_id"],
        evidence={"provider_truth_verified": True},
        actor="worker",
        now=NOW,
    )
    resumed = store.transition(
        job["job_id"],
        "resuming",
        expected_state="awaiting_cal",
        expected_version=1,
        actor="worker",
        reason_code="verified_gate",
        now=NOW,
    )
    assert resumed["current_state"] == "resuming"
    assert resumed["current_gate_id"] == ""
    with raw_connection(store) as connection:
        assert (
            connection.execute(
                "SELECT status FROM gates WHERE gate_id=?", (gate["gate_id"],)
            ).fetchone()[0]
            == "consumed"
        )


def test_timeout_sweep_enters_timed_out_once_and_resume_returns_ready(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    assert store.sweep_timeouts(now=NOW + timedelta(hours=71, minutes=59)) == []
    assert store.get_job(job["job_id"])["current_state"] == "requested"
    assert store.sweep_timeouts(now=NOW + timedelta(hours=72)) == [job["job_id"]]
    timed_out = store.get_job(job["job_id"])
    assert timed_out["current_state"] == "timed_out"
    assert timed_out["deadline_at"] is None
    assert store.list_events(job["job_id"])[-1]["event_type"] == "state_timed_out"
    event_count = len(store.list_events(job["job_id"]))
    assert store.sweep_timeouts(now=NOW + timedelta(days=5)) == []
    assert len(store.list_events(job["job_id"])) == event_count
    resumed = store.resume(
        job["job_id"],
        expected_version=1,
        actor="operator",
        now=NOW + timedelta(days=5),
    )
    assert resumed["current_state"] == "ready"


def test_timed_out_state_is_only_entered_by_due_timeout_sweep(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="timeout_not_due"
    ):
        store.transition(
            job["job_id"],
            "timed_out",
            expected_state="requested",
            expected_version=0,
            actor="caller",
            reason_code="early_timeout",
            now=NOW,
        )
    assert store.get_job(job["job_id"])["current_state"] == "requested"
    assert store.sweep_timeouts(now=NOW + timedelta(hours=72)) == [job["job_id"]]
    assert store.get_job(job["job_id"])["current_state"] == "timed_out"


def test_timeout_sweep_defers_inflight_gate_without_rolling_back_batch(tmp_path):
    store = make_store(tmp_path)

    challenge_job = make_job(store, objective="Provider challenge")
    seed_state(store, challenge_job["job_id"], "awaiting_purchase_approval")
    action = record_action(
        store,
        challenge_job["job_id"],
        effect_class="consequential",
        action_type="register_domain",
    )
    store.dispatch_action(action["action_id"], now=NOW)
    store.transition(
        challenge_job["job_id"],
        "executing",
        expected_state="awaiting_purchase_approval",
        expected_version=0,
        actor="worker",
        reason_code="action_dispatched",
        action_id=action["action_id"],
        now=NOW,
    )
    gate = store.open_gate(
        challenge_job["job_id"],
        gate_type="provider_challenge",
        human_action="Complete provider challenge",
        provider_truth_reference="provider-challenge-ref",
        opening_evidence={},
        now=NOW,
    )
    store.transition(
        challenge_job["job_id"],
        "awaiting_cal",
        expected_state="executing",
        expected_version=1,
        actor="worker",
        reason_code="provider_challenge",
        gate_id=gate["gate_id"],
        now=NOW,
    )
    with raw_connection(store) as connection:
        connection.execute(
            "UPDATE jobs SET deadline_at=? WHERE job_id=?",
            (commerce.iso_utc(NOW), challenge_job["job_id"]),
        )

    due_job = make_job(store, objective="Independent due job")
    seed_state(store, due_job["job_id"], "planning", deadline=NOW)

    assert store.sweep_timeouts(now=NOW) == [due_job["job_id"]]
    assert store.get_job(challenge_job["job_id"])["current_state"] == "awaiting_cal"
    assert store.get_gate(gate["gate_id"])["status"] == "open"
    assert store.get_job(due_job["job_id"])["current_state"] == "timed_out"


def test_uncertain_action_cannot_bypass_reconciliation_via_timeout_or_pause(
    tmp_path,
):
    store = make_store(tmp_path)
    job = make_job(store, objective="Uncertain challenged action")
    seed_state(store, job["job_id"], "awaiting_purchase_approval")
    action = record_action(
        store,
        job["job_id"],
        effect_class="consequential",
        action_type="register_domain",
    )
    store.dispatch_action(action["action_id"], now=NOW)
    store.transition(
        job["job_id"],
        "executing",
        expected_state="awaiting_purchase_approval",
        expected_version=0,
        actor="worker",
        reason_code="action_dispatched",
        action_id=action["action_id"],
        now=NOW,
    )
    gate = store.open_gate(
        job["job_id"],
        gate_type="provider_challenge",
        human_action="Complete provider challenge",
        provider_truth_reference="provider-challenge-ref",
        opening_evidence={},
        now=NOW,
    )
    store.transition(
        job["job_id"],
        "awaiting_cal",
        expected_state="executing",
        expected_version=1,
        actor="worker",
        reason_code="provider_challenge",
        gate_id=gate["gate_id"],
        now=NOW,
    )
    store.finish_action(
        action["action_id"],
        status="uncertain",
        result={"reason": "provider_outcome_unknown"},
        now=NOW,
    )
    with raw_connection(store) as connection:
        connection.execute(
            "UPDATE jobs SET deadline_at=? WHERE job_id=?",
            (commerce.iso_utc(NOW), job["job_id"]),
        )

    assert store.sweep_timeouts(now=NOW) == []
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="unresolved_action"
    ):
        store.pause(
            job["job_id"],
            expected_version=2,
            actor="operator",
            reason="Pause requested",
            now=NOW,
        )

    assert store.recover(now=NOW + timedelta(minutes=1))["uncertain"] == 1
    uncertain = store.get_job(job["job_id"])
    assert uncertain["current_state"] == "uncertain_external_state"
    reconciling = store.transition(
        job["job_id"],
        "reconciliation_required",
        expected_state="uncertain_external_state",
        expected_version=uncertain["row_version"],
        actor="worker",
        reason_code="provider_truth_ambiguous",
        now=NOW + timedelta(minutes=1),
    )
    assert store.recover(now=NOW + timedelta(minutes=2)) == {
        "recoverable": 0,
        "uncertain": 0,
        "inconsistent": 0,
    }
    assert store.get_job(job["job_id"]) == reconciling
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="unresolved_action"
    ):
        store.transition(
            job["job_id"],
            "ready",
            expected_state="reconciliation_required",
            expected_version=reconciling["row_version"],
            actor="operator",
            reason_code="skip_reconciliation",
            now=NOW + timedelta(minutes=1),
        )
    with pytest.raises(
        commerce.CommerceActionError, match="reconciliation_verification_required"
    ):
        store.resolve_uncertain_action(
            action["action_id"],
            status="succeeded",
            evidence={"provider_truth_verified": False},
            expected_version=reconciling["row_version"],
            actor="operator",
            now=NOW + timedelta(minutes=2),
        )
    next_action = record_action(
        store,
        job["job_id"],
        effect_class="read_only",
        action_type="read_dns",
        key="work-after-uncertain",
    )
    with pytest.raises(commerce.CommerceActionError, match="inflight_action_exists"):
        store.dispatch_action(next_action["action_id"], now=NOW + timedelta(minutes=1))

    evidence = {
        "provider_truth_verified": True,
        "provider_reference": "domain-list-evidence",
    }
    resolved = store.resolve_uncertain_action(
        action["action_id"],
        status="succeeded",
        evidence=evidence,
        expected_version=reconciling["row_version"],
        actor="operator",
        now=NOW + timedelta(minutes=2),
    )
    assert resolved["action"]["action_status"] == "succeeded"
    assert resolved["action"]["uncertainty"] is False
    assert resolved["action"]["result"]["uncertain_result"] == {
        "reason": "provider_outcome_unknown"
    }
    assert resolved["action"]["result"]["reconciliation"] == evidence
    assert resolved["action"]["idempotent_replay"] is False
    assert resolved["job"]["current_state"] == "ready"
    dispatched = store.dispatch_action(
        next_action["action_id"], now=NOW + timedelta(minutes=3)
    )
    assert dispatched["action_status"] == "dispatched"
    advanced = store.transition(
        job["job_id"],
        "executing_read_only",
        expected_state="ready",
        expected_version=resolved["job"]["row_version"],
        actor="worker",
        reason_code="next_step",
        action_id=next_action["action_id"],
        now=NOW + timedelta(minutes=3),
    )
    replay = store.resolve_uncertain_action(
        action["action_id"],
        status="succeeded",
        evidence=evidence,
        expected_version=reconciling["row_version"],
        actor="operator",
        now=NOW + timedelta(minutes=4),
    )
    assert replay["action"]["idempotent_replay"] is True
    assert replay["job"] == advanced


def test_uncertain_action_can_resolve_to_failed_atomically(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "awaiting_purchase_approval")
    action = record_action(
        store,
        job["job_id"],
        effect_class="consequential",
        action_type="register_domain",
    )
    store.dispatch_action(action["action_id"], now=NOW)
    store.transition(
        job["job_id"],
        "executing",
        expected_state="awaiting_purchase_approval",
        expected_version=0,
        actor="worker",
        reason_code="action_dispatched",
        action_id=action["action_id"],
        now=NOW,
    )
    store.finish_action(
        action["action_id"],
        status="uncertain",
        result={"reason": "provider_outcome_unknown"},
        now=NOW,
    )
    uncertain = store.transition(
        job["job_id"],
        "uncertain_external_state",
        expected_state="executing",
        expected_version=1,
        actor="worker",
        reason_code="outcome_unknown",
        now=NOW,
    )
    reconciling = store.transition(
        job["job_id"],
        "reconciliation_required",
        expected_state="uncertain_external_state",
        expected_version=uncertain["row_version"],
        actor="worker",
        reason_code="provider_truth_ambiguous",
        now=NOW,
    )
    resolved = store.resolve_uncertain_action(
        action["action_id"],
        status="failed",
        evidence={
            "provider_truth_verified": True,
            "provider_reference": "domain-absent-evidence",
        },
        expected_version=reconciling["row_version"],
        actor="operator",
        now=NOW,
    )
    assert resolved["action"]["action_status"] == "failed"
    assert resolved["job"]["current_state"] == "failed"
    assert resolved["job"]["active"] is False


def test_normal_success_cannot_impersonate_reconciliation_replay(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "ready")
    evidence = {
        "provider_truth_verified": True,
        "provider_reference": "domain-list-evidence",
    }
    action = record_action(
        store,
        job["job_id"],
        effect_class="read_only",
        action_type="read_dns",
    )
    store.dispatch_action(action["action_id"], now=NOW)
    store.transition(
        job["job_id"],
        "executing_read_only",
        expected_state="ready",
        expected_version=0,
        actor="worker",
        reason_code="action_dispatched",
        action_id=action["action_id"],
        now=NOW,
    )
    store.finish_action(
        action["action_id"],
        status="succeeded",
        result={"reconciliation": evidence},
        now=NOW,
    )
    ready = store.transition(
        job["job_id"],
        "ready",
        expected_state="executing_read_only",
        expected_version=1,
        actor="worker",
        reason_code="step_complete",
        now=NOW,
    )
    with pytest.raises(commerce.CommerceActionError, match="resolution_conflict"):
        store.resolve_uncertain_action(
            action["action_id"],
            status="succeeded",
            evidence=evidence,
            expected_version=ready["row_version"],
            actor="operator",
            now=NOW,
        )


def test_recovery_binds_dispatch_crash_to_resolvable_action(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "awaiting_purchase_approval")
    action = record_action(
        store,
        job["job_id"],
        effect_class="consequential",
        action_type="register_domain",
    )
    store.dispatch_action(action["action_id"], now=NOW)

    assert store.recover(now=NOW + timedelta(minutes=1))["uncertain"] == 1
    uncertain = store.get_job(job["job_id"])
    assert uncertain["current_state"] == "uncertain_external_state"
    assert uncertain["current_step"] == action["action_type"]
    reconciling = store.transition(
        job["job_id"],
        "reconciliation_required",
        expected_state="uncertain_external_state",
        expected_version=uncertain["row_version"],
        actor="worker",
        reason_code="provider_truth_ambiguous",
        now=NOW + timedelta(minutes=1),
    )
    resolved = store.resolve_uncertain_action(
        action["action_id"],
        status="succeeded",
        evidence={
            "provider_truth_verified": True,
            "provider_reference": "domain-list-evidence",
        },
        expected_version=reconciling["row_version"],
        actor="operator",
        now=NOW + timedelta(minutes=2),
    )
    assert resolved["action"]["action_status"] == "succeeded"
    assert resolved["job"]["current_state"] == "ready"


def test_timeout_sweep_excludes_unsafe_waiting_and_terminal_states(tmp_path):
    store = make_store(tmp_path)
    states = (
        "uncertain_external_state",
        "reconciliation_required",
        "executing",
        "timed_out",
        "paused",
        "completed",
    )
    jobs = []
    for state in states:
        job = make_job(store, objective=f"timeout excluded {state}")
        seed_state(
            store,
            job["job_id"],
            state,
            deadline=NOW - timedelta(seconds=1),
        )
        jobs.append(job)
    assert store.sweep_timeouts(now=NOW) == []
    assert [store.get_job(job["job_id"])["current_state"] for job in jobs] == list(
        states
    )


def test_reopen_preserves_committed_state_and_uncommitted_changes_disappear(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    store.transition(
        job["job_id"],
        "planning",
        expected_state="requested",
        expected_version=0,
        actor="test",
        reason_code="advance",
        now=NOW,
    )
    reopened = commerce.CommerceJobStore(store.path)
    assert reopened.get_job(job["job_id"])["current_state"] == "planning"
    connection = raw_connection(store)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE jobs SET current_state='ready' WHERE job_id=?", (job["job_id"],)
    )
    connection.close()
    assert reopened.get_job(job["job_id"])["current_state"] == "planning"


def test_recovery_parks_consequential_inflight_action_as_uncertain_once(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "awaiting_purchase_approval")
    action = record_action(store, job["job_id"], effect_class="consequential")
    store.dispatch_action(action["action_id"], now=NOW)
    store.transition(
        job["job_id"],
        "executing",
        expected_state="awaiting_purchase_approval",
        expected_version=0,
        actor="worker",
        reason_code="action_dispatched",
        action_id=action["action_id"],
        now=NOW,
    )
    gate = store.open_gate(
        job["job_id"],
        gate_type="provider_challenge",
        human_action="Complete provider challenge",
        provider_truth_reference="provider-challenge-ref",
        opening_evidence={},
        now=NOW,
    )
    store.transition(
        job["job_id"],
        "awaiting_cal",
        expected_state="executing",
        expected_version=1,
        actor="worker",
        reason_code="provider_challenge",
        gate_id=gate["gate_id"],
        now=NOW,
    )
    _, token = store.issue_gate_handoff(gate["gate_id"], now=NOW)

    reopened = commerce.CommerceJobStore(store.path)
    first = reopened.recover(now=NOW + timedelta(minutes=1))
    assert first["uncertain"] == 1
    assert reopened.get_action(action["action_id"])["action_status"] == "uncertain"
    recovered_job = reopened.get_job(job["job_id"])
    assert recovered_job["current_state"] == "uncertain_external_state"
    assert recovered_job["current_gate_id"] == ""
    invalidated = reopened.get_gate(gate["gate_id"])
    assert invalidated["status"] == "invalidated"
    assert invalidated["handoff_expires_at"] is None
    with pytest.raises(commerce.CommerceGateError, match="gate_not_open"):
        reopened.authorize_gate_handoff(gate["gate_id"], token, now=NOW)
    replacement = reopened.open_gate(
        job["job_id"],
        gate_type="reconciliation",
        human_action="Review provider truth",
        provider_truth_reference="provider-reconciliation-ref",
        opening_evidence={},
        now=NOW + timedelta(minutes=1),
    )
    assert replacement["status"] == "open"
    event_count = len(reopened.list_events(job["job_id"]))
    second = reopened.recover(now=NOW + timedelta(minutes=2))
    assert second == {"recoverable": 0, "uncertain": 0, "inconsistent": 0}
    assert len(reopened.list_events(job["job_id"])) == event_count


def test_recovery_redispatches_read_only_action_after_execution_transition(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "ready")
    action = record_action(store, job["job_id"], effect_class="read_only")
    store.dispatch_action(action["action_id"], now=NOW)
    store.transition(
        job["job_id"],
        "executing_read_only",
        expected_state="ready",
        expected_version=0,
        actor="worker",
        reason_code="read_dispatched",
        action_id=action["action_id"],
        now=NOW,
    )
    recovered = store.recover(now=NOW + timedelta(minutes=1))
    assert recovered["recoverable"] == 1
    assert store.get_action(action["action_id"])["action_status"] == "recoverable"
    current = store.get_job(job["job_id"])
    assert current["current_state"] == "executing_read_only"
    assert current["current_step"] == action["action_type"]
    event_count = len(store.list_events(job["job_id"]))
    assert store.recover(now=NOW + timedelta(minutes=2))["recoverable"] == 0
    assert len(store.list_events(job["job_id"])) == event_count
    with raw_connection(store) as connection:
        connection.execute(
            "UPDATE jobs SET current_step='different_step' WHERE job_id=?",
            (job["job_id"],),
        )
    with pytest.raises(commerce.CommerceActionError, match="job_not_ready_for_action"):
        store.dispatch_action(action["action_id"], now=NOW + timedelta(minutes=3))
    with raw_connection(store) as connection:
        connection.execute(
            "UPDATE jobs SET current_step=? WHERE job_id=?",
            (action["action_type"], job["job_id"]),
        )
    redispatched = store.dispatch_action(
        action["action_id"], now=NOW + timedelta(minutes=3)
    )
    assert redispatched["action_status"] == "dispatched"
    finished = store.finish_action(
        action["action_id"],
        status="succeeded",
        result={"provider_truth": "confirmed"},
        now=NOW + timedelta(minutes=4),
    )
    assert finished["action_status"] == "succeeded"


def test_recovered_action_cannot_be_abandoned_or_duplicated(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "ready")
    action = record_action(
        store,
        job["job_id"],
        effect_class="read_only",
        action_type="read_inventory",
        key="first-read",
    )
    store.dispatch_action(action["action_id"], now=NOW)
    store.transition(
        job["job_id"],
        "executing_read_only",
        expected_state="ready",
        expected_version=0,
        actor="worker",
        reason_code="read_dispatched",
        action_id=action["action_id"],
        now=NOW,
    )
    assert store.recover(now=NOW + timedelta(minutes=1))["recoverable"] == 1

    duplicate = record_action(
        store,
        job["job_id"],
        effect_class="read_only",
        action_type="read_inventory",
        key="replacement-read",
    )
    with pytest.raises(commerce.CommerceActionError, match="inflight_action_exists"):
        store.dispatch_action(duplicate["action_id"], now=NOW + timedelta(minutes=2))
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="inflight_action_not_terminal"
    ):
        store.transition(
            job["job_id"],
            "ready",
            expected_state="executing_read_only",
            expected_version=1,
            actor="worker",
            reason_code="abandon_recovered_action",
            now=NOW + timedelta(minutes=2),
        )

    store.dispatch_action(action["action_id"], now=NOW + timedelta(minutes=2))
    store.finish_action(
        action["action_id"],
        status="succeeded",
        result={"provider_truth": "confirmed"},
        now=NOW + timedelta(minutes=3),
    )
    ready = store.transition(
        job["job_id"],
        "ready",
        expected_state="executing_read_only",
        expected_version=1,
        actor="worker",
        reason_code="step_complete",
        now=NOW + timedelta(minutes=3),
    )
    assert ready["current_step"] == ""


def test_already_uncertain_action_and_job_are_recovery_idempotent(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "awaiting_purchase_approval")
    action = record_action(store, job["job_id"], effect_class="consequential")
    store.dispatch_action(action["action_id"], now=NOW)
    store.finish_action(
        action["action_id"], status="uncertain", result={"reason": "unknown"}
    )
    seed_state(store, job["job_id"], "uncertain_external_state")
    event_count = len(store.list_events(job["job_id"]))
    assert store.recover(now=NOW) == {
        "recoverable": 0,
        "uncertain": 0,
        "inconsistent": 0,
    }
    assert len(store.list_events(job["job_id"])) == event_count


def test_recovery_records_and_reports_inconsistent_terminal_inflight_action(
    tmp_path,
):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "awaiting_purchase_approval")
    action = record_action(store, job["job_id"], effect_class="consequential")
    store.dispatch_action(action["action_id"], now=NOW)
    seed_state(store, job["job_id"], "completed")
    with pytest.raises(
        commerce.CommerceRecoveryError, match="inconsistent_recovery_state"
    ):
        store.recover(now=NOW)
    assert store.list_events(job["job_id"])[-1]["event_type"] == (
        "recovery_inconsistent"
    )
    count = len(store.list_events(job["job_id"]))
    with pytest.raises(
        commerce.CommerceRecoveryError, match="inconsistent_recovery_state"
    ):
        store.recover(now=NOW + timedelta(minutes=1))
    assert len(store.list_events(job["job_id"])) == count


def test_blocked_on_cogitator_substatus_events_do_not_change_state(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    blocked = store.set_blocked_on_cogitator(
        job["job_id"],
        True,
        expected_version=0,
        actor="worker",
        reason_code="approved_facts_unavailable",
        now=NOW,
    )
    assert blocked["current_state"] == "requested"
    assert blocked["substatus"] == "blocked_on_cogitator"
    cleared = store.set_blocked_on_cogitator(
        job["job_id"],
        False,
        expected_version=1,
        actor="worker",
        reason_code="approved_facts_available",
        now=NOW,
    )
    assert cleared["current_state"] == "requested"
    assert cleared["substatus"] == ""
    assert [event["event_type"] for event in store.list_events(job["job_id"])] == [
        "job_created",
        "substatus_set",
        "substatus_cleared",
    ]


def test_cli_list_status_pause_resume_cancel_and_explicit_db(tmp_path):
    db_path = tmp_path / "cli.db"
    store = commerce.CommerceJobStore(db_path)
    job = make_job(store)
    listed = run_cli(db_path, "list")
    assert listed.returncode == 0
    assert json.loads(listed.stdout)["result"]["jobs"][0]["job_id"] == job["job_id"]
    status = run_cli(db_path, "status", job["job_id"])
    assert status.returncode == 0
    assert json.loads(status.stdout)["result"]["job"]["current_state"] == "requested"
    paused = run_cli(db_path, "pause", job["job_id"], "--reason", "operator pause")
    assert paused.returncode == 0
    resumed = run_cli(db_path, "resume", job["job_id"])
    assert resumed.returncode == 0
    cancelled = run_cli(db_path, "cancel", job["job_id"], "--reason", "operator cancel")
    assert cancelled.returncode == 0
    assert json.loads(cancelled.stdout)["result"]["job"]["current_state"] == (
        "cancelled"
    )


def test_cli_stable_error_codes_and_no_secret_echo(tmp_path):
    db_path = tmp_path / "cli-errors.db"
    store = commerce.CommerceJobStore(db_path)
    job = make_job(store)
    missing = run_cli(db_path, "status", "missing-job")
    assert missing.returncode == 3
    assert json.loads(missing.stderr)["error"]["code"] == "job_not_found"
    invalid = run_cli(db_path, "resume", job["job_id"])
    assert invalid.returncode == 4
    secret = "password=never-print-this"
    forbidden = run_cli(db_path, "pause", job["job_id"], "--reason", secret)
    assert forbidden.returncode == 6
    assert secret not in forbidden.stdout + forbidden.stderr


def test_cli_has_no_job_creation_or_arbitrary_transition_command(tmp_path):
    db_path = tmp_path / "must-not-exist.db"
    for command in ("create", "transition"):
        result = run_cli(db_path, command)
        assert result.returncode == 2
        assert not db_path.exists()


def test_cli_redacts_malformed_forbidden_database_content(tmp_path):
    db_path = tmp_path / "malformed.db"
    store = commerce.CommerceJobStore(db_path)
    job = make_job(store)
    leaked = "Bearer abcdefghijklmnopqrstuvwxyz"
    with raw_connection(store) as connection:
        connection.execute(
            "UPDATE jobs SET original_objective=?,plan_json=? WHERE job_id=?",
            (leaked, json.dumps({"note": leaked}), job["job_id"]),
        )
    result = run_cli(db_path, "status", job["job_id"])
    assert result.returncode == 0
    assert leaked not in result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["result"]["job"]["original_objective"].startswith("[REDACTED")
    assert payload["result"]["job"]["plan"]["redacted"] is True


def test_cli_explicit_db_does_not_touch_default_database(tmp_path):
    hermes_home = tmp_path / "isolated-hermes-home"
    explicit = tmp_path / "explicit.db"
    result = run_cli(
        explicit,
        "list",
        env={"HERMES_HOME": str(hermes_home)},
    )
    assert result.returncode == 0
    assert explicit.exists()
    assert not (hermes_home / "commerce" / "commerce_jobs.db").exists()


@pytest.mark.parametrize(
    "unsafe_key",
    [
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "4242 4242 4242 4242",
        "password_hunter2",
    ],
)
def test_sensitive_mapping_keys_are_rejected_without_persistence(tmp_path, unsafe_key):
    store = make_store(tmp_path)
    job = make_job(store)
    with pytest.raises(commerce.CommerceForbiddenDataError) as error:
        store.record_action(
            job["job_id"],
            action_type="read_provider",
            provider="fake",
            effect_class="read_only",
            idempotency_key="unsafe-key",
            request={unsafe_key: "never"},
            target_state="executing_read_only",
            now=NOW,
        )
    assert unsafe_key not in str(error.value)
    with raw_connection(store) as connection:
        persisted = "\n".join(connection.iterdump())
    assert unsafe_key not in persisted


def test_non_string_mapping_keys_are_rejected():
    with pytest.raises(commerce.CommerceConfigurationError, match="invalid_json_key"):
        commerce.canonical_json({1: "not-json-object-shape"})


def test_handoff_token_cannot_be_persisted_through_public_text(tmp_path, monkeypatch):
    monkeypatch.setattr(commerce.secrets, "token_urlsafe", lambda _: "A" * 42 + "-")
    store = make_store(tmp_path)
    job = make_job(store)
    gate = open_active_handoff_gate(store, job, expires_at=NOW + timedelta(hours=1))
    _, token = store.issue_gate_handoff(gate["gate_id"], now=NOW)
    assert token.endswith("-")

    for unsafe_text in (token, f"x{token}", f"{token}x", f"x{token}x"):
        with pytest.raises(commerce.CommerceForbiddenDataError):
            commerce.canonical_json({"note": unsafe_text})
        with pytest.raises(commerce.CommerceForbiddenDataError) as error:
            store.request_gate_done(
                gate["gate_id"],
                token,
                actor=unsafe_text,
                now=NOW + timedelta(minutes=1),
            )
        assert token not in str(error.value)
    assert store.get_gate(gate["gate_id"])["done_requested_at"] is None
    with raw_connection(store) as connection:
        persisted = "\n".join(connection.iterdump())
    assert token not in persisted


def test_gate_cannot_open_after_job_is_cancelled(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    store.cancel(
        job["job_id"],
        expected_version=0,
        actor="operator",
        reason="stop",
        now=NOW,
    )
    with pytest.raises(commerce.CommerceGateError, match="gate_job_inactive"):
        store.open_gate(
            job["job_id"],
            gate_type="merchant_login",
            human_action="Log in",
            provider_truth_reference="provider-account-status-ref",
            opening_evidence={},
            now=NOW,
        )


def test_concurrent_cancel_and_gate_open_leave_no_active_gate(tmp_path):
    path = tmp_path / "cancel-gate-race.db"
    store = commerce.CommerceJobStore(path)
    job = make_job(store)
    barrier = threading.Barrier(2)

    def cancel_job():
        barrier.wait()
        return commerce.CommerceJobStore(path).cancel(
            job["job_id"],
            expected_version=0,
            actor="operator",
            reason="stop",
            now=NOW,
        )

    def open_job_gate():
        barrier.wait()
        try:
            return commerce.CommerceJobStore(path).open_gate(
                job["job_id"],
                gate_type="merchant_login",
                human_action="Log in",
                provider_truth_reference="provider-account-status-ref",
                opening_evidence={},
                now=NOW,
            )["status"]
        except commerce.CommerceGateError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled_future = pool.submit(cancel_job)
        gate_future = pool.submit(open_job_gate)
        cancelled = cancelled_future.result()
        gate_result = gate_future.result()

    assert cancelled["active"] is False
    assert gate_result in {"open", "gate_job_inactive"}
    with raw_connection(store) as connection:
        active_gate_count = connection.execute(
            """SELECT COUNT(*) FROM gates
               WHERE job_id=? AND status IN ('open','completed')""",
            (job["job_id"],),
        ).fetchone()[0]
    assert active_gate_count == 0


def test_action_approval_cannot_exceed_or_outlive_fifteen_minutes(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "awaiting_purchase_approval")
    action = record_action(
        store,
        job["job_id"],
        effect_class="consequential",
        key="late-authorization",
        approve=False,
    )
    with pytest.raises(
        commerce.CommerceGateError, match="action_approval_ttl_exceeded"
    ):
        store.open_gate(
            job["job_id"],
            gate_type="action_approval",
            human_action="Approve exact action",
            provider_truth_reference="approval-verification-ref",
            opening_evidence={},
            approval_reference="approval-too-long",
            approval_fingerprint=action["action_fingerprint"],
            expires_at=NOW + timedelta(minutes=16),
            now=NOW,
        )

    gate = store.open_gate(
        job["job_id"],
        gate_type="action_approval",
        human_action="Approve exact action",
        provider_truth_reference="approval-verification-ref",
        opening_evidence={},
        approval_reference="approval-expiring",
        approval_fingerprint=action["action_fingerprint"],
        now=NOW,
    )
    assert gate["expires_at"] == commerce.iso_utc(NOW + commerce.ACTION_APPROVAL_TTL)
    store.complete_gate(
        gate["gate_id"],
        evidence={"approval_granted": True, "provider_truth_verified": True},
        actor="operator",
        now=NOW + timedelta(minutes=1),
    )
    with pytest.raises(commerce.CommerceActionError, match="approval_gate_expired"):
        store.authorize_action(
            action["action_id"],
            gate_id=gate["gate_id"],
            actor="operator",
            now=NOW + timedelta(minutes=15),
        )
    assert store.get_action(action["action_id"])["approval_status"] == "unbound"
    replacement_gate = store.open_gate(
        job["job_id"],
        gate_type="action_approval",
        human_action="Approve refreshed exact action",
        provider_truth_reference="approval-verification-ref",
        opening_evidence={},
        approval_reference="approval-refreshed",
        approval_fingerprint=action["action_fingerprint"],
        now=NOW + timedelta(minutes=16),
    )
    assert store.get_gate(gate["gate_id"])["status"] == "expired"
    store.complete_gate(
        replacement_gate["gate_id"],
        evidence={"approval_granted": True, "provider_truth_verified": True},
        actor="operator",
        now=NOW + timedelta(minutes=17),
    )
    refreshed = store.authorize_action(
        action["action_id"],
        gate_id=replacement_gate["gate_id"],
        actor="operator",
        now=NOW + timedelta(minutes=18),
    )
    assert refreshed["approval_reference"] == "approval-refreshed"


def test_authorized_action_cannot_dispatch_after_approval_expiry(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "awaiting_purchase_approval")
    action = record_action(
        store,
        job["job_id"],
        effect_class="consequential",
        key="late-dispatch",
        approve=False,
    )
    gate = store.open_gate(
        job["job_id"],
        gate_type="action_approval",
        human_action="Approve exact action",
        provider_truth_reference="approval-verification-ref",
        opening_evidence={},
        approval_reference="approval-live-briefly",
        approval_fingerprint=action["action_fingerprint"],
        now=NOW,
    )
    store.complete_gate(
        gate["gate_id"],
        evidence={"approval_granted": True, "provider_truth_verified": True},
        actor="operator",
        now=NOW + timedelta(minutes=1),
    )
    authorized = store.authorize_action(
        action["action_id"],
        gate_id=gate["gate_id"],
        actor="operator",
        now=NOW + timedelta(minutes=2),
    )
    assert authorized["approval_expires_at"] == gate["expires_at"]
    with pytest.raises(commerce.CommerceActionError, match="action_approval_expired"):
        store.dispatch_action(
            action["action_id"],
            now=NOW + timedelta(minutes=15),
        )
    assert store.get_action(action["action_id"])["action_status"] == "planned"
    refresh_gate = store.open_gate(
        job["job_id"],
        gate_type="action_approval",
        human_action="Approve refreshed exact action",
        provider_truth_reference="approval-verification-ref",
        opening_evidence={},
        approval_reference="approval-refreshed",
        approval_fingerprint=action["action_fingerprint"],
        now=NOW + timedelta(minutes=16),
    )
    store.complete_gate(
        refresh_gate["gate_id"],
        evidence={"approval_granted": True, "provider_truth_verified": True},
        actor="operator",
        now=NOW + timedelta(minutes=17),
    )
    refreshed = store.authorize_action(
        action["action_id"],
        gate_id=refresh_gate["gate_id"],
        actor="operator",
        now=NOW + timedelta(minutes=18),
    )
    assert refreshed["approval_reference"] == "approval-refreshed"
    dispatched = store.dispatch_action(
        action["action_id"],
        now=NOW + timedelta(minutes=19),
    )
    assert dispatched["action_status"] == "dispatched"


@pytest.mark.parametrize("state", ["paused", "timed_out"])
def test_gate_cannot_open_while_job_is_dormant(tmp_path, state):
    store = make_store(tmp_path)
    job = make_job(store)
    if state == "paused":
        store.pause(
            job["job_id"],
            expected_version=0,
            actor="operator",
            reason="wait",
            now=NOW,
        )
    else:
        seed_state(store, job["job_id"], "requested", deadline=NOW)
        assert store.sweep_timeouts(now=NOW) == [job["job_id"]]

    assert store.get_job(job["job_id"])["current_state"] == state
    with pytest.raises(commerce.CommerceGateError, match="gate_job_inactive"):
        store.open_gate(
            job["job_id"],
            gate_type="merchant_login",
            human_action="Log in",
            provider_truth_reference="provider-account-status-ref",
            opening_evidence={},
            now=NOW,
        )


@pytest.mark.parametrize("state", ["paused", "timed_out"])
def test_dormancy_and_gate_open_race_leaves_no_active_gate(tmp_path, state):
    path = tmp_path / f"{state}-gate-race.db"
    store = commerce.CommerceJobStore(path)
    job = make_job(store)
    if state == "timed_out":
        seed_state(store, job["job_id"], "requested", deadline=NOW)
    barrier = threading.Barrier(2)

    def make_dormant():
        barrier.wait()
        thread_store = commerce.CommerceJobStore(path)
        if state == "paused":
            return thread_store.pause(
                job["job_id"],
                expected_version=0,
                actor="operator",
                reason="wait",
                now=NOW,
            )
        assert thread_store.sweep_timeouts(now=NOW) == [job["job_id"]]
        return thread_store.get_job(job["job_id"])

    def open_job_gate():
        barrier.wait()
        try:
            return commerce.CommerceJobStore(path).open_gate(
                job["job_id"],
                gate_type="merchant_login",
                human_action="Log in",
                provider_truth_reference="provider-account-status-ref",
                opening_evidence={},
                now=NOW,
            )["status"]
        except commerce.CommerceGateError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        dormant_future = pool.submit(make_dormant)
        gate_future = pool.submit(open_job_gate)
        dormant = dormant_future.result()
        gate_result = gate_future.result()

    assert dormant["current_state"] == state
    assert gate_result in {"open", "gate_job_inactive"}
    resumed = store.resume(
        job["job_id"],
        expected_version=1,
        actor="operator",
        now=NOW + timedelta(minutes=1),
    )
    assert resumed["current_state"] == "ready"
    with raw_connection(store) as connection:
        active_gate_count = connection.execute(
            """SELECT COUNT(*) FROM gates
               WHERE job_id=? AND status IN ('open','completed')""",
            (job["job_id"],),
        ).fetchone()[0]
    assert active_gate_count == 0


def test_delivery_snapshot_and_checkpoint_are_durable_and_idempotent(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)

    snapshot = store.delivery_snapshot(job["job_id"])

    assert snapshot["job"]["job_id"] == job["job_id"]
    assert snapshot["actions"] == []
    assert snapshot["gates"] == []
    assert store.record_delivery(job["job_id"], "a" * 64, actor="gateway") is True
    assert store.record_delivery(job["job_id"], "a" * 64, actor="gateway") is False
    delivered = [
        event
        for event in store.list_events(job["job_id"])
        if event["event_type"] == "commerce_gateway_delivered"
    ]
    assert len(delivered) == 1
    assert delivered[0]["evidence"] == {"delivery_key": "a" * 64}

    with pytest.raises(commerce.CommerceJobError, match="delivery_key"):
        store.record_delivery(job["job_id"], "not-a-digest", actor="gateway")

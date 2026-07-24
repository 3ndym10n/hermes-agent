from __future__ import annotations

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
    approval_reference: str = "",
    approval_fingerprint: str = "",
) -> dict:
    return store.record_action(
        job_id,
        action_type=action_type,
        provider="fake",
        effect_class=effect_class,
        idempotency_key=key,
        request={"operation": action_type, "target": "example.com"},
        target_state=target_state,
        approval_reference=approval_reference,
        approval_fingerprint=approval_fingerprint,
        now=NOW,
    )


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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
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
        assert (
            connection.execute("SELECT value FROM preserved_marker").fetchone()[0]
            == "kept"
        )


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
        connection.execute("PRAGMA user_version=2")
    with pytest.raises(
        commerce.CommerceConfigurationError, match="future_schema_version"
    ):
        commerce.CommerceJobStore(path).initialize()


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


def test_every_allowed_transition_updates_atomically_with_one_event(tmp_path):
    store = make_store(tmp_path)
    index = 0
    for source, targets in commerce.ALLOWED_TRANSITIONS.items():
        for target in targets:
            index += 1
            job = make_job(store, objective=f"transition {index}")
            seed_state(store, job["job_id"], source)
            action_id = ""
            approval_reference = ""
            if target == "rolling_back":
                approval_reference = f"approval-{index}"
                action_id = record_action(
                    store,
                    job["job_id"],
                    effect_class="consequential",
                    target_state="rolling_back",
                    action_type="rollback",
                    key=f"rollback-{index}",
                    approval_reference=approval_reference,
                )["action_id"]
            elif source == "ready_to_execute" and target in commerce.EXECUTION_STATES:
                action_id = record_action(
                    store,
                    job["job_id"],
                    effect_class="consequential",
                    target_state=target,
                    action_type=f"enter_{target}",
                    key=f"dispatch-{index}",
                )["action_id"]
            before = len(store.list_events(job["job_id"]))
            updated = store.transition(
                job["job_id"],
                target,
                expected_state=source,
                expected_version=0,
                actor="test",
                reason_code="allowed_transition",
                action_id=action_id,
                approval_reference=approval_reference,
                now=NOW,
            )
            assert updated["current_state"] == target
            assert updated["row_version"] == 1
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
            "live",
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
            "recovering_existing_state",
            expected_state="invented",
            expected_version=0,
            actor="test",
            reason_code="illegal",
        )
    with pytest.raises(commerce.CommerceStaleVersionError, match="stale_row_version"):
        store.transition(
            job["job_id"],
            "recovering_existing_state",
            expected_state="requested",
            expected_version=9,
            actor="test",
            reason_code="stale",
        )
    assert store.get_job(job["job_id"])["current_state"] == "requested"
    assert store.list_events(job["job_id"]) == original_events
    updated = store.transition(
        job["job_id"],
        "recovering_existing_state",
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
            "recovering_existing_state",
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
            "recovering_existing_state",
            expected_state="requested",
            expected_version=0,
            actor="test",
            reason_code="atomic",
        )
    current = store.get_job(job["job_id"])
    assert current["current_state"] == "requested"
    assert current["row_version"] == 0
    assert len(store.list_events(job["job_id"])) == 1


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
    monkeypatch.setattr(
        commerce.uuid,
        "uuid4",
        lambda: type("SyntheticUuid", (), {"hex": generated})(),
    )
    store = make_store(tmp_path)
    job = make_job(store)
    assert job["job_id"] == f"cj_{generated}"
    assert store.get_job(job["job_id"])["job_id"] == f"cj_{generated}"
    assert len(store.list_events(job["job_id"])) == 1


def test_rolling_back_requires_matching_approval_and_action(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    with pytest.raises(commerce.CommerceConfigurationError, match="missing_field"):
        store.transition(
            job["job_id"],
            "rolling_back",
            expected_state="requested",
            expected_version=0,
            actor="test",
            reason_code="rollback",
        )
    action = record_action(
        store,
        job["job_id"],
        effect_class="consequential",
        target_state="rolling_back",
        action_type="rollback",
        approval_reference="approval-a",
    )
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="approval_mismatch"
    ):
        store.transition(
            job["job_id"],
            "rolling_back",
            expected_state="requested",
            expected_version=0,
            actor="test",
            reason_code="rollback",
            action_id=action["action_id"],
            approval_reference="approval-b",
        )


def test_ready_to_execute_requires_bound_next_action(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "ready_to_execute")
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="action_required"
    ):
        store.transition(
            job["job_id"],
            "registering_domain",
            expected_state="ready_to_execute",
            expected_version=0,
            actor="test",
            reason_code="dispatch",
        )
    wrong = record_action(
        store,
        job["job_id"],
        effect_class="consequential",
        target_state="configuring_dns",
    )
    with pytest.raises(
        commerce.CommerceInvalidTransitionError, match="action_not_bound"
    ):
        store.transition(
            job["job_id"],
            "registering_domain",
            expected_state="ready_to_execute",
            expected_version=0,
            actor="test",
            reason_code="dispatch",
            action_id=wrong["action_id"],
        )


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
    assert resumed["current_state"] == "ready_to_execute"
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
        target_state="registering_domain",
        request={"domain": "example.com", "timestamp": "first", "attempt": 1},
    )
    changed_runtime = commerce.action_fingerprint(
        action_type="register",
        provider="porkbun",
        effect_class="consequential",
        target_state="registering_domain",
        request={"attempt": 99, "timestamp": "second", "domain": "example.com"},
    )
    changed_payload = commerce.action_fingerprint(
        action_type="register",
        provider="porkbun",
        effect_class="consequential",
        target_state="registering_domain",
        request={"domain": "different.example"},
    )
    assert base == changed_runtime
    assert base != changed_payload


def test_material_plan_change_invalidates_only_matching_bindings(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    first = store.set_plan(
        job["job_id"], sample_plan(), expected_version=0, actor="planner", now=NOW
    )
    old_fp = first["plan_fingerprint"]
    matching = record_action(
        store,
        job["job_id"],
        key="matching",
        approval_reference="approval-old",
        approval_fingerprint=old_fp,
    )
    other = record_action(
        store,
        job["job_id"],
        key="other",
        approval_reference="approval-other",
        approval_fingerprint="different-fingerprint",
    )
    matching_gate = store.open_gate(
        job["job_id"],
        gate_type="approval",
        human_action="Review exact terms",
        provider_truth_reference="provider-read-ref",
        opening_evidence={},
        approval_reference="approval-old",
        approval_fingerprint=old_fp,
        now=NOW,
    )
    other_gate = store.open_gate(
        job["job_id"],
        gate_type="other",
        human_action="Review unrelated terms",
        provider_truth_reference="provider-read-ref-2",
        opening_evidence={},
        approval_reference="approval-other",
        approval_fingerprint="different-fingerprint",
        now=NOW,
    )
    changed = {**sample_plan(), "domain": "new.example"}
    store.set_plan(job["job_id"], changed, expected_version=1, actor="planner", now=NOW)
    assert store.get_action(matching["action_id"])["approval_status"] == "stale"
    assert store.get_action(other["action_id"])["approval_status"] == "live"
    with raw_connection(store) as connection:
        statuses = {
            row["gate_id"]: row["status"]
            for row in connection.execute(
                "SELECT gate_id,status FROM gates WHERE job_id=?", (job["job_id"],)
            )
        }
    assert statuses[matching_gate["gate_id"]] == "invalidated"
    assert statuses[other_gate["gate_id"]] == "open"


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
    completed = store.complete_gate(
        gate["gate_id"],
        evidence={"provider_check": "complete"},
        actor="operator",
        now=NOW + timedelta(minutes=1),
    )
    assert completed["status"] == "completed"
    with pytest.raises(commerce.CommerceGateError, match="gate_not_open"):
        store.complete_gate(gate["gate_id"], evidence={}, actor="operator", now=NOW)
    expiring = store.open_gate(
        job["job_id"],
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


def test_timeout_sweep_pauses_once_at_72_hours(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    assert store.sweep_timeouts(now=NOW + timedelta(hours=71, minutes=59)) == []
    assert store.get_job(job["job_id"])["current_state"] == "requested"
    assert store.sweep_timeouts(now=NOW + timedelta(hours=72)) == [job["job_id"]]
    assert store.get_job(job["job_id"])["current_state"] == "paused"
    event_count = len(store.list_events(job["job_id"]))
    assert store.sweep_timeouts(now=NOW + timedelta(days=5)) == []
    assert len(store.list_events(job["job_id"])) == event_count


def test_timeout_sweep_excludes_uncertain_reconciliation_rollback_and_terminal(
    tmp_path,
):
    store = make_store(tmp_path)
    jobs = []
    for state in (
        "uncertain_external_state",
        "awaiting_reconciliation",
        "rolling_back",
        "completed",
    ):
        job = make_job(store, objective=f"timeout excluded {state}")
        seed_state(
            store,
            job["job_id"],
            state,
            deadline=NOW - timedelta(seconds=1),
        )
        jobs.append(job)
    assert store.sweep_timeouts(now=NOW) == []
    assert [store.get_job(job["job_id"])["current_state"] for job in jobs] == [
        "uncertain_external_state",
        "awaiting_reconciliation",
        "rolling_back",
        "completed",
    ]


def test_reopen_preserves_committed_state_and_uncommitted_changes_disappear(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    store.transition(
        job["job_id"],
        "recovering_existing_state",
        expected_state="requested",
        expected_version=0,
        actor="test",
        reason_code="advance",
        now=NOW,
    )
    reopened = commerce.CommerceJobStore(store.path)
    assert reopened.get_job(job["job_id"])["current_state"] == (
        "recovering_existing_state"
    )
    connection = raw_connection(store)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE jobs SET current_state='planning' WHERE job_id=?", (job["job_id"],)
    )
    connection.close()
    assert reopened.get_job(job["job_id"])["current_state"] == (
        "recovering_existing_state"
    )


def test_recovery_parks_consequential_inflight_action_as_uncertain_once(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    seed_state(store, job["job_id"], "registering_domain")
    action = record_action(store, job["job_id"], effect_class="consequential")
    store.dispatch_action(action["action_id"], now=NOW)
    reopened = commerce.CommerceJobStore(store.path)
    first = reopened.recover(now=NOW + timedelta(minutes=1))
    assert first["uncertain"] == 1
    assert reopened.get_action(action["action_id"])["action_status"] == "uncertain"
    assert reopened.get_job(job["job_id"])["current_state"] == (
        "uncertain_external_state"
    )
    event_count = len(reopened.list_events(job["job_id"]))
    second = reopened.recover(now=NOW + timedelta(minutes=2))
    assert second == {"recoverable": 0, "uncertain": 0, "inconsistent": 0}
    assert len(reopened.list_events(job["job_id"])) == event_count


def test_recovery_marks_read_only_action_recoverable_without_execution(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
    action = record_action(store, job["job_id"], effect_class="read_only")
    store.dispatch_action(action["action_id"], now=NOW)
    recovered = store.recover(now=NOW + timedelta(minutes=1))
    assert recovered["recoverable"] == 1
    assert store.get_action(action["action_id"])["action_status"] == "recoverable"
    assert store.get_job(job["job_id"])["current_state"] == "requested"
    event_count = len(store.list_events(job["job_id"]))
    assert store.recover(now=NOW + timedelta(minutes=2))["recoverable"] == 0
    assert len(store.list_events(job["job_id"])) == event_count


def test_already_uncertain_action_and_job_are_recovery_idempotent(tmp_path):
    store = make_store(tmp_path)
    job = make_job(store)
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

from __future__ import annotations

import copy
import json
import stat
from pathlib import Path

import pytest

import commerce_receipt
from commerce_jobs import CommerceJobError

FIXTURE = Path(__file__).parent / "fixtures" / "acceptance" / "receipt.json"
FACT_KEYS = (
    "checkout_absent_verified",
    "dns",
    "domain",
    "no_payment_collected",
    "public_url",
    "shopify",
    "total_spend",
    "unresolved",
    "verification",
    "waitlist_test",
)


class SnapshotStore:
    def __init__(self, snapshot: dict):
        self.snapshot = snapshot
        self.calls: list[str] = []

    def delivery_snapshot(self, job_id: str) -> dict:
        self.calls.append(job_id)
        return copy.deepcopy(self.snapshot)


def golden() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def provider_facts(receipt: dict | None = None) -> dict:
    source = receipt or golden()
    return {key: copy.deepcopy(source[key]) for key in FACT_KEYS}


def snapshot(*, facts: dict | None = None) -> dict:
    receipt = golden()
    verified = copy.deepcopy(facts if facts is not None else provider_facts(receipt))
    return {
        "job": {
            "job_id": receipt["job_id"],
            "state_machine_version": 2,
            "current_state": "verifying",
            "original_objective": receipt["objective"],
        },
        "actions": [
            {
                "action_type": item["step"],
                "action_status": "succeeded",
                "effect_class": "consequential" if index == 0 else "read_only",
                "uncertainty": False,
                "terminal_at": item["at"],
                "result": {
                    "evidence_ref": item["evidence"],
                    **(
                        {
                            "provider_truth_verified": True,
                            "receipt_facts": verified,
                        }
                        if index == 1
                        else {}
                    ),
                },
            }
            for index, item in enumerate(receipt["actions_completed"])
        ],
        "gates": [
            {
                "gate_id": item["gate_id"],
                "gate_type": item["type"],
                "status": "consumed",
                "opened_at": item["opened"],
                "completed_at": item["verified"],
            }
            for item in receipt["human_gates_completed"]
        ],
        "events": [],
    }


def delete_path(value: dict, dotted: str) -> None:
    target = value
    parts = dotted.split(".")
    for part in parts[:-1]:
        target = target[part]
    del target[parts[-1]]


def test_golden_receipt_is_rebuilt_from_one_ledger_snapshot():
    store = SnapshotStore(snapshot())
    assert set(golden()) == {
        "actions_completed",
        "checkout_absent_verified",
        "completed_at",
        "dns",
        "domain",
        "human_gates_completed",
        "job_id",
        "no_payment_collected",
        "objective",
        "public_url",
        "shopify",
        "state_machine_version",
        "total_spend",
        "unresolved",
        "verification",
        "waitlist_test",
    }

    assert (
        commerce_receipt.build_execution_receipt(store, golden()["job_id"]) == golden()
    )
    assert store.calls == [golden()["job_id"]]


def test_persist_is_atomic_0600_and_byte_stable(tmp_path):
    store = SnapshotStore(snapshot())
    job_id = golden()["job_id"]

    path = commerce_receipt.persist_execution_receipt(
        store, job_id, receipts_root=tmp_path / "receipts"
    )
    first = path.read_bytes()
    assert json.loads(first) == golden()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    assert (
        commerce_receipt.persist_execution_receipt(
            store, job_id, receipts_root=tmp_path / "receipts"
        )
        == path
    )
    assert path.read_bytes() == first


def test_default_receipts_root_uses_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setattr(commerce_receipt, "get_hermes_home", lambda: tmp_path)
    job_id = golden()["job_id"]

    path = commerce_receipt.persist_execution_receipt(SnapshotStore(snapshot()), job_id)

    assert path == tmp_path / "commerce" / "receipts" / f"{job_id}.json"


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("verification.all_green", "verification_not_green"),
        ("no_payment_collected", "payment_absence_not_verified"),
        ("checkout_absent_verified", "checkout_absence_not_verified"),
    ],
)
def test_completion_attestations_fail_closed(path, code):
    facts = provider_facts()
    target = facts
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = False

    with pytest.raises(commerce_receipt.CommerceReceiptError, match=code):
        commerce_receipt.build_execution_receipt(
            SnapshotStore(snapshot(facts=facts)), golden()["job_id"]
        )


@pytest.mark.parametrize(
    "path",
    [
        "domain.name",
        "domain.registrar",
        "domain.order_id",
        "domain.spend.amount_usd_cents",
        "domain.spend.display",
        "domain.cogitator.proposal_id",
        "domain.cogitator.approval_id",
        "domain.cogitator.receipt_ref",
        "domain.auto_renew",
        "domain.whois_privacy",
        "shopify.myshopify_domain",
        "shopify.shop_id",
        "shopify.plan",
        "shopify.admin_url",
        "public_url",
        "dns.status",
        "dns.records",
        "waitlist_test.result",
        "waitlist_test.test_address_used",
        "waitlist_test.consent_recorded",
        "waitlist_test.test_subscriber_deleted",
        "verification.checklist",
        "verification.all_green",
        "verification.evidence_bundle",
        "unresolved",
        "no_payment_collected",
        "checkout_absent_verified",
        "total_spend",
    ],
)
def test_every_provider_receipt_field_is_required(path):
    facts = provider_facts()
    delete_path(facts, path)

    with pytest.raises(CommerceJobError, match="missing_receipt_field"):
        commerce_receipt.build_execution_receipt(
            SnapshotStore(snapshot(facts=facts)), golden()["job_id"]
        )


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda data: data["actions"][0]["result"].update(
                evidence_ref="/tmp/evidence.json"
            ),
            "unsafe_evidence_reference",
        ),
        (
            lambda data: data["actions"][0]["result"].update(
                evidence_ref=f"evidence/{golden()['job_id']}/../secret"
            ),
            "unsafe_evidence_reference",
        ),
        (
            lambda data: data["actions"][1]["result"]["receipt_facts"].update(
                public_url="http://siliconcurrent.com/"
            ),
            "invalid_receipt_url",
        ),
        (
            lambda data: data["actions"][1]["result"]["receipt_facts"][
                "shopify"
            ].update(
                admin_url="https://admin.shopify.com/store/silicon-current?token=x"
            ),
            "invalid_receipt_url",
        ),
        (
            lambda data: data["actions"][1]["result"]["receipt_facts"][
                "waitlist_test"
            ].update(test_address_used="customer@example.com"),
            "receipt_pii_forbidden",
        ),
    ],
)
def test_urls_evidence_and_pii_are_rejected(mutate, code):
    data = snapshot()
    mutate(data)

    with pytest.raises(CommerceJobError, match=code):
        commerce_receipt.build_execution_receipt(
            SnapshotStore(data), golden()["job_id"]
        )


def test_existing_forbidden_data_screen_rejects_secret_fields():
    data = snapshot()
    data["actions"][1]["result"]["receipt_facts"]["domain"]["api_key"] = "unsafe"

    with pytest.raises(CommerceJobError, match="forbidden_sensitive_field"):
        commerce_receipt.build_execution_receipt(
            SnapshotStore(data), golden()["job_id"]
        )


@pytest.mark.parametrize(
    ("where", "value", "code"),
    [
        ("action_status", "uncertain", "receipt_action_unresolved"),
        ("gate_status", "open", "receipt_gate_unresolved"),
        ("job_state", "ready", "receipt_job_not_verified"),
    ],
)
def test_unresolved_or_unverified_ledger_cannot_produce_receipt(where, value, code):
    data = snapshot()
    if where == "action_status":
        data["actions"][0]["action_status"] = value
    elif where == "gate_status":
        data["gates"][0]["status"] = value
    else:
        data["job"]["current_state"] = value

    with pytest.raises(CommerceJobError, match=code):
        commerce_receipt.build_execution_receipt(
            SnapshotStore(data), golden()["job_id"]
        )


def test_explicit_provider_facts_must_match_ledger():
    facts = provider_facts()
    facts["domain"]["order_id"] = "different-order"

    with pytest.raises(CommerceJobError, match="receipt_facts_mismatch"):
        commerce_receipt.build_execution_receipt(
            SnapshotStore(snapshot()),
            golden()["job_id"],
            verified_facts=facts,
        )


def test_receipt_target_must_not_be_a_symlink(tmp_path):
    root = tmp_path / "receipts"
    root.mkdir()
    target = root / f"{golden()['job_id']}.json"
    target.symlink_to(tmp_path / "outside.json")

    with pytest.raises(CommerceJobError, match="unsafe_receipt_path"):
        commerce_receipt.persist_execution_receipt(
            SnapshotStore(snapshot()), golden()["job_id"], receipts_root=root
        )

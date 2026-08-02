"""Build and atomically persist the governed commerce execution receipt."""

from __future__ import annotations

import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from commerce_jobs import (
    STATE_MACHINE_VERSION,
    CommerceJobError,
    canonical_json,
    reject_forbidden_data,
)
from hermes_constants import get_hermes_home
from utils import atomic_json_write

_FACT_KEYS = frozenset({
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
})
_DOMAIN_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,239}\Z")
_JOB_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,119}\Z")
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_TEST_ADDRESS_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DNS_RECORDS = ("A", "AAAA", "CNAME www")


class CommerceReceiptError(CommerceJobError):
    """Safe validation failure while building or writing a receipt."""


def _fail(code: str, field: str = "") -> None:
    raise CommerceReceiptError(code, field)


def default_receipts_root() -> Path:
    return get_hermes_home() / "commerce" / "receipts"


def _object(
    value: Any,
    field: str,
    keys: frozenset[str],
) -> Mapping[str, Any]:
    reject_forbidden_data(value, field)
    if not isinstance(value, Mapping):
        _fail("invalid_receipt_object", field)
    present = frozenset(value)
    missing = keys - present
    if missing:
        _fail("missing_receipt_field", f"{field}.{sorted(missing)[0]}")
    extra = present - keys
    if extra:
        _fail("unexpected_receipt_field", f"{field}.{sorted(extra)[0]}")
    return value


def _text(value: Any, field: str, *, max_chars: int = 500) -> str:
    reject_forbidden_data(value, field)
    if not isinstance(value, str):
        _fail("invalid_receipt_text", field)
    normalized = " ".join(value.split())
    if not normalized:
        _fail("missing_receipt_field", field)
    if len(normalized) > max_chars:
        _fail("receipt_field_too_long", field)
    if _EMAIL_RE.search(normalized):
        _fail("receipt_pii_forbidden", field)
    return normalized


def _identifier(value: Any, field: str) -> str:
    result = _text(value, field, max_chars=240)
    if _ID_RE.fullmatch(result) is None:
        _fail("invalid_receipt_identifier", field)
    return result


def _domain(value: Any, field: str) -> str:
    result = _text(value, field, max_chars=253).lower()
    if _DOMAIN_RE.fullmatch(result) is None:
        _fail("invalid_receipt_domain", field)
    return result


def _timestamp(value: Any, field: str) -> tuple[str, datetime]:
    result = _text(value, field, max_chars=40)
    if not result.endswith("Z"):
        _fail("invalid_receipt_timestamp", field)
    try:
        parsed = datetime.fromisoformat(result[:-1] + "+00:00")
    except ValueError:
        _fail("invalid_receipt_timestamp", field)
    return result, parsed


def _evidence_ref(value: Any, field: str, job_id: str, *, bundle: bool = False) -> str:
    result = _text(value, field, max_chars=500)
    if (
        result.startswith("/")
        or "\\" in result
        or "://" in result
        or (bundle and not result.endswith("/"))
    ):
        _fail("unsafe_evidence_reference", field)
    parts = result.rstrip("/").split("/")
    if (
        len(parts) < 3
        or parts[:2] != ["evidence", job_id]
        or any(part in {"", ".", ".."} for part in parts)
    ):
        _fail("unsafe_evidence_reference", field)
    return result


def _https_url(value: Any, field: str) -> tuple[str, Any]:
    result = _text(value, field, max_chars=500)
    try:
        parsed = urlsplit(result)
        port = parsed.port
    except ValueError:
        _fail("invalid_receipt_url", field)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
    ):
        _fail("invalid_receipt_url", field)
    return result, parsed


def _verified_facts(value: Any, job_id: str) -> dict[str, Any]:
    facts = _object(value, "verified_facts", _FACT_KEYS)

    domain = _object(
        facts["domain"],
        "verified_facts.domain",
        frozenset({
            "auto_renew",
            "cogitator",
            "name",
            "order_id",
            "registrar",
            "spend",
            "whois_privacy",
        }),
    )
    domain_name = _domain(domain["name"], "verified_facts.domain.name")
    if domain["registrar"] != "porkbun":
        _fail("invalid_receipt_registrar", "verified_facts.domain.registrar")
    if domain["auto_renew"] is not True or domain["whois_privacy"] is not True:
        _fail("domain_safety_attestation_required", "verified_facts.domain")
    spend = _object(
        domain["spend"],
        "verified_facts.domain.spend",
        frozenset({"amount_usd_cents", "display"}),
    )
    cents = spend["amount_usd_cents"]
    if (
        isinstance(cents, bool)
        or not isinstance(cents, int)
        or not 0 <= cents <= 1_000_000
    ):
        _fail("invalid_receipt_amount", "verified_facts.domain.spend.amount_usd_cents")
    spend_display = _text(spend["display"], "verified_facts.domain.spend.display")
    cogitator = _object(
        domain["cogitator"],
        "verified_facts.domain.cogitator",
        frozenset({"approval_id", "proposal_id", "receipt_ref"}),
    )

    shopify = _object(
        facts["shopify"],
        "verified_facts.shopify",
        frozenset({"admin_url", "myshopify_domain", "plan", "shop_id"}),
    )
    myshopify_domain = _domain(
        shopify["myshopify_domain"], "verified_facts.shopify.myshopify_domain"
    )
    if not myshopify_domain.endswith(".myshopify.com"):
        _fail("invalid_myshopify_domain", "verified_facts.shopify.myshopify_domain")
    admin_url, admin = _https_url(
        shopify["admin_url"], "verified_facts.shopify.admin_url"
    )
    valid_admin = (
        admin.hostname == "admin.shopify.com" and admin.path.startswith("/store/")
    ) or (admin.hostname == myshopify_domain and admin.path.startswith("/admin"))
    if not valid_admin:
        _fail("invalid_shopify_admin_url", "verified_facts.shopify.admin_url")

    public_url, public = _https_url(facts["public_url"], "verified_facts.public_url")
    if public.hostname != domain_name or public.path not in {"", "/"}:
        _fail("invalid_public_url", "verified_facts.public_url")

    dns = _object(
        facts["dns"],
        "verified_facts.dns",
        frozenset({"records", "status"}),
    )
    if dns["status"] != "propagated":
        _fail("dns_not_propagated", "verified_facts.dns.status")
    records = dns["records"]
    if (
        not isinstance(records, list)
        or len(records) != len(_DNS_RECORDS)
        or any(not isinstance(record, str) for record in records)
        or set(records) != set(_DNS_RECORDS)
    ):
        _fail("invalid_dns_attestation", "verified_facts.dns.records")

    waitlist = _object(
        facts["waitlist_test"],
        "verified_facts.waitlist_test",
        frozenset({
            "consent_recorded",
            "result",
            "test_address_used",
            "test_subscriber_deleted",
        }),
    )
    test_address = _text(
        waitlist["test_address_used"],
        "verified_facts.waitlist_test.test_address_used",
    )
    if _TEST_ADDRESS_RE.fullmatch(test_address) is None:
        _fail(
            "test_address_must_be_hashed",
            "verified_facts.waitlist_test.test_address_used",
        )
    if (
        waitlist["result"] != "pass"
        or waitlist["consent_recorded"] is not True
        or waitlist["test_subscriber_deleted"] is not True
    ):
        _fail("waitlist_verification_required", "verified_facts.waitlist_test")

    verification = _object(
        facts["verification"],
        "verified_facts.verification",
        frozenset({"all_green", "checklist", "evidence_bundle"}),
    )
    if verification["checklist"] != "9.3":
        _fail("invalid_verification_checklist", "verified_facts.verification.checklist")
    if verification["all_green"] is not True:
        _fail("verification_not_green", "verified_facts.verification.all_green")
    if facts["no_payment_collected"] is not True:
        _fail("payment_absence_not_verified", "verified_facts.no_payment_collected")
    if facts["checkout_absent_verified"] is not True:
        _fail(
            "checkout_absence_not_verified", "verified_facts.checkout_absent_verified"
        )

    unresolved = facts["unresolved"]
    if not isinstance(unresolved, list):
        _fail("invalid_receipt_list", "verified_facts.unresolved")
    unresolved_items = sorted({
        _text(item, "verified_facts.unresolved[]", max_chars=500) for item in unresolved
    })

    total_spend = facts["total_spend"]
    if not isinstance(total_spend, list):
        _fail("invalid_receipt_list", "verified_facts.total_spend")
    spend_items: list[dict[str, str]] = []
    for index, raw_item in enumerate(total_spend):
        item = _object(
            raw_item,
            f"verified_facts.total_spend[{index}]",
            frozenset({"amount", "provider"}),
        )
        spend_items.append({
            "provider": _text(
                item["provider"],
                f"verified_facts.total_spend[{index}].provider",
                max_chars=120,
            ).lower(),
            "amount": _text(
                item["amount"], f"verified_facts.total_spend[{index}].amount"
            ),
        })
    providers = [item["provider"] for item in spend_items]
    if sorted(providers) != ["porkbun", "shopify"] or len(set(providers)) != len(
        providers
    ):
        _fail("invalid_total_spend", "verified_facts.total_spend")
    spend_items.sort(key=lambda item: item["provider"])
    if (
        next(item for item in spend_items if item["provider"] == "porkbun")["amount"]
        != spend_display
    ):
        _fail("spend_total_mismatch", "verified_facts.total_spend")

    result = {
        "domain": {
            "name": domain_name,
            "registrar": "porkbun",
            "order_id": _identifier(
                domain["order_id"], "verified_facts.domain.order_id"
            ),
            "spend": {"amount_usd_cents": cents, "display": spend_display},
            "cogitator": {
                "proposal_id": _identifier(
                    cogitator["proposal_id"],
                    "verified_facts.domain.cogitator.proposal_id",
                ),
                "approval_id": _identifier(
                    cogitator["approval_id"],
                    "verified_facts.domain.cogitator.approval_id",
                ),
                "receipt_ref": _identifier(
                    cogitator["receipt_ref"],
                    "verified_facts.domain.cogitator.receipt_ref",
                ),
            },
            "auto_renew": True,
            "whois_privacy": True,
        },
        "shopify": {
            "myshopify_domain": myshopify_domain,
            "shop_id": _identifier(
                shopify["shop_id"], "verified_facts.shopify.shop_id"
            ),
            "plan": _text(
                shopify["plan"], "verified_facts.shopify.plan", max_chars=120
            ),
            "admin_url": admin_url,
        },
        "public_url": public_url,
        "dns": {"status": "propagated", "records": list(_DNS_RECORDS)},
        "waitlist_test": {
            "result": "pass",
            "test_address_used": test_address,
            "consent_recorded": True,
            "test_subscriber_deleted": True,
        },
        "verification": {
            "checklist": "9.3",
            "all_green": True,
            "evidence_bundle": _evidence_ref(
                verification["evidence_bundle"],
                "verified_facts.verification.evidence_bundle",
                job_id,
                bundle=True,
            ),
        },
        "unresolved": unresolved_items,
        "no_payment_collected": True,
        "checkout_absent_verified": True,
        "total_spend": spend_items,
    }
    reject_forbidden_data(result, "verified_facts")
    return result


def build_execution_receipt(
    store: Any,
    job_id: str,
    *,
    verified_facts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the §16 receipt from one consistent ledger snapshot."""
    if not isinstance(job_id, str) or _JOB_ID_RE.fullmatch(job_id) is None:
        _fail("invalid_receipt_job_id", "job_id")
    snapshot = store.delivery_snapshot(job_id)
    envelope = _object(
        snapshot,
        "snapshot",
        frozenset({"actions", "events", "gates", "job"}),
    )
    job = envelope["job"]
    if not isinstance(job, Mapping):
        _fail("invalid_receipt_object", "snapshot.job")
    if job.get("job_id") != job_id:
        _fail("receipt_job_mismatch", "snapshot.job.job_id")
    if job.get("current_state") not in {"executing_read_only", "verifying", "completed"}:
        _fail("receipt_job_not_verified", "snapshot.job.current_state")
    if job.get("state_machine_version") != STATE_MACHINE_VERSION:
        _fail("receipt_state_machine_mismatch", "snapshot.job.state_machine_version")

    actions = envelope["actions"]
    if not isinstance(actions, list) or not actions:
        _fail("receipt_actions_missing", "snapshot.actions")
    completed_actions: list[dict[str, str]] = []
    completed_times: list[tuple[datetime, str]] = []
    ledger_facts: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        if not isinstance(action, Mapping):
            _fail("invalid_receipt_object", f"snapshot.actions[{index}]")
        status = action.get("action_status")
        if (
            status in {"dispatched", "planned", "recoverable", "uncertain"}
            or action.get("uncertainty") is True
        ):
            _fail("receipt_action_unresolved", f"snapshot.actions[{index}]")
        if action.get("effect_class") == "consequential" and status != "succeeded":
            _fail("receipt_consequential_action_failed", f"snapshot.actions[{index}]")
        if status != "succeeded":
            continue
        terminal_at, parsed = _timestamp(
            action.get("terminal_at"), f"snapshot.actions[{index}].terminal_at"
        )
        result = action.get("result")
        if not isinstance(result, Mapping):
            _fail("invalid_receipt_object", f"snapshot.actions[{index}].result")
        evidence = result.get("evidence_ref", result.get("evidence"))
        completed_actions.append({
            "step": _text(
                action.get("action_type"),
                f"snapshot.actions[{index}].action_type",
                max_chars=120,
            ),
            "at": terminal_at,
            "evidence": _evidence_ref(
                evidence, f"snapshot.actions[{index}].result.evidence_ref", job_id
            ),
        })
        completed_times.append((parsed, terminal_at))
        if "receipt_facts" in result:
            if result.get("provider_truth_verified") is not True:
                _fail(
                    "receipt_provider_truth_required",
                    f"snapshot.actions[{index}].result",
                )
            ledger_facts.append(_verified_facts(result["receipt_facts"], job_id))

    if not completed_actions:
        _fail("receipt_actions_missing", "snapshot.actions")
    supplied = (
        _verified_facts(verified_facts, job_id) if verified_facts is not None else None
    )
    if ledger_facts:
        reference = ledger_facts[-1]
        if any(
            canonical_json(item) != canonical_json(reference)
            for item in ledger_facts[:-1]
        ):
            _fail("conflicting_receipt_facts", "snapshot.actions")
        if supplied is not None and canonical_json(supplied) != canonical_json(
            reference
        ):
            _fail("receipt_facts_mismatch", "verified_facts")
        facts = reference
    elif supplied is not None:
        facts = supplied
    else:
        _fail("verified_receipt_facts_missing", "verified_facts")

    gates = envelope["gates"]
    if not isinstance(gates, list):
        _fail("invalid_receipt_list", "snapshot.gates")
    human_gates: list[dict[str, str]] = []
    for index, gate in enumerate(gates):
        if not isinstance(gate, Mapping):
            _fail("invalid_receipt_object", f"snapshot.gates[{index}]")
        if gate.get("status") == "open":
            _fail("receipt_gate_unresolved", f"snapshot.gates[{index}]")
        if not gate.get("completed_at"):
            continue
        opened, _ = _timestamp(
            gate.get("opened_at"), f"snapshot.gates[{index}].opened_at"
        )
        verified, _ = _timestamp(
            gate.get("completed_at"), f"snapshot.gates[{index}].completed_at"
        )
        human_gates.append({
            "gate_id": _identifier(
                gate.get("gate_id"), f"snapshot.gates[{index}].gate_id"
            ),
            "type": _text(
                gate.get("gate_type"),
                f"snapshot.gates[{index}].gate_type",
                max_chars=120,
            ),
            "opened": opened,
            "verified": verified,
        })

    receipt = {
        "job_id": job_id,
        "state_machine_version": STATE_MACHINE_VERSION,
        "completed_at": max(completed_times, key=lambda item: item[0])[1],
        "objective": _text(
            job.get("original_objective"),
            "snapshot.job.original_objective",
            max_chars=4_000,
        ),
        "actions_completed": completed_actions,
        **facts,
        "human_gates_completed": human_gates,
    }
    reject_forbidden_data(receipt, "receipt")
    return receipt


def persist_execution_receipt(
    store: Any,
    job_id: str,
    *,
    verified_facts: Mapping[str, Any] | None = None,
    receipts_root: str | Path | None = None,
) -> Path:
    """Atomically write a byte-stable 0600 receipt and return its path."""
    receipt = build_execution_receipt(store, job_id, verified_facts=verified_facts)
    root = Path(receipts_root) if receipts_root is not None else default_receipts_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        _fail("unsafe_receipts_root", "receipts_root")
    path = root / f"{job_id}.json"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        _fail("unsafe_receipt_path", "receipt_path")
    atomic_json_write(
        path,
        receipt,
        indent=2,
        mode=0o600,
        sort_keys=True,
        allow_nan=False,
    )
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        _fail("unsafe_receipt_permissions", "receipt_path")
    return path

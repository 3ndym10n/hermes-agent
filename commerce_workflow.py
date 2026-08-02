"""Deterministic Porkbun-to-Shopify workflow for the V1 waitlist launch."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from commerce_content import build_content, content_fingerprint
from commerce_jobs import canonical_json, reject_forbidden_data
from hermes_constants import get_hermes_home
from registrar_porkbun import (
    PorkbunAPIError,
    PorkbunClient,
    PorkbunConfigurationError,
    PorkbunIdempotencyError,
    PorkbunMutationUncertainError,
)
from shopify_admin import ShopifyAdminClient
from utils import atomic_json_write

WORKFLOW = "virgil_ecommerce_v1"
CANDIDATE_DOMAINS = (
    "siliconcurrent.com",
    "siliconcurrent.net",
    "siliconcurrentgpu.com",
    "siliconcurrentau.com",
    "siliconcurrenttech.com",
    "siliconcurrentstore.com",
    "siliconcurrentshop.com",
    "siliconcurrenthardware.com",
    "getsiliconcurrent.com",
    "joinsiliconcurrent.com",
)
SHOPIFY_DNS_BUNDLE = (
    {"type": "A", "name": "", "content": "23.227.38.65"},
    {"type": "AAAA", "name": "", "content": "2620:0127:f00f:5::"},
    {"type": "CNAME", "name": "www", "content": "shops.myshopify.com."},
)
REQUIRED_FACTS = frozenset({
    "contact_email",
    "business_identity_sentence",
    "double_opt_in",
    "brand_signoff",
    "privacy_signoff",
})

_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,119}\Z")
_SAFE_LABEL = re.compile(r"[a-z][a-z0-9_-]{0,79}\Z")
_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_PROTECTED_DNS_TYPES = frozenset({
    "CAA",
    "MX",
    "NS",
    "SRV",
    "SSHFP",
    "TLSA",
    "TXT",
})

Verify = Callable[[Mapping[str, Any], Any, Mapping[str, Any], str], Mapping[str, Any]]
Publish = Callable[[Mapping[str, Any], Any], Mapping[str, Any]]


def _error(
    code: str,
    *,
    uncertain: bool = False,
    evidence: Mapping[str, Any] | None = None,
) -> Exception:
    # Imported lazily so commerce_operator can import this module without a cycle.
    from commerce_operator import ProviderStepError

    return ProviderStepError(code, uncertain=uncertain, evidence=evidence)


def _step(
    step_id: str,
    action_type: str,
    provider: str,
    effect_class: str,
    request: Mapping[str, Any],
    *,
    key: str,
    next_state: str = "ready",
    approval_kind: str = "purchase",
    approval_reference: str = "",
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "action_type": action_type,
        "provider": provider,
        "effect_class": effect_class,
        "idempotency_key": key,
        "request": dict(request),
        "next_state": next_state,
        "approval_kind": approval_kind,
        "approval_reference": approval_reference,
    }


def production_plan(
    job: Mapping[str, Any], facts: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Return the initial one-read plan; discovery expands it with the live quote."""
    missing = sorted(REQUIRED_FACTS - facts.keys())
    if missing:
        return {"missing_facts": missing}
    package = build_content(facts)
    content_sha256 = content_fingerprint(package)
    prior = job.get("plan")
    if isinstance(prior, Mapping) and prior.get("workflow") == WORKFLOW:
        if prior.get("content_sha256") != content_sha256:
            raise ValueError("approved launch facts changed after workflow planning")
        if prior.get("domain"):
            return dict(prior)
    job_id = str(job.get("job_id", ""))
    if not _JOB_ID.fullmatch(job_id):
        raise ValueError("invalid commerce job id")
    generation = int(job.get("row_version", 0))
    return {
        "workflow": WORKFLOW,
        "content_sha256": content_sha256,
        "provider_account": "porkbun-production",
        "steps": [
            _step(
                f"s01_porkbun_discovery_{generation}",
                "porkbun_discover",
                "porkbun",
                "read_only",
                {"candidates": list(CANDIDATE_DOMAINS)},
                key=f"{job_id}_discover_{generation}",
            )
        ],
    }


def _cents(value: object) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _error("porkbun_quote_invalid") from None
    cents = amount * 100
    if (
        not amount.is_finite()
        or cents != cents.to_integral_value()
        or not 0 < cents <= 1_000_000
    ):
        raise _error("porkbun_quote_invalid")
    return int(cents)


def _iso_z(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("clock must return an aware datetime")
    return (
        now
        .astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _action_input(action: Mapping[str, Any]) -> dict[str, Any]:
    request = action.get("request")
    if not isinstance(request, Mapping):
        raise _error("workflow_action_invalid")
    value = request.get("input", request)
    if not isinstance(value, Mapping):
        raise _error("workflow_action_invalid")
    return dict(value)


def _action_key(action: Mapping[str, Any]) -> str:
    request = action.get("request")
    value = (
        request.get("provider_idempotency_key")
        if isinstance(request, Mapping)
        else None
    )
    if not isinstance(value, str) or not value:
        raise _error("workflow_action_invalid")
    return value


def _human_gate(
    gate_type: str,
    human_action: str,
    provider_truth_reference: str,
    entry_url: str,
    **evidence: Any,
) -> dict[str, Any]:
    return {
        "gate_type": gate_type,
        "human_action": human_action,
        "provider_truth_reference": provider_truth_reference,
        "opening_evidence": {"entry_url": entry_url, **evidence},
    }


def _write_evidence(
    root: Path, job_id: str, label: str, payload: Mapping[str, Any]
) -> str:
    if not _JOB_ID.fullmatch(job_id) or not _SAFE_LABEL.fullmatch(label):
        raise _error("evidence_reference_invalid")
    reject_forbidden_data(payload, "commerce_evidence")
    encoded = canonical_json(payload)
    if _EMAIL.search(encoded):
        raise _error("evidence_pii_forbidden")
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise _error("evidence_root_unsafe")
    os.chmod(root, 0o700)
    job_root = root / job_id
    job_root.mkdir(mode=0o700, exist_ok=True)
    if job_root.is_symlink() or not job_root.is_dir():
        raise _error("evidence_root_unsafe")
    os.chmod(job_root, 0o700)
    filename = f"{label}-{digest[:16]}.json"
    target = job_root / filename
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise _error("evidence_path_unsafe")
    atomic_json_write(target, payload, mode=0o600, sort_keys=True, allow_nan=False)
    return f"evidence/{job_id}/{filename}"


def _replace_step(
    plan: Mapping[str, Any], step_id: str, **updates: Any
) -> dict[str, Any]:
    replacement = dict(plan)
    steps = []
    found = False
    for raw in plan.get("steps", []):
        step = dict(raw)
        if step.get("step_id") == step_id:
            step.update(updates)
            found = True
        steps.append(step)
    if not found:
        raise _error("workflow_plan_invalid")
    replacement["steps"] = steps
    return replacement


def _expanded_plan(
    job: Mapping[str, Any],
    *,
    table: list[dict[str, Any]],
    domain: str,
    registration_cents: int,
    renewal_cents: int,
    quote_timestamp: str,
    renewal_date: str,
    cancellation_deadline: str,
    discovery_evidence: str,
) -> dict[str, Any]:
    current = job.get("plan")
    if not isinstance(current, Mapping):
        raise _error("workflow_plan_invalid")
    job_id = str(job["job_id"])
    registration = {
        "domain": domain,
        "cost_usd_cents": registration_cents,
        "currency": "USD",
        "quote_timestamp": quote_timestamp,
        "renewal_usd_cents": renewal_cents,
        "renewal_date": renewal_date,
        "cancellation_deadline": cancellation_deadline,
        "dns_bundle": [dict(record) for record in SHOPIFY_DNS_BUNDLE],
        "auto_renew": True,
        "whois_privacy": True,
    }
    discovery_step = dict(current["steps"][0])
    steps = [
        discovery_step,
        _step(
            "s02_porkbun_register",
            "porkbun_register_domain",
            "porkbun",
            "consequential",
            registration,
            key=f"{job_id}_register_{domain}",
            approval_kind="purchase",
            approval_reference="cogitator.purchase",
        ),
        _step(
            "s03_porkbun_dns_snapshot",
            "porkbun_dns_snapshot",
            "porkbun",
            "read_only",
            {"domain": domain},
            key=f"{job_id}_dns_snapshot",
        ),
        _step(
            "s04_porkbun_dns_apply",
            "porkbun_dns_apply",
            "porkbun",
            "idempotent_write",
            {
                "domain": domain,
                "dns_bundle": [dict(record) for record in SHOPIFY_DNS_BUNDLE],
            },
            key=f"{job_id}_dns_apply",
        ),
        _step(
            "s05_shopify_credentials",
            "shopify_credentials",
            "shopify",
            "read_only",
            {},
            key=f"{job_id}_shopify_credentials",
        ),
        _step(
            "s06_shopify_identity",
            "shopify_identity",
            "shopify",
            "read_only",
            {},
            key=f"{job_id}_shopify_identity",
        ),
        _step(
            "s07_shopify_build",
            "shopify_build",
            "shopify",
            "idempotent_write",
            {"content_sha256": current["content_sha256"]},
            key=f"{job_id}_shopify_build",
        ),
        _step(
            "s08_shopify_theme_verify",
            "shopify_theme_verify",
            "shopify",
            "read_only",
            {},
            key=f"{job_id}_shopify_theme_verify",
        ),
        _step(
            "s09_shopify_plan_gate",
            "shopify_plan_gate",
            "shopify",
            "read_only",
            {"required_plan": "paid"},
            key=f"{job_id}_shopify_plan_gate",
        ),
        _step(
            "s10_shopify_plan_verify",
            "shopify_plan_verify",
            "shopify",
            "read_only",
            {"required_plan": "paid"},
            key=f"{job_id}_shopify_plan_verify",
        ),
        _step(
            "s11_shopify_domain_gate",
            "shopify_domain_gate",
            "shopify",
            "read_only",
            {"domain": domain},
            key=f"{job_id}_shopify_domain_gate",
        ),
        _step(
            "s12_shopify_domain_verify",
            "shopify_domain_verify",
            "shopify",
            "read_only",
            {"domain": domain},
            key=f"{job_id}_shopify_domain_verify",
        ),
        _step(
            "s13_prepublish_verify",
            "commerce_prepublish_verify",
            "commerce",
            "read_only",
            {"domain": domain, "content_sha256": current["content_sha256"]},
            key=f"{job_id}_prepublish_verify",
            next_state="verifying",
        ),
        _step(
            "s14_shopify_publish",
            "shopify_publish",
            "shopify",
            "consequential",
            {
                "remove_storefront_lock": True,
                "checkout_disabled": True,
                "verification_sha256": "pending",
            },
            key=f"{job_id}_shopify_publish",
            approval_kind="publication",
            approval_reference="commerce.publication",
        ),
        _step(
            "s15_final_verify",
            "commerce_final_verify",
            "commerce",
            "read_only",
            {"domain": domain, "content_sha256": current["content_sha256"]},
            key=f"{job_id}_final_verify",
            next_state="verifying",
        ),
    ]
    return {
        **dict(current),
        "domain": domain,
        "tld": domain.partition(".")[2],
        "registrar": "porkbun",
        "prices": {
            "registration_usd_cents": registration_cents,
            "renewal_usd_cents": renewal_cents,
        },
        "currencies": ["USD", "AUD"],
        "term_length": "1_year",
        "auto_renew": True,
        "whois_privacy": True,
        "subscription_recurrence": "annual",
        "shopify_plan_tier": "unselected",
        "product_price": None,
        "availability": table,
        "recommendation": domain,
        "discovery_evidence": discovery_evidence,
        "steps": steps,
    }


def _relative_name(value: object, domain: str) -> str:
    if not isinstance(value, str):
        raise _error("dns_snapshot_invalid")
    name = value.rstrip(".").lower()
    if name in {"", "@", domain}:
        return ""
    suffix = "." + domain
    return name[: -len(suffix)] if name.endswith(suffix) else name


def _dns_content(record_type: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _error("dns_snapshot_invalid")
    if record_type in {"A", "AAAA"}:
        try:
            return ipaddress.ip_address(value).compressed
        except ValueError:
            raise _error("dns_snapshot_invalid") from None
    if record_type == "CNAME":
        return value.rstrip(".").lower() + "."
    return value


def _dns_state(raw: object, domain: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("records"), list):
        raise _error("dns_snapshot_invalid")
    records = []
    for value in raw["records"]:
        if not isinstance(value, Mapping):
            raise _error("dns_snapshot_invalid")
        record_type = str(value.get("type", "")).upper()
        record_id = str(value.get("id", ""))
        if not record_type or not record_id.isdigit():
            raise _error("dns_snapshot_invalid")
        records.append({
            "id": record_id,
            "type": record_type,
            "name": _relative_name(value.get("name"), domain),
            "content": _dns_content(record_type, value.get("content")),
        })
    records.sort(
        key=lambda item: (item["name"], item["type"], item["content"], item["id"])
    )
    desired = [
        {
            "type": record["type"],
            "name": record["name"],
            "content": _dns_content(record["type"], record["content"]),
        }
        for record in SHOPIFY_DNS_BUNDLE
    ]
    conflicts: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for target in desired:
        same_name = [record for record in records if record["name"] == target["name"]]
        exact = [
            record
            for record in same_name
            if record["type"] == target["type"]
            and record["content"] == target["content"]
        ]
        if not exact:
            missing.append(target)
        conflicts.extend(exact[1:])
        for record in same_name:
            if record in exact:
                continue
            incompatible = (
                record["type"] == target["type"]
                or record["type"] == "CNAME"
                or target["type"] == "CNAME"
            )
            if incompatible and record not in conflicts:
                conflicts.append(record)
    conflicts.sort(key=lambda item: int(item["id"]))
    diff = {"domain": domain, "delete": conflicts, "create": missing}
    return {
        "records": records,
        "missing": missing,
        "conflicts": conflicts,
        "snapshot_sha256": hashlib.sha256(canonical_json(records).encode()).hexdigest(),
        "diff_hash": hashlib.sha256(canonical_json(diff).encode()).hexdigest(),
        "protected_conflict": any(
            record["type"] in _PROTECTED_DNS_TYPES for record in conflicts
        ),
    }


def _safe_report(
    raw: Mapping[str, Any],
) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    if not isinstance(raw, Mapping):
        raise _error("verification_result_invalid")
    report = dict(raw)
    receipt_facts = report.pop("receipt_facts", None)
    if report.get("all_green") is not True:
        raise _error("verification_not_green")
    if (
        report.get("checkout_absent_verified") is not True
        or report.get("no_payment_collected") is not True
    ):
        raise _error("verification_safety_check_failed")
    reject_forbidden_data(report, "verification_report")
    if _EMAIL.search(canonical_json(report)):
        raise _error("verification_pii_forbidden")
    if receipt_facts is not None:
        if not isinstance(receipt_facts, Mapping):
            raise _error("verification_receipt_invalid")
        reject_forbidden_data(receipt_facts, "receipt_facts")
        if _EMAIL.search(canonical_json(receipt_facts)):
            raise _error("verification_pii_forbidden")
    return report, receipt_facts


def production_handlers(
    store: Any,
    *,
    porkbun_factory: Callable[[], Any] = PorkbunClient,
    shopify_factory: Callable[[], Any] | None = None,
    facts_loader: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    verify: Verify | None = None,
    publish: Publish | None = None,
    theme_verify: Callable[[Mapping[str, Any], Any], bool] | None = None,
    record_purchase: Callable[..., Mapping[str, Any]] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    check_interval: float = 10.0,
    evidence_root: str | Path | None = None,
) -> dict[str, Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]]:
    """Build operator handlers; real Shopify auth is client-credentials env only."""
    root = (
        Path(evidence_root)
        if evidence_root is not None
        else get_hermes_home() / "commerce" / "evidence"
    )

    def evidence(job: Mapping[str, Any], label: str, payload: Mapping[str, Any]) -> str:
        return _write_evidence(root, str(job["job_id"]), label, payload)

    def porkbun() -> Any:
        return porkbun_factory()

    def shopify() -> Any:
        if shopify_factory is not None:
            return shopify_factory()
        values = tuple(
            os.environ.get(name, "").strip()
            for name in (
                "SHOPIFY_SHOP",
                "SHOPIFY_CLIENT_ID",
                "SHOPIFY_CLIENT_SECRET",
            )
        )
        if not all(values):
            raise _error("shopify_credentials_missing")
        return ShopifyAdminClient.from_client_credentials(*values)

    def facts(job: Mapping[str, Any]) -> Mapping[str, Any]:
        raw = (
            facts_loader(job)
            if facts_loader is not None
            else store.latest_facts(str(job["job_id"]))
        )
        if not isinstance(raw, Mapping):
            raise _error("launch_facts_unavailable")
        return raw

    def checked_shopify(job: Mapping[str, Any]) -> tuple[Any, Mapping[str, Any]]:
        client = shopify()
        identity = client.shop_identity()
        expected = job.get("plan", {}).get("shopify")
        if isinstance(expected, Mapping) and (
            identity.get("id") != expected.get("shop_id")
            or identity.get("myshopify_domain") != expected.get("myshopify_domain")
        ):
            raise _error("shopify_identity_mismatch")
        return client, identity

    def discover(
        job: Mapping[str, Any], action: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        request = _action_input(action)
        if request != {"candidates": list(CANDIDATE_DOMAINS)}:
            raise _error("discovery_candidates_mismatch")
        try:
            client = porkbun()
            ping = client.ping()
            requirements = {
                tld: client.get_registration_requirements(tld)
                for tld in ("com", "net", "com.au")
            }
            pricing = client.get_default_pricing()
            account_domains = client.list_domains()
            table = []
            for index, domain in enumerate(CANDIDATE_DOMAINS):
                checked = client.check_domain(domain)["response"]
                tld = domain.partition(".")[2]
                renewal = pricing["pricing"][tld]["renewal"]
                table.append({
                    "domain": domain,
                    "available": checked["avail"] == "yes",
                    "registration_usd_cents": _cents(checked["price"]),
                    "renewal_usd_cents": _cents(renewal),
                })
                if index + 1 < len(CANDIDATE_DOMAINS):
                    sleep(check_interval)
            selected = next((row for row in table if row["available"]), None)
            if selected is None:
                raise _error("no_candidate_domain_available")
            dry_run = client.create_domain(
                selected["domain"],
                cost=selected["registration_usd_cents"],
                dry_run=True,
            )
        except PorkbunMutationUncertainError:
            raise _error("porkbun_dry_run_invalid") from None
        except Exception as exc:
            from commerce_operator import ProviderStepError

            if isinstance(exc, ProviderStepError):
                raise
            code = (
                "porkbun_credentials_missing"
                if isinstance(exc, PorkbunConfigurationError)
                else "porkbun_discovery_failed"
            )
            if code == "porkbun_credentials_missing":
                ref = evidence(job, "porkbun_credentials_gate", {"configured": False})
                return {
                    "evidence_ref": ref,
                    "_human_gate": _human_gate(
                        "porkbun_credentials",
                        "Create or restrict the Porkbun API key, then stage it in the mode-0600 credentials file.",
                        "porkbun.ping",
                        "https://porkbun.com/account/api",
                    ),
                }
            raise _error(code) from None
        if (
            dry_run.get("domain") != selected["domain"]
            or dry_run.get("cost") != selected["registration_usd_cents"]
        ):
            raise _error("porkbun_dry_run_mismatch")
        now = clock()
        renewal = now.date() + timedelta(days=365)
        payload = {
            "credentials_valid": ping.get("credentialsValid") is True,
            "prior_domain_count": len(account_domains.get("domains", [])),
            "requirements": {
                tld: {"api_registerable": value.get("apiRegisterable") is True}
                for tld, value in requirements.items()
            },
            "availability": table,
            "dry_run": {
                "domain": selected["domain"],
                "cost_usd_cents": selected["registration_usd_cents"],
                "would_succeed": dry_run.get("wouldSucceed") is True,
                "sufficient_funds": dry_run.get("sufficientFunds") is True,
            },
        }
        ref = evidence(job, "porkbun_discovery", payload)
        plan = _expanded_plan(
            job,
            table=table,
            domain=selected["domain"],
            registration_cents=selected["registration_usd_cents"],
            renewal_cents=selected["renewal_usd_cents"],
            quote_timestamp=_iso_z(now),
            renewal_date=renewal.isoformat(),
            cancellation_deadline=(renewal - timedelta(days=30)).isoformat(),
            discovery_evidence=ref,
        )
        result: dict[str, Any] = {
            "evidence_ref": ref,
            "candidate_count": len(table),
            "recommended_domain": selected["domain"],
            "_replace_plan": plan,
        }
        if (
            dry_run.get("wouldSucceed") is not True
            or dry_run.get("sufficientFunds") is not True
        ):
            result["_human_gate"] = _human_gate(
                "porkbun_credit",
                "Top up the Porkbun account credit for the exact quoted registration, then tap DONE.",
                "porkbun.domain_create_dry_run",
                "https://porkbun.com/account/billing",
            )
        return result

    def purchase_approval(job: Mapping[str, Any], action: Mapping[str, Any]) -> dict:
        """Return the approval refs Cal's completed purchase gate recorded."""
        fingerprint = action.get("action_fingerprint")
        try:
            gates = store.list_gates(str(job["job_id"]))
        except Exception:
            raise _error("registration_approval_unreadable") from None
        for gate in gates:
            if (
                gate.get("gate_type") == "action_approval"
                and gate.get("approval_fingerprint") == fingerprint
                and gate.get("status") in {"completed", "consumed"}
            ):
                evidence = gate.get("completion_evidence")
                if isinstance(evidence, Mapping) and evidence.get("approval_granted"):
                    return dict(evidence)
        raise _error("registration_approval_unreadable")

    def register(
        job: Mapping[str, Any], action: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        request = _action_input(action)
        plan = job.get("plan", {})
        expected = next(
            step["request"]
            for step in plan.get("steps", [])
            if step.get("step_id") == "s02_porkbun_register"
        )
        if request != expected or action.get("approval_status") != "live":
            raise _error("registration_approval_mismatch")
        client = porkbun()
        try:
            checked = client.check_domain(request["domain"])["response"]
            if (
                checked.get("avail") != "yes"
                or _cents(checked.get("price")) != request["cost_usd_cents"]
            ):
                raise _error("registration_quote_changed")
            dry_run = client.create_domain(
                request["domain"], cost=request["cost_usd_cents"], dry_run=True
            )
            if (
                dry_run.get("wouldSucceed") is not True
                or dry_run.get("cost") != request["cost_usd_cents"]
            ):
                raise _error("registration_preflight_failed")
        except Exception as exc:
            from commerce_operator import ProviderStepError

            if isinstance(exc, ProviderStepError):
                raise
            raise _error("registration_preflight_failed") from None
        try:
            created = client.create_domain(
                request["domain"],
                cost=request["cost_usd_cents"],
                dry_run=False,
                idempotency_key=_action_key(action),
            )
        except PorkbunMutationUncertainError:
            raise _error("registration_outcome_unknown", uncertain=True) from None
        except PorkbunIdempotencyError as exc:
            raise _error(
                "registration_outcome_unknown"
                if exc.outcome_uncertain
                else "registration_idempotency_rejected",
                uncertain=exc.outcome_uncertain,
            ) from None
        except PorkbunAPIError:
            raise _error("registration_rejected") from None
        except Exception:
            raise _error("registration_outcome_unknown", uncertain=True) from None
        if (
            created.get("domain") != request["domain"]
            or created.get("cost") != request["cost_usd_cents"]
        ):
            raise _error("registration_outcome_unknown", uncertain=True)
        order_id = str(created.get("orderId", ""))
        if not order_id.isdigit():
            raise _error("registration_outcome_unknown", uncertain=True)
        payload = {
            "domain": request["domain"],
            "order_id": order_id,
            "amount_usd_cents": request["cost_usd_cents"],
        }
        if record_purchase is not None:
            # The registrar has already charged and created the domain, so this
            # is bookkeeping. If it cannot be recorded the money authority and
            # the ledger disagree, which is exactly an uncertain outcome: park
            # for reconciliation rather than report a clean success. The safe
            # registration facts ride along on the error so reconciliation can
            # retry the completion report instead of losing the order.
            approval = purchase_approval(job, action)
            try:
                recorded = record_purchase(
                    job=job,
                    action=action,
                    approval=approval,
                    order_id=order_id,
                    amount_usd_cents=request["cost_usd_cents"],
                )
                payload["cogitator"] = {
                    "proposal_id": str(recorded["proposal_id"]),
                    "approval_id": str(recorded["approval_id"]),
                    "receipt_ref": str(recorded["receipt_ref"]),
                }
            except Exception:
                raise _error(
                    "registration_completion_unrecorded",
                    uncertain=True,
                    evidence={
                        "domain": request["domain"],
                        "order_id": order_id,
                        "amount_usd_cents": request["cost_usd_cents"],
                        "action_fingerprint": str(action.get("action_fingerprint", "")),
                        "approval_reference": str(
                            approval.get("approval_reference", "")
                        ),
                        "proposal_id": str(approval.get("proposal_id", "")),
                        "ticket_id": str(approval.get("ticket_id", "")),
                    },
                ) from None
        return {
            **payload,
            "provider_truth_verified": True,
            "evidence_ref": evidence(job, "porkbun_registration", payload),
        }

    def dns_snapshot(
        job: Mapping[str, Any], action: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        domain = _action_input(action).get("domain")
        if domain != job.get("plan", {}).get("domain"):
            raise _error("dns_domain_mismatch")
        state = _dns_state(porkbun().get_dns_records(domain), domain)
        ref = evidence(
            job, "porkbun_dns_snapshot", {"domain": domain, "records": state["records"]}
        )
        result: dict[str, Any] = {
            "evidence_ref": ref,
            "snapshot_sha256": state["snapshot_sha256"],
            "diff_hash": state["diff_hash"],
            "record_count": len(state["records"]),
            "conflict_count": len(state["conflicts"]),
            "protected_conflict": state["protected_conflict"],
        }
        if state["conflicts"]:
            plan = _replace_step(
                job["plan"],
                "s04_porkbun_dns_apply",
                effect_class="consequential",
                approval_kind="dns",
                approval_reference="commerce.dns",
                request={
                    "domain": domain,
                    "dns_bundle": [dict(record) for record in SHOPIFY_DNS_BUNDLE],
                    "diff_hash": state["diff_hash"],
                },
            )
            result["_replace_plan"] = plan
        return result

    def dns_apply(
        job: Mapping[str, Any], action: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        request = _action_input(action)
        domain = request.get("domain")
        if domain != job.get("plan", {}).get("domain") or request.get("dns_bundle") != [
            dict(record) for record in SHOPIFY_DNS_BUNDLE
        ]:
            raise _error("dns_request_mismatch")
        try:
            prior_actions = store.list_actions(str(job["job_id"]))
        except Exception:
            raise _error("dns_purchase_approval_missing") from None
        purchase_approved = False
        for prior in prior_actions:
            wrapper = prior.get("request")
            purchase_input = (
                wrapper.get("input") if isinstance(wrapper, Mapping) else None
            )
            if (
                prior.get("action_type") == "porkbun_register_domain"
                and prior.get("action_status") == "succeeded"
                and prior.get("approval_status") == "live"
                and isinstance(purchase_input, Mapping)
                and purchase_input.get("domain") == domain
                and purchase_input.get("dns_bundle")
                == [dict(record) for record in SHOPIFY_DNS_BUNDLE]
            ):
                purchase_approved = True
                break
        if not purchase_approved:
            raise _error("dns_purchase_approval_missing")
        client = porkbun()
        state = _dns_state(client.get_dns_records(domain), domain)
        if state["conflicts"]:
            if request.get("diff_hash") != state["diff_hash"]:
                raise _error("dns_snapshot_changed")
            if action.get("approval_status") != "live":
                raise _error("dns_approval_missing")
        mutated = 0
        key = _action_key(action)
        try:
            for record in state["conflicts"]:
                client.dns_delete(
                    domain, record["id"], idempotency_key=f"{key}:delete:{record['id']}"
                )
                mutated += 1
            for record in state["missing"]:
                client.dns_create(
                    domain,
                    record_type=record["type"],
                    name=record["name"],
                    content=record["content"],
                    idempotency_key=f"{key}:create:{record['type'].lower()}:{record['name'] or 'apex'}",
                )
                mutated += 1
        except PorkbunMutationUncertainError:
            raise _error("dns_outcome_unknown", uncertain=True) from None
        except PorkbunIdempotencyError as exc:
            uncertain = exc.outcome_uncertain or bool(mutated)
            raise _error(
                "dns_outcome_unknown" if uncertain else "dns_write_rejected",
                uncertain=uncertain,
            ) from None
        except PorkbunAPIError:
            raise _error(
                "dns_outcome_unknown" if mutated else "dns_write_rejected",
                uncertain=bool(mutated),
            ) from None
        except Exception:
            raise _error("dns_outcome_unknown", uncertain=True) from None
        try:
            after = _dns_state(client.get_dns_records(domain), domain)
        except Exception:
            raise _error("dns_outcome_unknown", uncertain=bool(mutated)) from None
        if after["missing"] or after["conflicts"]:
            raise _error("dns_outcome_unknown", uncertain=True)
        payload = {
            "domain": domain,
            "records": [
                f"{item['type']} {item['name'] or '@'}" for item in SHOPIFY_DNS_BUNDLE
            ],
            "writes": mutated,
        }
        return {
            **payload,
            "provider_truth_verified": True,
            "evidence_ref": evidence(job, "porkbun_dns_applied", payload),
        }

    def shopify_credentials(
        job: Mapping[str, Any], _action: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        configured = shopify_factory is not None or all(
            os.environ.get(name, "").strip()
            for name in (
                "SHOPIFY_SHOP",
                "SHOPIFY_CLIENT_ID",
                "SHOPIFY_CLIENT_SECRET",
            )
        )
        result: dict[str, Any] = {
            "configured": configured,
            "evidence_ref": evidence(
                job, "shopify_credentials", {"configured": configured}
            ),
        }
        if not configured:
            result["_human_gate"] = _human_gate(
                "shopify_store_token",
                "Create the Shopify store and custom app, then stage SHOPIFY_SHOP, SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET outside chat.",
                "shopify.shop_identity",
                "https://admin.shopify.com/",
                credential_flow="client_credentials",
            )
        return result

    def shopify_identity(
        job: Mapping[str, Any], _action: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        client = shopify()
        identity = client.shop_identity()
        safe = {
            "shop_id": identity["id"],
            "myshopify_domain": identity["myshopify_domain"],
            "currency": identity["currency"],
            "timezone": identity["timezone"],
            "plan": identity["plan"],
            "partner_development": identity["partner_development"],
        }
        store.upsert_provider_account(
            account_reference=f"shopify:{identity['myshopify_domain']}",
            provider="shopify",
            environment_tag="production",
            identity=safe,
            status="active",
            metadata={"api_version": "2026-07"},
        )
        plan = dict(job["plan"])
        plan["shopify"] = safe
        plan["shopify_plan_tier"] = identity["plan"].lower()
        result: dict[str, Any] = {
            "shopify_identity_sha256": hashlib.sha256(
                canonical_json(safe).encode()
            ).hexdigest(),
            "evidence_ref": evidence(job, "shopify_identity", safe),
            "_replace_plan": plan,
        }
        capabilities = (
            client.capabilities()
            if callable(getattr(client, "capabilities", None))
            else {}
        )
        if (
            identity["currency"] != "AUD"
            or capabilities.get("double_opt_in_setting", {}).get("supported")
            is not True
        ):
            result["_human_gate"] = _human_gate(
                "shopify_settings",
                "Set store currency to AUD, confirm the Australian timezone and apply the approved double-opt-in choice, then tap DONE.",
                "shopify.shop_identity_and_waitlist_consent",
                "https://admin.shopify.com/",
            )
        return result

    def shopify_build(
        job: Mapping[str, Any], action: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if _action_input(action).get("content_sha256") != job.get("plan", {}).get(
            "content_sha256"
        ):
            raise _error("shopify_content_mismatch")
        client, identity = checked_shopify(job)
        if identity.get("currency") != "AUD":
            raise _error("shopify_settings_not_verified")
        package = build_content(facts(job))
        if content_fingerprint(package) != job["plan"]["content_sha256"]:
            raise _error("shopify_content_mismatch")
        if not callable(getattr(client, "upsert_page", None)) or not callable(
            getattr(client, "upsert_menu", None)
        ):
            ref = evidence(job, "shopify_content_capability", {"api_supported": False})
            return {
                "evidence_ref": ref,
                "_human_gate": _human_gate(
                    "shopify_content",
                    "Create the approved priority-access and contact pages plus footer menu in Shopify Admin, then tap DONE.",
                    "shopify.pages_and_navigation",
                    "https://admin.shopify.com/",
                ),
            }
        pages = [
            client.upsert_page(handle=handle, **page)
            for handle, page in package["pages"].items()
        ]
        navigation = package["navigation"]
        menu = client.upsert_menu(**navigation)
        payload = {
            "content_sha256": job["plan"]["content_sha256"],
            "page_handles": sorted(page["handle"] for page in pages),
            "menu_handle": menu["handle"],
            "changed": sum(bool(item.get("changed")) for item in [*pages, menu]),
        }
        result: dict[str, Any] = {
            **payload,
            "evidence_ref": evidence(job, "shopify_build", payload),
        }
        capabilities = (
            client.capabilities()
            if callable(getattr(client, "capabilities", None))
            else {}
        )
        theme_supported = (
            capabilities.get("theme_file_write", {}).get("supported") is True
        )
        if not theme_supported:
            result["_human_gate"] = _human_gate(
                "shopify_theme",
                "In the Dawn editor, add the native email-signup section and make the priority-access page the landing content, then tap DONE.",
                "shopify.main_theme",
                "https://admin.shopify.com/",
            )
        return result

    def shopify_theme_verify(
        job: Mapping[str, Any], _action: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        client, _identity = checked_shopify(job)
        passed = (
            theme_verify(job, client)
            if theme_verify is not None
            else client.main_theme().get("name", "").casefold() == "dawn"
        )
        if passed is not True:
            raise _error("shopify_theme_not_verified")
        payload = {"main_theme": "dawn", "verified": True}
        return {**payload, "evidence_ref": evidence(job, "shopify_theme", payload)}

    @staticmethod
    def paid(identity: Mapping[str, Any]) -> bool:
        plan = str(identity.get("plan", "")).casefold()
        return (
            identity.get("partner_development") is False
            and bool(plan)
            and not any(word in plan for word in ("trial", "development", "partner"))
        )

    def shopify_plan_gate(
        job: Mapping[str, Any], _action: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        _client, identity = checked_shopify(job)
        is_paid = paid(identity)
        result: dict[str, Any] = {
            "paid_plan": is_paid,
            "evidence_ref": evidence(
                job, "shopify_plan", {"paid_plan": is_paid, "plan": identity["plan"]}
            ),
        }
        if not is_paid:
            result["_human_gate"] = _human_gate(
                "shopify_plan",
                "Choose the approved paid Shopify plan and complete Shopify billing, then tap DONE.",
                "shopify.shop_plan",
                "https://admin.shopify.com/",
            )
        return result

    def shopify_plan_verify(
        job: Mapping[str, Any], _action: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        _client, identity = checked_shopify(job)
        if not paid(identity):
            raise _error("shopify_paid_plan_not_verified")
        payload = {"paid_plan": True, "plan": identity["plan"]}
        return {
            **payload,
            "evidence_ref": evidence(job, "shopify_plan_verified", payload),
        }

    def shopify_domain_gate(
        job: Mapping[str, Any], action: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        domain = _action_input(action).get("domain")
        client, _identity = checked_shopify(job)
        status = client.domain_status(domain)
        complete = status.get("connected") is True and status.get("ssl_enabled") is True
        result: dict[str, Any] = {
            "connected": complete,
            "evidence_ref": evidence(
                job,
                "shopify_domain",
                {
                    "domain": domain,
                    "connected": status.get("connected") is True,
                    "ssl_enabled": status.get("ssl_enabled") is True,
                },
            ),
        }
        if not complete:
            result["_human_gate"] = _human_gate(
                "shopify_domain",
                f"Connect {domain} in Shopify Domains and wait for SSL to show active, then tap DONE.",
                "shopify.domain_status",
                "https://admin.shopify.com/",
                domain=domain,
            )
        return result

    def shopify_domain_verify(
        job: Mapping[str, Any], action: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        domain = _action_input(action).get("domain")
        client, _identity = checked_shopify(job)
        status = client.domain_status(domain)
        if status.get("connected") is not True or status.get("ssl_enabled") is not True:
            raise _error("shopify_domain_not_verified")
        payload = {"domain": domain, "connected": True, "ssl_enabled": True}
        return {
            **payload,
            "evidence_ref": evidence(job, "shopify_domain_verified", payload),
        }

    def run_verify(
        job: Mapping[str, Any], action: Mapping[str, Any], phase: str
    ) -> Mapping[str, Any]:
        if verify is None:
            raise _error("verification_not_configured")
        client, _identity = checked_shopify(job)
        package = build_content(facts(job))
        request = _action_input(action)
        if request.get("domain") != job.get("plan", {}).get("domain") or request.get(
            "content_sha256"
        ) != content_fingerprint(package):
            raise _error("verification_input_mismatch")
        report, receipt_facts = _safe_report(verify(job, client, package, phase))
        ref = evidence(job, f"{phase}_verification", report)
        digest = hashlib.sha256(canonical_json(report).encode()).hexdigest()
        result: dict[str, Any] = {
            "evidence_ref": ref,
            "verification_sha256": digest,
            "all_green": True,
            "checkout_absent_verified": True,
            "no_payment_collected": True,
            "provider_truth_verified": True,
        }
        if phase == "prepublish":
            plan = _replace_step(
                job["plan"],
                "s14_shopify_publish",
                request={
                    "remove_storefront_lock": True,
                    "checkout_disabled": True,
                    "verification_sha256": digest,
                },
            )
            result["_replace_plan"] = plan
        else:
            if receipt_facts is None:
                raise _error("verification_receipt_missing")
            result["_complete"] = {"verified_facts": dict(receipt_facts)}
        return result

    def prepublish(
        job: Mapping[str, Any], action: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return run_verify(job, action, "prepublish")

    def publish_store(
        job: Mapping[str, Any], action: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        request = _action_input(action)
        if (
            set(request)
            != {"remove_storefront_lock", "checkout_disabled", "verification_sha256"}
            or request["remove_storefront_lock"] is not True
            or request["checkout_disabled"] is not True
            or not re.fullmatch(r"[0-9a-f]{64}", str(request["verification_sha256"]))
            or action.get("approval_status") != "live"
        ):
            raise _error("publication_approval_mismatch")
        client, _identity = checked_shopify(job)
        if publish is None:
            payload = {
                "handoff": True,
                "verification_sha256": request["verification_sha256"],
            }
            return {
                **payload,
                "evidence_ref": evidence(job, "shopify_publish_handoff", payload),
                "_human_gate": _human_gate(
                    "shopify_publication",
                    "Remove the Shopify storefront password without enabling checkout or payments, then tap DONE.",
                    "shopify.storefront_public",
                    "https://admin.shopify.com/",
                ),
            }
        try:
            published = publish(job, client)
        except Exception:
            raise _error("publication_outcome_unknown", uncertain=True) from None
        if not isinstance(published, Mapping) or published.get("public") is not True:
            raise _error("publication_outcome_unknown", uncertain=True)
        payload = {
            "public": True,
            "verification_sha256": request["verification_sha256"],
        }
        return {**payload, "evidence_ref": evidence(job, "shopify_published", payload)}

    def final_verify(
        job: Mapping[str, Any], action: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return run_verify(job, action, "final")

    return {
        "porkbun_discover": discover,
        "porkbun_register_domain": register,
        "porkbun_dns_snapshot": dns_snapshot,
        "porkbun_dns_apply": dns_apply,
        "shopify_credentials": shopify_credentials,
        "shopify_identity": shopify_identity,
        "shopify_build": shopify_build,
        "shopify_theme_verify": shopify_theme_verify,
        "shopify_plan_gate": shopify_plan_gate,
        "shopify_plan_verify": shopify_plan_verify,
        "shopify_domain_gate": shopify_domain_gate,
        "shopify_domain_verify": shopify_domain_verify,
        "commerce_prepublish_verify": prepublish,
        "shopify_publish": publish_store,
        "commerce_final_verify": final_verify,
    }


def _factory_shopify(shopify_factory: Callable[[], Any] | None) -> Any:
    if shopify_factory is not None:
        return shopify_factory()
    values = tuple(
        os.environ.get(name, "").strip()
        for name in ("SHOPIFY_SHOP", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET")
    )
    if not all(values):
        raise _error("shopify_credentials_missing")
    return ShopifyAdminClient.from_client_credentials(*values)


def _paid_shopify_identity(identity: Mapping[str, Any]) -> bool:
    plan = str(identity.get("plan", "")).casefold()
    return (
        identity.get("partner_development") is False
        and bool(plan)
        and not any(word in plan for word in ("trial", "development", "partner"))
    )


def production_gate_verifiers(
    *,
    porkbun_factory: Callable[[], Any] = PorkbunClient,
    shopify_factory: Callable[[], Any] | None = None,
    settings_verify: Callable[[Mapping[str, Any], Any], bool] | None = None,
    content_verify: Callable[[Mapping[str, Any], Any], bool] | None = None,
    theme_verify: Callable[[Mapping[str, Any], Any], bool] | None = None,
    publication_verify: Callable[[Mapping[str, Any], Any], bool] | None = None,
) -> dict[
    str,
    Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any] | None],
]:
    """Provider-truth probes for every non-facts human gate emitted above."""

    def porkbun_credentials(_job, _gate):
        ping = porkbun_factory().ping()
        return (
            {"provider_truth_verified": True, "credentials_valid": True}
            if ping.get("credentialsValid") is True
            else None
        )

    def porkbun_credit(job, _gate):
        plan = job.get("plan", {})
        domain = plan.get("domain")
        prices = plan.get("prices", {})
        cost = prices.get("registration_usd_cents")
        if (
            not isinstance(domain, str)
            or isinstance(cost, bool)
            or not isinstance(cost, int)
        ):
            return None
        preview = porkbun_factory().create_domain(domain, cost=cost, dry_run=True)
        if (
            preview.get("wouldSucceed") is not True
            or preview.get("sufficientFunds") is not True
        ):
            return None
        return {
            "provider_truth_verified": True,
            "domain": domain,
            "cost_usd_cents": cost,
            "sufficient_funds": True,
        }

    def shopify_identity(_job, _gate):
        identity = _factory_shopify(shopify_factory).shop_identity()
        return {
            "provider_truth_verified": True,
            "shop_id": identity["id"],
            "myshopify_domain": identity["myshopify_domain"],
        }

    def shopify_settings(job, _gate):
        client = _factory_shopify(shopify_factory)
        identity = client.shop_identity()
        basic_truth = identity.get("currency") == "AUD" and str(
            identity.get("timezone", "")
        ).startswith("Australia/")
        basic_truth = (
            basic_truth
            and settings_verify is not None
            and settings_verify(job, client) is True
        )
        return (
            {
                "provider_truth_verified": True,
                "currency": "AUD",
                "timezone": identity["timezone"],
            }
            if basic_truth
            else None
        )

    def shopify_content(job, _gate):
        client = _factory_shopify(shopify_factory)
        if content_verify is None or content_verify(job, client) is not True:
            return None
        return {"provider_truth_verified": True, "content_present": True}

    def shopify_theme(job, _gate):
        client = _factory_shopify(shopify_factory)
        passed = (
            theme_verify is not None
            and theme_verify(job, client) is True
            and client.main_theme().get("name", "").casefold() == "dawn"
        )
        return (
            {"provider_truth_verified": True, "main_theme": "dawn"}
            if passed is True
            else None
        )

    def shopify_plan(_job, _gate):
        identity = _factory_shopify(shopify_factory).shop_identity()
        return (
            {"provider_truth_verified": True, "plan": identity["plan"]}
            if _paid_shopify_identity(identity)
            else None
        )

    def shopify_domain(job, _gate):
        domain = job.get("plan", {}).get("domain")
        status = _factory_shopify(shopify_factory).domain_status(domain)
        return (
            {
                "provider_truth_verified": True,
                "domain": domain,
                "connected": True,
                "ssl_enabled": True,
            }
            if status.get("connected") is True and status.get("ssl_enabled") is True
            else None
        )

    def shopify_publication(job, _gate):
        client = _factory_shopify(shopify_factory)
        probe = client.storefront_probe("/")
        verified = (
            publication_verify is not None and publication_verify(job, client) is True
        )
        return (
            {
                "provider_truth_verified": True,
                "public": True,
                "status": probe["status"],
            }
            if verified
            and probe.get("status") == 200
            and probe.get("password_protected") is False
            else None
        )

    return {
        "porkbun_credentials": porkbun_credentials,
        "porkbun_credit": porkbun_credit,
        "shopify_store_token": shopify_identity,
        "shopify_settings": shopify_settings,
        "shopify_content": shopify_content,
        "shopify_theme": shopify_theme,
        "shopify_plan": shopify_plan,
        "shopify_domain": shopify_domain,
        "shopify_publication": shopify_publication,
    }


def production_reconcilers(
    *,
    porkbun_factory: Callable[[], Any] = PorkbunClient,
    shopify_factory: Callable[[], Any] | None = None,
    record_purchase: Callable[..., Mapping[str, Any]] | None = None,
    store: Any = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[
    str,
    Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any] | None],
]:
    """Read-only reconciliation for every workflow mutation that can be uncertain.

    The registration leg is the exception that also *writes*, to Cogitator
    only: a domain that Porkbun charged for but the money authority never
    recorded is not a resolved action, so reconciliation retries the
    completion report (idempotently, same key) before it will call the
    registration succeeded.
    """

    def _uncertain_evidence(action: Mapping[str, Any]) -> Mapping[str, Any]:
        result = action.get("result")
        return result if isinstance(result, Mapping) else {}

    def registration(job, action):
        domain = _action_input(action).get("domain")
        domains = porkbun_factory().list_domains().get("domains", [])
        match = next(
            (
                item
                for item in domains
                if isinstance(item, Mapping) and item.get("domain") == domain
            ),
            None,
        )
        if match is not None:
            carried = _uncertain_evidence(action)
            evidence = {
                "provider_truth_verified": True,
                "domain": domain,
                "present": True,
                "auto_renew": match.get("autoRenew") == 1,
                "whois_privacy": match.get("whoisPrivacy") == 1,
            }
            for field in ("order_id", "amount_usd_cents"):
                if carried.get(field) not in (None, ""):
                    evidence[field] = carried[field]
            cogitator = carried.get("cogitator")
            if not isinstance(cogitator, Mapping) and record_purchase is not None:
                # Porkbun agrees the domain exists; the ledger and the money
                # authority still disagree. Retry the report with the same
                # idempotency key so a duplicate is a replay, not a re-charge.
                try:
                    recorded = record_purchase(
                        job=job,
                        action=action,
                        approval={
                            "approval_granted": True,
                            "proposal_id": carried.get("proposal_id", ""),
                            "ticket_id": carried.get("ticket_id", ""),
                            "approval_reference": carried.get("approval_reference", ""),
                            "action_fingerprint": carried.get("action_fingerprint", ""),
                        },
                        order_id=str(carried.get("order_id", "")),
                        amount_usd_cents=int(carried.get("amount_usd_cents", 0)),
                    )
                    cogitator = {
                        "proposal_id": str(recorded["proposal_id"]),
                        "approval_id": str(recorded["approval_id"]),
                        "receipt_ref": str(recorded["receipt_ref"]),
                    }
                except Exception:
                    # Leave the job in reconciliation_required; Cal is notified
                    # by the watcher rendering that state. Never advance to DNS
                    # or Shopify on a purchase the authority has not recorded.
                    return {"status": "pending"}
            if isinstance(cogitator, Mapping):
                evidence["cogitator"] = dict(cogitator)
            elif record_purchase is not None:
                # A money bridge is configured, so a registration it never
                # recorded is not reconciled. Stay parked.
                return {"status": "pending"}
            return {"status": "succeeded", "evidence": evidence}
        dispatched = action.get("dispatched_at")
        try:
            then = datetime.fromisoformat(str(dispatched).replace("Z", "+00:00"))
        except ValueError:
            return {"status": "pending"}
        if then.tzinfo is None or clock().astimezone(timezone.utc) - then.astimezone(
            timezone.utc
        ) < timedelta(hours=24):
            return {"status": "pending"}
        return {
            "status": "failed",
            "evidence": {
                "provider_truth_verified": True,
                "domain": domain,
                "present": False,
                "idempotency_window_elapsed": True,
            },
        }

    def dns(_job, action):
        domain = _action_input(action).get("domain")
        state = _dns_state(porkbun_factory().get_dns_records(domain), domain)
        succeeded = not state["missing"] and not state["conflicts"]
        return {
            "status": "succeeded" if succeeded else "failed",
            "evidence": {
                "provider_truth_verified": True,
                "domain": domain,
                "desired_state_present": succeeded,
                "snapshot_sha256": state["snapshot_sha256"],
                "missing_count": len(state["missing"]),
                "conflict_count": len(state["conflicts"]),
            },
        }

    def publication(_job, _action):
        probe = _factory_shopify(shopify_factory).storefront_probe("/")
        if probe.get("status") == 200 and probe.get("password_protected") is False:
            status = "succeeded"
        elif probe.get("password_protected") is True:
            status = "failed"
        else:
            return {"status": "pending"}
        return {
            "status": status,
            "evidence": {
                "provider_truth_verified": True,
                "public": status == "succeeded",
                "status": probe.get("status"),
            },
        }

    return {
        "porkbun_register_domain": registration,
        "porkbun_dns_apply": dns,
        "shopify_publish": publication,
    }

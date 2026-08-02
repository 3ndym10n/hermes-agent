from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from commerce_operator import CommerceOperator, ProviderStepError
from commerce_workflow import (
    CANDIDATE_DOMAINS,
    SHOPIFY_DNS_BUNDLE,
    production_gate_verifiers,
    production_handlers,
    production_plan,
    production_reconcilers,
)
from registrar_porkbun import PorkbunMutationUncertainError


FACTS = {
    "contact_email": "hello@example.com",
    "business_identity_sentence": "Silicon Current is operated by Example Trading.",
    "double_opt_in": True,
    "brand_signoff": True,
    "privacy_signoff": True,
}
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


class Store:
    def __init__(self):
        self.accounts = []
        self.actions = []
        self.gates = []

    def latest_facts(self, _job_id):
        return dict(FACTS)

    def list_actions(self, _job_id):
        return deepcopy(self.actions)

    def upsert_provider_account(self, **value):
        self.accounts.append(value)
        return value

    def list_gates(self, _job_id):
        return deepcopy(getattr(self, "gates", []))


class Porkbun:
    def __init__(self):
        self.price = "12.00"
        self.checks = []
        self.dry_runs = 0
        self.registrations = 0
        self.uncertain_registration = False
        self.records = []
        self.writes = 0
        self.account_domains = [{"domain": "prior.example"}]

    def ping(self):
        return {"credentialsValid": True}

    def get_registration_requirements(self, tld):
        return {"tld": tld, "apiRegisterable": tld != "com.au"}

    def get_default_pricing(self):
        return {
            "pricing": {
                "com": {"renewal": "14.25"},
                "net": {"renewal": "15.50"},
            }
        }

    def list_domains(self):
        return {"domains": deepcopy(self.account_domains)}

    def check_domain(self, domain):
        self.checks.append(domain)
        return {"response": {"avail": "yes", "price": self.price}}

    def create_domain(self, domain, *, cost, dry_run, idempotency_key=None):
        if dry_run:
            self.dry_runs += 1
            return {
                "domain": domain,
                "cost": cost,
                "wouldSucceed": True,
                "sufficientFunds": True,
            }
        self.registrations += 1
        if self.uncertain_registration:
            raise PorkbunMutationUncertainError("domain/create")
        assert idempotency_key
        return {"domain": domain, "cost": cost, "orderId": 1234}

    def get_dns_records(self, domain):
        records = []
        for record in self.records:
            item = dict(record)
            relative = item["name"]
            item["name"] = domain if not relative else f"{relative}.{domain}"
            item.setdefault("ttl", "600")
            records.append(item)
        return {"records": records}

    def dns_create(
        self,
        domain,
        *,
        record_type,
        content,
        idempotency_key,
        name="",
    ):
        assert domain and idempotency_key
        self.writes += 1
        identifier = str(max([int(row["id"]) for row in self.records] or [0]) + 1)
        self.records.append({
            "id": identifier,
            "name": name,
            "type": record_type,
            "content": content,
        })
        return {"id": identifier}

    def dns_delete(self, domain, record_id, *, idempotency_key):
        assert domain and idempotency_key
        self.writes += 1
        self.records = [row for row in self.records if row["id"] != record_id]
        return {"status": "SUCCESS"}


class Shopify:
    def __init__(self):
        self.pages = {}
        self.menu = None
        self.public = True

    def shop_identity(self):
        return {
            "id": "gid://shopify/Shop/1",
            "myshopify_domain": "silicon-current.myshopify.com",
            "currency": "AUD",
            "timezone": "Australia/Sydney",
            "plan": "Basic",
            "partner_development": False,
        }

    def capabilities(self):
        return {"theme_file_write": {"supported": False}}

    def upsert_page(self, *, handle, **page):
        changed = self.pages.get(handle) != page
        self.pages[handle] = dict(page)
        return {
            "id": f"gid://shopify/Page/{len(self.pages)}",
            "handle": handle,
            "changed": changed,
        }

    def upsert_menu(self, **menu):
        changed = self.menu != menu
        self.menu = deepcopy(menu)
        return {
            "id": "gid://shopify/Menu/1",
            "handle": menu["handle"],
            "changed": changed,
        }

    def main_theme(self):
        return {"name": "Dawn"}

    def domain_status(self, domain):
        return {"host": domain, "connected": True, "ssl_enabled": True}

    def storefront_probe(self, _path):
        return {
            "status": 200,
            "password_protected": not self.public,
            "body_bytes": 100,
        }


def job():
    return {"job_id": "jb_workflow_test", "row_version": 7, "plan": {}}


def action(step, *, approval_status=None):
    value = {
        "request": {
            "input": deepcopy(step["request"]),
            "provider_idempotency_key": step["idempotency_key"],
        }
    }
    if approval_status is not None:
        value["approval_status"] = approval_status
    return value


def expanded(tmp_path, porkbun=None):
    provider = porkbun or Porkbun()
    store = Store()
    current = job()
    current["plan"] = production_plan(current, FACTS)
    handlers = production_handlers(
        store,
        porkbun_factory=lambda: provider,
        shopify_factory=lambda: Shopify(),
        facts_loader=lambda _job: FACTS,
        clock=lambda: NOW,
        sleep=lambda _seconds: None,
        evidence_root=tmp_path,
    )
    discovery = current["plan"]["steps"][0]
    result = handlers["porkbun_discover"](current, action(discovery))
    current["plan"] = result["_replace_plan"]
    registration = find_step(current, "s02_porkbun_register")
    store.actions.append({
        "action_type": "porkbun_register_domain",
        "action_status": "succeeded",
        "approval_status": "live",
        "request": {"input": deepcopy(registration["request"])},
    })
    return current, provider, handlers


def find_step(current, step_id):
    return next(step for step in current["plan"]["steps"] if step["step_id"] == step_id)


def test_discovery_expands_one_read_into_exact_governed_packet(tmp_path):
    current, provider, _handlers = expanded(tmp_path)

    assert provider.checks == list(CANDIDATE_DOMAINS)
    assert len(provider.checks) == 10
    assert provider.dry_runs == 1
    assert current["plan"]["recommendation"] == "siliconcurrent.com"
    assert len(current["plan"]["availability"]) == 10
    assert current["plan"]["prices"] == {
        "registration_usd_cents": 1200,
        "renewal_usd_cents": 1425,
    }

    request = find_step(current, "s02_porkbun_register")["request"]
    assert set(request) == {
        "domain",
        "cost_usd_cents",
        "currency",
        "quote_timestamp",
        "renewal_usd_cents",
        "renewal_date",
        "cancellation_deadline",
        "dns_bundle",
        "auto_renew",
        "whois_privacy",
    }
    assert request["domain"] == "siliconcurrent.com"
    assert request["cost_usd_cents"] == 1200
    assert request["renewal_usd_cents"] == 1425
    assert request["auto_renew"] is request["whois_privacy"] is True
    assert request["dns_bundle"] == [dict(record) for record in SHOPIFY_DNS_BUNDLE]
    publication = find_step(current, "s14_shopify_publish")["request"]
    assert set(publication) == {
        "remove_storefront_lock",
        "checkout_disabled",
        "verification_sha256",
    }
    assert len(CommerceOperator._normalize_plan(current["plan"])["steps"]) == 15


def test_registration_requotes_before_the_only_real_mutation(tmp_path):
    current, provider, handlers = expanded(tmp_path)
    registration = find_step(current, "s02_porkbun_register")
    provider.price = "12.01"

    with pytest.raises(ProviderStepError, match="registration_quote_changed") as caught:
        handlers["porkbun_register_domain"](
            current, action(registration, approval_status="live")
        )

    assert caught.value.uncertain is False
    assert provider.registrations == 0


def test_registration_transport_uncertainty_is_typed_and_never_retried(tmp_path):
    current, provider, handlers = expanded(tmp_path)
    provider.uncertain_registration = True
    registration = find_step(current, "s02_porkbun_register")

    with pytest.raises(
        ProviderStepError, match="registration_outcome_unknown"
    ) as caught:
        handlers["porkbun_register_domain"](
            current, action(registration, approval_status="live")
        )

    assert caught.value.uncertain is True
    assert provider.registrations == 1


def test_dns_apply_is_desired_state_and_does_not_duplicate_records(tmp_path):
    current, provider, handlers = expanded(tmp_path)
    snapshot = find_step(current, "s03_porkbun_dns_snapshot")
    snapshot_result = handlers["porkbun_dns_snapshot"](current, action(snapshot))
    assert "_replace_plan" not in snapshot_result

    apply = find_step(current, "s04_porkbun_dns_apply")
    first = handlers["porkbun_dns_apply"](current, action(apply))
    second = handlers["porkbun_dns_apply"](current, action(apply))

    assert first["writes"] == 3
    assert second["writes"] == 0
    assert provider.writes == 3
    assert len(provider.records) == 3


def test_dns_protected_conflict_fails_closed_into_exact_diff_approval(tmp_path):
    provider = Porkbun()
    provider.records = [
        {
            "id": "9",
            "name": "www",
            "type": "TXT",
            "content": "provider-verification-value",
        }
    ]
    current, provider, handlers = expanded(tmp_path, provider)
    snapshot = find_step(current, "s03_porkbun_dns_snapshot")

    result = handlers["porkbun_dns_snapshot"](current, action(snapshot))
    replacement = next(
        step
        for step in result["_replace_plan"]["steps"]
        if step["step_id"] == "s04_porkbun_dns_apply"
    )

    assert result["protected_conflict"] is True
    assert provider.writes == 0
    assert replacement["effect_class"] == "consequential"
    assert replacement["approval_kind"] == "dns"
    assert len(replacement["request"]["diff_hash"]) == 64
    assert "provider-verification-value" not in repr(replacement)


def test_shopify_build_is_idempotent_and_uses_theme_fallback_gate(tmp_path):
    current, _provider, _handlers = expanded(tmp_path)
    shopify = Shopify()
    handlers = production_handlers(
        Store(),
        porkbun_factory=Porkbun,
        shopify_factory=lambda: shopify,
        facts_loader=lambda _job: FACTS,
        evidence_root=tmp_path,
    )
    build = find_step(current, "s07_shopify_build")

    first = handlers["shopify_build"](current, action(build))
    second = handlers["shopify_build"](current, action(build))

    assert first["changed"] == 3
    assert second["changed"] == 0
    assert sorted(shopify.pages) == ["contact", "priority-access"]
    assert shopify.menu["handle"] == "footer"
    assert first["_human_gate"]["gate_type"] == "shopify_theme"
    assert "_human_gate" in second


def test_verification_hash_binds_publication_and_final_receipt_inputs(tmp_path):
    current, _provider, _handlers = expanded(tmp_path)
    receipt_facts = {
        "domain": {
            "name": "siliconcurrent.com",
            "registrar": "porkbun",
            "order_id": "1234",
            "spend": {"amount_usd_cents": 1200, "display": "12.00"},
            "cogitator": {
                "proposal_id": "proposal-1",
                "approval_id": "approval-1",
                "receipt_ref": "receipt-1",
            },
            "auto_renew": True,
            "whois_privacy": True,
        },
        "shopify": {
            "myshopify_domain": "silicon-current.myshopify.com",
            "shop_id": "gid://shopify/Shop/1",
            "plan": "Basic",
            "admin_url": "https://admin.shopify.com/store/silicon-current",
        },
        "public_url": "https://siliconcurrent.com/",
        "dns": {"status": "propagated", "records": ["A", "AAAA", "CNAME www"]},
        "waitlist_test": {
            "result": "pass",
            "test_address_used": "sha256:" + "a" * 64,
            "consent_recorded": True,
            "test_subscriber_deleted": True,
        },
        "verification": {
            "checklist": "9.3",
            "all_green": True,
            "evidence_bundle": "evidence/jb_workflow_test/final/",
        },
        "unresolved": [".com.au deferred pending ABN"],
        "no_payment_collected": True,
        "checkout_absent_verified": True,
        "total_spend": [
            {"provider": "porkbun", "amount": "12.00"},
            {"provider": "shopify", "amount": "plan billed at trial end"},
        ],
    }

    def verify(_job, _client, _package, phase):
        report = {
            "all_green": True,
            "checkout_absent_verified": True,
            "no_payment_collected": True,
            "checklist": "9.3",
        }
        if phase == "final":
            report["receipt_facts"] = receipt_facts
        return report

    handlers = production_handlers(
        Store(),
        shopify_factory=Shopify,
        facts_loader=lambda _job: FACTS,
        verify=verify,
        evidence_root=tmp_path,
    )
    pre = find_step(current, "s13_prepublish_verify")
    verified = handlers["commerce_prepublish_verify"](current, action(pre))
    current["plan"] = verified["_replace_plan"]
    publish = find_step(current, "s14_shopify_publish")
    handoff = handlers["shopify_publish"](
        current, action(publish, approval_status="live")
    )
    final = find_step(current, "s15_final_verify")
    completed = handlers["commerce_final_verify"](current, action(final))

    assert publish["request"]["verification_sha256"] == verified["verification_sha256"]
    assert handoff["_human_gate"]["gate_type"] == "shopify_publication"
    assert completed["_complete"] == {"verified_facts": receipt_facts}


def test_every_emitted_human_gate_has_a_provider_truth_verifier():
    porkbun = Porkbun()
    shopify = Shopify()
    verifiers = production_gate_verifiers(
        porkbun_factory=lambda: porkbun,
        shopify_factory=lambda: shopify,
        content_verify=lambda _job, _client: True,
        settings_verify=lambda _job, _client: True,
        theme_verify=lambda _job, _client: True,
        publication_verify=lambda _job, _client: True,
    )
    current = {
        "plan": {
            "domain": "siliconcurrent.com",
            "prices": {"registration_usd_cents": 1200},
        }
    }

    assert set(verifiers) == {
        "porkbun_credentials",
        "porkbun_credit",
        "shopify_store_token",
        "shopify_settings",
        "shopify_content",
        "shopify_theme",
        "shopify_plan",
        "shopify_domain",
        "shopify_publication",
    }
    for gate_type, verifier in verifiers.items():
        result = verifier(current, {"gate_type": gate_type})
        assert result is not None
        assert result["provider_truth_verified"] is True


def test_mutation_reconcilers_use_only_provider_truth():
    porkbun = Porkbun()
    porkbun.account_domains = [
        {"domain": "siliconcurrent.com", "autoRenew": 1, "whoisPrivacy": 1}
    ]
    for index, record in enumerate(SHOPIFY_DNS_BUNDLE, start=1):
        porkbun.records.append({
            "id": str(index),
            "name": record["name"],
            "type": record["type"],
            "content": record["content"],
        })
    shopify = Shopify()
    reconcilers = production_reconcilers(
        porkbun_factory=lambda: porkbun,
        shopify_factory=lambda: shopify,
        clock=lambda: NOW,
    )
    registration = {
        "request": {"input": {"domain": "siliconcurrent.com"}},
        "dispatched_at": "2026-08-02T11:00:00Z",
    }
    dns = {"request": {"input": {"domain": "siliconcurrent.com"}}}

    assert set(reconcilers) == {
        "porkbun_register_domain",
        "porkbun_dns_apply",
        "shopify_publish",
    }
    for action_type, action_value in (
        ("porkbun_register_domain", registration),
        ("porkbun_dns_apply", dns),
        ("shopify_publish", {}),
    ):
        result = reconcilers[action_type]({}, action_value)
        assert result["status"] == "succeeded"
        assert result["evidence"]["provider_truth_verified"] is True


APPROVAL_FINGERPRINT = "a" * 64


def _approval_gate(fingerprint=APPROVAL_FINGERPRINT):
    return {
        "gate_type": "action_approval",
        "approval_fingerprint": fingerprint,
        "status": "consumed",
        "completion_evidence": {
            "approval_granted": True,
            "proposal_id": "pp_1",
            "ticket_id": "pt_1",
            "approval_reference": "pa_1",
            "action_fingerprint": fingerprint,
        },
    }


def test_registration_reconciliation_restores_cogitator_truth(tmp_path):
    """Provider charged, first completion report failed: reconcile, never re-buy."""
    porkbun = Porkbun()
    current, provider, _handlers = expanded(tmp_path, porkbun=porkbun)
    store = Store()
    store.gates = [_approval_gate()]
    calls: list[str] = []

    def flaky_record(**kwargs):
        calls.append(str(kwargs.get("order_id")))
        if len(calls) == 1:
            raise RuntimeError("cogitator unreachable")
        return {
            "proposal_id": "pp_1",
            "approval_id": "pa_1",
            "receipt_ref": "purchase-receipts/domain-1",
        }

    handlers = production_handlers(
        store,
        porkbun_factory=lambda: provider,
        shopify_factory=lambda: Shopify(),
        facts_loader=lambda _job: FACTS,
        record_purchase=flaky_record,
        clock=lambda: NOW,
        sleep=lambda _seconds: None,
        evidence_root=tmp_path,
    )
    step = find_step(current, "s02_porkbun_register")
    register_action = action(step, approval_status="live")
    register_action["action_fingerprint"] = APPROVAL_FINGERPRINT

    # 1-3. Provider create succeeds; the completion report fails; the step
    # reports an uncertain outcome carrying the safe registration facts.
    with pytest.raises(ProviderStepError) as raised:
        handlers["porkbun_register_domain"](current, register_action)
    assert raised.value.code == "registration_completion_unrecorded"
    assert raised.value.uncertain is True
    carried = raised.value.evidence
    assert carried["order_id"] == "1234"
    assert carried["amount_usd_cents"] == 1200
    assert carried["proposal_id"] == "pp_1"
    assert provider.registrations == 1

    # 4-5. Reconciliation confirms the domain and retries the report.
    provider.account_domains = [
        {"domain": "siliconcurrent.com", "autoRenew": 1, "whoisPrivacy": 1}
    ]
    reconcilers = production_reconcilers(
        porkbun_factory=lambda: provider,
        record_purchase=flaky_record,
        store=store,
        clock=lambda: NOW,
    )
    uncertain_action = {
        "request": {"input": deepcopy(step["request"])},
        "action_fingerprint": APPROVAL_FINGERPRINT,
        "dispatched_at": "2026-08-02T11:00:00Z",
        "result": {"error_code": "registration_completion_unrecorded", **carried},
    }
    verdict = reconcilers["porkbun_register_domain"](current, uncertain_action)

    # 6-8. Exactly one purchase, and the reconciled result carries the refs.
    assert verdict["status"] == "succeeded"
    assert provider.registrations == 1
    assert len(calls) == 2
    assert verdict["evidence"]["cogitator"] == {
        "proposal_id": "pp_1",
        "approval_id": "pa_1",
        "receipt_ref": "purchase-receipts/domain-1",
    }
    assert verdict["evidence"]["order_id"] == "1234"
    assert verdict["evidence"]["amount_usd_cents"] == 1200


def test_registration_stays_parked_when_cogitator_cannot_be_reconciled(tmp_path):
    """Porkbun agreeing is not enough; the money authority must agree too."""
    porkbun = Porkbun()
    current, provider, _handlers = expanded(tmp_path, porkbun=porkbun)
    provider.account_domains = [
        {"domain": "siliconcurrent.com", "autoRenew": 1, "whoisPrivacy": 1}
    ]

    def always_fails(**_kwargs):
        raise RuntimeError("cogitator down")

    reconcilers = production_reconcilers(
        porkbun_factory=lambda: provider,
        record_purchase=always_fails,
        store=Store(),
        clock=lambda: NOW,
    )
    verdict = reconcilers["porkbun_register_domain"](
        current,
        {
            "request": {"input": {"domain": "siliconcurrent.com"}},
            "dispatched_at": "2026-08-02T11:00:00Z",
            "result": {"order_id": "1234", "amount_usd_cents": 1200},
        },
    )

    # Parked, not advanced: DNS and Shopify must not run on an unrecorded buy.
    assert verdict == {"status": "pending"}

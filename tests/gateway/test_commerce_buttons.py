"""Security contract for governed commerce callback tokens."""

from __future__ import annotations

import hashlib
import re
import threading
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway.commerce_buttons import (
    MAX_TTL_SECONDS,
    CommerceButtonClaim,
    CommerceButtonError,
    CommerceButtonStore,
    CommerceGovernanceError,
    CommercePurchaseGovernance,
)


BOUND = {
    "job_id": "jb_01",
    "gate_id": "cg_01",
    "action_fingerprint": "a" * 64,
    "plan_fingerprint": "b" * 64,
    "expected_row_version": 7,
    "user_id": 42,
    "chat_id": -1007,
    "message_id": 91,
}


def _mint(store: CommerceButtonStore, **overrides: object) -> dict[str, str]:
    values = {**BOUND, **overrides}
    return store.mint_group(actions=("approve", "deny"), **values)


def _claim(
    store: CommerceButtonStore,
    raw_token: str,
    **overrides: object,
) -> CommerceButtonClaim:
    values = {
        "job_id": BOUND["job_id"],
        "gate_id": BOUND["gate_id"],
        "action_fingerprint": BOUND["action_fingerprint"],
        "plan_fingerprint": BOUND["plan_fingerprint"],
        "row_version": BOUND["expected_row_version"],
        "user_id": BOUND["user_id"],
        "chat_id": BOUND["chat_id"],
        "message_id": BOUND["message_id"],
        **overrides,
    }
    return store.claim(raw_token, **values)


def _rejection_code(
    store: CommerceButtonStore,
    raw_token: str,
    **overrides: object,
) -> str:
    with pytest.raises(CommerceButtonError) as caught:
        _claim(store, raw_token, **overrides)
    return caught.value.code


def test_raw_tokens_are_opaque_and_store_keeps_only_sha256_digests():
    store = CommerceButtonStore()
    tokens = _mint(store)

    assert set(tokens) == {"approve", "deny"}
    assert tokens["approve"] != tokens["deny"]
    for action, raw_token in tokens.items():
        assert action not in raw_token
        assert BOUND["job_id"] not in raw_token
        assert re.fullmatch(r"[A-Za-z0-9_-]{32}", raw_token)
        assert hashlib.sha256(raw_token.encode()).digest() in store._entries
        assert raw_token not in repr(store._entries)
        assert raw_token not in repr(store._groups)

    claim = _claim(store, tokens["approve"])
    assert tokens["approve"] not in repr(claim)
    assert claim.action == "approve"


def test_every_token_and_group_carry_the_full_binding():
    store = CommerceButtonStore()
    tokens = _mint(store)

    claim = _claim(store, tokens["approve"])
    binding = claim.binding
    assert binding.job_id == BOUND["job_id"]
    assert binding.gate_id == BOUND["gate_id"]
    assert binding.action_fingerprint == BOUND["action_fingerprint"]
    assert binding.plan_fingerprint == BOUND["plan_fingerprint"]
    assert binding.expected_row_version == BOUND["expected_row_version"]
    assert binding.user_id == str(BOUND["user_id"])
    assert binding.chat_id == str(BOUND["chat_id"])
    assert binding.message_id == str(BOUND["message_id"])
    assert store._groups[claim._group_id].binding == binding


def test_resolve_authorizes_context_without_claiming():
    store = CommerceButtonStore()
    raw_token = _mint(store)["approve"]

    binding = store.resolve(
        raw_token,
        user_id=BOUND["user_id"],
        chat_id=BOUND["chat_id"],
        message_id=BOUND["message_id"],
    )
    assert binding.job_id == BOUND["job_id"]
    assert binding.gate_id == BOUND["gate_id"]
    assert _claim(store, raw_token).action == "approve"


def test_resolve_rejects_wrong_context_without_echoing_token():
    store = CommerceButtonStore()
    raw_token = _mint(store)["approve"]

    with pytest.raises(CommerceButtonError, match="^wrong_message$") as caught:
        store.resolve(
            raw_token,
            user_id=BOUND["user_id"],
            chat_id=BOUND["chat_id"],
            message_id="wrong",
        )
    assert raw_token not in str(caught.value)


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"user_id": 99}, "wrong_user"),
        ({"chat_id": -999}, "wrong_chat"),
        ({"message_id": 92}, "wrong_message"),
        ({"job_id": "jb_other"}, "wrong_job"),
        ({"gate_id": "cg_other"}, "wrong_gate"),
    ],
)
def test_wrong_callback_binding_is_rejected_without_burning_valid_token(
    overrides: dict[str, object],
    expected_code: str,
):
    store = CommerceButtonStore()
    raw_token = _mint(store)["approve"]

    assert _rejection_code(store, raw_token, **overrides) == expected_code
    claim = _claim(store, raw_token)
    assert claim.action == "approve"


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"action_fingerprint": "c" * 64}, "stale_action_fingerprint"),
        ({"plan_fingerprint": "d" * 64}, "stale_plan_fingerprint"),
        ({"row_version": 8}, "stale_row_version"),
    ],
)
def test_stale_authority_is_rejected_and_invalidates_siblings(
    overrides: dict[str, object],
    expected_code: str,
):
    store = CommerceButtonStore()
    tokens = _mint(store)

    assert _rejection_code(store, tokens["approve"], **overrides) == expected_code
    assert _rejection_code(store, tokens["deny"]) == "replayed_token"


def test_ttl_is_capped_at_fifteen_minutes_and_expiry_burns_siblings():
    now = [100.0]
    store = CommerceButtonStore(clock=lambda: now[0])
    tokens = _mint(store, ttl_seconds=MAX_TTL_SECONDS)

    now[0] += MAX_TTL_SECONDS
    assert _rejection_code(store, tokens["approve"]) == "expired_token"
    assert _rejection_code(store, tokens["deny"]) == "expired_token"

    with pytest.raises(CommerceButtonError, match="^invalid_ttl$"):
        _mint(store, ttl_seconds=MAX_TTL_SECONDS + 1)
    with pytest.raises(CommerceButtonError, match="^invalid_ttl$"):
        CommerceButtonStore(default_ttl_seconds=MAX_TTL_SECONDS + 1)


def test_claim_blocks_siblings_until_release_then_allows_a_safe_retry():
    store = CommerceButtonStore()
    tokens = _mint(store)
    first_claim = _claim(store, tokens["approve"])

    assert _rejection_code(store, tokens["deny"]) == "token_in_flight"
    assert store.release(first_claim) is True

    retry_claim = _claim(store, tokens["deny"])
    assert retry_claim.action == "deny"
    with pytest.raises(CommerceButtonError, match="^invalid_claim$"):
        store.complete(first_claim)
    store.complete(retry_claim)


def test_complete_consumes_all_siblings_and_replay_is_rejected():
    store = CommerceButtonStore()
    tokens = _mint(store)
    claim = _claim(store, tokens["approve"])

    selected_digest = hashlib.sha256(tokens["approve"].encode()).digest()
    sibling_digest = hashlib.sha256(tokens["deny"].encode()).digest()
    assert store._entries[selected_digest].state == "in_flight"
    assert store._entries[sibling_digest].state == "available"

    # The callback handler persists its decision before this explicit boundary.
    persisted = True
    assert persisted
    store.complete(claim)

    assert store._entries[selected_digest].state == "consumed"
    assert store._entries[sibling_digest].state == "consumed"
    assert _rejection_code(store, tokens["approve"]) == "replayed_token"
    assert _rejection_code(store, tokens["deny"]) == "replayed_token"
    with pytest.raises(CommerceButtonError, match="^replayed_token$"):
        store.complete(claim)


def test_claim_is_atomic_across_threads():
    store = CommerceButtonStore()
    raw_token = _mint(store)["approve"]
    barrier = threading.Barrier(8)

    def attempt() -> CommerceButtonClaim | str:
        barrier.wait()
        try:
            return _claim(store, raw_token)
        except CommerceButtonError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _index: attempt(), range(8)))

    claims = [item for item in outcomes if isinstance(item, CommerceButtonClaim)]
    assert len(claims) == 1
    assert outcomes.count("token_in_flight") == 7
    assert store.release(claims[0]) is True


def test_completion_after_ttl_still_burns_a_successfully_persisted_claim():
    now = [0.0]
    store = CommerceButtonStore(clock=lambda: now[0], default_ttl_seconds=1)
    tokens = _mint(store)
    claim = _claim(store, tokens["approve"])

    now[0] = 2.0
    store.complete(claim)
    assert _rejection_code(store, tokens["deny"]) == "replayed_token"


def test_expired_claim_cannot_be_released_for_retry():
    now = [0.0]
    store = CommerceButtonStore(clock=lambda: now[0], default_ttl_seconds=1)
    tokens = _mint(store)
    claim = _claim(store, tokens["approve"])

    now[0] = 2.0
    assert store.release(claim) is False
    assert _rejection_code(store, tokens["approve"]) == "expired_token"


def test_rejections_never_echo_the_raw_token():
    store = CommerceButtonStore()
    raw_token = _mint(store)["approve"]

    with pytest.raises(CommerceButtonError) as caught:
        _claim(store, raw_token, user_id="intruder")
    assert raw_token not in str(caught.value)
    assert raw_token not in repr(caught.value)

    unknown = "opaque-but-unknown-token"
    with pytest.raises(CommerceButtonError) as caught:
        _claim(store, unknown)
    assert unknown not in str(caught.value)
    assert unknown not in repr(caught.value)

GOVERNANCE_ACTION_FINGERPRINT = "c" * 64
GOVERNANCE_PLAN_FINGERPRINT = "d" * 64
RAW_EXECUTION_TICKET = "ticket_SUPER_SECRET_DO_NOT_LOG"
PROPOSAL_ID = "pp_commerce_01"
RESERVATION_ID = "pr_commerce_01"
TICKET_ID = "pt_commerce_01"
APPROVAL_REFERENCE = "e" * 64


def _governance_job() -> dict[str, object]:
    return {
        "job_id": "cj_commerce_01",
        "current_state": "awaiting_purchase_approval",
        "requester": "telegram:4242",
        "plan_fingerprint": GOVERNANCE_PLAN_FINGERPRINT,
        "plan": {
            "domain": "warpsupply.com",
            "prices": {
                "registration_usd_cents": 1200,
                "renewal_usd_cents": 1300,
            },
            "auto_renew": True,
            "whois_privacy": True,
        },
    }


def _governance_action() -> dict[str, object]:
    return {
        "action_id": "ca_commerce_01",
        "job_id": "cj_commerce_01",
        "action_type": "register_domain",
        "provider": "porkbun",
        "effect_class": "consequential",
        "action_status": "planned",
        "action_fingerprint": GOVERNANCE_ACTION_FINGERPRINT,
        "plan_fingerprint": GOVERNANCE_PLAN_FINGERPRINT,
        "request": {
            "step_id": "register-domain",
            "provider_idempotency_key": "register-domain",
            "input": {
                "domain": "warpsupply.com",
                "cost_usd_cents": 1200,
                "currency": "USD",
                "quote_timestamp": "2026-08-02T12:00:00+00:00",
                "renewal_usd_cents": 1300,
                "renewal_date": "2027-08-02",
                "cancellation_deadline": "2027-08-01",
                "dns_bundle": [
                    {"type": "A", "name": "", "content": "23.227.38.65"},
                    {
                        "type": "AAAA",
                        "name": "",
                        "content": "2620:0127:f00f:5::",
                    },
                    {
                        "type": "CNAME",
                        "name": "www",
                        "content": "shops.myshopify.com.",
                    },
                ],
                "auto_renew": True,
                "whois_privacy": True,
            },
        },
    }


def _checkout_target() -> dict[str, object]:
    return {
        "merchant_id": "porkbun.com",
        "product_kind": "domain_registration",
        "product_id": "warpsupply.com",
        "session_requirement": "authenticated",
    }


def _recurrence() -> dict[str, object]:
    return {
        "commitment_type": "recurring",
        "billing_interval": "yearly",
        "renewal_amount": "13.00",
        "renewal_date": "2027-08-02",
        "cancellation_deadline": "2027-08-01",
        "contract_duration": "renews until cancelled.",
        "cancellation_terms": "Cancel auto-renew before the renewal date.",
        "auto_renew": True,
    }


def _checkout_terms() -> dict[str, object]:
    return {
        "product_or_service": "warpsupply.com domain registration",
        "quantity": 1,
        "quoted_subtotal": "12.00",
        "tax": "0.00",
        "mandatory_fees": "0.00",
        "final_quoted_total": "12.00",
        "currency": "USD",
        "recurrence_authorization": _recurrence(),
    }



class FakeGovernanceBridges:
    """Role-separated in-memory bridge fakes; never performs HTTP."""

    def __init__(
        self,
        *,
        mismatch: str = "",
        executor_failure: bool = False,
        operator_rejection: bool = False,
    ) -> None:
        self.mismatch = mismatch
        self.executor_failure = executor_failure
        self.operator_rejection = operator_rejection
        self.operator_actions: list[str] = []
        self.operator_contexts: list[tuple[str, dict[str, object]]] = []
        self.executor_actions: list[str] = []
        self.executor_context_ref: dict[str, object] | None = None
        self.issued_response: dict[str, object] | None = None

    def operator(
        self,
        base_url: str,
        action: str,
        context: dict[str, object],
        *,
        user_intent: str,
    ) -> dict[str, object]:
        assert base_url == "https://cogitator.invalid"
        assert user_intent
        self.operator_actions.append(action)
        self.operator_contexts.append((action, deepcopy(context)))
        if self.operator_rejection:
            return {
                "status": "rejected",
                "reason_code": RAW_EXECUTION_TICKET,
                "provider_prose": RAW_EXECUTION_TICKET,
            }
        if action == "create_purchase_proposal":
            return {
                "status": "ok",
                "proposal_id": PROPOSAL_ID,
                "lifecycle_state": "awaiting_human_approval",
            }
        if action == "get_purchase_approval_packet":
            response = self._approval_packet()
            approval = response["purchase_approval"]
            assert isinstance(approval, dict)
            if self.mismatch == "domain":
                approval["merchant_domain"] = "attacker.example"
            elif self.mismatch == "amount":
                approval["maximum_authorized_total"] = {
                    "amount": "12.01",
                    "currency": "USD",
                }
            return response
        if action == "approve_and_reserve_purchase":
            return {
                "status": "ok",
                "proposal_id": PROPOSAL_ID,
                "approval_status": "approved",
                "lifecycle_state": "budget_reserved",
                "reservation_id": RESERVATION_ID,
                "reserved_cents": 1200,
                "approval_expires_at": "2026-08-02T12:15:00+00:00",
                "policy_name": "commerce_launch_v1",
                "policy_version": "1",
            }
        if action == "issue_execution_ticket":
            response = {
                "status": "ok",
                "ticket_id": TICKET_ID,
                "ticket_token": RAW_EXECUTION_TICKET,
                "proposal_id": PROPOSAL_ID,
                "policy_name": "commerce_launch_v1",
                "policy_version": "1",
                "canonical_merchant_domain": "porkbun.com",
                "approved_item": "warpsupply.com domain registration",
                "quantity": 1,
                "maximum_total": "12.00",
                "currency": "USD",
                "recurrence_authorization": _recurrence(),
                "checkout_terms": _checkout_terms(),
                "checkout_target": _checkout_target(),
                "approval_reference": APPROVAL_REFERENCE,
                "reservation_reference": RESERVATION_ID,
                "expires_at": "2026-08-02T12:05:00+00:00",
                "audience": "virgil_website_pilot",
            }
            if self.mismatch == "ticket_amount":
                response["maximum_total"] = "12.01"
            elif self.mismatch == "ticket_checkout":
                checkout_terms = response["checkout_terms"]
                assert isinstance(checkout_terms, dict)
                checkout_terms["final_quoted_total"] = "12.01"
            self.issued_response = response
            return response
        if action in {
            "cancel_purchase_before_execution",
            "revoke_unexecuted_approval",
        }:
            return {"status": "ok", "proposal_id": PROPOSAL_ID}
        raise AssertionError(f"unexpected fake operator action: {action}")

    def executor(
        self,
        action: str,
        context: dict[str, object],
        user_intent: str,
    ) -> tuple[int, dict[str, object]]:
        assert user_intent
        self.executor_actions.append(action)
        self.executor_context_ref = context
        assert context == {"ticket_token": RAW_EXECUTION_TICKET}
        if self.executor_failure:
            raise RuntimeError(f"untrusted bridge prose: {RAW_EXECUTION_TICKET}")
        return 200, {
            "status": "ok",
            "requested_action": "claim_execution_ticket",
            "ticket_id": TICKET_ID,
            "proposal_id": PROPOSAL_ID,
            "state": "claimed",
            "audience": "virgil_website_pilot",
            "canonical_merchant_domain": "porkbun.com",
            "approved_item": "warpsupply.com domain registration",
            "quantity": 1,
            "maximum_total": "12.00",
            "currency": "USD",
            "recurrence_authorization": _recurrence(),
            "checkout_terms": _checkout_terms(),
            "checkout_target": _checkout_target(),
        }

    @staticmethod
    def _approval_packet() -> dict[str, object]:
        return {
            "status": "ok",
            "mutated": False,
            "proposal_only": True,
            "execution_authorized": False,
            "approval_required": True,
            "purchase_approval": {
                "proposal_id": PROPOSAL_ID,
                "purpose": (
                    "Register warpsupply.com and connect it to the "
                    "Shopify waitlist store."
                ),
                "merchant": "Porkbun",
                "merchant_domain": "porkbun.com",
                "checkout_target": _checkout_target(),
                "item": "warpsupply.com domain registration",
                "quantity": 1,
                "final_quoted_amount": {"amount": "12.00", "currency": "USD"},
                "tax": {"amount": "0.00", "currency": "USD"},
                "mandatory_fees": {"amount": "0.00", "currency": "USD"},
                "maximum_authorized_total": {
                    "amount": "12.00",
                    "currency": "USD",
                },
                "commitment_type": "recurring",
                "billing_interval": "yearly",
                "renewal_amount": {"amount": "13.00", "currency": "USD"},
                "renewal_date": "2027-08-02",
                "cancellation_deadline": "2027-08-01",
                "cancellation_terms": (
                    "Cancel auto-renew before the renewal date."
                ),
                "policy": {
                    "name": "commerce_launch_v1",
                    "version": "1",
                    "result": "requires_human_approval",
                    "reasons": [],
                    "missing_information": [],
                },
                "budget_before_cents": 3000,
                "budget_after_approval_cents": 1800,
                "approval_expiry_seconds": MAX_TTL_SECONDS,
                "approval_required": True,
                "current_lifecycle_state": "awaiting_human_approval",
            },
        }


def _governance_helper(
    fake: FakeGovernanceBridges,
) -> CommercePurchaseGovernance:
    return CommercePurchaseGovernance(
        bridge_url="https://cogitator.invalid",
        operator_call=fake.operator,
        executor_call=fake.executor,
    )


def test_governance_approve_revalidates_then_claims_and_returns_safe_evidence(
    caplog,
    capsys,
):
    fake = FakeGovernanceBridges()
    helper = _governance_helper(fake)

    result = helper.decide(
        job=_governance_job(),
        action=_governance_action(),
        decision="approve",
    )

    assert fake.operator_actions == [
        "create_purchase_proposal",
        "get_purchase_approval_packet",
        "approve_and_reserve_purchase",
        "issue_execution_ticket",
    ]
    assert fake.executor_actions == ["claim_execution_ticket"]
    assert fake.executor_context_ref == {}
    assert fake.issued_response == {}

    prefix = f"commerce:{GOVERNANCE_ACTION_FINGERPRINT}:"
    proposal_context = fake.operator_contexts[0][1]
    proposal = proposal_context["proposal"]
    assert isinstance(proposal, dict)
    assert proposal["idempotency_key"] == prefix + "proposal"
    assert proposal["budget_scope"] == "commerce_launch_v1"
    assert proposal["final_quoted_total"] == "12.00"
    assert proposal["currency"] == "USD"
    assert proposal["auto_renew"] is True
    assert proposal["dns_required"] is True
    assert proposal["checkout_target"] == _checkout_target()
    assert (
        fake.operator_contexts[2][1]["idempotency_key"]
        == prefix + "approve"
    )
    assert (
        fake.operator_contexts[3][1]["idempotency_key"]
        == prefix + "ticket"
    )

    assert set(result) == {
        "approval_granted",
        "proposal_id",
        "approval_reference",
        "reservation_id",
        "ticket_id",
        "approved_amount_usd_cents",
        "currency",
        "domain",
        "action_fingerprint",
        "dns_records",
        "auto_renew",
        "whois_privacy",
        "renewal_amount_usd_cents",
        "renewal_date",
        "cancellation_deadline",
        "cancellation_terms",
    }
    assert result["approved_amount_usd_cents"] == 1200
    assert result["approval_reference"] == APPROVAL_REFERENCE
    assert result["domain"] == "warpsupply.com"
    assert result["dns_records"] == [
        {"type": "A", "name": "", "content": "23.227.38.65"},
        {"type": "AAAA", "name": "", "content": "2620:127:f00f:5::"},
        {
            "type": "CNAME",
            "name": "www",
            "content": "shops.myshopify.com.",
        },
    ]
    output = capsys.readouterr()
    for captured in (
        repr(result),
        repr(helper),
        repr(fake),
        caplog.text,
        output.out,
        output.err,
    ):
        assert RAW_EXECUTION_TICKET not in captured


def test_governance_deny_creates_no_proposal():
    fake = FakeGovernanceBridges()

    result = _governance_helper(fake).decide(
        job=_governance_job(),
        action=_governance_action(),
        decision="deny",
    )

    assert result == {
        "approval_granted": False,
        "domain": "warpsupply.com",
        "action_fingerprint": GOVERNANCE_ACTION_FINGERPRINT,
    }
    assert fake.operator_actions == []
    assert fake.executor_actions == []


@pytest.mark.parametrize("mismatch", ["domain", "amount"])
def test_governance_approval_mismatch_stops_before_reserve_or_issue(
    mismatch: str,
):
    fake = FakeGovernanceBridges(mismatch=mismatch)

    with pytest.raises(
        CommerceGovernanceError, match="^governance_packet_mismatch$"
    ) as caught:
        _governance_helper(fake).decide(
            job=_governance_job(),
            action=_governance_action(),
            decision="approve",
        )

    assert caught.value.code == "governance_packet_mismatch"
    assert fake.operator_actions == [
        "create_purchase_proposal",
        "get_purchase_approval_packet",
        "cancel_purchase_before_execution",
    ]
    assert "approve_and_reserve_purchase" not in fake.operator_actions
    assert "issue_execution_ticket" not in fake.operator_actions
    assert fake.executor_actions == []

@pytest.mark.parametrize(
    "mismatch", ["ticket_amount", "ticket_checkout"]
)
def test_governance_malformed_ticket_is_cleared_and_revoked_without_claim(mismatch: str):
    fake = FakeGovernanceBridges(mismatch=mismatch)

    with pytest.raises(
        CommerceGovernanceError, match="^governance_ticket_failed$"
    ) as caught:
        _governance_helper(fake).decide(
            job=_governance_job(),
            action=_governance_action(),
            decision="approve",
        )

    assert fake.operator_actions[-2:] == [
        "issue_execution_ticket",
        "revoke_unexecuted_approval",
    ]
    assert fake.executor_actions == []
    assert fake.issued_response == {}
    assert RAW_EXECUTION_TICKET not in repr(caught.value)
    assert RAW_EXECUTION_TICKET not in repr(fake)



@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("domain", "other.example"),
        ("currency", "AUD"),
        ("auto_renew", False),
        ("whois_privacy", False),
    ],
)
def test_governance_invalid_local_packet_fails_before_any_bridge_call(
    field: str,
    value: object,
):
    action = _governance_action()
    request = action["request"]
    assert isinstance(request, dict)
    purchase_input = request["input"]
    assert isinstance(purchase_input, dict)
    purchase_input[field] = value
    fake = FakeGovernanceBridges()

    with pytest.raises(
        CommerceGovernanceError, match="^governance_packet_invalid$"
    ):
        _governance_helper(fake).decide(
            job=_governance_job(),
            action=action,
            decision="approve",
        )

    assert fake.operator_actions == []
    assert fake.executor_actions == []


def test_governance_wrong_dns_fails_before_any_bridge_call():
    action = _governance_action()
    request = action["request"]
    assert isinstance(request, dict)
    purchase_input = request["input"]
    assert isinstance(purchase_input, dict)
    dns_bundle = purchase_input["dns_bundle"]
    assert isinstance(dns_bundle, list)
    first = dns_bundle[0]
    assert isinstance(first, dict)
    first["content"] = "192.0.2.1"
    fake = FakeGovernanceBridges()

    with pytest.raises(
        CommerceGovernanceError, match="^governance_packet_invalid$"
    ):
        _governance_helper(fake).decide(
            job=_governance_job(),
            action=action,
            decision="approve",
        )

    assert fake.operator_actions == []
    assert fake.executor_actions == []


def test_governance_claim_failure_revokes_and_never_leaks_ticket(
    caplog,
    capsys,
):
    fake = FakeGovernanceBridges(executor_failure=True)
    helper = _governance_helper(fake)

    with pytest.raises(
        CommerceGovernanceError, match="^governance_ticket_claim_failed$"
    ) as caught:
        helper.decide(
            job=_governance_job(),
            action=_governance_action(),
            decision="approve",
        )

    assert fake.operator_actions[-1] == "revoke_unexecuted_approval"
    assert fake.executor_context_ref == {}
    assert fake.issued_response == {}
    output = capsys.readouterr()
    for captured in (
        str(caught.value),
        repr(caught.value),
        repr(helper),
        repr(fake),
        caplog.text,
        output.out,
        output.err,
    ):
        assert RAW_EXECUTION_TICKET not in captured


def test_governance_bridge_rejection_returns_only_stable_safe_code(
    caplog,
    capsys,
):
    fake = FakeGovernanceBridges(operator_rejection=True)

    with pytest.raises(
        CommerceGovernanceError, match="^governance_bridge_rejected$"
    ) as caught:
        _governance_helper(fake).decide(
            job=_governance_job(),
            action=_governance_action(),
            decision="approve",
        )

    output = capsys.readouterr()
    assert caught.value.code == "governance_bridge_rejected"
    assert RAW_EXECUTION_TICKET not in repr(caught.value)
    assert RAW_EXECUTION_TICKET not in caplog.text
    assert RAW_EXECUTION_TICKET not in output.out + output.err
    assert fake.operator_actions == ["create_purchase_proposal"]
    assert fake.executor_actions == []


class _CompletionBridge:
    """Records exactly what the completion report sends to Cogitator."""

    def __init__(self, response: dict | None = None, status: int = 200):
        self.calls: list[tuple[str, dict]] = []
        self.status = status
        self.response = response if response is not None else {
            "status": "ok",
            "requested_action": "record_completed_purchase",
            "receipt_ref": "purchase-receipts/domain-1",
        }

    def __call__(self, action, context, user_intent):
        assert user_intent
        self.calls.append((action, deepcopy(context)))
        return self.status, self.response


def _completion_approval(**overrides: object) -> dict[str, object]:
    return {
        "approval_granted": True,
        "proposal_id": PROPOSAL_ID,
        "ticket_id": TICKET_ID,
        "approval_reference": APPROVAL_REFERENCE,
        "action_fingerprint": GOVERNANCE_ACTION_FINGERPRINT,
        **overrides,
    }


def _completion_governance(bridge: _CompletionBridge) -> CommercePurchaseGovernance:
    return CommercePurchaseGovernance(
        bridge_url="https://cogitator.invalid",
        operator_call=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("completion must not use the operator role")
        ),
        executor_call=bridge,
    )


def test_record_completion_reports_exact_terms_and_returns_receipt_refs():
    bridge = _CompletionBridge()

    result = _completion_governance(bridge).record_completion(
        job=_governance_job(),
        action=_governance_action(),
        approval=_completion_approval(),
        order_id="1234",
        amount_usd_cents=1200,
    )

    assert result == {
        "proposal_id": PROPOSAL_ID,
        "approval_id": APPROVAL_REFERENCE,
        "receipt_ref": "purchase-receipts/domain-1",
    }
    [(action, context)] = bridge.calls
    assert action == "record_completed_purchase"
    assert context["proposal_id"] == PROPOSAL_ID
    assert context["ticket_id"] == TICKET_ID
    assert context["final_amount"] == "12.00"
    assert context["currency"] == "USD"
    assert context["receipt"] == {"order_id": "1234", "domain": "warpsupply.com"}
    # The registrar response never travels to the money authority verbatim.
    assert set(context["receipt"]) == {"order_id", "domain"}


def test_record_completion_refuses_an_amount_that_is_not_the_approved_one():
    bridge = _CompletionBridge()

    with pytest.raises(CommerceGovernanceError) as raised:
        _completion_governance(bridge).record_completion(
            job=_governance_job(),
            action=_governance_action(),
            approval=_completion_approval(),
            order_id="1234",
            amount_usd_cents=1201,
        )

    assert raised.value.code == "governance_completion_amount_mismatch"
    assert bridge.calls == []


def test_record_completion_refuses_an_approval_bound_to_another_action():
    bridge = _CompletionBridge()

    with pytest.raises(CommerceGovernanceError):
        _completion_governance(bridge).record_completion(
            job=_governance_job(),
            action=_governance_action(),
            approval=_completion_approval(action_fingerprint="f" * 64),
            order_id="1234",
            amount_usd_cents=1200,
        )

    assert bridge.calls == []


def test_record_completion_fails_closed_when_the_bridge_rejects_the_report():
    bridge = _CompletionBridge(response={"status": "error"}, status=200)

    with pytest.raises(CommerceGovernanceError) as raised:
        _completion_governance(bridge).record_completion(
            job=_governance_job(),
            action=_governance_action(),
            approval=_completion_approval(),
            order_id="1234",
            amount_usd_cents=1200,
        )

    assert raised.value.code == "governance_completion_failed"

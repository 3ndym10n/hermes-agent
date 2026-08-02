"""Replay-safe opaque callback tokens for governed commerce decisions.

The callback payload contains only a random token.  All authority-bearing
context lives in this in-memory store and callback tokens are retained only as
SHA-256 digests.  Callers must claim synchronously before their first await,
persist the decision, and only then call :meth:`CommerceButtonStore.complete`.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Iterable, Literal, Mapping, NoReturn, Protocol

DEFAULT_TTL_SECONDS = 15 * 60
MAX_TTL_SECONDS = 15 * 60
MAX_DOMAIN_COST_USD_CENTS = 3_000

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
# Matches commerce_receipt._ID_RE so a recorded ref survives the receipt build.
_RECEIPT_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,239}\Z")
_DOMAIN_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
)
_PURCHASE_INPUT_FIELDS = frozenset(
    {
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
)
_DNS_RECORDS = (
    ("A", "", "23.227.38.65"),
    ("AAAA", "", "2620:127:f00f:5::"),
    ("CNAME", "www", "shops.myshopify.com."),
)
_CANCELLATION_TERMS = "Cancel auto-renew before the renewal date."
_CONTRACT_DURATION = "Renews until cancelled."
_REFUND_TERMS = "Domain registration is non-refundable after creation."
_OPERATOR_INTENTS = {
    "create_purchase_proposal": "Create one exact governed domain purchase proposal.",
    "get_purchase_approval_packet": "Re-read exact commercial terms before approval.",
    "approve_and_reserve_purchase": "Approve exact terms and reserve the exact budget.",
    "issue_execution_ticket": "Issue one short-lived execution ticket.",
    "cancel_purchase_before_execution": "Cancel the unapproved purchase proposal.",
    "revoke_unexecuted_approval": "Revoke an unexecuted approval and release its reservation.",
}
_EXECUTOR_INTENT = "Claim one governed domain-registration execution ticket."
_COMPLETION_INTENT = "Record the finished governed domain registration."


class CommerceButtonError(ValueError):
    """Safe callback rejection containing no token or bound value."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class CommerceGovernanceError(CommerceButtonError):
    """Fail-closed governance rejection containing only a stable safe code."""


class OperatorBridgeCall(Protocol):
    def __call__(
        self,
        base_url: str,
        action: str,
        context: dict[str, Any],
        *,
        user_intent: str,
    ) -> dict[str, Any]: ...


class ExecutorBridgeCall(Protocol):
    def __call__(
        self,
        action: str,
        context: dict[str, Any],
        user_intent: str,
    ) -> tuple[int, dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class CommercePurchasePacket:
    """The exact local terms bound by the action and plan fingerprints."""

    job_id: str
    action_fingerprint: str
    domain: str
    cost_usd_cents: int
    quote_timestamp: str
    renewal_usd_cents: int
    renewal_date: str
    cancellation_deadline: str
    dns_records: tuple[tuple[str, str, str], ...]
    auto_renew: bool
    whois_privacy: bool
    requester: str

    @property
    def item(self) -> str:
        return f"{self.domain} domain registration"


def _governance_fail(code: str) -> NoReturn:
    raise CommerceGovernanceError(code)


def _governance_mapping(value: object, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _governance_fail(code)
    return value


def _governance_text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _governance_fail(code)
    return value


def _governance_ref(value: object, code: str) -> str:
    text = _governance_text(value, code)
    if not _SAFE_REF_RE.fullmatch(text):
        _governance_fail(code)
    return text


def _governance_cents(value: object, code: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > MAX_DOMAIN_COST_USD_CENTS
    ):
        _governance_fail(code)
    return value


def _money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _iso_datetime(value: object, code: str) -> str:
    text = _governance_text(value, code)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _governance_fail(code)
    if parsed.tzinfo is None:
        _governance_fail(code)
    return text


def _iso_date(value: object, code: str) -> str:
    text = _governance_text(value, code)
    try:
        date.fromisoformat(text)
    except ValueError:
        _governance_fail(code)
    return text


def _canonical_dns_records(value: object) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(value, list) or len(value) != len(_DNS_RECORDS):
        _governance_fail("governance_packet_invalid")
    records: list[tuple[str, str, str]] = []
    for raw_record in value:
        record = _governance_mapping(raw_record, "governance_packet_invalid")
        if set(record) != {"type", "name", "content"}:
            _governance_fail("governance_packet_invalid")
        record_type = _governance_text(
            record.get("type"), "governance_packet_invalid"
        ).upper()
        raw_name = record.get("name")
        if (
            not isinstance(raw_name, str)
            or raw_name != raw_name.strip()
        ):
            _governance_fail("governance_packet_invalid")
        name = raw_name
        content = _governance_text(
            record.get("content"), "governance_packet_invalid"
        )
        if record_type == "AAAA":
            try:
                content = ipaddress.IPv6Address(content).compressed
            except ipaddress.AddressValueError:
                _governance_fail("governance_packet_invalid")
        records.append((record_type, name, content))
    canonical = tuple(sorted(records))
    if canonical != tuple(sorted(_DNS_RECORDS)):
        _governance_fail("governance_packet_invalid")
    return _DNS_RECORDS


def _purchase_packet(
    job: Mapping[str, Any], action: Mapping[str, Any]
) -> CommercePurchasePacket:
    job_id = _governance_ref(job.get("job_id"), "governance_packet_invalid")
    if action.get("job_id") != job_id:
        _governance_fail("governance_packet_invalid")
    if job.get("current_state") != "awaiting_purchase_approval":
        _governance_fail("governance_packet_invalid")
    if (
        action.get("provider") != "porkbun"
        or action.get("effect_class") != "consequential"
        or action.get("action_status") != "planned"
        or action.get("action_type")
        not in {"register_domain", "porkbun_register_domain"}
    ):
        _governance_fail("governance_packet_invalid")

    action_fingerprint = _governance_text(
        action.get("action_fingerprint"), "governance_packet_invalid"
    )
    if not _FINGERPRINT_RE.fullmatch(action_fingerprint):
        _governance_fail("governance_packet_invalid")
    plan_fingerprint = _governance_text(
        job.get("plan_fingerprint"), "governance_packet_invalid"
    )
    if (
        not _FINGERPRINT_RE.fullmatch(plan_fingerprint)
        or action.get("plan_fingerprint") != plan_fingerprint
    ):
        _governance_fail("governance_packet_invalid")

    request_wrapper = _governance_mapping(
        action.get("request"), "governance_packet_invalid"
    )
    raw_input = request_wrapper.get("input", request_wrapper)
    purchase_input = _governance_mapping(raw_input, "governance_packet_invalid")
    if set(purchase_input) != _PURCHASE_INPUT_FIELDS:
        _governance_fail("governance_packet_invalid")

    domain = _governance_text(
        purchase_input.get("domain"), "governance_packet_invalid"
    )
    if domain != domain.lower() or not _DOMAIN_RE.fullmatch(domain):
        _governance_fail("governance_packet_invalid")
    cost = _governance_cents(
        purchase_input.get("cost_usd_cents"), "governance_packet_invalid"
    )
    if purchase_input.get("currency") != "USD":
        _governance_fail("governance_packet_invalid")
    renewal = _governance_cents(
        purchase_input.get("renewal_usd_cents"), "governance_packet_invalid"
    )
    if purchase_input.get("auto_renew") is not True:
        _governance_fail("governance_packet_invalid")
    if purchase_input.get("whois_privacy") is not True:
        _governance_fail("governance_packet_invalid")

    plan = _governance_mapping(job.get("plan"), "governance_packet_invalid")
    prices = _governance_mapping(plan.get("prices"), "governance_packet_invalid")
    if (
        plan.get("domain") != domain
        or prices.get("registration_usd_cents") != cost
        or prices.get("renewal_usd_cents") != renewal
        or plan.get("auto_renew") is not True
        or plan.get("whois_privacy") is not True
    ):
        _governance_fail("governance_packet_invalid")

    quote_timestamp = _iso_datetime(
        purchase_input.get("quote_timestamp"), "governance_packet_invalid"
    )
    renewal_date = _iso_date(
        purchase_input.get("renewal_date"), "governance_packet_invalid"
    )
    cancellation_deadline = _iso_date(
        purchase_input.get("cancellation_deadline"),
        "governance_packet_invalid",
    )
    if date.fromisoformat(cancellation_deadline) > date.fromisoformat(renewal_date):
        _governance_fail("governance_packet_invalid")

    requester = _governance_text(
        job.get("requester", "commerce-operator"), "governance_packet_invalid"
    )
    if len(requester) > 120:
        _governance_fail("governance_packet_invalid")
    return CommercePurchasePacket(
        job_id=job_id,
        action_fingerprint=action_fingerprint,
        domain=domain,
        cost_usd_cents=cost,
        quote_timestamp=quote_timestamp,
        renewal_usd_cents=renewal,
        renewal_date=renewal_date,
        cancellation_deadline=cancellation_deadline,
        dns_records=_canonical_dns_records(purchase_input.get("dns_bundle")),
        auto_renew=True,
        whois_privacy=True,
        requester=requester,
    )


class CommercePurchaseGovernance:
    """Deterministic proposal-to-claimed-ticket governance adapter."""

    def __init__(
        self,
        *,
        bridge_url: str,
        operator_call: OperatorBridgeCall,
        executor_call: ExecutorBridgeCall,
    ) -> None:
        self._bridge_url = _governance_text(
            bridge_url, "governance_bridge_not_configured"
        )
        self._operator_call = operator_call
        self._executor_call = executor_call

    @classmethod
    def from_runtime(
        cls, *, bridge_url: str, bridge_token: str
    ) -> CommercePurchaseGovernance:
        """Wire the existing role-separated bridge clients for production use."""

        from purchase_executor import bridge_post_factory
        from scripts.purchase_operator_cli import bridge_call

        executor_call = bridge_post_factory(bridge_url, bridge_token)
        return cls(
            bridge_url=bridge_url,
            operator_call=bridge_call,
            executor_call=executor_call,
        )

    def __repr__(self) -> str:
        return "CommercePurchaseGovernance()"

    def decide(
        self,
        *,
        job: Mapping[str, Any],
        action: Mapping[str, Any],
        decision: Literal["approve", "deny"],
    ) -> dict[str, Any]:
        packet = _purchase_packet(job, action)
        if decision == "deny":
            return {
                "approval_granted": False,
                "domain": packet.domain,
                "action_fingerprint": packet.action_fingerprint,
            }
        if decision != "approve":
            _governance_fail("governance_decision_invalid")

        proposal_id = ""
        approval_attempted = False
        claimed = False
        failure_code = "governance_proposal_failed"
        try:
            created = self._operator(
                "create_purchase_proposal", {"proposal": self._proposal(packet)}
            )
            proposal_id = _governance_ref(
                created.get("proposal_id"), "governance_proposal_failed"
            )

            failure_code = "governance_packet_mismatch"
            approval_result = self._operator(
                "get_purchase_approval_packet", {"proposal_id": proposal_id}
            )
            self._validate_approval_packet(approval_result, packet, proposal_id)

            failure_code = "governance_reservation_failed"
            approval_attempted = True
            reserved = self._operator(
                "approve_and_reserve_purchase",
                {
                    "proposal_id": proposal_id,
                    "approved_maximum": _money(packet.cost_usd_cents),
                    "idempotency_key": self._key(packet, "approve"),
                    "confirm": True,
                },
            )
            reservation_id = self._validate_reservation(
                reserved, packet, proposal_id
            )

            failure_code = "governance_ticket_failed"
            issued = self._operator(
                "issue_execution_ticket",
                {
                    "proposal_id": proposal_id,
                    "idempotency_key": self._key(packet, "ticket"),
                },
            )
            raw_ticket = issued.pop("ticket_token", None)
            safe_issued = dict(issued)
            issued.clear()
            try:
                ticket_id, approval_reference = self._validate_ticket(
                    safe_issued, packet, proposal_id, reservation_id
                )
                safe_issued.clear()
                if not isinstance(raw_ticket, str) or not raw_ticket:
                    _governance_fail("governance_ticket_failed")

                failure_code = "governance_ticket_claim_failed"
                claim_context = {"ticket_token": raw_ticket}
                try:
                    status, claim = self._executor_call(
                        "claim_execution_ticket", claim_context, _EXECUTOR_INTENT
                    )
                finally:
                    claim_context.clear()
                if not isinstance(claim, dict):
                    _governance_fail("governance_ticket_claim_failed")
                if status != 200 or "ticket_token" in claim:
                    claim.clear()
                    _governance_fail("governance_ticket_claim_failed")
                if claim.get("status") != "ok":
                    claim.clear()
                    _governance_fail("governance_ticket_claim_failed")
                try:
                    self._validate_claim(claim, packet, proposal_id, ticket_id)
                finally:
                    claim.clear()
                claimed = True
            finally:
                safe_issued.clear()
                raw_ticket = ""

            return {
                "approval_granted": True,
                "proposal_id": proposal_id,
                "approval_reference": approval_reference,
                "reservation_id": reservation_id,
                "ticket_id": ticket_id,
                "approved_amount_usd_cents": packet.cost_usd_cents,
                "currency": "USD",
                "domain": packet.domain,
                "action_fingerprint": packet.action_fingerprint,
                "dns_records": [
                    {"type": kind, "name": name, "content": content}
                    for kind, name, content in packet.dns_records
                ],
                "auto_renew": packet.auto_renew,
                "whois_privacy": packet.whois_privacy,
                "renewal_amount_usd_cents": packet.renewal_usd_cents,
                "renewal_date": packet.renewal_date,
                "cancellation_deadline": packet.cancellation_deadline,
                "cancellation_terms": _CANCELLATION_TERMS,
            }
        except CommerceGovernanceError:
            self._cleanup(packet, proposal_id, approval_attempted, claimed)
            raise
        except Exception:
            self._cleanup(packet, proposal_id, approval_attempted, claimed)
            raise CommerceGovernanceError(failure_code) from None

    def record_completion(
        self,
        *,
        job: Mapping[str, Any],
        action: Mapping[str, Any],
        approval: Mapping[str, Any],
        order_id: str,
        amount_usd_cents: int,
    ) -> dict[str, Any]:
        """Report one finished registration and return its Cogitator refs.

        Called after the registrar has confirmed the order, so this is a
        report, not an authorisation: it moves no money and grants nothing.
        The refs it returns are what the §16 receipt cites.
        """

        packet = _purchase_packet(job, action)
        if amount_usd_cents != packet.cost_usd_cents:
            _governance_fail("governance_completion_amount_mismatch")
        proposal_id = _governance_ref(
            approval.get("proposal_id"), "governance_completion_failed"
        )
        ticket_id = _governance_ref(
            approval.get("ticket_id"), "governance_completion_failed"
        )
        approval_reference = _governance_ref(
            approval.get("approval_reference"), "governance_completion_failed"
        )
        if approval.get("action_fingerprint") != packet.action_fingerprint:
            _governance_fail("governance_completion_failed")
        recurrence = self._expected_recurrence(packet)
        status, result = self._executor_call(
            "record_completed_purchase",
            {
                "proposal_id": proposal_id,
                "ticket_id": ticket_id,
                "idempotency_key": self._key(packet, "complete"),
                "merchant_display_name": "Porkbun",
                "merchant_domain": "porkbun.com",
                "product_or_service": packet.item,
                "quantity": 1,
                "final_amount": _money(packet.cost_usd_cents),
                "currency": "USD",
                "commitment_type": recurrence["commitment_type"],
                "billing_interval": recurrence["billing_interval"],
                "renewal_amount": recurrence["renewal_amount"],
                "renewal_date": recurrence["renewal_date"],
                "cancellation_deadline": recurrence["cancellation_deadline"],
                "contract_duration": recurrence["contract_duration"],
                "auto_renew": recurrence["auto_renew"],
                "cancellation_terms": recurrence["cancellation_terms"],
                # Order id only -- never a registrar credential or raw response.
                "receipt": {
                    "order_id": _governance_ref(
                        order_id, "governance_completion_failed"
                    ),
                    "domain": packet.domain,
                },
            },
            _COMPLETION_INTENT,
        )
        if (
            status != 200
            or not isinstance(result, dict)
            or result.get("status") != "ok"
        ):
            _governance_fail("governance_completion_failed")
        # Cogitator's receipt refs are path-shaped, which the identifier guard
        # above deliberately rejects; hold this one field to the receipt
        # schema's own character class instead of widening that guard.
        receipt_ref = _governance_text(
            result.get("receipt_ref") or result.get("purchase_receipt_id"),
            "governance_completion_failed",
        )
        if _RECEIPT_REF_RE.fullmatch(receipt_ref) is None:
            _governance_fail("governance_completion_failed")
        return {
            "proposal_id": proposal_id,
            "approval_id": approval_reference,
            "receipt_ref": receipt_ref,
        }

    def _operator(self, action: str, context: dict[str, Any]) -> dict[str, Any]:
        result = self._operator_call(
            self._bridge_url,
            action,
            context,
            user_intent=_OPERATOR_INTENTS[action],
        )
        if not isinstance(result, dict):
            _governance_fail("governance_bridge_rejected")
        if result.get("status") != "ok":
            result.clear()
            _governance_fail("governance_bridge_rejected")
        return result

    @staticmethod
    def _key(packet: CommercePurchasePacket, stage: str) -> str:
        return f"commerce:{packet.action_fingerprint}:{stage}"

    def _proposal(self, packet: CommercePurchasePacket) -> dict[str, Any]:
        total = _money(packet.cost_usd_cents)
        return {
            "project_id": "website_launch",
            "budget_scope": "commerce_launch_v1",
            "requester": packet.requester,
            "idempotency_key": self._key(packet, "proposal"),
            "source_ref": f"commerce:{packet.job_id}:{packet.action_fingerprint}",
            "purpose": (
                f"Register {packet.domain} and connect it to the Shopify "
                "waitlist store."
            ),
            "merchant_display_name": "Porkbun",
            "merchant_domain": "porkbun.com",
            "merchant_url": "https://porkbun.com/",
            "product_or_service": packet.item,
            "purchase_class": "domain",
            "quantity": 1,
            "quoted_subtotal": total,
            "tax": "0.00",
            "mandatory_fees": "0.00",
            "final_quoted_total": total,
            "currency": "USD",
            "quote_timestamp": packet.quote_timestamp,
            "commitment_type": "recurring",
            "billing_interval": "yearly",
            "renewal_amount": _money(packet.renewal_usd_cents),
            "renewal_date": packet.renewal_date,
            "cancellation_deadline": packet.cancellation_deadline,
            "contract_duration": _CONTRACT_DURATION,
            "auto_renew": True,
            "refund_terms": _REFUND_TERMS,
            "cancellation_terms": _CANCELLATION_TERMS,
            "premium_domain": False,
            "free_hosting_inadequate": False,
            "dns_required": True,
            "free_alternatives_considered": True,
            "necessary_to_launch": True,
            "optional_convenience": False,
            "checkout_target": {
                "merchant_id": "porkbun.com",
                "product_kind": "domain_registration",
                "product_id": packet.domain,
                "session_requirement": "authenticated",
            },
        }

    @staticmethod
    def _validate_approval_packet(
        result: Mapping[str, Any],
        packet: CommercePurchasePacket,
        proposal_id: str,
    ) -> None:
        if (
            result.get("mutated") is not False
            or result.get("proposal_only") is not True
            or result.get("execution_authorized") is not False
            or result.get("approval_required") is not True
        ):
            _governance_fail("governance_packet_mismatch")
        approval = _governance_mapping(
            result.get("purchase_approval"), "governance_packet_mismatch"
        )
        expected_target = {
            "merchant_id": "porkbun.com",
            "product_kind": "domain_registration",
            "product_id": packet.domain,
            "session_requirement": "authenticated",
        }
        if (
            approval.get("proposal_id") != proposal_id
            or approval.get("merchant") != "Porkbun"
            or approval.get("merchant_domain") != "porkbun.com"
            or approval.get("item") != packet.item
            or approval.get("quantity") != 1
            or approval.get("checkout_target") != expected_target
            or approval.get("commitment_type") != "recurring"
            or approval.get("billing_interval") != "yearly"
            or approval.get("renewal_date") != packet.renewal_date
            or approval.get("cancellation_deadline")
            != packet.cancellation_deadline
            or approval.get("cancellation_terms") != _CANCELLATION_TERMS
            or approval.get("approval_required") is not True
            or approval.get("current_lifecycle_state")
            != "awaiting_human_approval"
            or approval.get("approval_expiry_seconds") != MAX_TTL_SECONDS
        ):
            _governance_fail("governance_packet_mismatch")
        policy = _governance_mapping(
            approval.get("policy"), "governance_packet_mismatch"
        )
        if (
            policy.get("name") != "commerce_launch_v1"
            or policy.get("result") != "requires_human_approval"
            or policy.get("reasons") != []
            or policy.get("missing_information") != []
        ):
            _governance_fail("governance_packet_mismatch")
        before = approval.get("budget_before_cents")
        after = approval.get("budget_after_approval_cents")
        if (
            isinstance(before, bool)
            or not isinstance(before, int)
            or isinstance(after, bool)
            or not isinstance(after, int)
            or before - after != packet.cost_usd_cents
        ):
            _governance_fail("governance_packet_mismatch")
        for field, cents in (
            ("final_quoted_amount", packet.cost_usd_cents),
            ("maximum_authorized_total", packet.cost_usd_cents),
            ("tax", 0),
            ("mandatory_fees", 0),
            ("renewal_amount", packet.renewal_usd_cents),
        ):
            value = _governance_mapping(
                approval.get(field), "governance_packet_mismatch"
            )
            if value != {"amount": _money(cents), "currency": "USD"}:
                _governance_fail("governance_packet_mismatch")

    @staticmethod
    def _validate_reservation(
        result: Mapping[str, Any],
        packet: CommercePurchasePacket,
        proposal_id: str,
    ) -> str:
        if (
            result.get("proposal_id") != proposal_id
            or result.get("approval_status") != "approved"
            or result.get("lifecycle_state") != "budget_reserved"
            or result.get("reserved_cents") != packet.cost_usd_cents
            or result.get("policy_name") != "commerce_launch_v1"
        ):
            _governance_fail("governance_reservation_failed")
        _iso_datetime(
            result.get("approval_expires_at"), "governance_reservation_failed"
        )
        return _governance_ref(
            result.get("reservation_id"), "governance_reservation_failed"
        )

    @staticmethod
    def _expected_recurrence(packet: CommercePurchasePacket) -> dict[str, Any]:
        return {
            "commitment_type": "recurring",
            "billing_interval": "yearly",
            "renewal_amount": _money(packet.renewal_usd_cents),
            "renewal_date": packet.renewal_date,
            "cancellation_deadline": packet.cancellation_deadline,
            "contract_duration": _CONTRACT_DURATION.lower(),
            "cancellation_terms": _CANCELLATION_TERMS,
            "auto_renew": True,
        }


    @classmethod
    def _expected_checkout_terms(
        cls, packet: CommercePurchasePacket
    ) -> dict[str, Any]:
        return {
            "product_or_service": packet.item,
            "quantity": 1,
            "quoted_subtotal": _money(packet.cost_usd_cents),
            "tax": "0.00",
            "mandatory_fees": "0.00",
            "final_quoted_total": _money(packet.cost_usd_cents),
            "currency": "USD",
            "recurrence_authorization": cls._expected_recurrence(packet),
        }

    @classmethod
    def _validate_ticket(
        cls,
        result: Mapping[str, Any],
        packet: CommercePurchasePacket,
        proposal_id: str,
        reservation_id: str,
    ) -> tuple[str, str]:
        expected_target = {
            "merchant_id": "porkbun.com",
            "product_kind": "domain_registration",
            "product_id": packet.domain,
            "session_requirement": "authenticated",
        }
        if (
            result.get("proposal_id") != proposal_id
            or result.get("policy_name") != "commerce_launch_v1"
            or result.get("canonical_merchant_domain") != "porkbun.com"
            or result.get("approved_item") != packet.item
            or result.get("quantity") != 1
            or result.get("maximum_total") != _money(packet.cost_usd_cents)
            or result.get("currency") != "USD"
            or result.get("recurrence_authorization")
            != cls._expected_recurrence(packet)
            or result.get("checkout_terms")
            != cls._expected_checkout_terms(packet)
            or result.get("checkout_target") != expected_target
            or result.get("reservation_reference") != reservation_id
            or result.get("audience") != "virgil_website_pilot"
        ):
            _governance_fail("governance_ticket_failed")
        ticket_id = _governance_ref(
            result.get("ticket_id"), "governance_ticket_failed"
        )
        approval_reference = _governance_ref(
            result.get("approval_reference"), "governance_ticket_failed"
        )
        _iso_datetime(result.get("expires_at"), "governance_ticket_failed")
        return ticket_id, approval_reference

    @classmethod
    def _validate_claim(
        cls,
        result: Mapping[str, Any],
        packet: CommercePurchasePacket,
        proposal_id: str,
        ticket_id: str,
    ) -> None:
        expected_target = {
            "merchant_id": "porkbun.com",
            "product_kind": "domain_registration",
            "product_id": packet.domain,
            "session_requirement": "authenticated",
        }
        if (
            result.get("ticket_id") != ticket_id
            or result.get("proposal_id") != proposal_id
            or result.get("state") != "claimed"
            or result.get("audience") != "virgil_website_pilot"
            or result.get("canonical_merchant_domain") != "porkbun.com"
            or result.get("approved_item") != packet.item
            or result.get("quantity") != 1
            or result.get("maximum_total") != _money(packet.cost_usd_cents)
            or result.get("currency") != "USD"
            or result.get("recurrence_authorization")
            != cls._expected_recurrence(packet)
            or result.get("checkout_terms")
            != cls._expected_checkout_terms(packet)
            or result.get("checkout_target") != expected_target
        ):
            _governance_fail("governance_ticket_claim_failed")

    def _cleanup(
        self,
        packet: CommercePurchasePacket,
        proposal_id: str,
        approval_attempted: bool,
        claimed: bool,
    ) -> None:
        if not proposal_id or claimed:
            return
        if approval_attempted:
            action = "revoke_unexecuted_approval"
            stage = "revoke"
        else:
            action = "cancel_purchase_before_execution"
            stage = "cancel"
        try:
            self._operator_call(
                self._bridge_url,
                action,
                {
                    "proposal_id": proposal_id,

                    "idempotency_key": self._key(packet, stage),
                },
                user_intent=_OPERATOR_INTENTS[action],
            )
        except Exception:
            pass

@dataclass(frozen=True, slots=True)
class CommerceButtonBinding:
    """Server-side authority bound to every token in a decision group."""

    job_id: str
    gate_id: str
    action_fingerprint: str
    plan_fingerprint: str
    expected_row_version: int
    user_id: str
    chat_id: str
    message_id: str


@dataclass(frozen=True, slots=True)
class CommerceButtonClaim:
    """A claimed decision, safe to pass through an async persistence step."""

    action: str
    binding: CommerceButtonBinding
    _token_digest: bytes = field(repr=False, compare=False)
    _group_id: int = field(repr=False, compare=False)
    _generation: int = field(repr=False, compare=False)


@dataclass(slots=True)
class _TokenEntry:
    group_id: int
    action: str
    binding: CommerceButtonBinding
    expires_at: float
    state: str = "available"


@dataclass(slots=True)
class _TokenGroup:
    binding: CommerceButtonBinding
    token_digests: set[bytes]
    expires_at: float
    state: str = "available"
    claimed_digest: bytes | None = None
    generation: int = 0


def _required_text(value: object, field_name: str) -> str:
    if value is None or isinstance(value, bool):
        raise CommerceButtonError(f"invalid_{field_name}")
    text = str(value).strip()
    if not text:
        raise CommerceButtonError(f"invalid_{field_name}")
    return text


def _row_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CommerceButtonError("invalid_row_version")
    return value


def _token_digest(raw_token: object) -> bytes:
    if not isinstance(raw_token, str) or not raw_token:
        raise CommerceButtonError("invalid_token")
    return hashlib.sha256(raw_token.encode("utf-8")).digest()


class CommerceButtonStore:
    """Thread-safe, two-phase store for a group of single-use decisions."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        default_ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._clock = clock
        self._default_ttl_seconds = self._validate_ttl(default_ttl_seconds)
        self._lock = threading.Lock()
        self._entries: dict[bytes, _TokenEntry] = {}
        self._groups: dict[int, _TokenGroup] = {}
        self._next_group_id = 1

    @staticmethod
    def _validate_ttl(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CommerceButtonError("invalid_ttl")
        if value <= 0 or value > MAX_TTL_SECONDS:
            raise CommerceButtonError("invalid_ttl")
        return value

    @staticmethod
    def _binding(
        *,
        job_id: object,
        gate_id: object,
        action_fingerprint: object,
        plan_fingerprint: object,
        expected_row_version: object,
        user_id: object,
        chat_id: object,
        message_id: object,
    ) -> CommerceButtonBinding:
        return CommerceButtonBinding(
            job_id=_required_text(job_id, "job_id"),
            gate_id=_required_text(gate_id, "gate_id"),
            action_fingerprint=_required_text(action_fingerprint, "action_fingerprint"),
            plan_fingerprint=_required_text(plan_fingerprint, "plan_fingerprint"),
            expected_row_version=_row_version(expected_row_version),
            user_id=_required_text(user_id, "user_id"),
            chat_id=_required_text(chat_id, "chat_id"),
            message_id=_required_text(message_id, "message_id"),
        )

    def mint_group(
        self,
        *,
        job_id: object,
        gate_id: object,
        action_fingerprint: object,
        plan_fingerprint: object,
        expected_row_version: object,
        user_id: object,
        chat_id: object,
        message_id: object,
        actions: Iterable[str],
        ttl_seconds: int | None = None,
    ) -> dict[str, str]:
        """Mint one opaque callback token per action.

        The returned raw values are for immediate callback-data rendering.  The
        store itself retains only their SHA-256 digests.
        """

        binding = self._binding(
            job_id=job_id,
            gate_id=gate_id,
            action_fingerprint=action_fingerprint,
            plan_fingerprint=plan_fingerprint,
            expected_row_version=expected_row_version,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
        )
        action_names = tuple(_required_text(action, "action") for action in actions)
        if not action_names or len(action_names) != len(set(action_names)):
            raise CommerceButtonError("invalid_actions")
        ttl = self._validate_ttl(
            self._default_ttl_seconds if ttl_seconds is None else ttl_seconds
        )

        with self._lock:
            group_id = self._next_group_id
            self._next_group_id += 1
            expires_at = self._clock() + ttl
            digests: set[bytes] = set()
            raw_tokens: dict[str, str] = {}
            for action in action_names:
                while True:
                    raw_token = secrets.token_urlsafe(24)
                    digest = _token_digest(raw_token)
                    if digest not in self._entries and digest not in digests:
                        break
                digests.add(digest)
                raw_tokens[action] = raw_token
                self._entries[digest] = _TokenEntry(
                    group_id=group_id,
                    action=action,
                    binding=binding,
                    expires_at=expires_at,
                )
            self._groups[group_id] = _TokenGroup(
                binding=binding,
                token_digests=digests,
                expires_at=expires_at,
            )
            return raw_tokens

    def resolve(
        self,
        raw_token: object,
        *,
        user_id: object,
        chat_id: object,
        message_id: object,
    ) -> CommerceButtonBinding:
        """Resolve an authorized callback to the IDs needed for a current read.

        Resolution does not claim the token. The caller must synchronously read
        current job/gate authority and pass it to :meth:`claim`, which repeats
        these checks and is the atomic race boundary.
        """

        digest = _token_digest(raw_token)
        user = _required_text(user_id, "user_id")
        chat = _required_text(chat_id, "chat_id")
        message = _required_text(message_id, "message_id")
        with self._lock:
            entry, _group = self._available_entry(digest)
            self._validate_context(
                entry.binding,
                user_id=user,
                chat_id=chat,
                message_id=message,
            )
            return entry.binding

    def claim(
        self,
        raw_token: object,
        *,
        job_id: object,
        gate_id: object,
        action_fingerprint: object,
        plan_fingerprint: object,
        row_version: object,
        user_id: object,
        chat_id: object,
        message_id: object,
    ) -> CommerceButtonClaim:
        """Validate and atomically mark one decision group ``in_flight``.

        This synchronous call must finish before the callback handler's first
        await.  Only one sibling can be in flight at a time.
        """

        digest = _token_digest(raw_token)
        current = self._binding(
            job_id=job_id,
            gate_id=gate_id,
            action_fingerprint=action_fingerprint,
            plan_fingerprint=plan_fingerprint,
            expected_row_version=row_version,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
        )
        with self._lock:
            entry, group = self._available_entry(digest)

            expected = entry.binding
            self._validate_context(
                expected,
                user_id=current.user_id,
                chat_id=current.chat_id,
                message_id=current.message_id,
            )
            if current.job_id != expected.job_id:
                raise CommerceButtonError("wrong_job")
            if current.gate_id != expected.gate_id:
                raise CommerceButtonError("wrong_gate")
            if current.action_fingerprint != expected.action_fingerprint:
                self._consume_group(group)
                raise CommerceButtonError("stale_action_fingerprint")
            if current.plan_fingerprint != expected.plan_fingerprint:
                self._consume_group(group)
                raise CommerceButtonError("stale_plan_fingerprint")
            if current.expected_row_version != expected.expected_row_version:
                self._consume_group(group)
                raise CommerceButtonError("stale_row_version")

            group.state = "in_flight"
            group.claimed_digest = digest
            group.generation += 1
            entry.state = "in_flight"
            return CommerceButtonClaim(
                action=entry.action,
                binding=entry.binding,
                _token_digest=digest,
                _group_id=entry.group_id,
                _generation=group.generation,
            )

    def _available_entry(self, digest: bytes) -> tuple[_TokenEntry, _TokenGroup]:
        entry = self._entries.get(digest)
        if entry is None:
            raise CommerceButtonError("invalid_token")
        group = self._groups[entry.group_id]
        if group.state == "consumed":
            raise CommerceButtonError("replayed_token")
        if group.state == "expired":
            raise CommerceButtonError("expired_token")
        if group.state == "in_flight":
            raise CommerceButtonError("token_in_flight")
        if self._clock() >= group.expires_at:
            self._expire_group(group)
            raise CommerceButtonError("expired_token")
        return entry, group

    @staticmethod
    def _validate_context(
        binding: CommerceButtonBinding,
        *,
        user_id: str,
        chat_id: str,
        message_id: str,
    ) -> None:
        if user_id != binding.user_id:
            raise CommerceButtonError("wrong_user")
        if chat_id != binding.chat_id:
            raise CommerceButtonError("wrong_chat")
        if message_id != binding.message_id:
            raise CommerceButtonError("wrong_message")

    def complete(self, claim: CommerceButtonClaim) -> None:
        """Consume a claimed token and every sibling after persistence succeeds.

        The caller is responsible for persisting the bound decision before
        invoking this method.  Completion remains valid if TTL elapses while
        that persistence call is in progress.
        """

        with self._lock:
            group = self._active_claim_group(claim)
            self._consume_group(group)

    def release(self, claim: CommerceButtonClaim) -> bool:
        """Return an active claim to availability after a safe failed attempt."""

        with self._lock:
            group = self._active_claim_group(claim)
            if self._clock() >= group.expires_at:
                self._expire_group(group)
                return False
            entry = self._entries[claim._token_digest]
            entry.state = "available"
            group.state = "available"
            group.claimed_digest = None
            return True

    def _active_claim_group(self, claim: CommerceButtonClaim) -> _TokenGroup:
        if not isinstance(claim, CommerceButtonClaim):
            raise CommerceButtonError("invalid_claim")
        entry = self._entries.get(claim._token_digest)
        group = self._groups.get(claim._group_id)
        if entry is None or group is None:
            raise CommerceButtonError("invalid_claim")
        if group.state == "consumed":
            raise CommerceButtonError("replayed_token")
        if (
            group.state != "in_flight"
            or group.claimed_digest != claim._token_digest
            or group.generation != claim._generation
            or entry.state != "in_flight"
        ):
            raise CommerceButtonError("invalid_claim")
        return group

    def _consume_group(self, group: _TokenGroup) -> None:
        group.state = "consumed"
        group.claimed_digest = None
        for digest in group.token_digests:
            self._entries[digest].state = "consumed"

    def _expire_group(self, group: _TokenGroup) -> None:
        group.state = "expired"
        group.claimed_digest = None
        for digest in group.token_digests:
            self._entries[digest].state = "expired"

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

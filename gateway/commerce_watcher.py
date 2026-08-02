"""Telegram rendering for durable commerce jobs.

The worker owns execution. This mixin only reads the ledger, delivers
whitelisted status fields, and persists local approval decisions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from commerce_jobs import CommerceJobError, CommerceJobStore
from gateway.cogitator_intake_bridge import TOKEN_ENV
from gateway.commerce_buttons import (
    CommerceButtonError,
    CommerceButtonStore,
    CommercePurchaseGovernance,
)
from gateway.config import Platform
from gateway.delivery import DeliveryTarget
from hermes_attention import AttentionError, attention_public_url
from hermes_cli.config import cfg_get, load_config_readonly
from utils import is_truthy_value

logger = logging.getLogger("gateway.commerce")

COMMERCE_ACCEPTANCE_SENTENCE = "Set up the AMD GPU waitlist store."

_STATE_COPY = {
    "requested": "Checking what already exists…",
    "planning": "Planning the launch…",
    "ready": "Decision packet ready.",
    "executing_read_only": "Running read-only checks…",
    "awaiting_purchase_approval": "Approval required before domain registration.",
    "awaiting_dns_approval": "Approval required for the DNS change.",
    "awaiting_publication_approval": "Approval required before publication.",
    "awaiting_cal": "Waiting for your action.",
    "executing": "Applying the approved step…",
    "resuming": "Resuming…",
    "verifying": "Verifying the store…",
    "uncertain_external_state": "Outcome unknown; reconciling from provider truth.",
    "reconciliation_required": "Reconciliation needs review.",
    "timed_out": "Timed out; use /store resume to continue.",
    "paused": "Paused.",
    "completed": "Launch complete.",
    "cancelled": "Launch cancelled.",
    "failed": "Launch stopped safely.",
}

_APPROVAL_STATES = frozenset({
    "awaiting_purchase_approval",
    "awaiting_dns_approval",
    "awaiting_publication_approval",
})
_DELIVERY_IGNORED_EVENTS = frozenset({
    "commerce_gateway_delivered",
    "gate_handoff_issued",
    "gate_handoff_renewed",
})
_DOMAIN_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
)
_CODE_RE = re.compile(r"[a-z][a-z0-9_.-]{0,119}")


def commerce_enabled() -> bool:
    try:
        return is_truthy_value(
            cfg_get(load_config_readonly(), "commerce", "enabled", default=False)
        )
    except Exception:
        return False


def is_commerce_acceptance_event(event: Any) -> bool:
    source = getattr(event, "source", None)
    return bool(
        getattr(event, "text", "") == COMMERCE_ACCEPTANCE_SENTENCE
        and not getattr(event, "internal", False)
        and getattr(source, "platform", None) == Platform.TELEGRAM
        and getattr(source, "user_id", None)
        and getattr(source, "chat_id", None)
    )


def _origin_for(store: CommerceJobStore, job_id: str) -> dict[str, str]:
    events = store.list_events(job_id)
    if not events:
        return {}
    evidence = events[0].get("evidence")
    origin = evidence.get("origin") if isinstance(evidence, dict) else None
    if not isinstance(origin, dict) or origin.get("platform") != "telegram":
        return {}
    allowed = ("platform", "chat_id", "chat_type", "thread_id", "message_id")
    return {
        key: str(origin[key]) for key in allowed if origin.get(key) not in (None, "")
    }


def _domain(value: Any) -> str:
    candidate = str(value or "").strip().casefold()
    return candidate if _DOMAIN_RE.fullmatch(candidate) else ""


def _price_cents(plan: Any, field: str) -> int | None:
    prices = plan.get("prices") if isinstance(plan, dict) else None
    value = prices.get(field) if isinstance(prices, dict) else None
    return (
        value
        if isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= 1_000_000
        else None
    )


def _registration_cents(plan: Any) -> int | None:
    return _price_cents(plan, "registration_usd_cents")


def _renewal_cents(plan: Any) -> int | None:
    return _price_cents(plan, "renewal_usd_cents")


def _action_input(action: dict[str, Any]) -> dict[str, Any]:
    request = action.get("request")
    if not isinstance(request, dict):
        return {}
    nested = request.get("input")
    return nested if isinstance(nested, dict) else request


def _shopify_dns_bundle(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != 3:
        return False
    records = set()
    for raw in value:
        if not isinstance(raw, dict):
            return False
        record_type = str(raw.get("type") or "").upper()
        name = str(raw.get("name") or "").lower()
        content = str(raw.get("content") or "").lower()
        if record_type == "AAAA" and content == "2620:0127:f00f:5::":
            content = "2620:127:f00f:5::"
        records.add((record_type, name, content))
    return records == {
        ("A", "", "23.227.38.65"),
        ("AAAA", "", "2620:127:f00f:5::"),
        ("CNAME", "www", "shops.myshopify.com."),
    }


def _approval_matches_plan(job: dict[str, Any], action: dict[str, Any]) -> bool:
    plan = job.get("plan")
    plan_domain = _domain(plan.get("domain")) if isinstance(plan, dict) else ""
    action_data = _action_input(action)
    action_domain = _domain(action_data.get("domain"))
    if action_data.get("domain") is not None and not action_domain:
        return False
    if plan_domain and action_domain and plan_domain != action_domain:
        return False
    plan_cents = _registration_cents(plan)
    action_cents = next(
        (
            action_data[key]
            for key in ("cost_usd_cents", "registration_usd_cents", "cost")
            if key in action_data
        ),
        None,
    )
    if action_cents is not None and (
        isinstance(action_cents, bool)
        or not isinstance(action_cents, int)
        or not 0 <= action_cents <= 1_000_000
    ):
        return False
    action_type = str(action.get("action_type") or "")
    if action_type in {
        "porkbun_register_domain",
        "porkbun_register_and_dns",
        "register_domain",
    }:
        return bool(
            plan_domain
            and action_domain == plan_domain
            and plan_cents is not None
            and action_cents == plan_cents
            and _renewal_cents(plan) is not None
            and isinstance(plan, dict)
            and plan.get("auto_renew") is True
            and plan.get("whois_privacy") is True
            and action_data.get("auto_renew") is True
            and action_data.get("whois_privacy") is True
            and _shopify_dns_bundle(action_data.get("dns_bundle"))
        )
    if job.get("current_state") == "awaiting_dns_approval":
        return bool(
            plan_domain
            and action_domain == plan_domain
            and re.fullmatch(r"[0-9a-f]{64}", str(action_data.get("diff_hash") or ""))
        )
    if job.get("current_state") == "awaiting_publication_approval":
        return bool(
            action_data.get("remove_password") is True
            and action_data.get("checkout_disabled") is True
            and re.fullmatch(
                r"[0-9a-f]{64}",
                str(action_data.get("verification_sha256") or ""),
            )
        )
    return plan_cents is None or action_cents is None or plan_cents == action_cents


def _safe_action(value: Any) -> str:
    candidate = str(value or "").strip()
    return (
        candidate.replace("_", " ")
        if _CODE_RE.fullmatch(candidate)
        else "governed action"
    )


def render_commerce_job(job: dict[str, Any]) -> str:
    """Render only fixed labels and whitelisted decision fields."""
    state = str(job.get("current_state") or "failed")
    lines = [
        "🛒 Virgil Commerce",
        _STATE_COPY.get(state, "Status updated."),
        f"Job: {job.get('job_id', '')}",
        f"State: {state}",
    ]
    plan = job.get("plan")
    if state in ({"ready"} | _APPROVAL_STATES) and isinstance(plan, dict):
        domain = _domain(plan.get("domain"))
        if domain:
            lines.append(f"Recommended domain: {domain}")
        cents = _registration_cents(plan)
        if cents is not None:
            lines.append("Registration quote: USD {:.2f}".format(cents / 100))
    if job.get("substatus"):
        lines.append(f"Substatus: {job['substatus']}")
    return "\n".join(lines)


def _completion_summary(snapshot: dict[str, Any]) -> str:
    """Render only receipt fields already verified by the durable worker."""
    job = snapshot["job"]
    if job.get("current_state") != "completed":
        return ""
    job_id = str(job.get("job_id") or "")
    expected_ref = f"receipts/{job_id}.json"
    receipt_recorded = any(
        event.get("reason_code") == "receipt_persisted"
        and isinstance(event.get("evidence"), dict)
        and event["evidence"].get("receipt_ref") == expected_ref
        for event in snapshot["events"]
    )
    facts = None
    for action in reversed(snapshot["actions"]):
        result = action.get("result")
        control = result.get("operator_control") if isinstance(result, dict) else None
        completion = control.get("completion") if isinstance(control, dict) else None
        candidate = (
            completion.get("verified_facts") if isinstance(completion, dict) else None
        )
        if isinstance(candidate, dict):
            facts = candidate
            break
    plan = job.get("plan")
    domain = _domain(plan.get("domain")) if isinstance(plan, dict) else ""
    public_url = facts.get("public_url") if isinstance(facts, dict) else None
    if (
        not receipt_recorded
        or not domain
        or public_url != f"https://{domain}/"
        or facts.get("no_payment_collected") is not True
        or facts.get("checkout_absent_verified") is not True
    ):
        return ""
    return "\n".join((
        f"Public URL: {public_url}",
        "No payment collected: verified",
        "Checkout absent: verified",
        f"Receipt: {expected_ref}",
    ))


def render_commerce_approval(
    job: dict[str, Any], gate: dict[str, Any], action: dict[str, Any]
) -> str:
    """Render only the exact, fingerprint-bound safe approval fields."""
    if not _approval_matches_plan(job, action):
        raise CommerceButtonError("approval_packet_mismatch")
    text = render_commerce_job(job)
    action_fp = str(action.get("action_fingerprint") or "")
    plan_fp = str(job.get("plan_fingerprint") or "")
    if (
        re.fullmatch(r"[0-9a-f]{64}", action_fp) is None
        or re.fullmatch(r"[0-9a-f]{64}", plan_fp) is None
    ):
        raise CommerceButtonError("approval_packet_mismatch")
    lines = [text, f"Proposed action: {_safe_action(action.get('action_type'))}"]
    if str(action.get("action_type") or "") in {
        "porkbun_register_domain",
        "porkbun_register_and_dns",
        "register_domain",
    }:
        renewal = _renewal_cents(job.get("plan"))
        lines.extend([
            "WHOIS privacy: on",
            "Auto-renew: on (recurring commitment)",
            "Renewal quote: USD {:.2f} per year".format(renewal / 100),
            "DNS included: apex A + AAAA and www CNAME to Shopify",
            "Domain registration is irreversible; DNS can be rolled back.",
        ])
    lines.extend([
        f"Action fingerprint: {action_fp[:16]}",
        f"Plan fingerprint: {plan_fp[:16]}",
        "Approve only the exact terms and action shown above.",
    ])
    return "\n".join(lines)


def _active_gate(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    job = snapshot["job"]
    current_gate_id = str(job.get("current_gate_id") or "")
    gates = snapshot["gates"]
    if current_gate_id:
        return next(
            (
                gate
                for gate in reversed(gates)
                if gate["gate_id"] == current_gate_id and gate["status"] == "open"
            ),
            None,
        )
    return next((gate for gate in reversed(gates) if gate["status"] == "open"), None)


def _parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _reminder_stage(snapshot: dict[str, Any], now: datetime) -> int:
    gate = _active_gate(snapshot)
    if (
        gate is None
        or snapshot["job"].get("current_state") != "awaiting_cal"
        or not isinstance(gate.get("opening_evidence"), dict)
        or not gate["opening_evidence"].get("entry_url")
    ):
        return 0
    opened = _parse_utc(gate.get("opened_at"))
    if opened is None:
        return 0
    age_seconds = (now - opened).total_seconds()
    if age_seconds >= 24 * 60 * 60:
        return 2
    return 1 if age_seconds >= 6 * 60 * 60 else 0


def _delivery_key(snapshot: dict[str, Any], now: datetime) -> str:
    events = [
        event
        for event in snapshot["events"]
        if event.get("event_type") not in _DELIVERY_IGNORED_EVENTS
    ]
    job = snapshot["job"]
    material = {
        "job_id": job["job_id"],
        "row_version": job["row_version"],
        "state": job["current_state"],
        "plan_fingerprint": job.get("plan_fingerprint", ""),
        "event_sequence": events[-1]["sequence"] if events else 0,
        "reminder_stage": _reminder_stage(snapshot, now),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _latest_delivery_key(snapshot: dict[str, Any]) -> str:
    for event in reversed(snapshot["events"]):
        if event.get("event_type") != "commerce_gateway_delivered":
            continue
        evidence = event.get("evidence")
        if isinstance(evidence, dict):
            key = str(evidence.get("delivery_key") or "")
            if re.fullmatch(r"[0-9a-f]{64}", key):
                return key
    return ""


def _gate_action(value: Any) -> str:
    action = str(value or "").strip()
    if not action or len(action) > 1_000 or any(ord(char) < 32 for char in action):
        return "Complete the private provider step."
    return action


def _gate_status_from_snapshot(
    store: CommerceJobStore, snapshot: dict[str, Any], *, actor: str
) -> str:
    job = snapshot["job"]
    gate = _active_gate(snapshot)
    if gate is None or gate["gate_type"] == "action_approval":
        return ""
    text = f"Action: {_gate_action(gate.get('human_action'))}"
    evidence = gate.get("opening_evidence")
    if (
        job.get("current_state") != "awaiting_cal"
        or job.get("current_gate_id") != gate["gate_id"]
        or not isinstance(evidence, dict)
        or not evidence.get("entry_url")
    ):
        return text
    try:
        public_url = attention_public_url()
    except AttentionError:
        return text
    _, token = store.issue_gate_handoff(gate["gate_id"], actor=actor)
    return f"{text}\n{public_url}/gate/{gate['gate_id']}?t={token}"


def commerce_gate_status(store: CommerceJobStore, job_id: str, *, actor: str) -> str:
    """Render the current gate and mint a fresh viewer link when applicable."""
    return _gate_status_from_snapshot(
        store, store.delivery_snapshot(job_id), actor=actor
    )


class GatewayCommerceWatcherMixin:
    """Small durable-ledger watcher mixed into GatewayRunner."""

    def _commerce_store(self) -> CommerceJobStore:
        store = getattr(self, "_commerce_job_store", None)
        if store is None:
            store = CommerceJobStore()
            store.initialize()
            self._commerce_job_store = store
        return store

    def _commerce_buttons(self) -> CommerceButtonStore:
        store = getattr(self, "_commerce_button_store", None)
        if store is None:
            store = CommerceButtonStore()
            self._commerce_button_store = store
        return store

    def _commerce_governance(self) -> CommercePurchaseGovernance:
        governance = getattr(self, "_commerce_purchase_governance", None)
        if governance is not None:
            return governance
        config = load_config_readonly()
        bridge_url = str(
            cfg_get(config, "intake", "base_url", default="") or ""
        ).strip()
        bridge_token = str(os.environ.get(TOKEN_ENV, "") or "").strip()
        if not bridge_url or not bridge_token:
            raise CommerceButtonError("governance_not_configured")
        governance = CommercePurchaseGovernance.from_runtime(
            bridge_url=bridge_url,
            bridge_token=bridge_token,
        )
        self._commerce_purchase_governance = governance
        return governance

    async def _commerce_watcher(self, interval: float = 2.0) -> None:
        while getattr(self, "_running", True):
            try:
                if commerce_enabled():
                    await self._commerce_watcher_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("commerce watcher tick failed safely")
            await asyncio.sleep(interval)

    async def _commerce_watcher_tick(
        self,
        store: CommerceJobStore | None = None,
        *,
        now: datetime | None = None,
    ) -> int:
        if not commerce_enabled() and store is None:
            return 0
        store = store or self._commerce_store()
        timestamp = now or datetime.now(timezone.utc)
        jobs = await asyncio.to_thread(store.list_jobs, active_only=False)
        approval_seen = getattr(self, "_commerce_approval_seen", None)
        if approval_seen is None:
            approval_seen = set()
            self._commerce_approval_seen = approval_seen
        delivered = 0
        for listed_job in jobs:
            job_id = listed_job["job_id"]
            snapshot = await asyncio.to_thread(store.delivery_snapshot, job_id)
            gate = _active_gate(snapshot)
            approval_gate_id = (
                gate["gate_id"]
                if gate is not None and gate["gate_type"] == "action_approval"
                else ""
            )
            key = _delivery_key(snapshot, timestamp)
            if _latest_delivery_key(snapshot) == key and (
                not approval_gate_id or approval_gate_id in approval_seen
            ):
                continue
            if await self._deliver_commerce_job(store, snapshot):
                await asyncio.to_thread(
                    store.record_delivery,
                    job_id,
                    key,
                    actor="gateway-commerce-watcher",
                )
                if approval_gate_id:
                    approval_seen.add(approval_gate_id)
                delivered += 1
        return delivered

    async def _deliver_commerce_job(
        self, store: CommerceJobStore, snapshot: dict[str, Any]
    ) -> bool:
        job = snapshot["job"]
        origin = await asyncio.to_thread(_origin_for, store, job["job_id"])
        chat_id = origin.get("chat_id")
        if not chat_id:
            return False
        metadata: dict[str, str] = {"job_id": job["job_id"]}
        if origin.get("thread_id"):
            metadata["thread_id"] = origin["thread_id"]
        if origin.get("message_id"):
            metadata["reply_to_message_id"] = origin["message_id"]

        gates = snapshot["gates"]
        active_gate = next(
            (gate for gate in reversed(gates) if gate["status"] == "open"),
            None,
        )
        if (
            active_gate
            and active_gate["gate_type"] == "action_approval"
            and job.get("current_state") in _APPROVAL_STATES
        ):
            return await self._deliver_commerce_approval(
                store, snapshot, active_gate, origin, metadata
            )

        text = render_commerce_job(job)
        completion = _completion_summary(snapshot)
        if completion:
            text = f"{text}\n\n{completion}"
        if (
            active_gate
            and job.get("current_state") == "awaiting_cal"
            and job.get("current_gate_id") == active_gate["gate_id"]
        ):
            gate_status = await asyncio.to_thread(
                _gate_status_from_snapshot,
                store,
                snapshot,
                actor="gateway-commerce-watcher",
            )
            if gate_status:
                text = f"{text}\n\n{gate_status}"

        target = DeliveryTarget(
            platform=Platform.TELEGRAM,
            chat_id=chat_id,
            thread_id=origin.get("thread_id"),
            is_explicit=True,
        )
        result = await self.delivery_router.deliver(
            text,
            [target],
            job_id=job["job_id"],
            metadata=metadata,
        )
        return bool(result.get(target.to_string(), {}).get("success"))

    async def _deliver_commerce_approval(
        self,
        store: CommerceJobStore,
        snapshot: dict[str, Any],
        gate: dict[str, Any],
        origin: dict[str, str],
        metadata: dict[str, str],
    ) -> bool:
        adapter = self.adapters.get(Platform.TELEGRAM)
        job = snapshot["job"]
        if adapter is None or not hasattr(adapter, "edit_commerce_approval_message"):
            return False
        actions = snapshot["actions"]
        action = next(
            (
                item
                for item in reversed(actions)
                if item["action_fingerprint"] == gate["approval_fingerprint"]
            ),
            None,
        )
        if action is None:
            return False
        try:
            text = render_commerce_approval(job, gate, action)
        except CommerceButtonError:
            logger.warning("commerce approval packet mismatch job=%s", job["job_id"])
            return False
        sent = await adapter.send(origin["chat_id"], text, metadata=metadata)
        message_id = getattr(sent, "message_id", None)
        if getattr(sent, "success", False) is not True or not message_id:
            return False
        user_id = job["requester"].split(":", 1)[1]
        tokens = self._commerce_buttons().mint_group(
            job_id=job["job_id"],
            gate_id=gate["gate_id"],
            action_fingerprint=action["action_fingerprint"],
            plan_fingerprint=job["plan_fingerprint"],
            expected_row_version=int(job["row_version"]),
            user_id=user_id,
            chat_id=origin["chat_id"],
            message_id=str(message_id),
            actions=("approve", "deny"),
        )
        edited = await adapter.edit_commerce_approval_message(
            chat_id=origin["chat_id"],
            message_id=str(message_id),
            text=text,
            button_rows=[[("Approve", tokens["approve"]), ("Deny", tokens["deny"])]],
        )
        if getattr(edited, "success", False) is not True:
            return False
        return True

    def handle_commerce_button_action(
        self,
        raw_token: str,
        *,
        user_id: str,
        chat_id: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Claim, persist, then consume one approval token without awaiting."""
        buttons = self._commerce_buttons()
        binding = buttons.resolve(
            raw_token,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
        )
        store = self._commerce_store()
        job = store.get_job(binding.job_id)
        gate = store.get_gate(binding.gate_id)
        actions = store.list_actions(binding.job_id)
        action = next(
            (
                item
                for item in reversed(actions)
                if item["action_fingerprint"] == gate["approval_fingerprint"]
            ),
            None,
        )
        if action is None:
            raise CommerceButtonError("authority_changed")
        if (
            job.get("current_state") not in _APPROVAL_STATES
            or gate.get("status") != "open"
            or gate.get("gate_type") != "action_approval"
        ):
            raise CommerceButtonError("authority_changed")
        if (
            action.get("action_type")
            in {
                "register_domain",
                "porkbun_register_domain",
                "porkbun_register_and_dns",
            }
            and job.get("current_state") != "awaiting_purchase_approval"
        ):
            raise CommerceButtonError("authority_changed")
        claim = buttons.claim(
            raw_token,
            job_id=binding.job_id,
            gate_id=binding.gate_id,
            action_fingerprint=action["action_fingerprint"],
            plan_fingerprint=job["plan_fingerprint"],
            row_version=int(job["row_version"]),
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
        )
        try:
            if not _approval_matches_plan(job, action):
                raise CommerceButtonError("approval_packet_mismatch")
            evidence: dict[str, Any] = {
                "provider_truth_verified": True,
                "approval_granted": claim.action == "approve",
            }
            if job["current_state"] == "awaiting_purchase_approval":
                if claim.action == "approve":
                    evidence.update(
                        self._commerce_governance().decide(
                            job=job,
                            action=action,
                            decision="approve",
                        )
                    )
                else:
                    evidence.update({
                        "domain": _domain(_action_input(action).get("domain")),
                        "action_fingerprint": action["action_fingerprint"],
                    })
            store.complete_gate(
                binding.gate_id,
                evidence=evidence,
                actor=f"telegram:{user_id}",
            )
        except (CommerceJobError, CommerceButtonError):
            buttons.release(claim)
            raise
        except Exception:
            buttons.release(claim)
            raise CommerceButtonError("governance_failed") from None
        buttons.complete(claim)
        return {
            "success": True,
            "approved": claim.action == "approve",
            "job_id": binding.job_id,
        }


__all__ = [
    "COMMERCE_ACCEPTANCE_SENTENCE",
    "GatewayCommerceWatcherMixin",
    "commerce_gate_status",
    "commerce_enabled",
    "is_commerce_acceptance_event",
    "render_commerce_approval",
    "render_commerce_job",
]

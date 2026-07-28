"""Bounded Linxio Gmail adapter for the shared Attention Queue."""

from __future__ import annotations

from typing import Any

from attention_telegram import deliver_attention_result
from hermes_attention import (
    AttentionError,
    REASON_CODES,
    prune_attention,
    resolve_attention_by_source,
    upsert_attention,
)


_RECOMMENDATIONS = {
    "blocked_category": "Decide the commercial response.",
    "missing_approved_fact": "Provide or approve the missing fact.",
    "conflicting_facts": "Resolve the conflicting facts.",
    "cross_customer_risk": "Review the thread for cross-customer contamination.",
    "unsupported_claim": "Review the unsupported claim.",
    "low_confidence": "Review the Gmail thread and decide the reply.",
    "unclear_sender": "Confirm the sender before replying.",
    "stale_existing_draft": "Review the stale Gmail draft.",
    "rate_limit": "Review the thread after the standing limit resets.",
    "processing_failure": "Review the safety hold and Gmail thread.",
    "malformed_model_output": "Review the thread; no safe draft was prepared.",
    "unsafe_promise": "Decide whether the requested promise is acceptable.",
    "thread_changed": "Review the newer Gmail thread state.",
    "reply_needed": "Review the Gmail thread or await draft-mode approval.",
}

_ALERT_REASONS = {
    "wrong_account": "wrong_account",
    "oauth_failure": "oauth_failure",
    "cogitator_bridge_failure": "cogitator_bridge_failure",
    "history_gap": "history_gap",
    "daily_limit_reached": "daily_limit_reached",
    "telegram_notification_failure": "notification_failure",
    "processing_failure": "processing_failure",
    "polling_delay": "polling_delay",
    "stale_checkpoint": "stale_checkpoint",
    "queue_stuck": "queue_stuck",
    "possible_duplicate_draft": "possible_duplicate_draft",
    "state_corruption": "state_corruption",
    "worker_overlap": "worker_overlap",
    "gmail_api_failure": "gmail_api_failure",
}

_URGENT_ALERTS = {
    "wrong_account",
    "history_gap",
    "possible_duplicate_draft",
    "state_corruption",
    "worker_overlap",
}


def _deliver(result: dict[str, Any]) -> str:
    return deliver_attention_result(result)


def upsert_gmail_outcome(
    *,
    thread_id: str,
    message_id: str,
    kind: str,
    subject: str,
    sender_name: str,
    company: str,
    received_time: str,
    category: str,
    reason: str,
    confidence: float | None,
    processing_version: str,
) -> str:
    if kind not in {"shadowed", "drafted", "decision_required"}:
        raise AttentionError("invalid_gmail_outcome")
    reason = reason if reason in REASON_CODES else "processing_failure"
    safety = reason in {"processing_failure", "cross_customer_risk"}
    status = (
        "prepared"
        if kind in {"shadowed", "drafted"}
        else "safety_hold"
        if safety
        else "needs_cal"
    )
    prefix = (
        "SHADOW ONLY — NO GMAIL DRAFT CREATED"
        if kind == "shadowed"
        else "Gmail draft prepared"
        if kind == "drafted"
        else "Needs you"
    )
    payload = {
        "source_type": "gmail",
        "source_record_id": thread_id,
        "source_event_id": message_id,
        "project": "linxio",
        "item_type": "automation_failure"
        if reason == "processing_failure"
        else "customer_email",
        "priority": "urgent"
        if reason == "cross_customer_risk"
        else "high"
        if kind != "shadowed"
        else "normal",
        "status": status,
        "title": f"{prefix} — {subject}",
        "safe_summary": (
            f"Email from {sender_name} at {company}. "
            f"Category: {category or 'unclear'}. Received: {received_time}."
        ),
        "recommended_action": _RECOMMENDATIONS.get(
            reason, "Review the Gmail thread and decide the next action."
        ),
        "waiting_on": "cal",
        "reason_code": reason,
        "confidence": confidence,
        "due_at": None,
        "source_deep_link": f"https://mail.google.com/mail/u/0/#inbox/{thread_id}",
        "prepared_artifact_deep_link": (
            f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"
            if kind == "drafted"
            else None
        ),
        "processing_version": processing_version,
    }
    try:
        result = upsert_attention(payload)
    except AttentionError as exc:
        if not exc.code.startswith(("unsafe_title", "unsafe_safe_summary")):
            raise
        payload["title"] = (
            "SHADOW ONLY — NO GMAIL DRAFT CREATED"
            if kind == "shadowed"
            else "Linxio email requires attention"
        )
        payload["safe_summary"] = (
            f"A bounded Linxio email outcome was classified as {category or 'unclear'}."
        )
        result = upsert_attention(payload)
    prune_attention()
    return _deliver(result)


def upsert_worker_alert(code: str, *, processing_version: str) -> str:
    reason = _ALERT_REASONS.get(code)
    if reason is None:
        raise AttentionError("invalid_worker_alert")
    project = (
        "linxio"
        if code
        in {
            "cogitator_bridge_failure",
            "daily_limit_reached",
            "possible_duplicate_draft",
        }
        else "system"
    )
    result = upsert_attention({
        "source_type": "system",
        "source_record_id": f"linxio-gmail-worker:{code}",
        "source_event_id": code,
        "project": project,
        "item_type": "automation_failure",
        "priority": "urgent" if code in _URGENT_ALERTS else "high",
        "status": "safety_hold",
        "title": f"Linxio Gmail safety hold — {code.replace('_', ' ')}",
        "safe_summary": "The Linxio Gmail worker entered a fail-closed safety state.",
        "recommended_action": "Review the worker status and resolve the underlying safety condition.",
        "waiting_on": "cal",
        "reason_code": reason,
        "confidence": None,
        "due_at": None,
        "source_deep_link": None,
        "prepared_artifact_deep_link": None,
        "processing_version": processing_version,
    })
    prune_attention()
    return _deliver(result)


def resolve_gmail_thread(thread_id: str, message_id: str) -> None:
    resolve_attention_by_source("gmail", thread_id, message_id)


def resolve_worker_alert(code: str, event_id: str) -> None:
    if code not in _ALERT_REASONS:
        raise AttentionError("invalid_worker_alert")
    resolve_attention_by_source(
        "system",
        f"linxio-gmail-worker:{code}",
        event_id,
        recovered=True,
    )

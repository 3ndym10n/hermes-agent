"""Bounded inline-review-button state and layout for Intelligent Second Brain.

Pure, Telegram-free logic: an opaque-token server-side store and the button
layout/footer for one saved review candidate. Callback_data carries only an
opaque token (``isb:<token>``) — never a review ID, action word, path, secret,
or source text. Every field the click handler must validate (review ID, action,
chat, user, message ID, expiry, nonce, consumed) lives here, server-side.

The five buttons map onto already-merged deterministic Cogitator review actions;
no model, provider, or paid research is ever invoked from a click.
"""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional

# Button action -> Cogitator review action (or "" for the local no-op).
APPROVE = "approve"
PENDING = "pending"
RESEARCH = "research"
ARCHIVE = "archive"
DETAILS = "details"

BUTTON_ACTIONS = (APPROVE, PENDING, RESEARCH, ARCHIVE, DETAILS)
# Actions whose token is single-use and, for terminal ones, removes the buttons.
_TERMINAL_ACTIONS = frozenset({APPROVE, ARCHIVE})
_CONSUMING_ACTIONS = frozenset({APPROVE, ARCHIVE, RESEARCH})

_REVIEW_ACTION = {
    APPROVE: "approve",
    RESEARCH: "request_explicit_research",
    ARCHIVE: "archive",
    DETAILS: "view_related",
}

_LABELS = {
    APPROVE: "✅ Approve",
    PENDING: "⏸ Leave Pending",
    RESEARCH: "🔎 Research",
    ARCHIVE: "🗄 Archive",
    DETAILS: "📄 Details",
}

DEFAULT_TTL_SECONDS = 24 * 60 * 60
MAX_TOKENS = 4096


@dataclass
class ReviewButtonEntry:
    token: str
    group_id: str
    review_id: str
    item_id: str
    action: str
    chat_id: str
    user_id: str
    nonce: str
    expiry: float
    message_id: str = ""
    consumed: bool = False


@dataclass
class ReviewButtonStore:
    """Bounded, TTL'd opaque-token store shared by the send and click sites."""

    ttl_seconds: int = DEFAULT_TTL_SECONDS
    capacity: int = MAX_TOKENS
    clock: Callable[[], float] = time.time
    _entries: "OrderedDict[str, ReviewButtonEntry]" = field(
        default_factory=OrderedDict
    )
    _groups: dict = field(default_factory=dict)

    def _evict(self, now: float) -> None:
        for token in [t for t, e in self._entries.items() if now >= e.expiry]:
            self._drop(token)
        while len(self._entries) > max(1, self.capacity):
            oldest, _ = next(iter(self._entries.items()))
            self._drop(oldest)

    def _drop(self, token: str) -> None:
        entry = self._entries.pop(token, None)
        if entry and entry.group_id in self._groups:
            self._groups[entry.group_id].discard(token)
            if not self._groups[entry.group_id]:
                self._groups.pop(entry.group_id, None)

    def mint_group(
        self,
        *,
        review_id: str,
        item_id: str,
        chat_id: str,
        user_id: str,
        actions,
        ttl_seconds: Optional[int] = None,
    ) -> dict:
        """Mint one opaque token per action; return {action: token}."""
        now = self.clock()
        self._evict(now)
        group_id = secrets.token_urlsafe(9)
        expiry = now + int(ttl_seconds or self.ttl_seconds)
        tokens: dict = {}
        self._groups[group_id] = set()
        for action in actions:
            token = secrets.token_urlsafe(16)
            self._entries[token] = ReviewButtonEntry(
                token=token,
                group_id=group_id,
                review_id=str(review_id),
                item_id=str(item_id),
                action=action,
                chat_id=str(chat_id),
                user_id=str(user_id),
                nonce=secrets.token_hex(8),
                expiry=expiry,
            )
            self._groups[group_id].add(token)
            tokens[action] = token
        self._evict(now)
        return tokens

    def bind_message(self, tokens, message_id: str) -> None:
        for token in tokens:
            entry = self._entries.get(token)
            if entry is not None:
                entry.message_id = str(message_id)

    def validate(self, token: str, *, user_id: str) -> tuple:
        """Return (state, entry). Does not mutate. States: ok, not_found,
        expired, wrong_user, already_handled."""
        entry = self._entries.get(str(token or ""))
        if entry is None:
            return ("not_found", None)
        if self.clock() >= entry.expiry:
            self._drop(entry.token)
            return ("expired", None)
        if str(entry.user_id) != str(user_id or ""):
            return ("wrong_user", entry)
        if entry.consumed:
            return ("already_handled", entry)
        return ("ok", entry)

    def consume(self, entry: ReviewButtonEntry) -> None:
        """Single-use terminal/research tokens; terminal also burns siblings."""
        if entry.action not in _CONSUMING_ACTIONS:
            return
        entry.consumed = True
        if entry.action in _TERMINAL_ACTIONS:
            for sibling in list(self._groups.get(entry.group_id, ())):
                sib = self._entries.get(sibling)
                if sib is not None:
                    sib.consumed = True

    def __len__(self) -> int:
        return len(self._entries)


def is_terminal(action: str) -> bool:
    return action in _TERMINAL_ACTIONS


def review_action_for(action: str) -> str:
    return _REVIEW_ACTION.get(action, "")


def recommended_action(
    *, disposition: str, content_type: str, final_state: str
) -> str:
    """Pick the one recommended button to emphasize."""
    disposition = str(disposition or "").strip().lower()
    content_type = str(content_type or "").strip().lower()
    if final_state == "partial_assessment":
        return PENDING
    if disposition in {"promotion_candidate", "create_playbook_candidate"}:
        return APPROVE
    if disposition in {"research_later", "request_explicit_research"}:
        return RESEARCH
    if disposition in {"ignore", "archive"} or content_type == "noise":
        return ARCHIVE
    if disposition in {"escalate_to_cal"}:
        return PENDING
    return APPROVE


def _clip(value, limit: int = 200) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_review_footer(assessment: dict, review_id: str) -> str:
    decision = (assessment or {}).get("decision") or {}
    disposition = str(decision.get("recommended_disposition") or "unknown")
    why = _clip(decision.get("why_it_matters") or "—")
    return "\n".join(
        [
            "—",
            f"RECOMMENDED DECISION: {disposition.replace('_', ' ')}",
            f"WHY: {why}",
            f"REVIEW ID: {review_id}",
        ]
    )


def strip_saved_to(message: str) -> str:
    """Drop the SAVED TO: <path> line — no filesystem paths in normal messages."""
    return "\n".join(
        line
        for line in str(message or "").splitlines()
        if not line.strip().upper().startswith("SAVED TO:")
    )


def button_layout(recommended: str) -> list:
    """Two rows [(label, action)], the recommended button emphasized with a star.

    Row 1: Approve, Leave Pending. Row 2: Research, Archive, Details.
    """
    def label(action: str) -> str:
        base = _LABELS[action]
        return f"⭐ {base}" if action == recommended else base

    return [
        [(label(APPROVE), APPROVE), (label(PENDING), PENDING)],
        [
            (label(RESEARCH), RESEARCH),
            (label(ARCHIVE), ARCHIVE),
            (label(DETAILS), DETAILS),
        ],
    ]

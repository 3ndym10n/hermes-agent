"""Deterministic Decision Inbox reply parser (no LLM, no slash command).

Inside an open Decision Inbox (after ``/decision_batch``), Cal replies in plain
text — ``research 3``, ``show 2``, ``refresh``, ``skip 1`` — and the gateway
routes those replies to the read-only cockpit instead of the model. This module
is *only* the parser: a pure string→intent function so the routing decision is
deterministic and unit-testable on its own. It carries no authority to act, makes
no network call, and only recognises a tightly-bounded reply shape so it never
hijacks ordinary chat (e.g. "research three papers on X" is not a match).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Verbs that take a 1-based inbox number, and the one bare verb (refresh).
NUMBERED_VERBS = ("research", "show", "skip")
BARE_VERBS = ("refresh",)
# approve_candidate is a two-token verb (number + mode) — handled separately.
APPROVE_CANDIDATE_VERB = "approve_candidate"
ALL_VERBS = NUMBERED_VERBS + BARE_VERBS + (APPROVE_CANDIDATE_VERB,)

# "research 3", "show #2", "skip 1" — verb, optional '#', digits, nothing else.
_NUMBERED_RE = re.compile(
    r"^(research|show|skip)\s+#?(\d{1,4})$", re.IGNORECASE
)
_BARE_RE = re.compile(r"^(refresh)$", re.IGNORECASE)
# "approve-candidate 3 preview" / "approve candidate #3 confirm". The prefix is
# distinctive enough to recognise as a command attempt inside an open cockpit;
# the tail is then classified (valid number+mode, bulk, or malformed) so Cal gets
# a precise message instead of the reply silently falling through to the model.
# Deliberately NOT matching bare "approve <n>" (collides with a promote verb).
_APPROVE_CANDIDATE_RE = re.compile(r"^approve[-\s]+candidate\s+(.+)$", re.IGNORECASE)
_APPROVE_VALID_TAIL_RE = re.compile(r"^#?(\d{1,4})\s+(preview|confirm)$", re.IGNORECASE)
_APPROVE_BULK_TAIL_RE = re.compile(r"^all(\s+(preview|confirm))?$", re.IGNORECASE)


@dataclass(frozen=True)
class InboxReply:
    """Parsed cockpit reply intent. ``number`` is None for bare verbs. ``mode`` is
    set only for ``approve_candidate``: ``preview`` | ``confirm`` for a valid
    command, ``all`` for a (rejected) bulk attempt, or ``None`` for a malformed
    approve-candidate reply (router renders usage)."""

    verb: str
    number: int | None = None
    mode: str | None = None


def parse_inbox_reply(text: str) -> InboxReply | None:
    """Parse one plain-text cockpit reply into intent, or None if it isn't one.

    Pure and strict: a slash command, free-form prose, or an unrecognised verb
    return None (those fall through to normal handling). ``<verb> <number>`` and
    bare ``refresh`` match exactly; ``approve-candidate``/``approve candidate``
    replies are recognised (valid / bulk / malformed) so the cockpit can answer
    them precisely."""
    s = str(text or "").strip()
    if not s or s.startswith("/"):
        return None
    m = _NUMBERED_RE.match(s)
    if m:
        return InboxReply(verb=m.group(1).lower(), number=int(m.group(2)))
    if _BARE_RE.match(s):
        return InboxReply(verb="refresh", number=None)
    ac = _APPROVE_CANDIDATE_RE.match(s)
    if ac:
        tail = ac.group(1).strip()
        valid = _APPROVE_VALID_TAIL_RE.match(tail)
        if valid:
            return InboxReply(APPROVE_CANDIDATE_VERB, int(valid.group(1)), valid.group(2).lower())
        if _APPROVE_BULK_TAIL_RE.match(tail):
            return InboxReply(APPROVE_CANDIDATE_VERB, None, "all")  # bulk → rejected
        return InboxReply(APPROVE_CANDIDATE_VERB, None, None)       # malformed → usage
    return None


__all__ = [
    "InboxReply", "parse_inbox_reply",
    "NUMBERED_VERBS", "BARE_VERBS", "ALL_VERBS", "APPROVE_CANDIDATE_VERB",
]

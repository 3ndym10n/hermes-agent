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
ALL_VERBS = NUMBERED_VERBS + BARE_VERBS

# "research 3", "show #2", "skip 1" — verb, optional '#', digits, nothing else.
_NUMBERED_RE = re.compile(
    r"^(research|show|skip)\s+#?(\d{1,4})$", re.IGNORECASE
)
_BARE_RE = re.compile(r"^(refresh)$", re.IGNORECASE)


@dataclass(frozen=True)
class InboxReply:
    """Parsed cockpit reply intent. ``number`` is None for bare verbs."""

    verb: str
    number: int | None = None


def parse_inbox_reply(text: str) -> InboxReply | None:
    """Parse one plain-text cockpit reply into intent, or None if it isn't one.

    Pure and strict: a slash command, free-form prose, or a verb with trailing
    words all return None (those fall through to normal handling). Only an exact
    ``<verb> <number>`` or bare ``refresh`` matches."""
    s = str(text or "").strip()
    if not s or s.startswith("/"):
        return None
    m = _NUMBERED_RE.match(s)
    if m:
        return InboxReply(verb=m.group(1).lower(), number=int(m.group(2)))
    if _BARE_RE.match(s):
        return InboxReply(verb="refresh", number=None)
    return None


__all__ = ["InboxReply", "parse_inbox_reply", "NUMBERED_VERBS", "BARE_VERBS", "ALL_VERBS"]

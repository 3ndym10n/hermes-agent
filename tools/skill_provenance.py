"""Skill write-origin provenance — ContextVar for distinguishing agent-sediment skill writes from foreground user-directed writes.

The curator only consolidates/prunes skills it autonomously created via the
background self-improvement review fork. Skills a user asks a foreground
agent to write belong to the user and must never be auto-curated.

This module exposes a ContextVar that run_agent.py sets before each tool
loop so tool handlers (e.g. skill_manage create) can check whether they
are executing inside the background-review fork.

The signal piggybacks on AIAgent._memory_write_origin, which is already
set to "background_review" for review-fork instances (see
_spawn_background_review in run_agent.py) and defaults to "assistant_tool"
for normal (foreground) agents.

Usage:
    from tools.skill_provenance import (
        set_current_write_origin,
        reset_current_write_origin,
        get_current_write_origin,
    )

    token = set_current_write_origin("background_review")
    try:
        ...  # tool runs here
    finally:
        reset_current_write_origin(token)

    # inside a tool:
    if get_current_write_origin() == "background_review":
        mark_agent_created(skill_name)
"""

import contextlib
import contextvars


_write_origin: contextvars.ContextVar[str] = contextvars.ContextVar(
    "skill_write_origin",
    default="foreground",
)

# Skill-write protection — explicit opt-in for the curator/self-improvement
# flows that are allowed to mutate ~/.hermes/skills/. Normal conversational,
# operator, cron, and reflective/planning agent runs never set this, so the
# skill-write gate fails them closed. See docs/skill-write-protection-v0.md.
_writes_allowed: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "skill_writes_allowed",
    default=False,
)

# The sentinel value the background review fork uses; mirrors
# run_agent.py's AIAgent._memory_write_origin override in
# _spawn_background_review().
BACKGROUND_REVIEW = "background_review"


def set_current_write_origin(origin: str) -> contextvars.Token[str]:
    """Bind the active write origin to the current context.

    Returns a Token the caller must pass to reset_current_write_origin
    in a finally block.
    """
    return _write_origin.set(origin or "foreground")


def reset_current_write_origin(token: contextvars.Token[str]) -> None:
    """Restore the prior write origin context."""
    _write_origin.reset(token)


def get_current_write_origin() -> str:
    """Return the active write origin.

    Default: "foreground" — any tool call made by a regular (non-review)
    agent, from the CLI, the gateway, cron, or a subagent.

    "background_review" — the self-improvement review fork; only skills
    created under this origin should be marked agent-created for curator
    management.
    """
    return _write_origin.get()


def is_background_review() -> bool:
    """Convenience: True iff the current write origin is the background
    review fork."""
    return get_current_write_origin() == BACKGROUND_REVIEW


@contextlib.contextmanager
def allow_skill_writes():
    """Mark the current context as an explicit curator/self-improvement flow
    that may write skill files.

    Wrap the curator's review pass (and any other deliberate skill-write
    entry point) in this so the skill-write gate lets it through. Runs in the
    same thread as the wrapped tool loop, so the ContextVar is visible to the
    skill_manage tool calls made inside it. Normal agent runs never enter this
    context, so their skill writes fail closed.
    """
    token = _writes_allowed.set(True)
    try:
        yield
    finally:
        _writes_allowed.reset(token)


def skill_writes_allowed() -> bool:
    """True iff the current context is an explicit curator/self-improvement
    flow allowed to write skill files.

    Allowed: the background self-improvement review fork
    (``is_background_review()``), and any context wrapped in
    ``allow_skill_writes()`` (the curator pass). Everything else — foreground
    conversational/operator runs, cron jobs, reflective/planning answers,
    subagents — is denied.
    """
    return _writes_allowed.get() or is_background_review()

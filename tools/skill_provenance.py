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
import os
import re
from pathlib import Path


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

# Set once at the start of every top-level interactive turn. This is intent,
# not permission: an explicit request may only stage a proposed skill change
# for later approval. Raw file/terminal writes never consult this signal.
_explicit_skill_write_request: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "explicit_skill_write_request",
    default=False,
)

_SKILL_TARGET_RE = r"(?:skill(?:s)?|SKILL\.md)"
_SKILL_MUTATION_RE = (
    r"(?:improve|change|update|edit|modify|create|write|rewrite|patch|remove|"
    r"delete|restore|revert)"
)
_EXPLICIT_SKILL_WRITE_RE = re.compile(
    rf"^\s*(?:(?:please|can you|could you|would you|i (?:want|need) you to)\s+)?"
    rf"(?:{_SKILL_MUTATION_RE}\b[^\n]{{0,160}}\b{_SKILL_TARGET_RE}\b|"
    rf"{_SKILL_TARGET_RE}\b[^\n]{{0,160}}\b{_SKILL_MUTATION_RE}\b)",
    re.IGNORECASE,
)
_NEGATED_SKILL_WRITE_RE = re.compile(
    rf"\b(?:do not|don't|never|must not|without)\b[^\n]{{0,120}}"
    rf"(?:{_SKILL_MUTATION_RE}\b[^\n]{{0,120}}\b{_SKILL_TARGET_RE}\b|"
    rf"{_SKILL_TARGET_RE}\b[^\n]{{0,120}}\b{_SKILL_MUTATION_RE}\b)",
    re.IGNORECASE,
)
_INTERACTIVE_GATEWAY_PLATFORMS = frozenset({
    "telegram", "discord", "whatsapp", "whatsapp_cloud", "slack", "signal",
    "mattermost", "matrix", "homeassistant", "email", "sms", "dingtalk",
    "feishu", "wecom", "wecom_callback", "weixin", "bluebubbles", "qqbot",
    "yuanbao",
})

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
    """True only inside an explicit curator or approved replay context.

    Background review, ordinary foreground turns, cron jobs, and subagents are
    denied. User intent is deliberately separate: it can stage a proposal for
    approval through ``skill_manage`` but cannot authorize a direct write.
    """
    return _writes_allowed.get()


def explicit_skill_write_requested() -> bool:
    """Whether this trusted top-level turn explicitly requested a skill edit."""
    return _explicit_skill_write_request.get()


def is_explicit_skill_write_request(message: str) -> bool:
    """Recognize a direct request to mutate a skill, excluding prohibitions."""
    text = str(message or "").strip()
    if not text or _NEGATED_SKILL_WRITE_RE.search(text):
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    while lines and lines[0].startswith(("@", "/")):
        lines.pop(0)
    candidate = lines[0] if lines else ""
    return bool(_EXPLICIT_SKILL_WRITE_RE.search(candidate))


def bind_explicit_skill_write_request(
    message: str,
    *,
    origin: str,
    platform: str,
    parent_session_id: str = "",
) -> bool:
    """Overwrite per-turn skill-write intent and return the bound value.

    Only a top-level interactive user turn can bind intent. Background,
    subagent, cron, batch, webhook, and API turns always reset it to false.
    """
    platform_value = getattr(platform, "value", platform)
    surface = str(platform_value or "").strip().lower()
    interactive = surface in _INTERACTIVE_GATEWAY_PLATFORMS
    if os.environ.get("HERMES_INTERACTIVE") == "1":
        interactive = interactive or surface in {"", "cli", "tui", "acp", "local"}
    allowed = bool(
        origin != BACKGROUND_REVIEW
        and not parent_session_id
        and interactive
        and is_explicit_skill_write_request(message)
    )
    _explicit_skill_write_request.set(allowed)
    return allowed


def skills_root() -> Path:
    """Absolute, resolved path of the skills directory (``~/.hermes/skills``)."""
    from hermes_constants import get_hermes_home
    return (get_hermes_home() / "skills").resolve()


def path_targets_skills(path) -> bool:
    """True iff *path* resolves to the skills directory or a file/dir under it.

    Used by the raw-write guard (file_write/patch/terminal) to recognize a
    write aimed at ~/.hermes/skills/ so it can be held to the same gate as
    skill_manage. Resolution failures return False (caller falls through to its
    normal handling).
    """
    try:
        rp = Path(str(path)).expanduser().resolve()
    except Exception:
        return False
    try:
        root = skills_root()
    except Exception:
        return False
    return rp == root or root in rp.parents

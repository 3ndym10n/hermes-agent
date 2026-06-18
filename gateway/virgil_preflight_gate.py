"""Deterministic Virgil repository-task preflight gate.

A fail-closed gate that runs *before* any model/planner/tool activity in
``BasePlatformAdapter.handle_message``. For a qualifying ``/repo <task>`` Telegram
message it loads the Cogitator preflight builder, builds a read-only packet, renders
a four-section message, and delivers it to Cal. Only on a fully successful delivery
does the caller allow normal processing (and therefore ``_message_handler``) to run.

Any failure -> a deterministic four-section failure notice is attempted (best effort)
and the gate returns ``False`` so the caller returns *without* calling
``_message_handler``. Detection is deterministic and local — no LLM classifier.

Trigger parsing lives in exactly one function (:func:`parse_repo_command`) so trigger
detection and task extraction can never diverge.
"""

from __future__ import annotations

import enum
import hashlib
import importlib.util
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes.virgil_preflight")

DEFAULT_ROOT = "/home/v0id/Projects/Cogitator_clean"
_BUILDER_FILE = "cogitator_virgil_preflight.py"
_PACKET_TYPE = "virgil_preflight_v0"

# Bare sibling modules the builder imports; we verify each resolves from the
# *selected* Cogitator root rather than silently reusing a module from another root.
_REQUIRED_SIBLINGS = (
    "cogitator_learning_retrieval",
    "cogitator_pre_build_pattern_lookup",
    "cogitator_skill_library",
)


# --------------------------------------------------------------------------- #
# 1. Single deterministic trigger parser                                      #
# --------------------------------------------------------------------------- #
class RepoOutcome(enum.Enum):
    NO_MATCH = "no_match"
    MISSING_TASK = "missing_task"
    MATCHED = "matched"


@dataclass
class RepoParse:
    outcome: RepoOutcome
    task: str = ""


# /<cmd>[@bot][whitespace<task...>]  — DOTALL so tabs/newlines after the command count.
_REPO_RE = re.compile(r"^/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?(?:[ \t\r\n]+(.*))?$", re.DOTALL)


def parse_repo_command(text: Optional[str], bot_username: Optional[str] = None) -> RepoParse:
    """The *only* trigger logic. Returns NO_MATCH / MISSING_TASK / MATCHED(+task).

    Handles ``/repo task``, ``/repo@actual_bot task``, extra spaces, tabs/newlines
    after the command, bare ``/repo`` (MISSING_TASK), ``/repository`` (NO_MATCH), and
    ordinary text (NO_MATCH). A present ``@suffix`` must match ``bot_username`` (the
    current bot); an unverifiable suffix is NO_MATCH so we never hijack another bot.
    """
    s = (text or "").lstrip()
    m = _REPO_RE.match(s)
    if not m:
        return RepoParse(RepoOutcome.NO_MATCH)
    cmd, addressed, rest = m.group(1).lower(), m.group(2), m.group(3)
    if cmd != "repo":  # "/repository" -> cmd == "repository" -> no match
        return RepoParse(RepoOutcome.NO_MATCH)
    if addressed is not None:
        bot = (bot_username or "").lstrip("@").lower()
        if not bot or addressed.lower() != bot:
            return RepoParse(RepoOutcome.NO_MATCH)
    task = (rest or "").strip()
    if not task:
        return RepoParse(RepoOutcome.MISSING_TASK)
    return RepoParse(RepoOutcome.MATCHED, task)


# --------------------------------------------------------------------------- #
# 2. Safe explicit importlib loader (no permanent sys.path mutation)          #
# --------------------------------------------------------------------------- #
class PreflightError(Exception):
    """Internal gate error carrying a stable sanitized reason code."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


_CACHE: Optional[tuple] = None  # (build_fn, render_fn, failures_dir, patterns_dir)
_RECORDED_MODULES: set[str] = set()  # exact module keys we added during a successful exec


def _load_preflight() -> tuple:
    """Load + cache (build_fn, render_fn, failures_dir, patterns_dir) from COGITATOR_REPO_ROOT.

    Uses ``spec_from_file_location`` with a SHA-256-derived unique module name so a
    module from a different root can never be reused via ``sys.modules``. The
    Cogitator root is prepended to ``sys.path`` only during ``exec_module`` and
    restored in ``finally``. Required sibling imports must resolve from the selected
    root or we fail closed with ``PREFLIGHT_IMPORT_FAILED``.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    root = Path(os.environ.get("COGITATOR_REPO_ROOT", DEFAULT_ROOT)).resolve()
    if not root.is_dir():
        raise PreflightError("COGITATOR_ROOT_UNAVAILABLE", f"root not a dir: {root}")
    builder_path = (root / _BUILDER_FILE).resolve()
    if not builder_path.is_file():
        raise PreflightError("COGITATOR_ROOT_UNAVAILABLE", f"builder missing: {builder_path}")

    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    mod_name = f"cogitator_virgil_preflight__{digest}"

    spec = importlib.util.spec_from_file_location(mod_name, str(builder_path))
    if spec is None or spec.loader is None:
        raise PreflightError("PREFLIGHT_IMPORT_FAILED", "spec/loader missing")

    module = importlib.util.module_from_spec(spec)
    before = set(sys.modules)  # snapshot BEFORE exec so we can record only newcomers
    saved_path = list(sys.path)  # restore exactly in finally
    recorded: set[str] = set()
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))  # only during exec (builder uses bare sibling imports)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)

        newly = (set(sys.modules) - before) | {mod_name}
        recorded = {n for n in newly if n == mod_name or n.startswith("cogitator_")}

        # Provenance gate: each required sibling must resolve from THIS root.
        for sib in _REQUIRED_SIBLINGS:
            sib_mod = sys.modules.get(sib)
            sib_file = getattr(sib_mod, "__file__", None) if sib_mod else None
            try:
                ok = bool(sib_file) and Path(sib_file).resolve().is_relative_to(root)
            except Exception:
                ok = False
            if not ok:
                raise PreflightError(
                    "PREFLIGHT_IMPORT_FAILED",
                    f"sibling {sib} provenance != {root} (file={sib_file})",
                )
    except PreflightError:
        _purge(newly_only(before, mod_name))
        raise
    except Exception as e:
        _purge(newly_only(before, mod_name))
        raise PreflightError("PREFLIGHT_IMPORT_FAILED", repr(e))
    finally:
        sys.path[:] = saved_path

    build_fn = getattr(module, "build_virgil_preflight_packet", None)
    render_fn = getattr(module, "render_virgil_preflight_message", None)
    if not callable(build_fn) or not callable(render_fn):
        _purge(recorded)
        raise PreflightError("PREFLIGHT_IMPORT_FAILED", "builder/renderer not callable")

    _RECORDED_MODULES.update(recorded)
    _CACHE = (build_fn, render_fn, root / "failures", root / "operating_patterns")
    return _CACHE


def newly_only(before: set[str], mod_name: str) -> set[str]:
    """Module keys added since ``before`` that belong to Cogitator (plus the builder)."""
    added = (set(sys.modules) - before) | {mod_name}
    return {n for n in added if n == mod_name or n.startswith("cogitator_")}


def _purge(names: set[str]) -> None:
    for n in names:
        sys.modules.pop(n, None)


def reset_preflight_cache_for_tests() -> None:
    """Test-only seam. Drops the cache and removes ONLY the exact module keys recorded
    as newly imported during a successful exec (the SHA-named builder + its newly
    imported ``cogitator_*`` siblings). Never purges by directory, so an alternate-root
    reload re-imports siblings from the new root instead of reusing a prior import."""
    global _CACHE
    _CACHE = None
    _purge(set(_RECORDED_MODULES))
    _RECORDED_MODULES.clear()


# --------------------------------------------------------------------------- #
# 3. Hermes-owned deterministic four-section failure formatter                #
# --------------------------------------------------------------------------- #
_REASONS = {
    "MISSING_TASK": "No task was provided after /repo.",
    "COGITATOR_ROOT_UNAVAILABLE": "The Cogitator preflight environment is unavailable.",
    "PREFLIGHT_IMPORT_FAILED": "The Cogitator preflight module could not be loaded.",
    "PREFLIGHT_BUILD_FAILED": "The preflight packet could not be built.",
    "PREFLIGHT_RENDER_FAILED": "The preflight packet could not be rendered.",
    "PREFLIGHT_DELIVERY_FAILED": "The preflight message could not be delivered.",
}

_EMERGENCY = (
    "⚠️ Virgil preflight failed. Repository task halted before any model or "
    "tool action. No changes were made."
)


def format_failure_message(code: str, task: str) -> str:
    """Four sections: Status / Task / Failure / Required Action. Sanitized reason codes
    only — raw exception text never appears in the outgoing message."""
    reason = _REASONS.get(code, "Preflight failed for an unknown reason.")
    task_line = (task or "").strip()[:300] or "(none)"
    return (
        "⚠️ Virgil Preflight Failed\n\n"
        "Status\nHalted before any model or tool action. No changes were made.\n\n"
        f"Task\n{task_line}\n\n"
        f"Failure\n{code}: {reason}\n\n"
        "Required Action\nResolve the issue above and resend your /repo request."
    )


def _send_kwargs(adapter, event) -> dict:
    """Reply/metadata identical to Hermes' own user-visible sends (base.py:3997-4004)."""
    from gateway.platforms.base import (
        _mark_notify_metadata,
        _reply_anchor_for_event,
        _thread_metadata_for_source,
    )

    reply_to = _reply_anchor_for_event(event)
    metadata = _mark_notify_metadata(_thread_metadata_for_source(event.source, reply_to))
    return {"reply_to": reply_to, "metadata": metadata}


async def _send_failure(adapter, event, code: str, task: str) -> None:
    """Best-effort delivery of the four-section failure notice. Never raises."""
    try:
        try:
            body = format_failure_message(code, task)
        except Exception:
            logger.exception("[virgil_preflight] failure formatter raised; using emergency text")
            body = _EMERGENCY  # fixed one-liner ONLY if the formatter itself fails
        res = await adapter._send_with_retry(
            chat_id=event.source.chat_id, content=body, **_send_kwargs(adapter, event)
        )
        if not getattr(res, "success", False):
            logger.error(
                "[virgil_preflight] failure-packet delivery returned failure: %s",
                getattr(res, "error", None),
            )
    except Exception:
        logger.exception("[virgil_preflight] failure-packet delivery raised; suppressed")


# --------------------------------------------------------------------------- #
# 4. Gate orchestration                                                       #
# --------------------------------------------------------------------------- #
async def run_gate(adapter, event, parsed: RepoParse) -> bool:
    """Run the preflight for a parsed ``/repo`` event. Returns True only when the
    packet was built, rendered, AND delivered (``success=True``). Returns False on
    every failure (after a best-effort failure notice). NEVER raises.

    ``parsed`` is the single pre-computed parse from the caller, so trigger detection
    and task extraction cannot diverge.
    """
    task = parsed.task
    try:
        if parsed.outcome is RepoOutcome.MISSING_TASK:
            raise PreflightError("MISSING_TASK")

        build_fn, render_fn, failures_dir, patterns_dir = _load_preflight()

        try:
            packet = build_fn(
                task, failures_dir=str(failures_dir), patterns_dir=str(patterns_dir)
            )
        except Exception as e:
            raise PreflightError("PREFLIGHT_BUILD_FAILED", repr(e))
        if not isinstance(packet, dict) or packet.get("packet_type") != _PACKET_TYPE:
            raise PreflightError("PREFLIGHT_BUILD_FAILED", "invalid packet result")

        try:
            message = render_fn(packet)
            if not isinstance(message, str) or not message.strip():
                raise ValueError("empty render")
        except Exception as e:
            raise PreflightError("PREFLIGHT_RENDER_FAILED", repr(e))

        try:
            result = await adapter._send_with_retry(
                chat_id=event.source.chat_id, content=message, **_send_kwargs(adapter, event)
            )
        except Exception as e:  # success-packet send RAISES
            raise PreflightError("PREFLIGHT_DELIVERY_FAILED", repr(e))
        if not getattr(result, "success", False):  # success-packet send returns success=False
            raise PreflightError("PREFLIGHT_DELIVERY_FAILED", getattr(result, "error", "") or "")

        return True

    except PreflightError as pe:
        logger.warning("[virgil_preflight] gate failed code=%s detail=%s", pe.code, pe.detail)
        await _send_failure(adapter, event, pe.code, task)
        return False
    except Exception:  # defensive catch-all -> still fail closed, never reach the model
        logger.exception("[virgil_preflight] unexpected gate error")
        await _send_failure(adapter, event, "PREFLIGHT_BUILD_FAILED", task)
        return False

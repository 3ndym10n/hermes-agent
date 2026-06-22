"""Independent validator + scoped FIX loop for the backend automation pipeline.

The validator **never executes code**.  It reads a :class:`WorkerEvidence`
packet (the raw diff / test / policy evidence the worker emits) and returns a
deterministic :class:`Verdict`: a GREEN / YELLOW / RED classification plus a
PASS / FIX / STOP decision.  It holds **no reference to the worker**, so the
executor cannot validate or approve itself — independence is structural.

Decision model (deterministic, evidence-only):

* **PASS / GREEN** — a PR is open, every changed file is inside the allow-list
  and clear of forbidden/protected surfaces, all prescribed tests passed, the
  task risk is GREEN, and no deployment is required.  Only this is
  ``merge_eligible``.
* **FIX / YELLOW** — a *recoverable* problem on a GREEN-risk task: tests failed,
  the worker strayed outside the allow-list (but touched nothing
  forbidden/protected), it timed out, or it produced no changes.  The validator
  returns scoped fix instructions; the loop retries at most twice.
* **STOP / RED** — anything unsafe or needing a human: a forbidden/protected
  surface was touched, deployment is required, the task risk is YELLOW or RED,
  the live-worker sandbox gate fired, the packet was invalid, no worker was
  available, or two FIX rounds were exhausted.

Auto-merge is permitted only for ``merge_eligible`` verdicts; the caller still
checks CI-green + CLEAN + unchanged head SHA at merge time.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .task_packet import WorkerTaskPacket

_PASS = "PASS"
_FIX = "FIX"
_STOP = "STOP"

_GREEN = "GREEN"
_YELLOW = "YELLOW"
_RED = "RED"


@dataclass
class Verdict:
    """The validator's deterministic decision about one evidence packet."""

    decision: str  # PASS | FIX | STOP
    classification: str  # GREEN | YELLOW | RED
    merge_eligible: bool
    reasons: list[str] = field(default_factory=list)
    fix_instructions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "classification": self.classification,
            "merge_eligible": self.merge_eligible,
            "reasons": list(self.reasons),
            "fix_instructions": list(self.fix_instructions),
        }


def _as_dict(evidence: Any) -> dict:
    """Accept a WorkerEvidence, a dict, or anything with ``to_dict``."""

    if isinstance(evidence, dict):
        return evidence
    to_dict = getattr(evidence, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    # Last resort: read attributes off the object.
    return {
        k: getattr(evidence, k)
        for k in (
            "status",
            "allow_list_result",
            "forbidden_surface_scan",
            "tests",
            "risk_class",
            "deployment_required",
            "changed_files",
        )
        if hasattr(evidence, k)
    }


class EvidenceValidator:
    """Deterministic, executor-independent validator over evidence packets."""

    def validate(self, evidence: Any) -> Verdict:
        ev = _as_dict(evidence)
        status = str(ev.get("status") or "")
        risk = str(ev.get("risk_class") or "").upper()
        deployment_required = bool(ev.get("deployment_required"))
        allow = ev.get("allow_list_result") or {}
        forbidden = ev.get("forbidden_surface_scan") or {}
        tests = ev.get("tests") or []

        protected_hits = list(
            allow.get("protected_hits") or forbidden.get("protected_hits") or []
        )
        forbidden_hits = list(
            allow.get("forbidden_hits") or forbidden.get("forbidden_hits") or []
        )
        disallowed = list(allow.get("disallowed") or [])
        all_allowed = bool(allow.get("all_allowed"))
        failed_tests = [t for t in tests if not t.get("passed", False)]

        # ---- Hard STOP / RED: unsafe or human-only, regardless of anything else.
        red_reasons: list[str] = []
        if deployment_required:
            red_reasons.append("deployment required — Cal must approve any deploy")
        if protected_hits:
            red_reasons.append(
                "protected surface(s) touched: " + ", ".join(protected_hits)
            )
        if forbidden_hits:
            red_reasons.append(
                "forbidden surface(s) touched: " + ", ".join(forbidden_hits)
            )
        if status == "blocked_no_sandbox":
            red_reasons.append(
                "live worker blocked: sandbox/safe mode not enabled (Cal)"
            )
        if risk in (_YELLOW, _RED):
            red_reasons.append(f"task risk classified {risk} — requires Cal")
        if status == "invalid_packet":
            red_reasons.append("invalid task packet — cannot proceed")
        if status == "no_worker":
            red_reasons.append("no backend worker available — Cal must provide one")

        if red_reasons:
            return Verdict(
                decision=_STOP,
                classification=_RED if risk != _YELLOW else _YELLOW,
                merge_eligible=False,
                reasons=red_reasons,
            )

        # From here the task risk is GREEN and nothing forbidden was touched.

        # ---- PASS / GREEN: a clean, mergeable PR.
        if (
            status == "pr_opened"
            and all_allowed
            and not failed_tests
            and not disallowed
        ):
            return Verdict(
                decision=_PASS,
                classification=_GREEN,
                merge_eligible=True,
                reasons=["GREEN: PR open, allow-list clean, all tests passed"],
            )

        # ---- FIX / YELLOW: recoverable problems on a GREEN-risk task.
        fix_reasons: list[str] = []
        fix_instructions: list[str] = []

        if status == "blocked_policy" and disallowed:
            fix_reasons.append(
                "changed files outside the allow-list: " + ", ".join(disallowed)
            )
            fix_instructions.append(
                "Restrict your edits to the allowed files only; do not modify "
                + ", ".join(disallowed)
            )
        if failed_tests:
            cmds = ", ".join(str(t.get("cmd")) for t in failed_tests)
            fix_reasons.append(f"tests failing: {cmds}")
            fix_instructions.append(f"Make these tests pass: {cmds}")
        if status == "blocked_timeout":
            fix_reasons.append("worker timed out")
            fix_instructions.append(
                "The task timed out — reduce scope and make the smallest change "
                "that satisfies the success condition."
            )
        if status == "no_changes":
            fix_reasons.append("worker produced no changes")
            fix_instructions.append(
                "No file changes were made — implement the objective in the "
                "allowed files so the success condition and tests are met."
            )

        if fix_instructions:
            return Verdict(
                decision=_FIX,
                classification=_YELLOW,
                merge_eligible=False,
                reasons=fix_reasons,
                fix_instructions=fix_instructions,
            )

        # ---- Anything else (unknown/ambiguous state): stop and ask Cal.
        return Verdict(
            decision=_STOP,
            classification=_RED,
            merge_eligible=False,
            reasons=[f"unrecognized or non-mergeable evidence state: status={status!r}"],
        )


def build_fix_packet(packet: WorkerTaskPacket, verdict: Verdict) -> WorkerTaskPacket:
    """Return a follow-up packet carrying the validator's scoped fix instructions.

    Only the objective is augmented (with the precise instructions); the
    allow-list, forbidden surfaces, tests, risk and rollback are unchanged, so a
    FIX attempt cannot widen the worker's safety boundary.
    """

    instructions = "; ".join(verdict.fix_instructions) or "address the reported issues"
    new_objective = (
        f"{packet.objective}\n\n"
        f"FIX ATTEMPT — a previous attempt was not accepted. {instructions}"
    )
    return replace(packet, objective=new_objective)


@dataclass
class FixLoopResult:
    """Outcome of a scoped FIX loop."""

    final_evidence: Any
    final_verdict: Verdict
    fix_rounds: int
    history: list[tuple[Any, Verdict]] = field(default_factory=list)


def run_fix_loop(
    worker: Any,
    packet: WorkerTaskPacket,
    validator: EvidenceValidator | None = None,
    *,
    max_fix_rounds: int = 2,
) -> FixLoopResult:
    """Execute → validate → (scoped FIX, at most ``max_fix_rounds``) → STOP.

    ``worker`` is any object with ``execute(packet) -> evidence``.  On a FIX
    verdict the loop re-runs the worker with a fix-augmented packet, up to
    ``max_fix_rounds`` times; if it is still not accepted, the result becomes a
    STOP ("two failed FIX rounds") for Cal.  The loop never merges or deploys.
    """

    validator = validator or EvidenceValidator()
    history: list[tuple[Any, Verdict]] = []

    current_packet = packet
    evidence = worker.execute(current_packet)
    verdict = validator.validate(evidence)
    history.append((evidence, verdict))

    rounds = 0
    while verdict.decision == _FIX and rounds < max_fix_rounds:
        rounds += 1
        current_packet = build_fix_packet(current_packet, verdict)
        evidence = worker.execute(current_packet)
        verdict = validator.validate(evidence)
        history.append((evidence, verdict))

    if verdict.decision == _FIX:
        # Exhausted the FIX budget without acceptance — escalate to Cal.
        verdict = Verdict(
            decision=_STOP,
            classification=_RED,
            merge_eligible=False,
            reasons=[
                f"{rounds} FIX rounds exhausted without a PASS — escalate to Cal",
                *verdict.reasons,
            ],
        )

    return FixLoopResult(
        final_evidence=evidence,
        final_verdict=verdict,
        fix_rounds=rounds,
        history=history,
    )


__all__ = [
    "Verdict",
    "EvidenceValidator",
    "FixLoopResult",
    "build_fix_packet",
    "run_fix_loop",
]

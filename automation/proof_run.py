"""Day-2 proof runner for ``/auto_dev run``.

Composes the EXISTING bounded pipeline — :class:`~automation.worker.BackendWorker`
→ :class:`~automation.validator.EvidenceValidator` →
:class:`~automation.automerge.AutoMerger` — for a single GREEN, deterministic,
non-LLM proof task. It adds **no new policy**: every component already fails
closed. A live coding model (Claude/Codex/GPT) is never invoked here; the only
worker this path will run is the deterministic *proof worker*, which writes one
fixed Markdown artifact into the isolated worktree.

Gated three ways and default-OFF (all must be explicitly enabled):

* ``backend_automation.command_enabled``
* ``backend_automation.live_execution_enabled``
* ``backend_automation.proof_mode``

The proof worker is intentionally trivial — it exists to exercise the
worktree → tests → PR → validator → GREEN/PASS merge path end-to-end without a
model. Real coding workers remain gated behind the worker's own
``allow_live_workers`` rail (kept ``False`` here).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .adapters import FakeWorkerAdapter, _scrub_auth
from .automerge import AutoMerger
from .task_packet import WorkerTaskPacket
from .validator import EvidenceValidator
from .worker import BackendWorker

# Worker kinds that shell out to a live coding model. Never allowed in proof mode.
LIVE_WORKER_KINDS = frozenset({"auto", "claude", "codex"})
# The only worker kind this path will run: a deterministic, non-LLM worker.
PROOF_WORKER_KIND = "fake"
# The single artifact the proof worker writes. The packet's allow-list must
# permit it; the worker/validator still enforce the real protected-path policy.
PROOF_ARTIFACT_PATH = "docs/backend_automation/proof.md"
# Evidence lands on the Cogitator control issue, not via a Cal relay.
EVIDENCE_ISSUE = ("3ndym10n/Cogitator", 846)


def _truthy(value: Any) -> bool:
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    value = (config or {}).get("backend_automation", {})
    return value if isinstance(value, Mapping) else {}


def _allowed_repo_paths(config: Mapping[str, Any]) -> dict[str, str]:
    """Resolve ``{repo_key: local_path}`` from config.

    Only the mapping form carries paths; the bare-list form (names only) yields
    no usable paths and the worker fails closed on an unknown repo.
    """
    raw = _section(config).get("allowed_repos", {})
    if isinstance(raw, Mapping):
        return {str(k): str(v) for k, v in raw.items() if str(v or "").strip()}
    return {}


def _proof_content(packet: WorkerTaskPacket) -> str:
    digest = hashlib.sha256(
        f"{packet.objective}\n{packet.success_condition}".encode("utf-8")
    ).hexdigest()[:12]
    return (
        "# Backend Automation Proof\n\n"
        "Written by the deterministic proof worker. No live coding model ran.\n\n"
        f"- objective_digest: `{digest}`\n"
        f"- risk: {packet.risk_classification}\n"
    )


def build_proof_adapter(packet: WorkerTaskPacket) -> FakeWorkerAdapter:
    """A deterministic, non-LLM worker that writes one fixed Markdown artifact."""

    content = _proof_content(packet)

    def _apply(workdir: str) -> None:
        target = Path(workdir) / PROOF_ARTIFACT_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    return FakeWorkerAdapter(
        apply=_apply, stdout=f"proof worker wrote {PROOF_ARTIFACT_PATH}"
    )


@dataclass
class ProofRunResult:
    """Outcome of a ``/auto_dev run`` proof attempt."""

    refused: bool
    reason: str
    evidence: dict = field(default_factory=dict)
    verdict: dict = field(default_factory=dict)
    merge: dict = field(default_factory=lambda: {"merged": False, "reason": "not attempted"})


def _pr_number(pr_url: str | None) -> int | None:
    m = re.search(r"/pull/(\d+)", str(pr_url or ""))
    return int(m.group(1)) if m else None


def _default_evidence_publisher(target: tuple[str, int], body: str) -> None:
    repo, number = target
    subprocess.run(  # noqa: S603 - fixed argv shape
        ["gh", "issue", "comment", str(number), "--repo", repo, "--body", body],
        text=True,
        capture_output=True,
        check=False,
    )


def render_evidence_comment(result: ProofRunResult) -> str:
    ev = result.evidence
    return _scrub_auth(
        "### Backend automation proof run (Day 2)\n\n"
        f"- worker status: `{ev.get('status')}`\n"
        f"- worker: `{ev.get('worker_name')}`\n"
        f"- PR: {ev.get('pr_url') or '(none)'}\n"
        f"- validator: {result.verdict.get('decision')} / "
        f"{result.verdict.get('classification')} "
        f"(merge_eligible={result.verdict.get('merge_eligible')})\n"
        f"- merged: {result.merge.get('merged')} — {result.merge.get('reason')}\n"
        f"- changed files: {', '.join(ev.get('changed_files') or []) or '(none)'}\n\n"
        "Deterministic proof worker; no live coding model was run. No deploy."
    )


def run_proof_task(
    raw_payload: Any,
    config: Mapping[str, Any],
    *,
    worker: Any | None = None,
    validator: Any | None = None,
    merger: Any | None = None,
    evidence_publisher: Callable[[tuple[str, int], str], None] | None = None,
) -> ProofRunResult:
    """Run one GREEN deterministic proof packet through the existing pipeline.

    Returns a :class:`ProofRunResult`; never raises. The ``worker``/``validator``/
    ``merger``/``evidence_publisher`` seams exist so tests never touch git,
    GitHub, or the network.
    """

    section = _section(config)

    # --- Triple gate, default OFF.
    if not _truthy(section.get("command_enabled", False)):
        return ProofRunResult(True, "backend automation command disabled")
    if not _truthy(section.get("live_execution_enabled", False)):
        return ProofRunResult(True, "live execution not enabled")
    if not _truthy(section.get("proof_mode", False)):
        return ProofRunResult(True, "proof mode not enabled")

    # --- Parse the packet.
    if isinstance(raw_payload, Mapping):
        payload: Any = dict(raw_payload)
    else:
        try:
            payload = json.loads(raw_payload)
        except (TypeError, json.JSONDecodeError):
            return ProofRunResult(True, "invalid task-packet JSON")
    if not isinstance(payload, dict):
        return ProofRunResult(True, "task packet must be a JSON object")

    # --- Reuse the dry-run validator: GREEN + repo allow-listed + protected-clean
    # + structurally valid + tests present, in one shot.
    from plugins.auto_dev.command import validate_task_packet

    review = validate_task_packet(payload, config)
    if not review["policy_preview_eligible"]:
        detail = "; ".join(review["errors"]) or (
            "not GREEN / repo / protected-path / tests gate not satisfied"
        )
        return ProofRunResult(
            True, f"packet not GREEN-safe: {detail}", verdict={"preview": review}
        )

    packet = WorkerTaskPacket.from_dict(payload)

    # --- Only the deterministic proof worker may run; refuse live model workers.
    if packet.worker_kind != PROOF_WORKER_KIND:
        return ProofRunResult(
            True,
            f"worker_kind {packet.worker_kind!r} refused — proof mode runs only the "
            f"deterministic '{PROOF_WORKER_KIND}' worker (no live Claude/Codex/GPT)",
        )

    # --- Resolve the repo path; fail closed if not configured with a path.
    repo_paths = _allowed_repo_paths(config)
    if packet.repo not in repo_paths:
        return ProofRunResult(
            True,
            f"repo {packet.repo!r} has no configured local path — set "
            "backend_automation.allowed_repos as a name->path mapping",
        )

    # --- Compose the EXISTING pipeline. Live coding workers stay hard-disabled.
    validator = validator or EvidenceValidator()
    if worker is None:
        worker = BackendWorker(
            repo_paths,
            adapters=[build_proof_adapter(packet)],
            allow_live_workers=False,
        )
    merger = merger or AutoMerger(allowed_repos=set(repo_paths))
    publish = evidence_publisher or _default_evidence_publisher

    evidence = worker.execute(packet)
    ev = evidence.to_dict() if hasattr(evidence, "to_dict") else dict(evidence)
    verdict = validator.validate(evidence)

    merge_result: dict = {"merged": False, "reason": "validator not merge-eligible"}
    if verdict.merge_eligible:
        pr_number = _pr_number(ev.get("pr_url"))
        if pr_number is None:
            merge_result = {"merged": False, "reason": "no PR number in evidence"}
        else:
            decision = merger.evaluate_and_merge(
                packet.repo, pr_number, evidence, verdict, ev.get("head_sha", "")
            )
            merge_result = (
                decision.to_dict() if hasattr(decision, "to_dict") else dict(decision)
            )

    result = ProofRunResult(
        refused=False,
        reason=ev.get("status", ""),
        evidence=ev,
        verdict=verdict.to_dict(),
        merge=merge_result,
    )

    # --- Post evidence to GitHub (#846); best-effort, never via Cal relay.
    try:
        publish(EVIDENCE_ISSUE, render_evidence_comment(result))
    except Exception:  # noqa: BLE001 - evidence posting must not crash the run
        pass

    return result


__all__ = [
    "PROOF_ARTIFACT_PATH",
    "PROOF_WORKER_KIND",
    "LIVE_WORKER_KINDS",
    "ProofRunResult",
    "build_proof_adapter",
    "render_evidence_comment",
    "run_proof_task",
]

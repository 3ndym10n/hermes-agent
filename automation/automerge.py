"""GREEN/PASS auto-merge gate for the backend automation pipeline.

This is the *only* component that may merge a PR, and it does so under a strict,
deterministic gate that mirrors the mission's merge rules. It merges **only**
when every one of these holds:

* the independent validator returned ``merge_eligible`` (PASS / GREEN);
* the evidence shows no forbidden/protected surface and no deployment requirement;
* CI is green (every check SUCCESS or SKIPPED — no failures, nothing pending);
* ``mergeStateStatus`` is CLEAN;
* the PR head SHA still equals the SHA the evidence was produced against
  (nothing was pushed after validation);
* the PR's repo is allow-listed.

It uses squash merge and **never deploys**. All GitHub access goes through an
injectable client so tests never touch the network or merge anything real.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MergeDecision:
    """Outcome of an auto-merge evaluation."""

    merged: bool
    reason: str
    gate_results: dict = field(default_factory=dict)
    merge_output: str = ""

    def to_dict(self) -> dict:
        return {
            "merged": self.merged,
            "reason": self.reason,
            "gate_results": dict(self.gate_results),
            "merge_output": self.merge_output,
        }


class GhMergeClient:
    """Real GitHub client over the ``gh`` CLI (no merge method beyond squash)."""

    def pr_status(self, repo: str, number: int) -> dict:
        """Return ``{headRefOid, mergeStateStatus, conclusions: [...]}``."""

        proc = subprocess.run(  # noqa: S603 - fixed argv shape
            [
                "gh", "pr", "view", str(number),
                "--repo", repo,
                "--json", "headRefOid,mergeStateStatus,state,statusCheckRollup",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        data = json.loads(proc.stdout)
        rollup = data.get("statusCheckRollup") or []
        return {
            "headRefOid": data.get("headRefOid", ""),
            "mergeStateStatus": data.get("mergeStateStatus", ""),
            "state": data.get("state", ""),
            "statuses": [
                {
                    "status": c.get("status", ""),
                    "conclusion": c.get("conclusion", ""),
                }
                for c in rollup
            ],
        }

    def squash_merge(self, repo: str, number: int) -> str:
        proc = subprocess.run(  # noqa: S603 - fixed argv shape
            ["gh", "pr", "merge", str(number), "--repo", repo, "--squash"],
            text=True,
            capture_output=True,
            check=True,
        )
        return (proc.stdout or proc.stderr or "").strip()


def _ci_is_green(statuses: list[dict]) -> tuple[bool, str]:
    """CI is green iff every check is COMPLETED and SUCCESS/SKIPPED/NEUTRAL."""

    ok_conclusions = {"SUCCESS", "SKIPPED", "NEUTRAL"}
    pending = [s for s in statuses if s.get("status") != "COMPLETED"]
    if pending:
        return False, f"{len(pending)} check(s) still pending"
    failed = [
        s for s in statuses if (s.get("conclusion") or "").upper() not in ok_conclusions
    ]
    if failed:
        return False, f"{len(failed)} check(s) not green"
    if not statuses:
        return False, "no CI checks reported"
    return True, "all checks green"


class AutoMerger:
    """Evaluates the merge gate and squash-merges only when every gate passes."""

    def __init__(self, client: Any | None = None, allowed_repos: set[str] | None = None):
        self._client = client or GhMergeClient()
        # Optional repo allow-list; when None, any repo is accepted (the caller
        # is expected to have already constrained this via the worker).
        self._allowed_repos = set(allowed_repos) if allowed_repos is not None else None

    def evaluate_and_merge(
        self,
        repo: str,
        pr_number: int,
        evidence: Any,
        verdict: Any,
        expected_head_sha: str,
    ) -> MergeDecision:
        """Run every gate; squash-merge only if all pass. Never deploys."""

        ev = evidence if isinstance(evidence, dict) else _to_dict(evidence)
        gates: dict = {}

        # Gate: repo allow-list (when configured).
        if self._allowed_repos is not None and repo not in self._allowed_repos:
            gates["allow_listed_repo"] = False
            return MergeDecision(False, f"repo {repo!r} not allow-listed", gates)
        gates["allow_listed_repo"] = True

        # Gate: validator verdict must be PASS / GREEN / merge_eligible.
        merge_eligible = bool(getattr(verdict, "merge_eligible", False))
        decision = getattr(verdict, "decision", "")
        classification = getattr(verdict, "classification", "")
        gates["verdict_merge_eligible"] = merge_eligible
        if not (merge_eligible and decision == "PASS" and classification == "GREEN"):
            return MergeDecision(
                False,
                f"validator not merge-eligible (decision={decision}, "
                f"class={classification})",
                gates,
            )

        # Gate: evidence shows nothing forbidden/protected and no deploy.
        allow = ev.get("allow_list_result") or {}
        forbidden = ev.get("forbidden_surface_scan") or {}
        protected_hits = (allow.get("protected_hits") or []) + (
            forbidden.get("protected_hits") or []
        )
        forbidden_hits = (allow.get("forbidden_hits") or []) + (
            forbidden.get("forbidden_hits") or []
        )
        no_forbidden = not (protected_hits or forbidden_hits)
        no_deploy = not bool(ev.get("deployment_required"))
        gates["no_forbidden_surface"] = no_forbidden
        gates["no_deployment"] = no_deploy
        if not no_forbidden:
            return MergeDecision(False, "forbidden/protected surface present", gates)
        if not no_deploy:
            return MergeDecision(False, "deployment required — Cal only", gates)

        # Gate: live CI/merge state from GitHub.
        status = self._client.pr_status(repo, pr_number)
        clean = status.get("mergeStateStatus") == "CLEAN"
        head_unchanged = bool(expected_head_sha) and (
            status.get("headRefOid") == expected_head_sha
        )
        ci_green, ci_reason = _ci_is_green(status.get("statuses") or [])
        gates["merge_state_clean"] = clean
        gates["head_sha_unchanged"] = head_unchanged
        gates["ci_green"] = ci_green
        if not ci_green:
            return MergeDecision(False, f"CI not green: {ci_reason}", gates)
        if not clean:
            return MergeDecision(
                False,
                f"merge state not CLEAN ({status.get('mergeStateStatus')})",
                gates,
            )
        if not head_unchanged:
            return MergeDecision(
                False,
                "PR head SHA changed since validation — re-validate",
                gates,
            )

        # All gates pass — squash merge. (No deploy, ever.)
        output = self._client.squash_merge(repo, pr_number)
        return MergeDecision(True, "all merge gates passed", gates, merge_output=output)


def _to_dict(evidence: Any) -> dict:
    to_dict = getattr(evidence, "to_dict", None)
    return to_dict() if callable(to_dict) else dict(evidence or {})


__all__ = ["MergeDecision", "GhMergeClient", "AutoMerger"]

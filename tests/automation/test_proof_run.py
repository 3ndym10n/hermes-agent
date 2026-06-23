"""Tests for the Day-2 deterministic proof runner (``/auto_dev run``).

Covers the gated, non-LLM proof path that composes the existing
worker → validator → auto-merge pipeline. All git/GitHub access is stubbed, so
these tests never touch the network or merge anything real.
"""

from __future__ import annotations

import pytest

from automation.automerge import AutoMerger, GhMergeClient, MergeDecision
from automation.proof_run import (
    PROOF_ARTIFACT_PATH,
    build_proof_adapter,
    run_proof_task,
)
from automation.task_packet import WorkerTaskPacket
from automation.worker import BackendWorker, GhPublisher, WorkerEvidence


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _green_packet(**over):
    packet = {
        "objective": "Write the backend automation proof artifact",
        "success_condition": "proof.md exists under docs/backend_automation",
        "repo": "hermes",
        "allowed_files": ["docs/backend_automation/proof.md"],
        "forbidden_surfaces": ["storage/**", ".env*", "**/*.service"],
        "tests": ["true"],
        "rollback_boundary": "Revert the PR commit",
        "risk_classification": "GREEN",
        "approval_boundary": "PR only; no deploy",
        "worker_kind": "fake",
    }
    packet.update(over)
    return packet


def _full_config(repos=None):
    return {
        "backend_automation": {
            "command_enabled": True,
            "live_execution_enabled": True,
            "proof_mode": True,
            "allowed_repos": repos
            or {"hermes": "/tmp/hermes-proof", "cogitator": "/tmp/cog"},
        }
    }


def _evidence(
    *,
    status="pr_opened",
    risk="GREEN",
    all_allowed=True,
    tests=None,
    pr_url="https://github.com/3ndym10n/hermes-agent/pull/99",
    changed=None,
    protected=None,
    forbidden=None,
    disallowed=None,
    deployment=False,
):
    scan = {
        "all_allowed": all_allowed,
        "disallowed": disallowed or [],
        "forbidden_hits": forbidden or [],
        "protected_hits": protected or [],
    }
    return WorkerEvidence(
        task_packet={},
        repo="hermes",
        base_branch="main",
        base_sha="aaaaaaa",
        head_branch="auto/proof",
        head_sha="bbbbbbb",
        changed_files=changed or ["docs/backend_automation/proof.md"],
        diffstat="",
        tests=tests
        if tests is not None
        else [{"cmd": "true", "returncode": 0, "passed": True}],
        allow_list_result=scan,
        forbidden_surface_scan=scan,
        risk_class=risk,
        rollback="revert",
        deployment_required=deployment,
        worker_name="fake",
        timed_out=False,
        status=status,
        pr_url=pr_url,
        notes=[],
    )


class _StubWorker:
    def __init__(self, evidence):
        self._evidence = evidence
        self.calls = []

    def execute(self, packet):
        self.calls.append(packet)
        return self._evidence


class _SpyMerger:
    def __init__(self, decision):
        self._decision = decision
        self.calls = []

    def evaluate_and_merge(self, repo, pr_number, evidence, verdict, expected_head_sha):
        self.calls.append((repo, pr_number, expected_head_sha))
        return self._decision


# ---------------------------------------------------------------------------
# Gating: run refused unless explicitly enabled
# ---------------------------------------------------------------------------


def test_run_refused_by_default():
    result = run_proof_task(_green_packet(), {})
    assert result.refused
    assert "command disabled" in result.reason


def test_run_refused_when_live_execution_off():
    config = _full_config()
    config["backend_automation"]["live_execution_enabled"] = False
    result = run_proof_task(_green_packet(), config)
    assert result.refused
    assert "live execution not enabled" in result.reason


def test_run_refused_when_proof_mode_off():
    config = _full_config()
    config["backend_automation"]["proof_mode"] = False
    result = run_proof_task(_green_packet(), config)
    assert result.refused
    assert "proof mode not enabled" in result.reason


@pytest.mark.parametrize("kind", ["auto", "claude", "codex"])
def test_run_refuses_live_worker_kinds(kind):
    result = run_proof_task(_green_packet(worker_kind=kind), _full_config())
    assert result.refused
    assert kind in result.reason
    assert "deterministic" in result.reason


# ---------------------------------------------------------------------------
# Policy: invalid repo / protected paths / non-GREEN are rejected before running
# ---------------------------------------------------------------------------


def test_run_rejects_repo_outside_allow_list():
    result = run_proof_task(_green_packet(repo="evil-repo"), _full_config())
    assert result.refused
    assert "repo is not allow-listed" in result.reason


def test_run_rejects_protected_paths():
    result = run_proof_task(_green_packet(allowed_files=[".env"]), _full_config())
    assert result.refused
    assert "protected" in result.reason


def test_run_rejects_non_green_risk():
    result = run_proof_task(_green_packet(risk_classification="YELLOW"), _full_config())
    assert result.refused
    assert "GREEN" in result.reason


# ---------------------------------------------------------------------------
# Happy path: a GREEN deterministic packet reaches worker → validator → merge
# ---------------------------------------------------------------------------


def test_green_proof_packet_reaches_worker_validator_merge():
    worker = _StubWorker(_evidence())
    merger = _SpyMerger(MergeDecision(True, "all merge gates passed"))
    posted = []

    result = run_proof_task(
        _green_packet(),
        _full_config(),
        worker=worker,
        merger=merger,
        evidence_publisher=lambda target, body: posted.append((target, body)),
    )

    assert not result.refused
    assert len(worker.calls) == 1  # worker was driven
    assert result.verdict["decision"] == "PASS"
    assert result.verdict["classification"] == "GREEN"
    assert len(merger.calls) == 1  # merge gate was consulted
    assert merger.calls[0][1] == 99  # PR number parsed from evidence pr_url
    assert result.merge["merged"] is True
    # Evidence posted to GitHub (#846), not relayed through Cal.
    assert posted and posted[0][0] == ("3ndym10n/Cogitator", 846)


# ---------------------------------------------------------------------------
# Safety: YELLOW / RED / FIX / STOP evidence never auto-merges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evidence",
    [
        _evidence(risk="YELLOW"),  # STOP / YELLOW
        _evidence(
            status="blocked_policy", all_allowed=False, forbidden=["deploy.service"]
        ),  # STOP / RED (forbidden surface)
        _evidence(deployment=True),  # STOP / RED (deploy required)
        _evidence(
            status="blocked_policy", all_allowed=False, disallowed=["src/app.py"]
        ),  # FIX (strayed outside allow-list)
        _evidence(
            tests=[{"cmd": "pytest", "returncode": 1, "passed": False}]
        ),  # FIX (failing tests)
    ],
)
def test_non_green_evidence_never_merges(evidence):
    worker = _StubWorker(evidence)
    merger = _SpyMerger(MergeDecision(True, "should-not-be-called"))

    result = run_proof_task(
        _green_packet(),
        _full_config(),
        worker=worker,
        merger=merger,
        evidence_publisher=lambda target, body: None,
    )

    assert merger.calls == []  # merge gate never invoked
    assert result.merge["merged"] is False


# ---------------------------------------------------------------------------
# Deterministic proof worker
# ---------------------------------------------------------------------------


def test_proof_adapter_writes_artifact_deterministically(tmp_path):
    packet = WorkerTaskPacket.from_dict(_green_packet())

    d1 = tmp_path / "wt1"
    d1.mkdir()
    res = build_proof_adapter(packet).run(packet, str(d1), "prompt")
    artifact = d1 / PROOF_ARTIFACT_PATH
    assert artifact.exists()
    assert "proof worker wrote" in res.stdout

    # Same packet, fresh worktree -> byte-identical artifact (deterministic).
    d2 = tmp_path / "wt2"
    d2.mkdir()
    build_proof_adapter(packet).run(packet, str(d2), "prompt")
    assert (d2 / PROOF_ARTIFACT_PATH).read_text() == artifact.read_text()


# ---------------------------------------------------------------------------
# No deploy path exists anywhere in the merge/publish surface
# ---------------------------------------------------------------------------


def test_no_deploy_path_exists():
    for obj in (AutoMerger, GhMergeClient, BackendWorker, GhPublisher):
        names = [n for n in dir(obj) if not n.startswith("__")]
        assert not any("deploy" in n.lower() for n in names), obj

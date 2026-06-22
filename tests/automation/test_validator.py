"""Tests for the independent validator + scoped FIX loop.

The validator is pure and evidence-only — it never runs code and holds no
reference to the worker, so the executor cannot validate itself. The FIX loop is
driven here by a scripted fake worker (no real CLI, no network).
"""

from __future__ import annotations

from automation import (
    EvidenceValidator,
    WorkerTaskPacket,
    build_fix_packet,
    run_fix_loop,
)
from automation.validator import Verdict


# ---------------------------------------------------------------------------
# Evidence builders
# ---------------------------------------------------------------------------


def _evidence(**overrides) -> dict:
    base = {
        "status": "pr_opened",
        "risk_class": "GREEN",
        "deployment_required": False,
        "allow_list_result": {
            "all_allowed": True,
            "disallowed": [],
            "forbidden_hits": [],
            "protected_hits": [],
        },
        "forbidden_surface_scan": {
            "all_allowed": True,
            "disallowed": [],
            "forbidden_hits": [],
            "protected_hits": [],
        },
        "tests": [{"cmd": "pytest", "returncode": 0, "passed": True}],
        "changed_files": ["src/app.py"],
        "pr_url": "https://example.test/pr/1",
    }
    base.update(overrides)
    return base


def _packet(**overrides) -> WorkerTaskPacket:
    defaults = dict(
        objective="Add multiply",
        success_condition="multiply works",
        repo="sample",
        allowed_files=("src/**",),
        forbidden_surfaces=(),
        tests=("pytest",),
        rollback_boundary="revert",
        risk_classification="GREEN",
        approval_boundary="auto",
        worker_kind="fake",
    )
    defaults.update(overrides)
    return WorkerTaskPacket(**defaults)


V = EvidenceValidator()


# ---------------------------------------------------------------------------
# Validator verdicts
# ---------------------------------------------------------------------------


def test_clean_green_pr_passes_and_is_merge_eligible():
    verdict = V.validate(_evidence())
    assert verdict.decision == "PASS"
    assert verdict.classification == "GREEN"
    assert verdict.merge_eligible is True


def test_failing_tests_is_fix_yellow_with_instructions():
    ev = _evidence(tests=[{"cmd": "pytest -k x", "returncode": 1, "passed": False}])
    verdict = V.validate(ev)
    assert verdict.decision == "FIX"
    assert verdict.classification == "YELLOW"
    assert verdict.merge_eligible is False
    assert any("pytest -k x" in i for i in verdict.fix_instructions)


def test_disallowed_only_block_is_fix():
    ev = _evidence(
        status="blocked_policy",
        allow_list_result={
            "all_allowed": False,
            "disallowed": ["tests/other.py"],
            "forbidden_hits": [],
            "protected_hits": [],
        },
    )
    verdict = V.validate(ev)
    assert verdict.decision == "FIX"
    assert any("allow-list" in r for r in verdict.reasons)


def test_protected_hit_is_stop_red():
    ev = _evidence(
        status="blocked_policy",
        allow_list_result={
            "all_allowed": False,
            "disallowed": ["config.yaml"],
            "forbidden_hits": [],
            "protected_hits": ["config.yaml"],
        },
    )
    verdict = V.validate(ev)
    assert verdict.decision == "STOP"
    assert verdict.classification == "RED"
    assert verdict.merge_eligible is False


def test_deployment_required_is_stop():
    verdict = V.validate(_evidence(deployment_required=True))
    assert verdict.decision == "STOP"
    assert verdict.classification == "RED"


def test_blocked_no_sandbox_is_stop():
    verdict = V.validate(_evidence(status="blocked_no_sandbox"))
    assert verdict.decision == "STOP"
    assert any("sandbox" in r for r in verdict.reasons)


def test_yellow_and_red_task_risk_stop():
    assert V.validate(_evidence(risk_class="YELLOW")).decision == "STOP"
    assert V.validate(_evidence(risk_class="RED")).decision == "STOP"
    # YELLOW risk keeps a YELLOW classification (not RED).
    assert V.validate(_evidence(risk_class="YELLOW")).classification == "YELLOW"


def test_no_worker_and_invalid_packet_stop():
    assert V.validate(_evidence(status="no_worker")).decision == "STOP"
    assert V.validate(_evidence(status="invalid_packet")).decision == "STOP"


def test_timeout_and_no_changes_are_fix():
    assert V.validate(_evidence(status="blocked_timeout")).decision == "FIX"
    assert V.validate(_evidence(status="no_changes")).decision == "FIX"


def test_merge_eligible_only_for_pass_green():
    # A protected hit must never be merge-eligible even if other fields look ok.
    ev = _evidence(
        allow_list_result={
            "all_allowed": False,
            "disallowed": [],
            "forbidden_hits": [],
            "protected_hits": ["secrets.txt"],
        }
    )
    assert V.validate(ev).merge_eligible is False


# ---------------------------------------------------------------------------
# build_fix_packet
# ---------------------------------------------------------------------------


def test_build_fix_packet_augments_objective_only():
    packet = _packet()
    verdict = Verdict(
        decision="FIX",
        classification="YELLOW",
        merge_eligible=False,
        fix_instructions=["Make these tests pass: pytest"],
    )
    fixed = build_fix_packet(packet, verdict)
    assert "FIX ATTEMPT" in fixed.objective
    assert "pytest" in fixed.objective
    # Safety boundary is unchanged — a FIX cannot widen scope.
    assert fixed.allowed_files == packet.allowed_files
    assert fixed.forbidden_surfaces == packet.forbidden_surfaces
    assert fixed.risk_classification == packet.risk_classification


# ---------------------------------------------------------------------------
# FIX loop
# ---------------------------------------------------------------------------


class ScriptedWorker:
    """A fake worker that returns a queued evidence dict per execute() call."""

    def __init__(self, evidences: list[dict]) -> None:
        self._queue = list(evidences)
        self.calls: list[WorkerTaskPacket] = []

    def execute(self, packet: WorkerTaskPacket):
        self.calls.append(packet)
        # Repeat the last evidence if the loop calls more than scripted.
        return self._queue.pop(0) if len(self._queue) > 1 else self._queue[0]


def test_fix_loop_passes_after_one_fix():
    worker = ScriptedWorker(
        [
            _evidence(tests=[{"cmd": "pytest", "returncode": 1, "passed": False}]),
            _evidence(),  # second attempt is clean
        ]
    )
    result = run_fix_loop(worker, _packet())
    assert result.final_verdict.decision == "PASS"
    assert result.fix_rounds == 1
    assert len(worker.calls) == 2
    # The retry carried fix instructions in its objective.
    assert "FIX ATTEMPT" in worker.calls[1].objective


def test_fix_loop_stops_after_two_failed_rounds():
    failing = _evidence(tests=[{"cmd": "pytest", "returncode": 1, "passed": False}])
    worker = ScriptedWorker([failing, failing, failing])
    result = run_fix_loop(worker, _packet(), max_fix_rounds=2)
    assert result.final_verdict.decision == "STOP"
    assert result.fix_rounds == 2
    assert any("FIX rounds exhausted" in r for r in result.final_verdict.reasons)
    # 1 initial + 2 fix attempts.
    assert len(worker.calls) == 3


def test_fix_loop_passes_immediately_no_fix():
    worker = ScriptedWorker([_evidence()])
    result = run_fix_loop(worker, _packet())
    assert result.final_verdict.decision == "PASS"
    assert result.fix_rounds == 0
    assert len(worker.calls) == 1


def test_fix_loop_stops_immediately_on_protected_hit_no_retry():
    worker = ScriptedWorker(
        [
            _evidence(
                status="blocked_policy",
                allow_list_result={
                    "all_allowed": False,
                    "disallowed": ["config.yaml"],
                    "forbidden_hits": [],
                    "protected_hits": ["config.yaml"],
                },
            )
        ]
    )
    result = run_fix_loop(worker, _packet())
    assert result.final_verdict.decision == "STOP"
    assert result.fix_rounds == 0
    assert len(worker.calls) == 1  # no fix attempt on an unsafe STOP


def test_validator_takes_only_evidence_no_worker_reference():
    # Structural independence: validate's signature accepts evidence alone.
    import inspect

    params = list(inspect.signature(EvidenceValidator.validate).parameters)
    assert params == ["self", "evidence"]

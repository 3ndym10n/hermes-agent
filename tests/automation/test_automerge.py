"""Tests for the GREEN/PASS auto-merge gate.

A fake GitHub client records calls and returns canned PR status — no network, no
real merge. The gate must merge ONLY when the validator verdict is merge-eligible
AND CI is green AND merge state is CLEAN AND the head SHA is unchanged AND the
evidence is clean of forbidden surfaces.
"""

from __future__ import annotations

from automation import AutoMerger
from automation.validator import Verdict


HEAD = "abc123"


class FakeClient:
    def __init__(self, status: dict, merge_output: str = "merged"):
        self._status = status
        self.merge_output = merge_output
        self.pr_status_calls: list[tuple[str, int]] = []
        self.merge_calls: list[tuple[str, int]] = []

    def pr_status(self, repo: str, number: int) -> dict:
        self.pr_status_calls.append((repo, number))
        return self._status

    def squash_merge(self, repo: str, number: int) -> str:
        self.merge_calls.append((repo, number))
        return self.merge_output


def _green_status(head: str = HEAD) -> dict:
    return {
        "headRefOid": head,
        "mergeStateStatus": "CLEAN",
        "state": "OPEN",
        "statuses": [
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
            {"status": "COMPLETED", "conclusion": "SKIPPED"},
        ],
    }


def _pass_verdict() -> Verdict:
    return Verdict(
        decision="PASS", classification="GREEN", merge_eligible=True,
        reasons=["clean"],
    )


def _clean_evidence() -> dict:
    return {
        "deployment_required": False,
        "allow_list_result": {
            "all_allowed": True, "disallowed": [],
            "forbidden_hits": [], "protected_hits": [],
        },
        "forbidden_surface_scan": {
            "all_allowed": True, "disallowed": [],
            "forbidden_hits": [], "protected_hits": [],
        },
    }


def test_merges_when_all_gates_pass():
    client = FakeClient(_green_status())
    merger = AutoMerger(client=client, allowed_repos={"o/r"})
    d = merger.evaluate_and_merge("o/r", 7, _clean_evidence(), _pass_verdict(), HEAD)
    assert d.merged is True
    assert client.merge_calls == [("o/r", 7)]
    assert all(d.gate_results.values())


def test_does_not_merge_when_verdict_not_eligible():
    client = FakeClient(_green_status())
    merger = AutoMerger(client=client)
    fix = Verdict(decision="FIX", classification="YELLOW", merge_eligible=False)
    d = merger.evaluate_and_merge("o/r", 7, _clean_evidence(), fix, HEAD)
    assert d.merged is False
    assert client.merge_calls == []
    # Should not even query CI when the verdict already fails.
    assert client.pr_status_calls == []


def test_does_not_merge_on_ci_failure():
    status = _green_status()
    status["statuses"].append({"status": "COMPLETED", "conclusion": "FAILURE"})
    client = FakeClient(status)
    merger = AutoMerger(client=client)
    d = merger.evaluate_and_merge("o/r", 7, _clean_evidence(), _pass_verdict(), HEAD)
    assert d.merged is False
    assert "CI not green" in d.reason
    assert client.merge_calls == []


def test_does_not_merge_when_checks_pending():
    status = _green_status()
    status["statuses"].append({"status": "IN_PROGRESS", "conclusion": ""})
    client = FakeClient(status)
    merger = AutoMerger(client=client)
    d = merger.evaluate_and_merge("o/r", 7, _clean_evidence(), _pass_verdict(), HEAD)
    assert d.merged is False
    assert "pending" in d.reason
    assert client.merge_calls == []


def test_does_not_merge_when_not_clean():
    status = _green_status()
    status["mergeStateStatus"] = "BLOCKED"
    client = FakeClient(status)
    merger = AutoMerger(client=client)
    d = merger.evaluate_and_merge("o/r", 7, _clean_evidence(), _pass_verdict(), HEAD)
    assert d.merged is False
    assert "CLEAN" in d.reason
    assert client.merge_calls == []


def test_does_not_merge_when_head_sha_changed():
    client = FakeClient(_green_status(head="different"))
    merger = AutoMerger(client=client)
    d = merger.evaluate_and_merge("o/r", 7, _clean_evidence(), _pass_verdict(), HEAD)
    assert d.merged is False
    assert "head SHA changed" in d.reason
    assert client.merge_calls == []


def test_does_not_merge_with_forbidden_surface_even_if_verdict_pass():
    client = FakeClient(_green_status())
    merger = AutoMerger(client=client)
    ev = _clean_evidence()
    ev["allow_list_result"]["protected_hits"] = ["config.yaml"]
    d = merger.evaluate_and_merge("o/r", 7, ev, _pass_verdict(), HEAD)
    assert d.merged is False
    assert "forbidden" in d.reason
    assert client.merge_calls == []


def test_does_not_merge_when_deployment_required():
    client = FakeClient(_green_status())
    merger = AutoMerger(client=client)
    ev = _clean_evidence()
    ev["deployment_required"] = True
    d = merger.evaluate_and_merge("o/r", 7, ev, _pass_verdict(), HEAD)
    assert d.merged is False
    assert "deployment" in d.reason
    assert client.merge_calls == []


def test_rejects_repo_not_allow_listed():
    client = FakeClient(_green_status())
    merger = AutoMerger(client=client, allowed_repos={"only/this"})
    d = merger.evaluate_and_merge("other/repo", 7, _clean_evidence(), _pass_verdict(), HEAD)
    assert d.merged is False
    assert "not allow-listed" in d.reason
    assert client.merge_calls == []


def test_no_ci_checks_is_not_green():
    status = _green_status()
    status["statuses"] = []
    client = FakeClient(status)
    merger = AutoMerger(client=client)
    d = merger.evaluate_and_merge("o/r", 7, _clean_evidence(), _pass_verdict(), HEAD)
    assert d.merged is False
    assert client.merge_calls == []

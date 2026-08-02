from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_offline_commerce_acceptance_rehearsal():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "commerce_fake_e2e.py")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    ticks = report.pop("ticks_to_complete")
    ceiling = report.pop("tick_ceiling")
    assert report == {
        "actions": 15,
        "browser_handoffs": 2,
        "candidate_domains": 10,
        "dns_writes": 3,
        "fake_e2e": "PASS",
        "golden_receipt": "exact",
        "network": "loopback-only",
        "provider_mutations": "fake-only",
    }
    # The driver stops on durable progress, not on the ceiling. Pin the margin
    # so a change that makes the flow crawl toward the safety net is a failure
    # here rather than a slow surprise in CI.
    assert 0 < ticks <= 10, ticks
    assert ceiling >= 40 * ticks

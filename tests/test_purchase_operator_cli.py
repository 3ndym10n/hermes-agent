"""Tests for the deterministic operator CLI (Hermes #65, Phase 2/3).

No network, no model, no payment data. The bridge is stubbed.
"""

import importlib.util
import json
import stat
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "purchase_operator_cli.py"
spec = importlib.util.spec_from_file_location("purchase_operator_cli", _PATH)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


@pytest.fixture()
def stub_bridge(monkeypatch):
    calls = []

    def fake(base_url, action, context, *, user_intent):
        calls.append((action, context))
        if action == "create_purchase_proposal":
            return {"status": "ok", "requested_action": action, "proposal_id": "pp_1"}
        if action == "issue_execution_ticket":
            return {"status": "ok", "requested_action": action,
                    "ticket_id": "pt_1", "ticket_token": "SUPER-SECRET-TICKET"}
        if action == "get_purchase_status":
            return {"status": "ok", "requested_action": action,
                    "proposal_id": "pp_1", "lifecycle_state": "budget_reserved"}
        return {"status": "ok", "requested_action": action}

    monkeypatch.setattr(cli, "bridge_call", fake)
    return calls


def run(argv, tmp_path):
    return cli.main(["--bridge-url", "http://127.0.0.1:1",
                     "--state-dir", str(tmp_path / "op"), *argv])


def test_propose_creates_but_does_not_execute(tmp_path, stub_bridge, capsys):
    payload = {"merchant_domain": "porkbun.com"}
    f = tmp_path / "proposal.json"
    f.write_text(json.dumps(payload))
    assert run(["propose", "--file", str(f)], tmp_path) == 0
    assert stub_bridge[0][0] == "create_purchase_proposal"
    out = capsys.readouterr().out
    assert "pp_1" in out and "NOT approved" in out and "NOT executed" in out
    # No execution action was invoked by proposing.
    assert all(a != "issue_execution_ticket" for a, _ in stub_bridge)


def test_approve_requires_exact_confirmation_phrase(tmp_path, stub_bridge):
    # Wrong phrase aborts without calling approve.
    assert run(["approve", "pp_1", "--maximum", "22.00", "--currency", "AUD",
                "--idempotency-key", "k", "--confirm", "yes"], tmp_path) == 1
    assert all(a != "approve_and_reserve_purchase" for a, _ in stub_bridge)
    # Exact phrase proceeds.
    assert run(["approve", "pp_1", "--maximum", "22.00", "--currency", "AUD",
                "--idempotency-key", "k", "--confirm", "approve pp_1 22.00 AUD"], tmp_path) == 0
    assert any(a == "approve_and_reserve_purchase" for a, _ in stub_bridge)


def test_issue_never_prints_ticket_and_writes_0600(tmp_path, stub_bridge, capsys):
    out_path = tmp_path / "ticket.txt"
    assert run(["issue", "pp_1", "--idempotency-key", "k", "--out", str(out_path)], tmp_path) == 0
    captured = capsys.readouterr().out
    assert "SUPER-SECRET-TICKET" not in captured
    assert out_path.read_text() == "SUPER-SECRET-TICKET"
    assert stat.S_IMODE(out_path.stat().st_mode) == 0o600
    # Operator audit records the event but never the token.
    audit = (tmp_path / "op" / "operator_audit.jsonl").read_text()
    assert "SUPER-SECRET-TICKET" not in audit
    assert "issue" in audit


def test_issue_requires_launch_or_out(tmp_path, stub_bridge):
    assert run(["issue", "pp_1", "--idempotency-key", "k"], tmp_path) == 1


def test_status_is_sanitized(tmp_path, stub_bridge, capsys):
    assert run(["status", "pp_1"], tmp_path) == 0
    assert "budget_reserved" in capsys.readouterr().out


def test_disabled_bridge_is_a_clear_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "bridge_call",
                        lambda *a, **k: {"status": "disabled"})
    f = tmp_path / "p.json"
    f.write_text("{}")
    assert run(["propose", "--file", str(f)], tmp_path) == 1
    assert "disabled" in capsys.readouterr().err


def test_missing_token_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("COGITATOR_BRIDGE_TOKEN", raising=False)
    # Real bridge_call (not stubbed) must refuse without a token.
    assert run(["status", "pp_1"], tmp_path) == 1
    assert "COGITATOR_BRIDGE_TOKEN" in capsys.readouterr().err


def test_no_model_imports_in_cli():
    source = _PATH.read_text()
    for forbidden in ("openai", "anthropic", "requests", "litellm"):
        assert forbidden not in source

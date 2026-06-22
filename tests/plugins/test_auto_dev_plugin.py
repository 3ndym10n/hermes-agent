"""Tests for the opt-in /auto_dev status and dry-run plugin."""

from __future__ import annotations

import json

from plugins.auto_dev import command
from plugins.auto_dev import register


def _packet(**overrides):
    data = {
        "objective": "Clarify a docs-only status line",
        "success_condition": "The requested docs line is clear",
        "repo": "hermes",
        "allowed_files": ["docs/**"],
        "forbidden_surfaces": ["storage/**", ".env*", "**/*.service"],
        "tests": ["python -m pytest tests/test_docs.py -q"],
        "rollback_boundary": "Revert the PR commit",
        "risk_classification": "GREEN",
        "approval_boundary": "PR only; no deploy",
        "worker_kind": "fake",
    }
    data.update(overrides)
    return data


def _enabled_config():
    return {
        "backend_automation": {
            "command_enabled": True,
            "allowed_repos": ["hermes", "cogitator"],
        }
    }


def test_status_is_default_off_and_side_effect_free():
    result = command.status_packet({})

    assert result["package_available"] is True
    assert result["command_enabled"] is False
    assert result["live_execution_available"] is False
    assert result["worker_called"] is False
    assert result["git_changed"] is False
    assert result["pr_opened"] is False
    assert result["merge_performed"] is False
    assert result["deployment_performed"] is False


def test_status_lists_configured_repo_keys_only():
    result = command.status_packet(
        {
            "backend_automation": {
                "command_enabled": True,
                "allowed_repos": {"hermes": "/srv/hermes", "cogitator": "/srv/cog"},
            }
        }
    )

    assert result["command_enabled"] is True
    assert result["allowed_repos"] == ["hermes", "cogitator"]
    assert "/srv/hermes" not in str(result)


def test_valid_green_packet_is_dry_run_candidate_only():
    result = command.validate_task_packet(_packet(), _enabled_config())

    assert result["valid"] is True
    assert result["repo_allowed"] is True
    assert result["policy_preview_eligible"] is True
    assert result["requires_cal"] is False
    assert result["worker_called"] is False
    assert result["git_changed"] is False
    assert result["pr_opened"] is False
    assert result["merge_performed"] is False
    assert result["deployment_performed"] is False


def test_repo_outside_allow_list_fails_closed():
    result = command.validate_task_packet(
        _packet(repo="unknown-repo"), _enabled_config()
    )

    assert result["valid"] is False
    assert result["repo_allowed"] is False
    assert "repo is not allow-listed" in result["errors"]
    assert result["policy_preview_eligible"] is False


def test_protected_allowed_path_fails_closed():
    result = command.validate_task_packet(
        _packet(allowed_files=[".env"]), _enabled_config()
    )

    assert result["valid"] is False
    assert result["protected_conflicts"] == [".env"]
    assert "allowed_files intersects protected surfaces" in result["errors"]


def test_unknown_packet_field_is_rejected():
    result = command.validate_task_packet(
        _packet(deploy_now=True), _enabled_config()
    )

    assert result["valid"] is False
    assert result["errors"] == ["unknown fields: deploy_now"]


def test_handler_refuses_dry_run_when_gate_is_off(monkeypatch):
    monkeypatch.setattr(command, "_load_config", lambda: {})

    reply = command.handle_auto_dev("dry_run " + json.dumps(_packet()))

    assert "disabled by default" in reply
    assert "Live execution remains unavailable" in reply


def test_handler_validates_json_without_echoing_objective(monkeypatch):
    monkeypatch.setattr(command, "_load_config", _enabled_config)
    packet = _packet(objective="do not echo sk-example-secret-value")

    reply = command.handle_auto_dev("dry_run " + json.dumps(packet))

    assert "Status: valid" in reply
    assert "Repo: hermes — allowed" in reply
    assert "Execution performed: no" in reply
    assert "sk-example-secret-value" not in reply


def test_handler_has_no_live_execution_subcommand():
    reply = command.handle_auto_dev("run anything")

    assert reply == "Live execution is disabled. Only status and dry_run are available."


def test_plugin_registers_only_auto_dev_command():
    class FakeContext:
        def __init__(self):
            self.calls = []

        def register_command(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    ctx = FakeContext()
    register(ctx)

    assert len(ctx.calls) == 1
    args, kwargs = ctx.calls[0]
    assert args[0] == "auto_dev"
    assert args[1] is command.handle_auto_dev
    assert kwargs["args_hint"] == "status | dry_run <json>"

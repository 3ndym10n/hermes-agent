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


def test_broad_allowed_glob_that_can_reach_protected_paths_fails_closed():
    result = command.validate_task_packet(
        _packet(allowed_files=["**/*"]), _enabled_config()
    )

    assert result["valid"] is False
    assert result["protected_conflicts"] == ["**/*"]
    assert result["policy_preview_eligible"] is False


def test_markdown_glob_that_reaches_storage_fails_closed():
    result = command.validate_task_packet(
        _packet(allowed_files=["**/*.md"]), _enabled_config()
    )

    assert result["valid"] is False
    assert result["protected_conflicts"] == ["**/*.md"]


def test_string_sequence_fields_are_rejected_instead_of_split_into_characters():
    result = command.validate_task_packet(
        _packet(allowed_files="docs/**", tests="pytest -q"), _enabled_config()
    )

    assert result["valid"] is False
    assert "allowed_files must be an array of non-empty strings" in result["errors"]
    assert "tests must be an array of non-empty strings" in result["errors"]
    assert result["policy_preview_eligible"] is False


def test_empty_items_in_sequence_fields_are_rejected():
    result = command.validate_task_packet(
        _packet(forbidden_surfaces=["storage/**", ""]), _enabled_config()
    )

    assert result["valid"] is False
    assert (
        "forbidden_surfaces must contain only non-empty strings" in result["errors"]
    )


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


def test_handler_redacts_secret_shaped_values_in_rendered_errors(monkeypatch):
    monkeypatch.setattr(command, "_load_config", _enabled_config)
    secret = "sk-ant-example-secret-value-1234567890"
    packet = _packet(repo=secret, risk_classification=secret)

    reply = command.handle_auto_dev("dry_run " + json.dumps(packet))

    assert secret not in reply
    assert "***REDACTED***" in reply
    assert "Status: invalid" in reply


def test_handler_run_is_refused_by_default():
    # /auto_dev run is now a gated deterministic-proof path; with no config the
    # triple gate (command_enabled/live_execution_enabled/proof_mode) is off.
    reply = command.handle_auto_dev("run anything")

    assert "Backend Automation Run — refused" in reply


def test_handler_execute_and_start_aliases_refused():
    for alias in ("execute", "start"):
        reply = command.handle_auto_dev(f"{alias} anything")
        assert "aliases are disabled" in reply
        assert "/auto_dev run" in reply


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
    # Registered hyphenated so the gateway's "_"->"-" dispatch resolves the
    # Telegram "/auto_dev" form. See _telegram_dispatch_lookup below.
    assert args[0] == "auto-dev"
    assert args[1] is command.handle_auto_dev
    assert kwargs["args_hint"] == "status | dry_run <json>"


# ---------------------------------------------------------------------------
# Bundled-plugin discovery + gateway slash-command dispatch
#
# Proves the live doorway: the command is registered ONLY when enabled, and the
# Telegram "/auto_dev" form actually resolves through the gateway dispatcher.
# ---------------------------------------------------------------------------

import pytest  # noqa: E402
import yaml  # noqa: E402


@pytest.fixture
def _isolate_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME so plugins.enabled / config come from a temp file."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


def _write_config(hermes_home, config):
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(config))


def _discover():
    from hermes_cli import plugins as pmod

    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    return mgr


def _telegram_dispatch_lookup(mgr, slash_command):
    """Mirror the gateway slash-command dispatch (gateway/run.py).

    Telegram delivers "/auto_dev"; the dispatcher normalizes "_"->"-" before
    looking the handler up in the plugin command registry.
    """
    command_word = slash_command.lstrip("/").split()[0]
    entry = mgr._plugin_commands.get(command_word.replace("_", "-"))
    return entry["handler"] if entry else None


def test_auto_dev_unknown_when_plugin_not_enabled(_isolate_env):
    mgr = _discover()

    # Discovered as a bundled plugin, but not active and not dispatchable.
    assert "auto_dev" in mgr._plugins
    assert not mgr._plugins["auto_dev"].enabled
    assert _telegram_dispatch_lookup(mgr, "/auto_dev status") is None


def test_auto_dev_status_recognized_when_plugin_enabled(_isolate_env):
    _write_config(_isolate_env, {"plugins": {"enabled": ["auto_dev"]}})
    mgr = _discover()

    assert mgr._plugins["auto_dev"].enabled
    assert "auto-dev" in mgr._plugins["auto_dev"].commands_registered

    handler = _telegram_dispatch_lookup(mgr, "/auto_dev status")
    assert handler is not None
    reply = handler("status")
    assert "Backend Development Automation" in reply
    assert "Live execution: disabled" in reply


def test_auto_dev_dry_run_remains_dry_run_only_when_enabled(_isolate_env):
    _write_config(
        _isolate_env,
        {
            "plugins": {"enabled": ["auto_dev"]},
            "backend_automation": {
                "command_enabled": True,
                "allowed_repos": ["hermes", "cogitator"],
            },
        },
    )
    mgr = _discover()
    handler = _telegram_dispatch_lookup(mgr, "/auto_dev dry_run")
    assert handler is not None

    reply = handler("dry_run " + json.dumps(_packet()))
    assert "Status: valid" in reply
    assert "Execution performed: no" in reply


@pytest.mark.parametrize("subcommand", ["run", "execute", "start"])
def test_auto_dev_live_execution_subcommands_refused(_isolate_env, subcommand):
    # Plugin enabled but backend automation is NOT configured, so no live or
    # proof execution can occur. `run` is a gated deterministic-proof path
    # (refused here: command/proof config off); `execute`/`start` are hard
    # aliases pointing at `run`. None of them run a live worker.
    _write_config(_isolate_env, {"plugins": {"enabled": ["auto_dev"]}})
    mgr = _discover()
    handler = _telegram_dispatch_lookup(mgr, f"/auto_dev {subcommand}")
    assert handler is not None

    reply = handler(f"{subcommand} whatever")
    assert "refused" in reply or "aliases are disabled" in reply

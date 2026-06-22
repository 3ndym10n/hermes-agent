"""Read-only status and task-packet validation for backend automation."""

from __future__ import annotations

import json
from dataclasses import fields
from typing import Any, Mapping

from automation.safety import DEFAULT_PROTECTED_GLOBS, match_any
from automation.task_packet import WorkerTaskPacket

API_VERSION = "phase3c-dry-run-v1"
_PACKET_FIELDS = frozenset(f.name for f in fields(WorkerTaskPacket))


def _truthy(value: Any) -> bool:
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_config() -> Mapping[str, Any]:
    try:
        from hermes_cli.config import read_raw_config

        value = read_raw_config()
    except Exception:
        return {}
    return value if isinstance(value, Mapping) else {}


def _section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    value = config.get("backend_automation", {})
    return value if isinstance(value, Mapping) else {}


def _allowed_repos(config: Mapping[str, Any]) -> tuple[str, ...]:
    raw = _section(config).get("allowed_repos", ())
    if isinstance(raw, Mapping):
        values = raw.keys()
    elif isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, (list, tuple, set, frozenset)):
        values = raw
    else:
        values = ()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def status_packet(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    cfg = config if isinstance(config, Mapping) else _load_config()
    section = _section(cfg)
    return {
        "api_version": API_VERSION,
        "package_available": True,
        "command_enabled": _truthy(section.get("command_enabled", False)),
        "allowed_repos": list(_allowed_repos(cfg)),
        "live_execution_available": False,
        "worker_called": False,
        "git_changed": False,
        "pr_opened": False,
        "merge_performed": False,
        "deployment_performed": False,
    }


def validate_task_packet(
    payload: Mapping[str, Any], config: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    cfg = config if isinstance(config, Mapping) else _load_config()
    errors: list[str] = []
    warnings: list[str] = []

    unknown = sorted(set(payload) - _PACKET_FIELDS)
    if unknown:
        errors.append("unknown fields: " + ", ".join(unknown))

    packet: WorkerTaskPacket | None = None
    if not errors:
        try:
            packet = WorkerTaskPacket.from_dict(dict(payload))
        except (TypeError, ValueError) as exc:
            errors.append(f"invalid packet shape: {type(exc).__name__}")

    if packet is not None:
        for item in packet.validate():
            if item.startswith("WARNING:"):
                warnings.append(item.removeprefix("WARNING: "))
            else:
                errors.append(item)

    repos = _allowed_repos(cfg)
    repo_allowed = bool(packet is not None and packet.repo in repos)
    if packet is not None and not repo_allowed:
        errors.append("repo is not allow-listed")

    protected: list[str] = []
    if packet is not None:
        for pattern in packet.allowed_files:
            normalized = str(pattern).replace("\\", "/").strip()
            if normalized and match_any(normalized, DEFAULT_PROTECTED_GLOBS):
                protected.append(normalized)
    if protected:
        errors.append("allowed_files intersects protected surfaces")

    tests_present = bool(packet is not None and packet.tests)
    green = bool(packet is not None and packet.risk_classification == "GREEN")
    valid = not errors
    preview_eligible = valid and repo_allowed and green and tests_present and not protected

    summary = {
        "repo": packet.repo if packet is not None else "",
        "risk": packet.risk_classification if packet is not None else "",
        "allowed_file_count": len(packet.allowed_files) if packet is not None else 0,
        "forbidden_surface_count": len(packet.forbidden_surfaces) if packet is not None else 0,
        "test_count": len(packet.tests) if packet is not None else 0,
    }
    return {
        "valid": valid,
        "repo_allowed": repo_allowed,
        "protected_conflicts": protected,
        "policy_preview_eligible": preview_eligible,
        "requires_cal": not preview_eligible,
        "errors": errors,
        "warnings": warnings,
        "summary": summary,
        "live_execution_available": False,
        "worker_called": False,
        "git_changed": False,
        "pr_opened": False,
        "merge_performed": False,
        "deployment_performed": False,
    }


def _render_status(result: Mapping[str, Any]) -> str:
    repos = result.get("allowed_repos") or []
    gate = "enabled" if result.get("command_enabled") else "disabled (default)"
    return "\n".join(
        [
            "Backend Development Automation",
            f"Package: available ({result.get('api_version')})",
            f"Dry-run gate: {gate}",
            "Allowed repos: " + (", ".join(repos) if repos else "none configured"),
            "Live execution: disabled",
            "Repository changes, PR creation, merge, and deploy: disabled",
            "Usage: /auto_dev status | /auto_dev dry_run <task-packet-json>",
        ]
    )


def _render_validation(result: Mapping[str, Any]) -> str:
    summary = result.get("summary") or {}
    lines = [
        "Backend Automation Task-Packet Dry Run",
        "Status: " + ("valid" if result.get("valid") else "invalid"),
        f"Repo: {summary.get('repo') or '(missing)'} — "
        + ("allowed" if result.get("repo_allowed") else "not allowed"),
        f"Risk: {summary.get('risk') or '(missing)'}",
        f"Allowed files: {summary.get('allowed_file_count', 0)}; tests: {summary.get('test_count', 0)}",
        "Policy preview: "
        + ("GREEN-safe candidate" if result.get("policy_preview_eligible") else "escalate / not eligible"),
        "Execution performed: no",
    ]
    if result.get("errors"):
        lines.append("Errors:")
        lines.extend(f"- {item}" for item in result["errors"])
    if result.get("warnings"):
        lines.append("Warnings:")
        lines.extend(f"- {item}" for item in result["warnings"])
    return "\n".join(lines)


def handle_auto_dev(raw_args: str) -> str:
    text = str(raw_args or "").strip()
    if not text or text.lower() == "status":
        return _render_status(status_packet())

    command, _, remainder = text.partition(" ")
    command = command.lower()
    if command in {"run", "execute", "start"}:
        return "Live execution is disabled. Only status and dry_run are available."
    if command not in {"dry_run", "dry-run", "dryrun"}:
        return "Usage: /auto_dev status | /auto_dev dry_run <task-packet-json>"

    config = _load_config()
    if not status_packet(config)["command_enabled"]:
        return "Backend automation dry-run is disabled by default. Live execution remains unavailable."
    if not remainder.strip():
        return "Usage: /auto_dev dry_run <task-packet-json>"
    try:
        payload = json.loads(remainder)
    except json.JSONDecodeError:
        return "Invalid task-packet JSON. No action was performed."
    if not isinstance(payload, dict):
        return "Task packet must be a JSON object. No action was performed."
    return _render_validation(validate_task_packet(payload, config))


__all__ = ["API_VERSION", "handle_auto_dev", "status_packet", "validate_task_packet"]

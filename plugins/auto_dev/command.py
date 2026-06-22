"""Read-only status and task-packet validation for backend automation."""

from __future__ import annotations

import json
from dataclasses import fields
from typing import Any, Mapping

from automation.adapters import _scrub_auth
from automation.safety import DEFAULT_PROTECTED_GLOBS, match_any
from automation.task_packet import WorkerTaskPacket

API_VERSION = "phase3c-dry-run-v1"
_PACKET_FIELDS = frozenset(f.name for f in fields(WorkerTaskPacket))
_SEQUENCE_FIELDS = ("allowed_files", "forbidden_surfaces", "tests")

# Representative protected paths used to catch broad allow-list globs such as
# ``*``, ``**/*`` or ``**/*.md``.  The worker still enforces the real protected
# path policy after edits; this dry-run check prevents us from presenting an
# obviously over-broad packet as a GREEN-safe candidate before execution.
_PROTECTED_SAMPLE_PATHS = (
    ".env",
    ".env.production",
    "secrets.txt",
    "config.yaml",
    ".github/workflows/ci.yml",
    "deploy/hermes.service",
    "Dockerfile",
    "railway.toml",
    "migrations/001.sql",
    "storage/runtime.json",
    "storage/notes/checkpoint.md",
    "certs/private.pem",
    "id_rsa",
)


def _truthy(value: Any) -> bool:
    return value is True or str(value or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


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
        text = _scrub_auth(str(value or "").strip())
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


def _payload_shape_errors(payload: Mapping[str, Any]) -> list[str]:
    """Reject malformed JSON shapes before constructing ``WorkerTaskPacket``.

    ``WorkerTaskPacket.from_dict`` normalizes sequence fields with ``tuple(value)``.
    Without this guard, a JSON string would become a tuple of characters and could
    incorrectly look non-empty to the structural validator.
    """

    errors: list[str] = []
    for field_name in _SEQUENCE_FIELDS:
        if field_name not in payload:
            continue
        value = payload[field_name]
        if not isinstance(value, (list, tuple)):
            errors.append(f"{field_name} must be an array of non-empty strings")
            continue
        if any(not isinstance(item, str) or not item.strip() for item in value):
            errors.append(f"{field_name} must contain only non-empty strings")

    timeout = payload.get("timeout_seconds")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
    ):
        errors.append("timeout_seconds must be a positive integer")
    return errors


def _allow_pattern_intersects_protected(pattern: str) -> bool:
    normalized = str(pattern or "").replace("\\", "/").strip()
    if not normalized:
        return False
    if match_any(normalized, DEFAULT_PROTECTED_GLOBS):
        return True
    return any(
        match_any(sample_path, (normalized,))
        for sample_path in _PROTECTED_SAMPLE_PATHS
    )


def validate_task_packet(
    payload: Mapping[str, Any], config: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    cfg = config if isinstance(config, Mapping) else _load_config()
    errors: list[str] = []
    warnings: list[str] = []

    unknown = sorted(set(payload) - _PACKET_FIELDS)
    if unknown:
        errors.append("unknown fields: " + ", ".join(unknown))
    errors.extend(_payload_shape_errors(payload))

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
        protected = [
            pattern
            for pattern in packet.allowed_files
            if _allow_pattern_intersects_protected(pattern)
        ]
    if protected:
        errors.append("allowed_files intersects protected surfaces")

    tests_present = bool(packet is not None and packet.tests)
    green = bool(packet is not None and packet.risk_classification == "GREEN")
    valid = not errors
    preview_eligible = (
        valid and repo_allowed and green and tests_present and not protected
    )

    summary = {
        "repo": _scrub_auth(packet.repo) if packet is not None else "",
        "risk": (
            _scrub_auth(packet.risk_classification) if packet is not None else ""
        ),
        "allowed_file_count": len(packet.allowed_files) if packet is not None else 0,
        "forbidden_surface_count": (
            len(packet.forbidden_surfaces) if packet is not None else 0
        ),
        "test_count": len(packet.tests) if packet is not None else 0,
    }
    return {
        "valid": valid,
        "repo_allowed": repo_allowed,
        "protected_conflicts": [_scrub_auth(item) for item in protected],
        "policy_preview_eligible": preview_eligible,
        "requires_cal": not preview_eligible,
        "errors": [_scrub_auth(item) for item in errors],
        "warnings": [_scrub_auth(item) for item in warnings],
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
    rendered = "\n".join(
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
    return _scrub_auth(rendered)


def _render_validation(result: Mapping[str, Any]) -> str:
    summary = result.get("summary") or {}
    lines = [
        "Backend Automation Task-Packet Dry Run",
        "Status: " + ("valid" if result.get("valid") else "invalid"),
        f"Repo: {summary.get('repo') or '(missing)'} — "
        + ("allowed" if result.get("repo_allowed") else "not allowed"),
        f"Risk: {summary.get('risk') or '(missing)'}",
        f"Allowed files: {summary.get('allowed_file_count', 0)}; "
        f"tests: {summary.get('test_count', 0)}",
        "Policy preview: "
        + (
            "GREEN-safe candidate"
            if result.get("policy_preview_eligible")
            else "escalate / not eligible"
        ),
        "Execution performed: no",
    ]
    if result.get("errors"):
        lines.append("Errors:")
        lines.extend(f"- {item}" for item in result["errors"])
    if result.get("warnings"):
        lines.append("Warnings:")
        lines.extend(f"- {item}" for item in result["warnings"])
    return _scrub_auth("\n".join(lines))


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
        return (
            "Backend automation dry-run is disabled by default. "
            "Live execution remains unavailable."
        )
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

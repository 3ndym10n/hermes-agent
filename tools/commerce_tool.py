"""Trusted Telegram control surface for durable commerce jobs.

The model-facing tool only writes control decisions to ``CommerceJobStore``.
Provider work belongs to the deterministic commerce worker.
"""

from __future__ import annotations

import json
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping

from commerce_jobs import CommerceJobError, CommerceJobStore
from gateway.session_context import get_session_env
from hermes_cli.config import cfg_get, load_config_readonly
from tools.registry import registry

DEFAULT_OBJECTIVE = "Set up the AMD GPU waitlist store."
_OPERATIONS = frozenset({
    "start_or_resume",
    "status",
    "answer_facts",
    "pause",
    "resume",
    "cancel",
    "receipt",
})
_ARG_KEYS = frozenset({
    "operation",
    "objective",
    "job_id",
    "facts",
    "reason",
})
_ID_RE = re.compile(r"[-A-Za-z0-9_:.]{1,240}")


class _SafeControlError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _identifier(value: Any, *, required: bool) -> str:
    if value is None:
        value = ""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise _SafeControlError("invalid_trusted_origin")
    value = value.strip()
    if required and not value:
        raise _SafeControlError("missing_trusted_origin")
    if value and _ID_RE.fullmatch(value) is None:
        raise _SafeControlError("invalid_trusted_origin")
    return value


def _trusted_origin(origin: Mapping[str, Any]) -> Mapping[str, str]:
    if not isinstance(origin, Mapping) or origin.get("platform") != "telegram":
        raise _SafeControlError("telegram_only")
    safe = {
        "platform": "telegram",
        "chat_id": _identifier(origin.get("chat_id"), required=True),
        "thread_id": _identifier(origin.get("thread_id"), required=False),
        "user_id": _identifier(origin.get("user_id"), required=True),
        "message_id": _identifier(origin.get("message_id"), required=False),
    }
    return MappingProxyType(safe)


def _context_origin() -> Mapping[str, str]:
    return _trusted_origin({
        "platform": get_session_env("HERMES_SESSION_PLATFORM", ""),
        "chat_id": get_session_env("HERMES_SESSION_CHAT_ID", ""),
        "thread_id": get_session_env("HERMES_SESSION_THREAD_ID", ""),
        "user_id": get_session_env("HERMES_SESSION_USER_ID", ""),
        "message_id": get_session_env("HERMES_SESSION_MESSAGE_ID", ""),
    })


def _summary(job: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    state = str(job.get("current_state", "unknown"))
    result = {
        "job_id": str(job.get("job_id", "")),
        "state": state,
        "active": bool(job.get("active")),
        "row_version": int(job.get("row_version", 0)),
        "current_step": str(job.get("current_step", "")),
        "current_gate_id": str(job.get("current_gate_id", "")),
        "substatus": str(job.get("substatus", "")),
        "message": f"Commerce job {job.get('job_id', '')} is {state}.",
    }
    result.update(extra)
    return result


def _owned_job(
    store: CommerceJobStore,
    requester: str,
    job_id: Any = "",
) -> dict[str, Any]:
    if job_id:
        if not isinstance(job_id, str):
            raise _SafeControlError("invalid_job_id")
        job = store.get_job(job_id.strip())
        if job.get("requester") != requester:
            raise _SafeControlError("job_not_owned")
        return job
    owned = [job for job in store.list_jobs() if job.get("requester") == requester]
    if not owned:
        raise _SafeControlError("job_not_found")
    active = [job for job in owned if job.get("active")]
    return (active or owned)[-1]


def commerce_control_from_origin(
    operation: str,
    *,
    origin: Mapping[str, Any],
    objective: str = "",
    job_id: str = "",
    facts: Mapping[str, Any] | None = None,
    reason: str = "",
    store: CommerceJobStore | None = None,
    config_loader: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply one store-only control from an already-authorized Telegram event."""
    try:
        trusted = _trusted_origin(origin)
        config = (config_loader or load_config_readonly)()
        if cfg_get(config, "commerce", "enabled", default=False) is not True:
            raise _SafeControlError("commerce_disabled")
        if operation not in _OPERATIONS:
            raise _SafeControlError("invalid_operation")

        job_store = store or CommerceJobStore()
        requester = f"telegram:{trusted['user_id']}"
        actor = requester

        if operation == "start_or_resume":
            launch_objective = objective.strip() if isinstance(objective, str) else ""
            job = job_store.create_or_attach_job(
                requester=requester,
                objective=launch_objective or DEFAULT_OBJECTIVE,
                origin=trusted,
            )
            attached = bool(job.pop("attached", False))
            resumed = False
            if attached and job.get("current_state") in {"paused", "timed_out"}:
                job = job_store.resume(
                    str(job["job_id"]),
                    expected_version=int(job["row_version"]),
                    actor=actor,
                )
                resumed = True
            return {"ok": True, **_summary(job, attached=attached, resumed=resumed)}

        job = _owned_job(job_store, requester, job_id)
        if operation == "status":
            return {"ok": True, **_summary(job)}
        if operation == "receipt":
            return {"ok": True, **_summary(job), "receipt_status": "pending"}
        if operation == "answer_facts":
            if not isinstance(facts, Mapping) or not facts:
                raise _SafeControlError("facts_required")
            facts_gate = None
            if job.get("current_state") == "awaiting_cal" and job.get(
                "current_gate_id"
            ):
                candidate = job_store.get_gate(str(job["current_gate_id"]))
                if (
                    candidate.get("gate_type") == "facts"
                    and candidate.get("status") == "open"
                ):
                    facts_gate = candidate
            job = job_store.record_facts(
                str(job["job_id"]),
                facts,
                expected_version=int(job["row_version"]),
                actor=actor,
            )
            gate_completed = False
            if facts_gate is not None:
                if job_store.latest_facts(str(job["job_id"])) != dict(facts):
                    raise _SafeControlError("facts_verification_failed")
                _, handoff_token = job_store.issue_gate_handoff(
                    str(facts_gate["gate_id"]), actor=actor
                )
                job_store.request_gate_done(
                    str(facts_gate["gate_id"]), handoff_token, actor=actor
                )
                job_store.complete_gate(
                    str(facts_gate["gate_id"]),
                    evidence={
                        "provider_truth_verified": True,
                        "verification_code": "facts_persisted",
                    },
                    actor=actor,
                )
                gate_completed = True
            return {
                "ok": True,
                **_summary(job),
                "facts_recorded": True,
                "gate_completed": gate_completed,
            }
        if operation == "pause":
            if job.get("current_state") != "paused":
                job = job_store.pause(
                    str(job["job_id"]),
                    expected_version=int(job["row_version"]),
                    actor=actor,
                    reason=reason or "requested_by_owner",
                )
            return {"ok": True, **_summary(job)}
        if operation == "resume":
            if job.get("current_state") in {"paused", "timed_out"}:
                job = job_store.resume(
                    str(job["job_id"]),
                    expected_version=int(job["row_version"]),
                    actor=actor,
                )
            elif not job.get("active"):
                raise _SafeControlError("job_not_resumable")
            return {"ok": True, **_summary(job)}
        if operation == "cancel":
            if job.get("current_state") != "cancelled":
                job = job_store.cancel(
                    str(job["job_id"]),
                    expected_version=int(job["row_version"]),
                    actor=actor,
                    reason=reason or "requested_by_owner",
                )
            return {"ok": True, **_summary(job)}
    except CommerceJobError as exc:
        return {"ok": False, "error": exc.code}
    except _SafeControlError as exc:
        return {"ok": False, "error": exc.code}
    except Exception:
        return {"ok": False, "error": "commerce_control_failed"}

    return {"ok": False, "error": "invalid_operation"}


def _handle_commerce_launch(args: Mapping[str, Any]) -> str:
    if not isinstance(args, Mapping) or set(args) - _ARG_KEYS:
        return json.dumps({"ok": False, "error": "invalid_arguments"}, sort_keys=True)
    try:
        origin = _context_origin()
    except _SafeControlError as exc:
        return json.dumps({"ok": False, "error": exc.code}, sort_keys=True)
    result = commerce_control_from_origin(
        args.get("operation", ""),
        origin=origin,
        objective=args.get("objective", ""),
        job_id=args.get("job_id", ""),
        facts=args.get("facts"),
        reason=args.get("reason", ""),
    )
    return json.dumps(result, sort_keys=True, ensure_ascii=False)


COMMERCE_LAUNCH_SCHEMA = {
    "name": "commerce_launch",
    "description": (
        "Create, inspect, or control the governed AMD GPU waitlist-store job. "
        "This records decisions only and never performs a purchase or publication."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "operation": {
                "type": "string",
                "enum": sorted(_OPERATIONS),
                "description": "Store-only control operation.",
            },
            "objective": {"type": "string", "maxLength": 4000},
            "job_id": {"type": "string", "maxLength": 240},
            "facts": {"type": "object"},
            "reason": {"type": "string", "maxLength": 500},
        },
        "required": ["operation"],
    },
}


registry.register(
    name="commerce_launch",
    toolset="commerce",
    schema=COMMERCE_LAUNCH_SCHEMA,
    handler=lambda args, **_kw: _handle_commerce_launch(args),
    emoji="🏪",
)

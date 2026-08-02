"""Deterministic worker for durable commerce launch jobs.

The worker only interprets persisted, typed plan steps.  It never calls a
model and never grants an approval; completed approval gates are merely bound
to their exact action fingerprints before dispatch.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from commerce_browser import BrowserLifecycleError, ensure_browser_session
from commerce_jobs import (
    ALLOWED_TRANSITIONS,
    CommerceJobError,
    CommerceJobStore,
    CommerceStaleVersionError,
)
from gateway.cogitator_intake_bridge import (
    TOKEN_ENV,
    request_intelligent_retrieval,
)
from hermes_cli.config import cfg_get, load_config_readonly
from hermes_constants import get_hermes_home

WORKER_ACTOR = "commerce-worker"
DEFAULT_POLL_SECONDS = 2.0
MAX_ADVANCES_PER_JOB = 32
BROWSER_RECHECK_SECONDS = 30.0
_CODE_RE = re.compile(r"[a-z][a-z0-9_.-]{0,119}\Z")
_REFERENCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,239}\Z")
_APPROVAL_STATES = {
    "purchase": "awaiting_purchase_approval",
    "dns": "awaiting_dns_approval",
    "publication": "awaiting_publication_approval",
}
_NEXT_STATES = {
    "executing_read_only": frozenset({"ready", "verifying"}),
    "executing": frozenset({"ready"}),
}
_CONTROL_KEY = "operator_control"
REQUIRED_FACTS = (
    "contact_email",
    "business_identity_sentence",
    "double_opt_in",
    "brand_signoff",
    "privacy_signoff",
)

LAUNCH_FACT_RECORD_TYPE = "commerce_launch_fact"
LAUNCH_FACT_SCOPE = "silicon_current_v1"

MAX_RETRIEVED_LAUNCH_FACTS = 64


class CommerceOperatorError(RuntimeError):
    """A safe, stable worker error code."""

    def __init__(self, code: str):
        self.code = _code(code, "operator_error")
        super().__init__(self.code)


class ProviderStepError(CommerceOperatorError):
    """Typed provider failure; exception prose is never persisted."""

    def __init__(self, code: str, *, uncertain: bool = False):
        super().__init__(code)
        self.uncertain = bool(uncertain)


class WorkerAlreadyRunningError(CommerceOperatorError):
    def __init__(self):
        super().__init__("worker_already_running")


def _code(value: Any, fallback: str) -> str:
    if isinstance(value, str):
        candidate = value.strip().lower()
        if _CODE_RE.fullmatch(candidate):
            return candidate
    return fallback


def _reference(value: Any) -> str:
    if isinstance(value, str):
        candidate = value.strip()
        if _REFERENCE_RE.fullmatch(candidate):
            return candidate
    return ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def commerce_enabled() -> bool:
    """Read the kill switch afresh; malformed values fail closed."""

    return cfg_get(load_config_readonly(), "commerce", "enabled", default=False) is True


def _strict_fact_value(value: Any) -> bool:
    if type(value) is str:
        return (
            bool(value)
            and value == value.strip()
            and len(value) <= 4_000
            and all(ord(char) >= 32 for char in value)
        )
    if type(value) is bool:
        return True
    if type(value) is int:
        return -(2**53) < value < 2**53
    if type(value) is float:
        return math.isfinite(value) and abs(value) < 1e100
    return False


def _approved_launch_facts(records: Any) -> dict[str, Any]:
    """Extract only typed, current launch-fact metadata; never inspect prose."""

    if not isinstance(records, list) or len(records) > MAX_RETRIEVED_LAUNCH_FACTS:
        return {}
    facts: dict[str, Any] = {}
    conflicted: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        if record.get("lifecycle_state") not in {"approved", "promoted"}:
            continue
        if record.get("record_type") != LAUNCH_FACT_RECORD_TYPE:
            continue
        if record.get("scope") != LAUNCH_FACT_SCOPE:
            continue
        provenance = record.get("provenance")
        if not isinstance(provenance, str) or not provenance.strip():
            continue
        if record.get("superseded_by") not in (None, ""):
            continue
        superseded = record.get("superseded")
        if superseded is not None and superseded is not False:
            continue
        fact_code = record.get("fact_code")
        if fact_code not in REQUIRED_FACTS:
            continue
        conflicts_with = record.get("conflicts_with")
        conflicting = record.get("conflicting")
        if conflicts_with or (conflicting is not None and conflicting is not False):
            conflicted.add(fact_code)
            continue
        value = record.get("fact_value")
        if not _strict_fact_value(value):
            continue
        if fact_code in facts and (
            type(facts[fact_code]) is not type(value) or facts[fact_code] != value
        ):
            conflicted.add(fact_code)
            continue
        facts[fact_code] = value
    for fact_code in conflicted:
        facts.pop(fact_code, None)
    return facts


def load_approved_launch_facts() -> dict[str, Any]:
    """Read approved launch facts from Cogitator, failing closed to no facts."""

    try:
        config = load_config_readonly()
        if cfg_get(config, "intake", "enabled", default=False) is not True:
            return {}
        base_url = cfg_get(config, "intake", "base_url", default="")
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not isinstance(base_url, str) or not base_url.strip() or not token:
            return {}
        response = request_intelligent_retrieval(
            base_url=base_url.strip(),
            token=token,
            task_description=(
                "Retrieve only typed approved or promoted commerce launch facts. "
                "Require record_type commerce_launch_fact, scope silicon_current_v1, "
                "fact_code, scalar fact_value, and provenance metadata. Exclude "
                "superseded or conflicting records. Do not infer from prose."
            ),
        )
        return _approved_launch_facts(response.get("records"))
    except Exception:
        return {}


def default_planner(
    _job: Mapping[str, Any], facts: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Build the smallest safe V1 packet without provider or model work."""

    missing = [name for name in REQUIRED_FACTS if name not in facts]
    if missing:
        return {"missing_facts": missing}
    return {
        "provider_account": "unconfigured",
        "steps": [],
        "status_code": "provider_steps_pending",
    }


def fake_planner(
    _job: Mapping[str, Any], _facts: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Explicitly inert planner used by WP1 acceptance tests and smoke runs."""

    return {
        "provider_account": "fake-provider",
        "steps": [],
        "status_code": "fake_ready",
    }


class WorkerLock:
    """One non-blocking POSIX process lock held for the worker lifetime."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise WorkerAlreadyRunningError() from None
        self._fd = descriptor

    def release(self) -> None:
        if self._fd is None:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> WorkerLock:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class CommerceOperator:
    """Advance commerce jobs with injected pure planning and step handlers."""

    def __init__(
        self,
        *,
        store: CommerceJobStore | None = None,
        planner: Callable[
            [Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]
        ] = default_planner,
        step_handlers: Mapping[
            str, Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]
        ]
        | None = None,
        enabled_fn: Callable[[], bool] = commerce_enabled,
        lock_path: str | Path | None = None,
        clock: Callable[[], datetime] = _now,
        browser_ensure: Callable[[str, str, str], Mapping[str, Any]] = (
            ensure_browser_session
        ),
        approved_facts_loader: Callable[[], Mapping[str, Any]] = (
            load_approved_launch_facts
        ),
        gate_verifiers: Mapping[
            str,
            Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any] | None],
        ]
        | None = None,
        reconcilers: Mapping[
            str,
            Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any] | None],
        ]
        | None = None,
        completion_handler: Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
        | None = None,
    ):
        self.store = store or CommerceJobStore()
        self.planner = planner
        self.step_handlers = dict(step_handlers or {})
        self.enabled_fn = enabled_fn
        self.lock_path = Path(
            lock_path or (get_hermes_home() / "commerce/operator.lock")
        )
        self.clock = clock
        self.browser_ensure = browser_ensure
        self.approved_facts_loader = approved_facts_loader
        self.gate_verifiers = dict(gate_verifiers or {})
        self.reconcilers = dict(reconcilers or {})
        self.completion_handler = completion_handler
        self._browser_checked_at: dict[str, float] = {}

    def _enabled(self) -> bool:
        try:
            return self.enabled_fn() is True
        except Exception:
            return False

    def run(
        self, *, once: bool = False, poll_seconds: float = DEFAULT_POLL_SECONDS
    ) -> dict[str, Any]:
        if poll_seconds <= 0:
            raise CommerceOperatorError("invalid_poll_seconds")
        with WorkerLock(self.lock_path):
            recovery = self.store.recover(now=self.clock(), actor=WORKER_ACTOR)
            if once:
                return {"recovery": recovery, "tick": self.tick()}
            while True:
                self.tick()
                time.sleep(poll_seconds)

    def tick(self) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "enabled": False,
            "timed_out": 0,
            "jobs": 0,
            "advances": 0,
            "errors": 0,
        }
        if not self._enabled():
            return stats
        stats["enabled"] = True
        stats["timed_out"] = len(
            self.store.sweep_timeouts(now=self.clock(), actor=WORKER_ACTOR)
        )
        for job in self.store.list_jobs(active_only=True):
            if not self._enabled():
                stats["enabled"] = False
                break
            stats["jobs"] += 1
            try:
                stats["advances"] += self._advance_job(str(job["job_id"]))
            except CommerceStaleVersionError:
                continue
            except (CommerceJobError, CommerceOperatorError):
                stats["errors"] += 1
        return stats

    def _advance_job(self, job_id: str) -> int:
        advances = 0
        while advances < MAX_ADVANCES_PER_JOB and self._enabled():
            job = self.store.get_job(job_id)
            state = str(job["current_state"])
            moved = False
            if state == "requested":
                self.store.transition(
                    job_id,
                    "planning",
                    expected_state=state,
                    expected_version=int(job["row_version"]),
                    actor=WORKER_ACTOR,
                    reason_code="worker_claimed_job",
                    now=self.clock(),
                )
                moved = True
            elif state == "planning":
                moved = self._plan(job)
            elif state == "awaiting_cal":
                self._maintain_browser_gate(job)
                moved = self._advance_waiting_gate(job)
            elif state == "resuming":
                moved = self._resume(job)
            elif state == "ready":
                moved = self._advance_ready(job)
            elif state in _APPROVAL_STATES.values():
                moved = self._advance_approval(job)
            elif state in {"executing_read_only", "executing"}:
                moved = self._advance_execution(job)
            elif state == "verifying":
                moved = self._advance_verifying(job)
            elif state == "uncertain_external_state":
                self.store.transition(
                    job_id,
                    "reconciliation_required",
                    expected_state=state,
                    expected_version=int(job["row_version"]),
                    actor=WORKER_ACTOR,
                    reason_code="provider_truth_reconciliation_started",
                    now=self.clock(),
                )
                moved = True
            elif state == "reconciliation_required":
                moved = self._reconcile(job)
            if not moved:
                break
            advances += 1
        return advances

    def _maintain_browser_gate(self, job: Mapping[str, Any]) -> None:
        """Keep a viewer gate attachable without navigating a live session."""

        gate_id = str(job.get("current_gate_id") or "")
        if not gate_id:
            return
        gate = self.store.get_gate(gate_id)
        if gate["status"] != "open" or gate["gate_type"] in {
            "facts",
            "action_approval",
        }:
            return
        evidence = gate.get("opening_evidence")
        entry_url = evidence.get("entry_url") if isinstance(evidence, Mapping) else None
        if not isinstance(entry_url, str) or not entry_url:
            raise CommerceOperatorError("browser_gate_entry_missing")
        checked_at = self._browser_checked_at.get(gate_id)
        now = self.clock().timestamp()
        if checked_at is not None and now - checked_at < BROWSER_RECHECK_SECONDS:
            return
        self._browser_checked_at[gate_id] = now
        try:
            self.browser_ensure(
                str(job["job_id"]), str(job["browser_session"]), entry_url
            )
        except BrowserLifecycleError:
            raise CommerceOperatorError("browser_session_unavailable") from None
        except Exception:
            raise CommerceOperatorError("browser_session_unavailable") from None

    def _pause(self, job: Mapping[str, Any], reason: str) -> bool:
        self.store.pause(
            str(job["job_id"]),
            expected_version=int(job["row_version"]),
            actor=WORKER_ACTOR,
            reason=_code(reason, "operator_error"),
            now=self.clock(),
        )
        return True

    def _planning_facts(self, job_id: str) -> dict[str, Any]:
        try:
            candidate = self.approved_facts_loader()
        except Exception:
            candidate = {}
        approved = (
            {
                fact_code: candidate[fact_code]
                for fact_code in REQUIRED_FACTS
                if fact_code in candidate and _strict_fact_value(candidate[fact_code])
            }
            if isinstance(candidate, Mapping)
            else {}
        )
        approved.update(self.store.latest_facts(job_id))
        return approved

    def _plan(self, job: Mapping[str, Any]) -> bool:
        try:
            raw = self.planner(job, self._planning_facts(str(job["job_id"])))
            if not isinstance(raw, Mapping):
                raise CommerceOperatorError("planner_output_invalid")
            missing = self._missing_fact_codes(raw.get("missing_facts", []))
            if missing:
                gate = self.store.open_gate(
                    str(job["job_id"]),
                    gate_type="facts",
                    human_action="Provide the missing approved launch facts.",
                    provider_truth_reference="commerce.facts",
                    opening_evidence={"missing_fact_codes": missing},
                    actor=WORKER_ACTOR,
                    now=self.clock(),
                )
                self.store.transition(
                    str(job["job_id"]),
                    "awaiting_cal",
                    expected_state="planning",
                    expected_version=int(job["row_version"]),
                    actor=WORKER_ACTOR,
                    reason_code="required_facts_missing",
                    gate_id=str(gate["gate_id"]),
                    evidence={"missing_fact_codes": missing},
                    now=self.clock(),
                )
                return True
            plan = self._normalize_plan(raw)
        except CommerceJobError:
            raise
        except CommerceOperatorError as error:
            return self._pause(job, error.code)
        except Exception:
            return self._pause(job, "planner_failed")
        updated = self.store.set_plan(
            str(job["job_id"]),
            plan,
            expected_version=int(job["row_version"]),
            actor=WORKER_ACTOR,
            now=self.clock(),
        )
        self.store.transition(
            str(job["job_id"]),
            "ready",
            expected_state="planning",
            expected_version=int(updated["row_version"]),
            actor=WORKER_ACTOR,
            reason_code="plan_ready",
            evidence={"step_count": len(plan["steps"])},
            now=self.clock(),
        )
        return True

    @staticmethod
    def _missing_fact_codes(value: Any) -> list[str]:
        if value in (None, []):
            return []
        if not isinstance(value, list):
            raise CommerceOperatorError("missing_facts_invalid")
        codes = sorted({_code(item, "facts_required") for item in value})
        return codes[:32]

    @staticmethod
    def _normalize_plan(raw: Mapping[str, Any]) -> dict[str, Any]:
        plan = dict(raw)
        plan.pop("missing_facts", None)
        steps = plan.get("steps", [])
        if not isinstance(steps, list) or len(steps) > 64:
            raise CommerceOperatorError("plan_steps_invalid")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw_step in enumerate(steps):
            if not isinstance(raw_step, Mapping):
                raise CommerceOperatorError("plan_step_invalid")
            action_type = _code(raw_step.get("action_type"), "")
            provider = _code(raw_step.get("provider"), "")
            if not action_type or not provider:
                raise CommerceOperatorError("plan_step_code_invalid")
            step_id = _code(raw_step.get("step_id"), f"s{index}_{action_type}")
            if step_id in seen:
                raise CommerceOperatorError("plan_step_duplicate")
            seen.add(step_id)
            effect = raw_step.get("effect_class")
            if effect not in {"read_only", "idempotent_write", "consequential"}:
                raise CommerceOperatorError("plan_effect_invalid")
            request = raw_step.get("request", {})
            if not isinstance(request, Mapping):
                raise CommerceOperatorError("plan_request_invalid")
            base_key = _reference(raw_step.get("idempotency_key"))
            if not base_key or len(base_key) > 200:
                raise CommerceOperatorError("plan_idempotency_key_invalid")
            target = "executing_read_only" if effect == "read_only" else "executing"
            next_state = raw_step.get("next_state", "ready")
            if next_state not in _NEXT_STATES[target]:
                raise CommerceOperatorError("plan_next_state_invalid")
            approval_kind = raw_step.get("approval_kind", "purchase")
            if effect == "consequential" and approval_kind not in _APPROVAL_STATES:
                raise CommerceOperatorError("plan_approval_kind_invalid")
            attempts = raw_step.get("max_attempts", 3)
            if effect == "consequential":
                attempts = 1
            if (
                not isinstance(attempts, int)
                or isinstance(attempts, bool)
                or not 1 <= attempts <= 3
            ):
                raise CommerceOperatorError("plan_attempts_invalid")
            normalized.append({
                "step_id": step_id,
                "action_type": action_type,
                "provider": provider,
                "effect_class": effect,
                "idempotency_key": base_key,
                "request": dict(request),
                "target_state": target,
                "next_state": next_state,
                "max_attempts": attempts,
                "approval_kind": approval_kind,
                "approval_reference": _reference(raw_step.get("approval_reference")),
            })
        plan["steps"] = normalized
        return plan

    def _resume_completed_gate(self, job: Mapping[str, Any]) -> bool:
        gate_id = str(job.get("current_gate_id") or "")
        if not gate_id:
            return False
        gate = self.store.get_gate(gate_id)
        if gate["status"] != "completed":
            return False
        self.store.transition(
            str(job["job_id"]),
            "resuming",
            expected_state="awaiting_cal",
            expected_version=int(job["row_version"]),
            actor=WORKER_ACTOR,
            reason_code="verified_gate_complete",
            now=self.clock(),
        )
        return True

    def _advance_waiting_gate(self, job: Mapping[str, Any]) -> bool:
        gate_id = str(job.get("current_gate_id") or "")
        if not gate_id:
            return False
        gate = self.store.get_gate(gate_id)
        if gate["status"] == "completed":
            return self._resume_completed_gate(job)
        if gate["status"] != "open":
            return False
        evidence: Mapping[str, Any] | None = None
        if gate["gate_type"] == "facts":
            facts = self._planning_facts(str(job["job_id"]))
            if all(name in facts for name in REQUIRED_FACTS):
                evidence = {
                    "provider_truth_verified": True,
                    "fact_codes": sorted(REQUIRED_FACTS),
                }
        elif gate.get("done_requested_at"):
            verifier = self.gate_verifiers.get(str(gate["gate_type"]))
            if verifier is not None:
                try:
                    candidate = verifier(job, gate)
                except Exception:
                    candidate = None
                if isinstance(candidate, Mapping):
                    evidence = candidate
        if evidence is None or evidence.get("provider_truth_verified") is not True:
            return False
        self.store.complete_gate(
            gate_id,
            evidence=evidence,
            actor=WORKER_ACTOR,
            now=self.clock(),
        )
        return self._resume_completed_gate(self.store.get_job(str(job["job_id"])))

    def _resume(self, job: Mapping[str, Any]) -> bool:
        unresolved = [
            action
            for action in self.store.list_actions(str(job["job_id"]))
            if action["action_status"] in {"dispatched", "recoverable", "uncertain"}
        ]
        if unresolved:
            return False
        step, action, exhausted = self._next_step(job)
        if step is not None:
            if exhausted and action is None:
                return self._pause(job, "step_attempts_exhausted")
            action = action or self._record_action(job, step)
            if action["action_status"] != "planned":
                return False
            if step["effect_class"] == "consequential":
                return self._open_approval(job, step, action)
            return self._dispatch(job, step, action)

        self.store.transition(
            str(job["job_id"]),
            "planning",
            expected_state="resuming",
            expected_version=int(job["row_version"]),
            actor=WORKER_ACTOR,
            reason_code="rebuild_plan_after_gate",
            now=self.clock(),
        )
        return True

    @staticmethod
    def _steps(job: Mapping[str, Any]) -> list[dict[str, Any]]:
        steps = job.get("plan", {}).get("steps", [])
        return [dict(step) for step in steps] if isinstance(steps, list) else []

    def _step_actions(
        self, job_id: str, step: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        return [
            action
            for action in self.store.list_actions(job_id)
            if action.get("request", {}).get("step_id") == step["step_id"]
        ]

    def _next_step(
        self, job: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
        for step in self._steps(job):
            actions = self._step_actions(str(job["job_id"]), step)
            if any(action["action_status"] == "succeeded" for action in actions):
                continue
            active = next(
                (
                    action
                    for action in reversed(actions)
                    if action["action_status"]
                    in {"planned", "dispatched", "recoverable", "uncertain"}
                ),
                None,
            )
            exhausted = len(actions) >= int(step["max_attempts"])
            return step, active, exhausted
        return None, None, False

    def _record_action(
        self, job: Mapping[str, Any], step: Mapping[str, Any]
    ) -> dict[str, Any]:
        prior = self._step_actions(str(job["job_id"]), step)
        attempt = len(prior) + 1
        base_key = str(step["idempotency_key"])
        store_key = (
            base_key
            if step["effect_class"] == "consequential"
            else f"{base_key}:attempt:{attempt}"
        )
        return self.store.record_action(
            str(job["job_id"]),
            action_type=str(step["action_type"]),
            provider=str(step["provider"]),
            effect_class=str(step["effect_class"]),
            idempotency_key=store_key,
            request={
                "step_id": step["step_id"],
                "provider_idempotency_key": base_key,
                "input": step["request"],
            },
            target_state=str(step["target_state"]),
            now=self.clock(),
        )

    def _advance_ready(self, job: Mapping[str, Any]) -> bool:
        step, action, exhausted = self._next_step(job)
        if step is None:
            return False
        if exhausted and action is None:
            return self._pause(job, "step_attempts_exhausted")
        action = action or self._record_action(job, step)
        if action["action_status"] != "planned":
            return False
        if step["effect_class"] == "consequential":
            return self._open_approval(job, step, action)
        if str(step["target_state"]) not in ALLOWED_TRANSITIONS["ready"]:
            return self._pause(job, "plan_step_order_invalid")
        return self._dispatch(job, step, action)

    def _open_approval(
        self,
        job: Mapping[str, Any],
        step: Mapping[str, Any],
        action: Mapping[str, Any],
    ) -> bool:
        approval_state = _APPROVAL_STATES[str(step["approval_kind"])]
        if approval_state not in ALLOWED_TRANSITIONS[str(job["current_state"])]:
            return self._pause(job, "approval_step_order_invalid")
        approval_reference = str(step["approval_reference"])
        if not approval_reference:
            return self._pause(job, "approval_reference_missing")
        gate = self._create_approval_gate(job, step, action)
        self.store.transition(
            str(job["job_id"]),
            approval_state,
            expected_state=str(job["current_state"]),
            expected_version=int(job["row_version"]),
            actor=WORKER_ACTOR,
            reason_code="action_approval_required",
            evidence={"action_id": action["action_id"], "gate_id": gate["gate_id"]},
            now=self.clock(),
        )
        return True

    def _create_approval_gate(
        self,
        job: Mapping[str, Any],
        step: Mapping[str, Any],
        action: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self.store.open_gate(
            str(job["job_id"]),
            gate_type="action_approval",
            human_action=f"Approve the exact {step['action_type'].replace('_', ' ')} action.",
            provider_truth_reference="commerce.action_fingerprint",
            opening_evidence={"action_id": action["action_id"]},
            approval_reference=str(step["approval_reference"]),
            approval_fingerprint=str(action["action_fingerprint"]),
            actor=WORKER_ACTOR,
            now=self.clock(),
        )

    def _approval_action(self, job: Mapping[str, Any]) -> dict[str, Any] | None:
        actions = self.store.list_actions(str(job["job_id"]))
        return next(
            (
                action
                for action in reversed(actions)
                if action["effect_class"] == "consequential"
                and action["action_status"] == "planned"
            ),
            None,
        )

    def _advance_approval(self, job: Mapping[str, Any]) -> bool:
        action = self._approval_action(job)
        if action is None:
            return False
        step = next(
            (
                step
                for step in self._steps(job)
                if step["step_id"] == action.get("request", {}).get("step_id")
            ),
            None,
        )
        if step is None:
            return False
        expiry = _parse_time(action.get("approval_expires_at"))
        if action["approval_status"] == "live" and expiry and expiry > self.clock():
            return self._dispatch(job, step, action)
        gates = [
            gate
            for gate in self.store.list_gates(str(job["job_id"]))
            if gate["gate_type"] == "action_approval"
            and gate["approval_fingerprint"] == action["action_fingerprint"]
        ]
        gate = next(
            (item for item in gates if item["status"] in {"open", "completed"}),
            gates[-1] if gates else None,
        )
        gate_expiry = _parse_time(gate.get("expires_at")) if gate else None
        if (
            gate
            and gate["status"] == "completed"
            and (gate_expiry is None or gate_expiry <= self.clock())
        ):
            self._create_approval_gate(job, step, action)
            return True
        if gate and gate["status"] == "completed":
            if gate["completion_evidence"].get("approval_granted") is not True:
                self.store.transition(
                    str(job["job_id"]),
                    "cancelled",
                    expected_state=str(job["current_state"]),
                    expected_version=int(job["row_version"]),
                    actor=WORKER_ACTOR,
                    reason_code="action_approval_denied",
                    evidence={"action_id": action["action_id"]},
                    now=self.clock(),
                )
                return True
            action = self.store.authorize_action(
                str(action["action_id"]),
                gate_id=str(gate["gate_id"]),
                actor=WORKER_ACTOR,
                now=self.clock(),
            )
            return self._dispatch(job, step, action)
        if gate and gate["status"] == "open":
            if gate_expiry and gate_expiry <= self.clock():
                self.store.expire_gate(
                    str(gate["gate_id"]), now=self.clock(), actor=WORKER_ACTOR
                )
                gate = None
            else:
                return False
        if gate is None or gate["status"] in {
            "consumed",
            "expired",
            "invalidated",
        }:
            self._create_approval_gate(job, step, action)
            return True
        return False

    def _dispatch(
        self,
        job: Mapping[str, Any],
        step: Mapping[str, Any],
        action: Mapping[str, Any],
    ) -> bool:
        if not self._enabled():
            return False
        self.store.dispatch_and_transition(
            str(action["action_id"]),
            expected_state=str(job["current_state"]),
            expected_version=int(job["row_version"]),
            actor=WORKER_ACTOR,
            now=self.clock(),
        )
        return True

    def _advance_execution(self, job: Mapping[str, Any]) -> bool:
        matching = [
            item
            for item in self.store.list_actions(str(job["job_id"]))
            if item["action_type"] == job["current_step"]
        ]
        action = next(
            (
                item
                for item in matching
                if item["action_status"] in {"dispatched", "recoverable", "uncertain"}
            ),
            matching[-1] if matching else None,
        )
        if action is None:
            return False
        step = next(
            (
                item
                for item in self._steps(job)
                if item["step_id"] == action.get("request", {}).get("step_id")
            ),
            None,
        )
        if step is None:
            return False
        if action["action_status"] == "recoverable":
            if not self._enabled():
                return False
            result = self.store.dispatch_and_transition(
                str(action["action_id"]),
                expected_state=str(job["current_state"]),
                expected_version=int(job["row_version"]),
                actor=WORKER_ACTOR,
                reason_code="recoverable_action_redispatched",
                now=self.clock(),
            )
            action = result["action"]
        if action["action_status"] == "dispatched":
            return self._execute(job, step, action)
        if action["action_status"] == "succeeded":
            return self._after_success(job, step, action)
        if action["action_status"] == "failed":
            self.store.transition(
                str(job["job_id"]),
                "ready",
                expected_state=str(job["current_state"]),
                expected_version=int(job["row_version"]),
                actor=WORKER_ACTOR,
                reason_code="step_failed",
                evidence={"action_id": action["action_id"]},
                now=self.clock(),
            )
            return True
        return False

    def _advance_verifying(self, job: Mapping[str, Any]) -> bool:
        step, action, exhausted = self._next_step(job)
        if step is None or (exhausted and action is None):
            self.store.transition(
                str(job["job_id"]),
                "ready",
                expected_state="verifying",
                expected_version=int(job["row_version"]),
                actor=WORKER_ACTOR,
                reason_code="verification_fix_loop",
                now=self.clock(),
            )
            return True
        action = action or self._record_action(job, step)
        if (
            step["effect_class"] == "consequential"
            and step["approval_kind"] == "publication"
        ):
            return self._open_approval(job, step, action)
        self.store.transition(
            str(job["job_id"]),
            "ready",
            expected_state="verifying",
            expected_version=int(job["row_version"]),
            actor=WORKER_ACTOR,
            reason_code="verification_step_ready",
            now=self.clock(),
        )
        return True

    def _reconcile(self, job: Mapping[str, Any]) -> bool:
        action = next(
            (
                item
                for item in reversed(self.store.list_actions(str(job["job_id"])))
                if item["action_status"] == "uncertain"
            ),
            None,
        )
        if action is None:
            return False
        reconciler = self.reconcilers.get(str(action["action_type"]))
        if reconciler is None:
            return False
        try:
            result = reconciler(job, action)
        except Exception:
            return False
        if not isinstance(result, Mapping) or result.get("status") == "pending":
            return False
        status = result.get("status")
        evidence = result.get("evidence")
        if (
            status not in {"succeeded", "failed"}
            or not isinstance(evidence, Mapping)
            or evidence.get("provider_truth_verified") is not True
        ):
            return False
        self.store.resolve_uncertain_action(
            str(action["action_id"]),
            status=str(status),
            evidence=evidence,
            expected_version=int(job["row_version"]),
            actor=WORKER_ACTOR,
            now=self.clock(),
        )
        return True

    def _execute(
        self,
        job: Mapping[str, Any],
        step: Mapping[str, Any],
        action: Mapping[str, Any],
    ) -> bool:
        if not self._enabled():
            return False
        handler = self.step_handlers.get(str(step["action_type"]))
        if handler is None:
            error = ProviderStepError("step_handler_missing")
        else:
            try:
                result = handler(job, action)
                if not isinstance(result, Mapping):
                    raise ProviderStepError("provider_result_invalid")
                return self._finish_handler_result(job, step, action, result)
            except ProviderStepError as typed_error:
                error = typed_error
            except Exception:
                error = ProviderStepError(
                    "provider_outcome_unknown"
                    if step["effect_class"] == "consequential"
                    else "provider_step_failed",
                    uncertain=step["effect_class"] == "consequential",
                )
        uncertain = error.uncertain
        self.store.finish_action(
            str(action["action_id"]),
            status="uncertain" if uncertain else "failed",
            result={"error_code": error.code},
            now=self.clock(),
        )
        self.store.transition(
            str(job["job_id"]),
            "uncertain_external_state" if uncertain else "ready",
            expected_state=str(job["current_state"]),
            expected_version=int(job["row_version"]),
            actor=WORKER_ACTOR,
            reason_code=(
                "consequential_outcome_unknown"
                if uncertain
                else "recoverable_step_failed"
            ),
            evidence={"action_id": action["action_id"], "error_code": error.code},
            now=self.clock(),
        )
        return True

    def _finish_handler_result(
        self,
        job: Mapping[str, Any],
        step: Mapping[str, Any],
        action: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> bool:
        clean_result = dict(result)
        replacement = clean_result.pop("_replace_plan", None)
        human_gate = clean_result.pop("_human_gate", None)
        completion = clean_result.pop("_complete", None)
        if any(str(key).startswith("_") for key in clean_result):
            raise ProviderStepError("provider_result_control_invalid")
        if replacement is not None:
            if not isinstance(replacement, Mapping):
                raise ProviderStepError("replacement_plan_invalid")
            normalized = self._normalize_plan(replacement)
            current = self.store.get_job(str(job["job_id"]))
            self.store.set_plan(
                str(job["job_id"]),
                normalized,
                expected_version=int(current["row_version"]),
                actor=WORKER_ACTOR,
                now=self.clock(),
            )
        control: dict[str, Any] = {}
        if human_gate is not None:
            control["human_gate"] = self._normalize_human_gate(human_gate)
        if completion is not None:
            if not isinstance(completion, Mapping):
                raise ProviderStepError("completion_payload_invalid")
            control["completion"] = dict(completion)
        if control:
            clean_result[_CONTROL_KEY] = control
        self.store.finish_action(
            str(action["action_id"]),
            status="succeeded",
            result=clean_result,
            now=self.clock(),
        )
        return self._after_success(
            job, step, self.store.get_action(str(action["action_id"]))
        )

    def _after_success(
        self,
        job: Mapping[str, Any],
        finished_step: Mapping[str, Any],
        action: Mapping[str, Any] | None = None,
    ) -> bool:
        current = self.store.get_job(str(job["job_id"]))
        result = action.get("result") if isinstance(action, Mapping) else None
        control = result.get(_CONTROL_KEY) if isinstance(result, Mapping) else None
        if isinstance(control, Mapping) and "human_gate" in control:
            return self._open_human_gate(current, action, control["human_gate"])
        if isinstance(control, Mapping) and "completion" in control:
            completion = control["completion"]
            if (
                not isinstance(completion, Mapping)
                or current["current_state"] != "executing_read_only"
            ):
                return self._pause(current, "receipt_completion_invalid")
            if self.completion_handler is None:
                return self._pause(current, "receipt_writer_missing")
            try:
                receipt = self.completion_handler(str(job["job_id"]), completion)
            except Exception:
                raise CommerceOperatorError("receipt_persistence_failed") from None
            receipt_ref = (
                _reference(receipt.get("receipt_ref"))
                if isinstance(receipt, Mapping)
                else ""
            )
            if not receipt_ref:
                raise CommerceOperatorError("receipt_persistence_failed")
            self.store.transition(
                str(job["job_id"]),
                "completed",
                expected_state="executing_read_only",
                expected_version=int(current["row_version"]),
                actor=WORKER_ACTOR,
                reason_code="receipt_persisted",
                evidence={"receipt_ref": receipt_ref},
                now=self.clock(),
            )
            return True
        next_step, next_action, exhausted = self._next_step(current)
        if next_step is None:
            self.store.transition(
                str(job["job_id"]),
                str(finished_step["next_state"]),
                expected_state=str(current["current_state"]),
                expected_version=int(current["row_version"]),
                actor=WORKER_ACTOR,
                reason_code="step_succeeded",
                now=self.clock(),
            )
            return True
        if exhausted and next_action is None:
            self.store.transition(
                str(job["job_id"]),
                "ready",
                expected_state=str(current["current_state"]),
                expected_version=int(current["row_version"]),
                actor=WORKER_ACTOR,
                reason_code="step_attempts_exhausted",
                now=self.clock(),
            )
            return True
        next_action = next_action or self._record_action(current, next_step)
        approval_state = _APPROVAL_STATES[str(next_step["approval_kind"])]
        if next_step["effect_class"] == "consequential":
            if approval_state in ALLOWED_TRANSITIONS[str(current["current_state"])]:
                return self._open_approval(current, next_step, next_action)
        elif (
            str(next_step["target_state"])
            in ALLOWED_TRANSITIONS[str(current["current_state"])]
        ):
            return self._dispatch(current, next_step, next_action)
        self.store.transition(
            str(job["job_id"]),
            str(finished_step["next_state"]),
            expected_state=str(current["current_state"]),
            expected_version=int(current["row_version"]),
            actor=WORKER_ACTOR,
            reason_code="step_succeeded",
            now=self.clock(),
        )
        return True

    @staticmethod
    def _normalize_human_gate(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ProviderStepError("human_gate_invalid")
        required = {
            "gate_type",
            "human_action",
            "provider_truth_reference",
            "opening_evidence",
        }
        if set(value) != required or not isinstance(value["opening_evidence"], Mapping):
            raise ProviderStepError("human_gate_invalid")
        gate_type = _code(value["gate_type"], "")
        human_action = value["human_action"]
        truth = value["provider_truth_reference"]
        if (
            not gate_type
            or not isinstance(human_action, str)
            or not human_action.strip()
            or len(human_action) > 1_000
            or not isinstance(truth, str)
            or not truth.strip()
            or len(truth) > 1_000
        ):
            raise ProviderStepError("human_gate_invalid")
        return {
            "gate_type": gate_type,
            "human_action": human_action.strip(),
            "provider_truth_reference": truth.strip(),
            "opening_evidence": dict(value["opening_evidence"]),
        }

    def _open_human_gate(
        self,
        job: Mapping[str, Any],
        action: Mapping[str, Any] | None,
        spec: Any,
    ) -> bool:
        current_state = str(job["current_state"])
        if not isinstance(action, Mapping) or current_state not in {
            "executing_read_only",
            "executing",
        }:
            return self._pause(job, "human_gate_state_invalid")
        normalized = self._normalize_human_gate(spec)
        evidence = dict(normalized["opening_evidence"])
        evidence["source_action_id"] = str(action["action_id"])
        gate = next(
            (
                candidate
                for candidate in reversed(self.store.list_gates(str(job["job_id"])))
                if candidate["status"] == "open"
                and isinstance(candidate.get("opening_evidence"), Mapping)
                and candidate["opening_evidence"].get("source_action_id")
                == action["action_id"]
            ),
            None,
        )
        if gate is None:
            gate = self.store.open_gate(
                str(job["job_id"]),
                gate_type=normalized["gate_type"],
                human_action=normalized["human_action"],
                provider_truth_reference=normalized["provider_truth_reference"],
                opening_evidence=evidence,
                actor=WORKER_ACTOR,
                now=self.clock(),
            )
        self.store.transition(
            str(job["job_id"]),
            "awaiting_cal",
            expected_state=current_state,
            expected_version=int(job["row_version"]),
            actor=WORKER_ACTOR,
            reason_code="provider_human_gate_required",
            gate_id=str(gate["gate_id"]),
            now=self.clock(),
        )
        return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Virgil commerce worker")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--fake-provider", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    operator = CommerceOperator(
        store=CommerceJobStore(args.db) if args.db else None,
        planner=fake_planner if args.fake_provider else default_planner,
        lock_path=args.lock,
    )
    try:
        result = operator.run(once=args.once, poll_seconds=args.poll_seconds)
    except WorkerAlreadyRunningError as error:
        print(json.dumps({"success": False, "error_code": error.code}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    if args.once:
        print(json.dumps({"success": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

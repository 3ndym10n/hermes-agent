"""Structured task packet for the bounded backend coding worker.

A :class:`WorkerTaskPacket` is the *contract* handed to the controller
(``BackendWorker``).  It describes a single, narrowly-scoped backend coding
task together with the safety rails the controller must enforce.  The packet is
intentionally a frozen dataclass built from stdlib only so it can be serialized
to/from JSON, logged as evidence, and compared in tests without surprises.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

# Risk classifications the controller understands.  GREEN = trivially safe,
# YELLOW = needs care, RED = high-blast-radius.  The controller does not (yet)
# vary behaviour by class, but it records and validates it so escalation
# tooling can reason about it.
VALID_RISK_CLASSES = ("GREEN", "YELLOW", "RED")

# Valid backend coder selectors.  "auto" lets the controller pick the first
# available adapter; the others pin a specific backend.
VALID_WORKER_KINDS = ("auto", "claude", "codex", "fake")


@dataclass(frozen=True)
class WorkerTaskPacket:
    """An immutable description of one backend coding task.

    Glob fields (``allowed_files``, ``forbidden_surfaces``) and the ``tests``
    tuple are kept as tuples so the dataclass stays hashable and so callers
    cannot mutate the safety boundaries after construction.
    """

    objective: str
    success_condition: str
    repo: str
    allowed_files: tuple[str, ...]
    forbidden_surfaces: tuple[str, ...]
    tests: tuple[str, ...]
    rollback_boundary: str
    risk_classification: str
    approval_boundary: str
    base_branch: str = "main"
    branch_name: str | None = None
    timeout_seconds: int = 900
    worker_kind: str = "auto"

    def validate(self) -> list[str]:
        """Return a list of human-readable problems with this packet.

        An empty list means the packet is structurally valid.  ``tests`` being
        empty is allowed but produces a (non-fatal) warning entry so the caller
        can surface it.
        """

        errors: list[str] = []

        # Required, non-empty string fields.  We strip so whitespace-only
        # values count as empty.
        required_strings = {
            "objective": self.objective,
            "success_condition": self.success_condition,
            "repo": self.repo,
            "rollback_boundary": self.rollback_boundary,
            "approval_boundary": self.approval_boundary,
            "base_branch": self.base_branch,
        }
        for name, value in required_strings.items():
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{name} must be a non-empty string")

        # Risk classification must be one of the known levels.
        if self.risk_classification not in VALID_RISK_CLASSES:
            errors.append(
                "risk_classification must be one of "
                f"{'/'.join(VALID_RISK_CLASSES)} (got {self.risk_classification!r})"
            )

        # The worker must have at least one allowed-file glob, otherwise it has
        # no surface it is permitted to edit and every change would be a policy
        # violation.
        if not self.allowed_files:
            errors.append("allowed_files must contain at least one glob pattern")

        # Worker kind selector sanity.
        if self.worker_kind not in VALID_WORKER_KINDS:
            errors.append(
                "worker_kind must be one of "
                f"{'/'.join(VALID_WORKER_KINDS)} (got {self.worker_kind!r})"
            )

        # Timeout must be a positive integer.
        if not isinstance(self.timeout_seconds, int) or self.timeout_seconds <= 0:
            errors.append("timeout_seconds must be a positive integer")

        # Empty tests is allowed but worth flagging — a task with no test
        # commands cannot prove its own success condition.
        if not self.tests:
            errors.append(
                "WARNING: tests is empty — the worker cannot verify the "
                "success condition automatically"
            )

        return errors

    def to_dict(self) -> dict:
        """Serialize to a plain JSON-friendly dict (tuples become lists)."""

        result: dict = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, tuple):
                value = list(value)
            result[f.name] = value
        return result

    @classmethod
    def from_dict(cls, data: dict) -> WorkerTaskPacket:
        """Build a packet from a dict, normalizing list fields to tuples.

        Unknown keys are ignored so the packet schema can evolve without
        breaking older serialized payloads.
        """

        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}

        # Normalize the sequence fields to tuples so the frozen dataclass stays
        # hashable and immutable regardless of how the input was encoded.
        for seq_field in ("allowed_files", "forbidden_surfaces", "tests"):
            if seq_field in kwargs and kwargs[seq_field] is not None:
                kwargs[seq_field] = tuple(kwargs[seq_field])

        return cls(**kwargs)

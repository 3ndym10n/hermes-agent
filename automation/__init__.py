"""Bounded backend coding worker (the "Virgil" controller).

Public surface: a structured task packet, the safety policy helpers, the
pluggable coder adapters, and the controller that turns a packet into a PR.
"""

from __future__ import annotations

from .adapters import (
    AdapterResult,
    ClaudeWorkerAdapter,
    CodexWorkerAdapter,
    FakeWorkerAdapter,
    WorkerAdapter,
)
from .safety import (
    DEFAULT_PROTECTED_GLOBS,
    classify_changed_files,
    is_destructive_git,
    match_any,
)
from .task_packet import WorkerTaskPacket
from .worker import (
    BackendWorker,
    DestructiveGitError,
    GhPublisher,
    GitRunner,
    WorkerEvidence,
)

__all__ = [
    "AdapterResult",
    "BackendWorker",
    "ClaudeWorkerAdapter",
    "CodexWorkerAdapter",
    "DEFAULT_PROTECTED_GLOBS",
    "DestructiveGitError",
    "FakeWorkerAdapter",
    "GhPublisher",
    "GitRunner",
    "WorkerAdapter",
    "WorkerEvidence",
    "WorkerTaskPacket",
    "classify_changed_files",
    "is_destructive_git",
    "match_any",
]

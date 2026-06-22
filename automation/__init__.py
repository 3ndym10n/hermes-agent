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
from .automerge import AutoMerger, GhMergeClient, MergeDecision
from .safety import (
    DEFAULT_PROTECTED_GLOBS,
    classify_changed_files,
    is_destructive_git,
    match_any,
)
from .task_packet import WorkerTaskPacket
from .validator import (
    EvidenceValidator,
    FixLoopResult,
    Verdict,
    build_fix_packet,
    run_fix_loop,
)
from .worker import (
    BackendWorker,
    DestructiveGitError,
    GhPublisher,
    GitRunner,
    WorkerEvidence,
)

__all__ = [
    "AdapterResult",
    "AutoMerger",
    "BackendWorker",
    "ClaudeWorkerAdapter",
    "CodexWorkerAdapter",
    "DEFAULT_PROTECTED_GLOBS",
    "DestructiveGitError",
    "EvidenceValidator",
    "FakeWorkerAdapter",
    "FixLoopResult",
    "GhMergeClient",
    "GhPublisher",
    "GitRunner",
    "MergeDecision",
    "Verdict",
    "WorkerAdapter",
    "WorkerEvidence",
    "WorkerTaskPacket",
    "build_fix_packet",
    "classify_changed_files",
    "is_destructive_git",
    "match_any",
    "run_fix_loop",
]

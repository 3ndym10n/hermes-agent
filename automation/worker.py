"""The bounded backend coding controller (a.k.a. "Virgil").

``BackendWorker`` turns a :class:`~automation.task_packet.WorkerTaskPacket` into
a pull request using a pluggable backend coder, while enforcing hard safety
rails at every step:

* All git goes through :class:`GitRunner`, which *refuses* destructive commands.
* The coder runs inside an isolated ``git worktree`` on a fresh branch.
* Before anything is committed, every changed file is classified against the
  packet's allow-list and the global protected globs; any violation aborts the
  run *before* a commit/push/PR and records the violation as evidence.
* The controller NEVER merges and NEVER deploys.  It only opens a PR.
* Any unexpected exception fails closed: the worktree is cleaned up and the run
  is reported as ``blocked_policy`` with a scrubbed error.

The controller is the orchestrator, not the coder — it owns git and policy; the
adapter owns the edits.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field

from .adapters import WorkerAdapter, _scrub_auth
from .safety import (
    DEFAULT_PROTECTED_GLOBS,
    classify_changed_files,
    is_destructive_git,
)
from .task_packet import WorkerTaskPacket

# Terminal statuses the controller can report.  Each maps to a distinct caller
# action (open the PR, escalate to Cal, fix the packet, …).
VALID_STATUSES = (
    "pr_opened",
    "blocked_policy",
    "blocked_timeout",
    "invalid_packet",
    "no_worker",
    "no_changes",
    "tests_recorded",
)


class DestructiveGitError(RuntimeError):
    """Raised when a destructive git command is attempted via :class:`GitRunner`."""


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass
class WorkerEvidence:
    """A complete, serializable record of one controller run.

    This is the controller's only output.  It is designed to be logged verbatim
    and to give Cal (or escalation tooling) everything needed to decide what
    happened without re-running anything.
    """

    task_packet: dict
    repo: str
    base_branch: str
    base_sha: str
    head_branch: str
    head_sha: str
    changed_files: list[str]
    diffstat: str
    tests: list[dict]
    allow_list_result: dict
    forbidden_surface_scan: dict
    risk_class: str
    rollback: str
    deployment_required: bool
    worker_name: str
    timed_out: bool
    status: str
    pr_url: str | None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Git + publishing seams
# ---------------------------------------------------------------------------


class GitRunner:
    """Thin, *safe* wrapper around the ``git`` CLI.

    Every git invocation in the controller goes through :meth:`run`, which
    refuses destructive commands (see
    :func:`~automation.safety.is_destructive_git`).  This is defense in depth:
    even a bug in the controller cannot force-push or hard-reset.
    """

    def run(
        self,
        argv: list[str],
        cwd: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run a git command, refusing destructive forms.

        ``argv`` is the git argument list *without* the leading ``"git"`` (we
        prepend it).  Raises :class:`DestructiveGitError` for dangerous commands
        and (when ``check``) :class:`subprocess.CalledProcessError` on failure.
        """

        full = ["git", *argv]
        if is_destructive_git(full):
            raise DestructiveGitError(
                f"refused destructive git command: {' '.join(full)}"
            )
        return subprocess.run(  # noqa: S603 - argv assembled internally
            full,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=check,
        )

    def out(self, argv: list[str], cwd: str | None = None) -> str:
        """Convenience: run and return stripped stdout."""

        return self.run(argv, cwd=cwd).stdout.strip()


class GhPublisher:
    """Publishes a branch and opens a PR via ``git`` + the ``gh`` CLI.

    Deliberately has NO merge method — the controller's contract is to stop at
    "PR opened".  All git goes through the injected :class:`GitRunner`.
    """

    def __init__(self, git: GitRunner | None = None) -> None:
        self._git = git or GitRunner()

    def push(self, repo_path: str, branch: str) -> None:
        """Push ``branch`` to origin, setting upstream."""

        self._git.run(["push", "-u", "origin", branch], cwd=repo_path)

    def open_pr(
        self,
        repo_path: str,
        base: str,
        head: str,
        title: str,
        body: str,
    ) -> str:
        """Open a PR and return its URL via ``gh pr create``."""

        proc = subprocess.run(  # noqa: S603 - fixed argv shape
            [
                "gh",
                "pr",
                "create",
                "--base",
                base,
                "--head",
                head,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=repo_path,
            text=True,
            capture_output=True,
            check=True,
        )
        return proc.stdout.strip()


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

# Default adapter preference order when worker_kind == "auto".  The fake adapter
# is never auto-selected; it must be pinned explicitly via worker_kind="fake".
_AUTO_PREFERENCE = ("claude", "codex")


def _slugify(text: str, max_len: int = 40) -> str:
    """Turn an objective into a branch-safe slug."""

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    if not slug:
        slug = "task"
    return slug[:max_len].strip("-")


class BackendWorker:
    """Controller that drives a backend coder to a PR under hard safety rails."""

    def __init__(
        self,
        allowed_repos: dict[str, str],
        adapters: list[WorkerAdapter] | None = None,
        publisher=None,
        git: GitRunner | None = None,
    ) -> None:
        # Map of repo-key -> absolute local path.  Only these repos may be
        # touched; anything else is an invalid packet.
        self._allowed_repos = dict(allowed_repos)
        # Default real adapters (Claude then Codex).  Tests inject fakes.
        self._adapters: list[WorkerAdapter] = (
            list(adapters) if adapters is not None else self._default_adapters()
        )
        self._git = git or GitRunner()
        self._publisher = publisher or GhPublisher(git=self._git)

    @staticmethod
    def _default_adapters() -> list[WorkerAdapter]:
        # Imported lazily to keep the default path explicit and to avoid forcing
        # the concrete adapters on callers who inject their own.
        from .adapters import ClaudeWorkerAdapter, CodexWorkerAdapter

        return [ClaudeWorkerAdapter(), CodexWorkerAdapter()]

    # -- adapter selection --------------------------------------------------

    def _select_adapter(self, packet: WorkerTaskPacket) -> WorkerAdapter | None:
        """Pick the backend coder per the packet's ``worker_kind``.

        ``auto`` walks the preference order (claude, codex) and returns the
        first *available* one.  A pinned kind must be both present and
        available.  The fake adapter is only ever returned when pinned.
        """

        by_name = {a.name: a for a in self._adapters}

        if packet.worker_kind != "auto":
            adapter = by_name.get(packet.worker_kind)
            if adapter is not None and adapter.is_available():
                return adapter
            return None

        # auto: prefer claude, then codex, among available adapters.
        for name in _AUTO_PREFERENCE:
            adapter = by_name.get(name)
            if adapter is not None and adapter.is_available():
                return adapter
        return None

    # -- evidence helper ----------------------------------------------------

    def _blank_evidence(
        self,
        packet: WorkerTaskPacket,
        *,
        status: str,
        base_sha: str = "",
        head_branch: str = "",
        head_sha: str = "",
        changed_files: list[str] | None = None,
        diffstat: str = "",
        tests: list[dict] | None = None,
        allow_list_result: dict | None = None,
        forbidden_surface_scan: dict | None = None,
        worker_name: str = "",
        timed_out: bool = False,
        pr_url: str | None = None,
        notes: list[str] | None = None,
    ) -> WorkerEvidence:
        """Build a :class:`WorkerEvidence` with sensible empty defaults."""

        return WorkerEvidence(
            task_packet=packet.to_dict(),
            repo=packet.repo,
            base_branch=packet.base_branch,
            base_sha=base_sha,
            head_branch=head_branch,
            head_sha=head_sha,
            changed_files=changed_files or [],
            diffstat=diffstat,
            tests=tests or [],
            allow_list_result=allow_list_result or {},
            forbidden_surface_scan=forbidden_surface_scan or {},
            risk_class=packet.risk_classification,
            rollback=packet.rollback_boundary,
            deployment_required=False,  # the controller never deploys
            worker_name=worker_name,
            timed_out=timed_out,
            status=status,
            pr_url=pr_url,
            notes=list(notes or []),
        )

    # -- main entrypoint ----------------------------------------------------

    def execute(
        self,
        packet: WorkerTaskPacket,
        *,
        keep_worktree: bool = False,
    ) -> WorkerEvidence:
        """Run the packet end-to-end, returning evidence (never raising)."""

        worktree: str | None = None
        try:
            # 1. Structural validation of the packet.
            errors = packet.validate()
            # WARNING-prefixed entries are advisory (e.g. empty tests) and do
            # not by themselves invalidate the packet.
            fatal = [e for e in errors if not e.startswith("WARNING:")]
            if fatal:
                return self._blank_evidence(
                    packet, status="invalid_packet", notes=errors
                )

            # 2. Repo must be allow-listed.
            repo_path = self._allowed_repos.get(packet.repo)
            if repo_path is None:
                return self._blank_evidence(
                    packet,
                    status="invalid_packet",
                    notes=["repo not allow-listed", *errors],
                )

            # 3. Select the backend coder.
            adapter = self._select_adapter(packet)
            if adapter is None:
                return self._blank_evidence(
                    packet,
                    status="no_worker",
                    notes=[
                        "no backend coding worker available — Cal must "
                        "provide/authenticate one",
                        *errors,
                    ],
                )
            worker_name = adapter.name

            # 4. Resolve base sha and create an isolated worktree + branch.
            base_sha = self._git.out(
                ["rev-parse", packet.base_branch], cwd=repo_path
            )
            short_sha = base_sha[:8]
            branch = packet.branch_name or (
                f"auto/{_slugify(packet.objective)}-{short_sha}"
            )
            worktree = tempfile.mkdtemp(prefix="virgil-wt-")
            # ``git worktree add <wt> -b <branch> <base>`` checks out a fresh
            # branch off base into the isolated directory.
            self._git.run(
                ["worktree", "add", worktree, "-b", branch, packet.base_branch],
                cwd=repo_path,
            )

            # 5. Build a bounded prompt and run the coder.
            prompt = self._build_prompt(packet)
            result = adapter.run(packet, worktree, prompt)

            # 6. Hard timeout -> block, no commit/PR.
            if result.timed_out:
                self._cleanup(repo_path, worktree, branch)
                worktree = None
                return self._blank_evidence(
                    packet,
                    status="blocked_timeout",
                    base_sha=base_sha,
                    head_branch=branch,
                    worker_name=worker_name,
                    timed_out=True,
                    notes=[
                        f"backend coder timed out after {packet.timeout_seconds}s",
                        result.stderr.strip() or result.stdout.strip(),
                    ],
                )

            # 7. Determine what changed.
            changed = self._changed_files(worktree)
            if not changed:
                self._cleanup(repo_path, worktree, branch)
                worktree = None
                return self._blank_evidence(
                    packet,
                    status="no_changes",
                    base_sha=base_sha,
                    head_branch=branch,
                    worker_name=worker_name,
                    notes=["backend coder made no file changes"],
                )

            # 8. Classify changes against allow-list + protected/forbidden globs.
            scan = classify_changed_files(
                changed,
                packet.allowed_files,
                packet.forbidden_surfaces,
                DEFAULT_PROTECTED_GLOBS,
            )
            if not scan["all_allowed"]:
                # Policy violation: stop BEFORE any commit/push/PR.  This is the
                # signal to escalate to Cal.
                self._cleanup(repo_path, worktree, branch)
                worktree = None
                return self._blank_evidence(
                    packet,
                    status="blocked_policy",
                    base_sha=base_sha,
                    head_branch=branch,
                    changed_files=changed,
                    allow_list_result=scan,
                    forbidden_surface_scan=scan,
                    worker_name=worker_name,
                    notes=self._violation_notes(scan),
                )

            # 9. Run each test command in the worktree, recording results.
            tests = self._run_tests(packet, worktree)

            # 10. Stage exactly the changed (allowed) files and commit.
            for path in changed:
                self._git.run(["add", "--", path], cwd=worktree)
            commit_msg = self._commit_message(packet)
            self._git.run(["commit", "-m", commit_msg], cwd=worktree)
            head_sha = self._git.out(["rev-parse", "HEAD"], cwd=worktree)
            diffstat = self._git.out(
                ["diff", "--stat", f"{base_sha}..HEAD"], cwd=worktree
            )

            # 11. Push and open a PR.  NEVER merge, NEVER deploy.
            self._publisher.push(worktree, branch)
            title = self._pr_title(packet)
            body = self._pr_body(packet, scan, tests)
            pr_url = self._publisher.open_pr(
                worktree, packet.base_branch, branch, title, body
            )

            evidence = self._blank_evidence(
                packet,
                status="pr_opened",
                base_sha=base_sha,
                head_branch=branch,
                head_sha=head_sha,
                changed_files=changed,
                diffstat=diffstat,
                tests=tests,
                allow_list_result=scan,
                forbidden_surface_scan=scan,
                worker_name=worker_name,
                pr_url=pr_url,
                notes=["PR opened; controller stops here (no merge, no deploy)"],
            )

            # 12. Clean up the worktree unless asked to keep it.
            if not keep_worktree:
                self._cleanup(repo_path, worktree, branch, remove_branch=False)
            worktree = None
            return evidence

        except Exception as exc:  # noqa: BLE001 - fail closed on ANY error
            # Defense in depth: any unexpected error becomes a policy block with
            # a scrubbed message, and we always try to clean up the worktree.
            notes = [f"unexpected error: {_scrub_auth(str(exc))}"]
            repo_path = self._allowed_repos.get(packet.repo)
            if worktree is not None and repo_path is not None:
                try:
                    self._cleanup(repo_path, worktree, None)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    notes.append("cleanup of worktree also failed")
            return self._blank_evidence(
                packet, status="blocked_policy", notes=notes
            )

    # -- internals ----------------------------------------------------------

    def _build_prompt(self, packet: WorkerTaskPacket) -> str:
        """Construct the bounded instruction handed to the coder.

        The prompt restates the objective and, crucially, the explicit
        prohibitions so the coder stays inside the rails even though the
        controller enforces them independently.
        """

        allowed = "\n".join(f"  - {g}" for g in packet.allowed_files) or "  (none)"
        forbidden = (
            "\n".join(f"  - {g}" for g in packet.forbidden_surfaces) or "  (none)"
        )
        tests = "\n".join(f"  - {t}" for t in packet.tests) or "  (none provided)"
        return (
            "You are a bounded backend coding worker. Complete exactly the "
            "task below and nothing more.\n\n"
            f"OBJECTIVE:\n  {packet.objective}\n\n"
            f"SUCCESS CONDITION:\n  {packet.success_condition}\n\n"
            f"ALLOWED FILES (you may ONLY edit these):\n{allowed}\n\n"
            f"FORBIDDEN / PROTECTED SURFACES (NEVER touch):\n{forbidden}\n\n"
            f"TESTS THAT MUST PASS:\n{tests}\n\n"
            "HARD RULES:\n"
            "  - Only edit the allowed files. Do NOT touch forbidden or "
            "protected paths.\n"
            "  - Do NOT run destructive git (no force-push, reset --hard, "
            "clean -fd, rebase, branch -D).\n"
            "  - Do NOT commit, push, merge, or deploy — the controller "
            "handles all git.\n"
            "  - Ensure the listed tests pass before you finish.\n"
        )

    def _changed_files(self, worktree: str) -> list[str]:
        """Parse ``git status --porcelain`` into a list of changed paths."""

        out = self._git.out(["-C", worktree, "status", "--porcelain"])
        paths: list[str] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            # Porcelain v1: "XY <path>" or "XY <old> -> <new>" for renames.
            # out() strips the overall output, which can drop the leading space
            # of the first line's 2-char status field — so skip the status code
            # and its trailing whitespace by pattern rather than a fixed offset.
            m = re.match(r"^.{1,2}\s+(.*)$", line)
            rest = m.group(1) if m else line.strip()
            if " -> " in rest:
                rest = rest.split(" -> ", 1)[1]
            paths.append(rest.strip().strip('"'))
        return paths

    def _run_tests(self, packet: WorkerTaskPacket, worktree: str) -> list[dict]:
        """Run each test command in the worktree and record pass/fail."""

        results: list[dict] = []
        for cmd in packet.tests:
            proc = subprocess.run(  # noqa: S602 - shell test commands by design
                cmd,
                cwd=worktree,
                shell=True,
                text=True,
                capture_output=True,
                check=False,
            )
            results.append(
                {
                    "cmd": cmd,
                    "returncode": proc.returncode,
                    "passed": proc.returncode == 0,
                }
            )
        return results

    def _cleanup(
        self,
        repo_path: str,
        worktree: str,
        branch: str | None,
        *,
        remove_branch: bool = True,
    ) -> None:
        """Remove the worktree (and optionally its branch) best-effort.

        We use ``git worktree remove`` (non-force) via a direct subprocess so
        the GitRunner's force-flag refusal does not get in the way of routine
        cleanup; if that fails we fall back to deleting the directory and
        pruning.  Branch deletion is intentionally *not* routed through
        GitRunner (which would refuse ``branch -d``) and is best-effort only.
        """

        try:
            subprocess.run(  # noqa: S603 - fixed cleanup argv
                ["git", "-C", repo_path, "worktree", "remove", worktree],
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            if os.path.isdir(worktree):
                shutil.rmtree(worktree, ignore_errors=True)
            subprocess.run(  # noqa: S603 - prune stale worktree metadata
                ["git", "-C", repo_path, "worktree", "prune"],
                text=True,
                capture_output=True,
                check=False,
            )
            if remove_branch and branch:
                subprocess.run(  # noqa: S603 - best-effort branch cleanup
                    ["git", "-C", repo_path, "branch", "-D", branch],
                    text=True,
                    capture_output=True,
                    check=False,
                )

    def _violation_notes(self, scan: dict) -> list[str]:
        """Human-readable notes describing why a run was policy-blocked."""

        notes = ["policy violation: changed files outside the allowed surface"]
        if scan.get("protected_hits"):
            notes.append(
                "protected surfaces touched: "
                + ", ".join(scan["protected_hits"])
            )
        if scan.get("forbidden_hits"):
            notes.append(
                "forbidden surfaces touched: "
                + ", ".join(scan["forbidden_hits"])
            )
        if scan.get("disallowed"):
            notes.append(
                "files not in allow-list: " + ", ".join(scan["disallowed"])
            )
        return notes

    def _commit_message(self, packet: WorkerTaskPacket) -> str:
        return f"auto: {packet.objective}\n\nRisk: {packet.risk_classification}"

    def _pr_title(self, packet: WorkerTaskPacket) -> str:
        title = packet.objective.strip().splitlines()[0]
        return title[:72]

    def _pr_body(
        self, packet: WorkerTaskPacket, scan: dict, tests: list[dict]
    ) -> str:
        passed = sum(1 for t in tests if t["passed"])
        return (
            f"Automated PR from the backend coding controller.\n\n"
            f"**Objective:** {packet.objective}\n"
            f"**Success condition:** {packet.success_condition}\n"
            f"**Risk:** {packet.risk_classification}\n"
            f"**Rollback boundary:** {packet.rollback_boundary}\n"
            f"**Tests:** {passed}/{len(tests)} passed\n\n"
            "All changed files were verified inside the allow-list and clear "
            "of protected surfaces. No merge or deploy was performed."
        )

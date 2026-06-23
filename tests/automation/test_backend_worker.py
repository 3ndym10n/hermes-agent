"""Tests for the bounded backend coding worker (``automation`` package).

These tests build a *real* throwaway git repo in ``tmp_path`` and drive the
controller end-to-end with a :class:`FakeWorkerAdapter` (which mutates the
worktree directly) and a ``FakePublisher`` (which records calls and returns a
fake URL).  No real ``claude``/``codex`` CLI is invoked and there is no network
access — the ``gh``/push surface is replaced by the fake publisher.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from automation import (
    BackendWorker,
    ClaudeWorkerAdapter,
    FakeWorkerAdapter,
    WorkerTaskPacket,
    classify_changed_files,
    is_destructive_git,
)
from automation.adapters import _scrub_auth, default_subprocess_runner
from automation.safety import DEFAULT_PROTECTED_GLOBS, match_any
from automation.worker import GitRunner


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _git(repo: str, *args: str) -> str:
    """Run git in ``repo`` and return stripped stdout (test helper, raw git)."""

    proc = subprocess.run(
        ["git", "-C", repo, *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> str:
    """A real git repo on ``main`` with src/app.py and a test file."""

    repo_dir = tmp_path / "sample_repo"
    repo_dir.mkdir()
    rp = str(repo_dir)

    _git(rp, "init", "-b", "main")
    _git(rp, "config", "user.email", "test@example.com")
    _git(rp, "config", "user.name", "Test User")
    # Keep commits simple/deterministic.
    _git(rp, "config", "commit.gpgsign", "false")

    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "app.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (repo_dir / "config.yaml").write_text("name: sample\n", encoding="utf-8")
    (repo_dir / "tests").mkdir()
    (repo_dir / "tests" / "test_app.py").write_text(
        "from src.app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )

    _git(rp, "add", "-A")
    _git(rp, "commit", "-m", "initial commit")
    return rp


class FakePublisher:
    """Records push/open_pr calls and returns a fixed fake PR URL.

    Notably has NO merge method — asserting the controller never merges.
    """

    def __init__(self, url: str = "https://example.test/pr/1") -> None:
        self.url = url
        self.push_calls: list[tuple[str, str]] = []
        self.pr_calls: list[dict] = []

    def push(self, repo_path: str, branch: str) -> None:
        self.push_calls.append((repo_path, branch))

    def open_pr(
        self, repo_path: str, base: str, head: str, title: str, body: str
    ) -> str:
        self.pr_calls.append(
            {
                "repo_path": repo_path,
                "base": base,
                "head": head,
                "title": title,
                "body": body,
            }
        )
        return self.url


def make_packet(**overrides) -> WorkerTaskPacket:
    """Build a valid packet, overriding individual fields as needed."""

    defaults = dict(
        objective="Add a multiply helper to app",
        success_condition="multiply(2,3) == 6",
        repo="sample",
        allowed_files=("src/**",),
        forbidden_surfaces=(),
        tests=("true",),
        rollback_boundary="revert the PR",
        risk_classification="GREEN",
        approval_boundary="Cal reviews the PR",
        base_branch="main",
        worker_kind="fake",
    )
    defaults.update(overrides)
    return WorkerTaskPacket(**defaults)


def edit_app(workdir: str) -> None:
    """Adapter apply fn: append a function to the allowed src/app.py."""

    p = Path(workdir) / "src" / "app.py"
    text = p.read_text(encoding="utf-8")
    p.write_text(
        text + "\n\ndef multiply(a, b):\n    return a * b\n", encoding="utf-8"
    )


def edit_config(workdir: str) -> None:
    """Adapter apply fn: touch the protected config.yaml."""

    p = Path(workdir) / "config.yaml"
    p.write_text("name: tampered\n", encoding="utf-8")


def edit_disallowed(workdir: str) -> None:
    """Adapter apply fn: edit a file outside the allow-list."""

    p = Path(workdir) / "tests" / "test_app.py"
    p.write_text(
        p.read_text(encoding="utf-8") + "\n# touched\n", encoding="utf-8"
    )


def no_edit(workdir: str) -> None:
    """Adapter apply fn: change nothing."""


def write_new_dir_file(workdir: str) -> None:
    """Adapter apply fn: create a file inside a brand-new directory."""

    p = Path(workdir) / "docs" / "newdir" / "note.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("hello\n", encoding="utf-8")


def worker_for(repo: str, adapter, publisher=None) -> BackendWorker:
    return BackendWorker(
        allowed_repos={"sample": repo},
        adapters=[adapter],
        publisher=publisher or FakePublisher(),
    )


# ---------------------------------------------------------------------------
# Controller: happy path + status outcomes
# ---------------------------------------------------------------------------


def test_happy_path_opens_pr(repo):
    publisher = FakePublisher()
    worker = worker_for(repo, FakeWorkerAdapter(apply=edit_app), publisher)

    ev = worker.execute(make_packet())

    assert ev.status == "pr_opened"
    assert ev.pr_url == publisher.url
    assert "src/app.py" in ev.changed_files
    assert ev.allow_list_result["all_allowed"] is True
    assert ev.deployment_required is False
    # Publisher called exactly once each.
    assert len(publisher.push_calls) == 1
    assert len(publisher.pr_calls) == 1
    # The branch was created and the commit landed (head_sha differs from base).
    assert ev.head_sha and ev.head_sha != ev.base_sha
    # Hard guarantee: there is NO merge method anywhere on the publisher.
    assert not hasattr(publisher, "merge")
    # A test was recorded.
    assert ev.tests and ev.tests[0]["passed"] is True


def test_file_in_new_directory_is_classified_per_file(repo):
    # Regression: a file created inside a brand-new directory must be reported
    # per-file (not collapsed to "docs/newdir/"), so an exact-path allow-list
    # matches and the run reaches pr_opened instead of blocked_policy.
    publisher = FakePublisher()
    worker = worker_for(repo, FakeWorkerAdapter(apply=write_new_dir_file), publisher)

    ev = worker.execute(
        make_packet(
            objective="add a note in a new dir",
            success_condition="note.md exists",
            allowed_files=("docs/newdir/note.md",),
        )
    )

    assert ev.changed_files == ["docs/newdir/note.md"]
    assert ev.status == "pr_opened"
    assert ev.allow_list_result["all_allowed"] is True


def test_protected_surface_blocks(repo):
    publisher = FakePublisher()
    # Allow config.yaml in the packet to prove the *global* protected glob still
    # blocks it (defense in depth), not just the allow-list.
    worker = worker_for(repo, FakeWorkerAdapter(apply=edit_config), publisher)

    ev = worker.execute(make_packet(allowed_files=("src/**", "config.yaml")))

    assert ev.status == "blocked_policy"
    assert "config.yaml" in ev.forbidden_surface_scan["protected_hits"]
    # Nothing was published.
    assert publisher.push_calls == []
    assert publisher.pr_calls == []
    assert ev.pr_url is None


def test_disallowed_file_blocks(repo):
    publisher = FakePublisher()
    worker = worker_for(repo, FakeWorkerAdapter(apply=edit_disallowed), publisher)

    # allowed_files is src/** only; editing tests/ is disallowed.
    ev = worker.execute(make_packet())

    assert ev.status == "blocked_policy"
    assert any(
        "tests/test_app.py" in f for f in ev.allow_list_result["disallowed"]
    )
    assert publisher.pr_calls == []


def test_timeout_blocks(repo):
    publisher = FakePublisher()
    adapter = FakeWorkerAdapter(apply=edit_app, timed_out=True)
    worker = worker_for(repo, adapter, publisher)

    ev = worker.execute(make_packet())

    assert ev.status == "blocked_timeout"
    assert ev.timed_out is True
    assert publisher.push_calls == []
    assert ev.pr_url is None


def test_invalid_packet_empty_objective(repo):
    worker = worker_for(repo, FakeWorkerAdapter(apply=edit_app))
    ev = worker.execute(make_packet(objective="   "))
    assert ev.status == "invalid_packet"
    assert any("objective" in n for n in ev.notes)


def test_invalid_packet_bad_risk(repo):
    worker = worker_for(repo, FakeWorkerAdapter(apply=edit_app))
    ev = worker.execute(make_packet(risk_classification="PURPLE"))
    assert ev.status == "invalid_packet"
    assert any("risk_classification" in n for n in ev.notes)


def test_no_changes(repo):
    publisher = FakePublisher()
    worker = worker_for(repo, FakeWorkerAdapter(apply=no_edit), publisher)
    ev = worker.execute(make_packet())
    assert ev.status == "no_changes"
    assert publisher.pr_calls == []


def test_repo_not_allow_listed(repo):
    worker = BackendWorker(
        allowed_repos={"sample": repo},
        adapters=[FakeWorkerAdapter(apply=edit_app)],
        publisher=FakePublisher(),
    )
    ev = worker.execute(make_packet(repo="other"))
    assert ev.status == "invalid_packet"
    assert any("allow-listed" in n for n in ev.notes)


def test_no_worker_available(repo):
    # An adapter that reports unavailable; worker_kind auto -> no_worker.
    unavailable = FakeWorkerAdapter(apply=edit_app)
    unavailable.is_available = lambda which=None: False  # type: ignore[assignment]
    worker = BackendWorker(
        allowed_repos={"sample": repo},
        adapters=[unavailable],
        publisher=FakePublisher(),
    )
    ev = worker.execute(make_packet(worker_kind="auto"))
    assert ev.status == "no_worker"
    assert any("Cal" in n for n in ev.notes)


def test_worktree_cleaned_up_after_pr(repo):
    publisher = FakePublisher()
    worker = worker_for(repo, FakeWorkerAdapter(apply=edit_app), publisher)
    ev = worker.execute(make_packet())
    assert ev.status == "pr_opened"
    # No leftover virgil worktrees registered on the repo.
    wt_list = _git(repo, "worktree", "list")
    assert "virgil-wt-" not in wt_list


# ---------------------------------------------------------------------------
# Safety: is_destructive_git
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "push", "--force"],
        ["git", "push", "-f", "origin", "main"],
        ["git", "push", "--force-with-lease"],
        ["git", "reset", "--hard"],
        ["git", "reset", "--hard", "HEAD~1"],
        ["git", "clean", "-fdx"],
        ["git", "clean", "-f"],
        ["git", "branch", "-D", "x"],
        ["git", "branch", "-d", "x"],
        ["git", "rebase", "main"],
        ["git", "checkout", "--", "f"],
        ["git", "filter-branch"],
        ["git", "reflog"],
        ["git", "update-ref", "-d", "refs/heads/x"],
        ["git", "worktree", "remove", "--force", "wt"],
        ["git", "stash", "drop"],
        ["git", "gc", "--prune=now"],
        ["git", "-C", "/some/path", "push", "--force"],
    ],
)
def test_is_destructive_git_true(argv):
    assert is_destructive_git(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "status"],
        ["git", "add", "f"],
        ["git", "commit", "-m", "x"],
        ["git", "push", "-u", "origin", "b"],
        ["git", "rev-parse", "HEAD"],
        ["git", "diff", "--stat"],
        ["git", "worktree", "add", "wt", "-b", "br", "main"],
        ["git", "log"],
    ],
)
def test_is_destructive_git_false(argv):
    assert is_destructive_git(argv) is False


def test_git_runner_refuses_destructive(repo):
    runner = GitRunner()
    with pytest.raises(Exception) as exc:
        runner.run(["push", "--force"], cwd=repo)
    assert "destructive" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Safety: classify_changed_files
# ---------------------------------------------------------------------------


def test_classify_allowed():
    res = classify_changed_files(
        ["src/app.py"], ("src/**",), (), DEFAULT_PROTECTED_GLOBS
    )
    assert res["all_allowed"] is True
    assert res["disallowed"] == []


def test_classify_disallowed():
    res = classify_changed_files(
        ["docs/readme.md"], ("src/**",), (), DEFAULT_PROTECTED_GLOBS
    )
    assert res["all_allowed"] is False
    assert "docs/readme.md" in res["disallowed"]


def test_classify_forbidden():
    res = classify_changed_files(
        ["src/secrets/api.py"],
        ("src/**",),
        ("**/secrets/**",),
        (),
    )
    assert res["all_allowed"] is False
    assert "src/secrets/api.py" in res["forbidden_hits"]


def test_classify_protected():
    res = classify_changed_files(
        ["config.yaml", ".env.production"],
        ("**",),
        (),
        DEFAULT_PROTECTED_GLOBS,
    )
    assert res["all_allowed"] is False
    assert "config.yaml" in res["protected_hits"]
    assert ".env.production" in res["protected_hits"]


# ---------------------------------------------------------------------------
# Adapters: scrubbing + availability
# ---------------------------------------------------------------------------


def test_scrub_auth_redacts_token_and_header():
    text = (
        "fetching with token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ok\n"
        "Authorization: Bearer sk-ant-secretvalue1234567890\n"
        "Normal prose about adding two numbers stays intact."
    )
    out = _scrub_auth(text)
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in out
    assert "sk-ant-secretvalue" not in out
    assert "***REDACTED***" in out
    assert "Normal prose about adding two numbers stays intact." in out


def test_scrub_auth_redacts_key_value():
    out = _scrub_auth("OPENAI_API_KEY=sk-realkey1234567890 trailing")
    assert "sk-realkey1234567890" not in out
    assert "OPENAI_API_KEY=***REDACTED***" in out


def test_scrub_auth_leaves_prose():
    prose = "The quick brown fox commits c0ffee but does not leak anything."
    assert _scrub_auth(prose) == prose


def test_claude_adapter_availability_uses_injected_which():
    adapter = ClaudeWorkerAdapter()
    assert adapter.is_available(which=lambda _name: "/usr/bin/claude") is True
    assert adapter.is_available(which=lambda _name: None) is False


def test_fake_adapter_always_available():
    adapter = FakeWorkerAdapter(apply=no_edit)
    assert adapter.is_available() is True


def test_claude_build_argv_print_mode():
    adapter = ClaudeWorkerAdapter()
    argv = adapter.build_argv(make_packet(), "/tmp/wt", "do the thing")
    assert argv == ["claude", "-p", "do the thing"]


# ---------------------------------------------------------------------------
# task_packet
# ---------------------------------------------------------------------------


def test_packet_roundtrip_normalizes_tuples():
    p = make_packet()
    d = p.to_dict()
    assert isinstance(d["allowed_files"], list)
    p2 = WorkerTaskPacket.from_dict(d)
    assert isinstance(p2.allowed_files, tuple)
    assert p2 == p


def test_packet_empty_tests_warns_but_valid():
    p = make_packet(tests=())
    errors = p.validate()
    assert any(e.startswith("WARNING:") for e in errors)
    assert not [e for e in errors if not e.startswith("WARNING:")]


def test_env_var_isolation_sanity():
    # Guard: tests must never depend on real provider creds being set.
    # (The controller itself never reads them; this documents the contract.)
    assert "AUTOMATION_FORCE_REAL_CLI" not in os.environ


# ---------------------------------------------------------------------------
# Hardening: root-level protected globs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "secrets.txt",
        "secret_keys.json",
        "migrations/001_init.sql",
        "storage/db.sqlite",
        "app.service",
        "deploy.pem",
        "id_rsa",
    ],
)
def test_root_level_protected_paths_are_caught(path):
    # Regression: ``**/x`` protected globs must also catch a repo-ROOT ``x``
    # (PurePath.match treats ** like * on 3.11, so the matcher strips a leading
    # ``**/`` and retries). Without this, root-level secrets/storage/migrations
    # would bypass the defense-in-depth net.
    assert match_any(path, DEFAULT_PROTECTED_GLOBS) is True


@pytest.mark.parametrize(
    "path", ["sub/secrets.txt", "a/b/storage/x.db", "deep/nested/.env"]
)
def test_nested_protected_paths_still_caught(path):
    assert match_any(path, DEFAULT_PROTECTED_GLOBS) is True


def test_ordinary_source_paths_not_protected():
    assert match_any("src/app.py", DEFAULT_PROTECTED_GLOBS) is False
    assert match_any("README.md", DEFAULT_PROTECTED_GLOBS) is False


# ---------------------------------------------------------------------------
# Hardening: broadened auth scrubbing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prefix,body",
    [
        ("glpat-", "A" * 22),
        ("AKIA", "B" * 16),
        ("xoxb-", "1" * 12 + "-" + "c" * 12),
        ("ghp_", "D" * 36),
        ("sk-ant-", "api03x" + "E" * 16),
    ],
)
def test_scrub_redacts_token_shapes(prefix, body):
    # Build the token-shaped string at RUNTIME so no contiguous secret literal
    # lives in source (avoids tripping GitHub push-protection on a fake token).
    secret = prefix + body
    out = _scrub_auth(f"the leaked value is {secret} end")
    assert secret not in out
    assert "***REDACTED***" in out


def test_scrub_redacts_credential_key_values_preserving_key():
    assert "hunter2secret" not in _scrub_auth("DB_PASSWORD=hunter2secret")
    out = _scrub_auth("client_secret: super-secret-value-123")
    assert "super-secret-value-123" not in out


def test_scrub_leaves_prose_and_shas_intact():
    s = "fixed in commit a1b2c3d4e5f6 and PR #12 with 1234 changes"
    assert _scrub_auth(s) == s


# ---------------------------------------------------------------------------
# Hardening: fail-closed live-worker gate
# ---------------------------------------------------------------------------


class _LiveNamedFakeAdapter(FakeWorkerAdapter):
    """A fake adapter that reports a *live* name so the sandbox gate applies.

    Still in-process (no real CLI) — it just lets us exercise the gate that
    keys on the adapter name (``claude``/``codex``).
    """

    def __init__(self, live_name: str, apply) -> None:
        super().__init__(apply=apply)
        self._live_name = live_name

    @property
    def name(self) -> str:
        return self._live_name


def test_live_worker_refused_without_sandbox(repo):
    publisher = FakePublisher()
    worker = BackendWorker(
        allowed_repos={"sample": repo},
        adapters=[_LiveNamedFakeAdapter("claude", edit_app)],
        publisher=publisher,
        # allow_live_workers defaults False -> fail closed.
    )
    ev = worker.execute(make_packet(worker_kind="claude"))

    assert ev.status == "blocked_no_sandbox"
    # Nothing was published, and no worktree work happened.
    assert publisher.push_calls == []
    assert publisher.pr_calls == []
    assert any("sandbox" in n for n in ev.notes)


def test_live_worker_allowed_when_explicitly_enabled(repo):
    publisher = FakePublisher()
    worker = BackendWorker(
        allowed_repos={"sample": repo},
        adapters=[_LiveNamedFakeAdapter("claude", edit_app)],
        publisher=publisher,
        allow_live_workers=True,
    )
    ev = worker.execute(make_packet(worker_kind="claude"))

    assert ev.status == "pr_opened"
    assert len(publisher.push_calls) == 1
    assert len(publisher.pr_calls) == 1


def test_fake_worker_is_not_gated_by_sandbox(repo):
    # The in-process fake adapter is not a "live" CLI and runs without the flag.
    worker = worker_for(repo, FakeWorkerAdapter(apply=edit_app))
    ev = worker.execute(make_packet())
    assert ev.status == "pr_opened"


# ---------------------------------------------------------------------------
# Hardening: real subprocess timeout enforcement
# ---------------------------------------------------------------------------


def test_default_subprocess_runner_enforces_real_timeout():
    import sys

    rc, out, err, timed_out = default_subprocess_runner(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=".",
        timeout=1,
    )
    assert timed_out is True
    assert rc == 124

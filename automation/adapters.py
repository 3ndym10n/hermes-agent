"""Pluggable backend-coder adapters.

The controller (``BackendWorker``) is *not* itself a coder — it delegates the
actual file edits to a backend coding CLI through a :class:`WorkerAdapter`.
Adapters are intentionally thin: they know how to build an argv for their CLI
and how to run it, but all policy lives in the controller.

The concrete CLIs (Claude, Codex) are shelled out to via an injected ``runner``
callable so tests never touch the real binaries or the network.  A
:class:`FakeWorkerAdapter` mutates a worktree directly for tests.

Every adapter scrubs auth material out of captured output before it is ever
stored in evidence or logged.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

# A runner abstracts "execute this argv and tell me what happened".  Returning a
# plain tuple keeps the seam trivial for fakes:
#   runner(argv, cwd, timeout) -> (returncode, stdout, stderr, timed_out)
Runner = Callable[[list[str], str, int], "tuple[int, str, str, bool]"]


# ---------------------------------------------------------------------------
# Auth scrubbing
# ---------------------------------------------------------------------------

_REDACTED = "***REDACTED***"

# Each entry is (pattern, replacement).  ``replacement`` may reference capture
# groups so we can keep the *name* of a key=value pair while redacting only its
# secret value.  We deliberately anchor on recognizable prefixes / key names so
# ordinary prose, commit SHAs and file hashes are left intact — we do NOT
# blanket-redact every long token.  Order matters: more specific prefixes first.
_SCRUB_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # Provider key=value auth lines (env-style): preserve the key name.
    (
        re.compile(r"(?i)\b(OPENAI_API_KEY|ANTHROPIC_API_KEY)\s*=\s*\S+"),
        rf"\1={_REDACTED}",
    ),
    # HTTP auth header: preserve the header name.
    (re.compile(r"(?i)\bAuthorization\s*:\s*\S.*"), f"Authorization: {_REDACTED}"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"), _REDACTED),
    # GitHub tokens (fine-grained, then personal-access / oauth / etc.).
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), _REDACTED),
    (re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}"), _REDACTED),
    # Anthropic secret keys (sk-ant- before sk- so the longer prefix wins).
    (re.compile(r"\bsk-ant-[A-Za-z0-9._\-]{10,}"), _REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9._\-]{10,}"), _REDACTED),
)


def _scrub_auth(text: str) -> str:
    """Redact credential-shaped substrings from ``text``.

    Scrubs only known credential prefixes (``ghp_``, ``gho_``, ``github_pat_``,
    ``sk-``, ``sk-ant-`` …), Bearer tokens, ``Authorization:`` headers and
    provider ``KEY=value`` lines.  Replacement is the literal ``***REDACTED***``
    so redaction is obvious in logs.  Ordinary prose is untouched.
    """

    if not text:
        return text

    redacted = text
    for pattern, replacement in _SCRUB_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class AdapterResult:
    """Outcome of running a backend coder.

    Construct via :meth:`create`, which scrubs auth from the captured streams so
    that no credential can leak into evidence.
    """

    stdout: str
    stderr: str
    returncode: int
    timed_out: bool
    command_display: str

    @classmethod
    def create(
        cls,
        stdout: str,
        stderr: str,
        returncode: int,
        timed_out: bool,
        command_display: str,
    ) -> AdapterResult:
        """Factory that scrubs auth from all human-facing fields."""

        return cls(
            stdout=_scrub_auth(stdout or ""),
            stderr=_scrub_auth(stderr or ""),
            returncode=returncode,
            timed_out=timed_out,
            command_display=_scrub_auth(command_display or ""),
        )


# ---------------------------------------------------------------------------
# Default real runner (subprocess)
# ---------------------------------------------------------------------------


def default_subprocess_runner(
    argv: list[str], cwd: str, timeout: int
) -> tuple[int, str, str, bool]:
    """Real runner: execute ``argv`` via subprocess with a hard timeout.

    Returns ``(returncode, stdout, stderr, timed_out)``.  On timeout the process
    is killed and ``timed_out`` is True.  This is the only place in the package
    that actually launches an external coding CLI, and tests never use it.
    """

    try:
        proc = subprocess.run(  # noqa: S603 - argv is built by the adapter
            argv,
            cwd=cwd,
            timeout=timeout,
            text=True,
            capture_output=True,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        # stdout/stderr may be bytes when text decoding was interrupted.
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        return 124, out, err, True


# ---------------------------------------------------------------------------
# Adapter base + concrete adapters
# ---------------------------------------------------------------------------


class WorkerAdapter(ABC):
    """Abstract base for a backend coding CLI adapter."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short stable identifier (e.g. ``"claude"``)."""

    @abstractmethod
    def is_available(self, which: Callable[[str], str | None] = shutil.which) -> bool:
        """Whether this backend can run (binary present / fake always-on)."""

    @abstractmethod
    def build_argv(self, packet, workdir: str, prompt: str) -> list[str]:
        """Build the CLI argv that runs the coder non-interactively."""

    def run(
        self,
        packet,
        workdir: str,
        prompt: str,
        runner: Runner = default_subprocess_runner,
    ) -> AdapterResult:
        """Build the argv, run it via ``runner``, and return a scrubbed result.

        The default implementation is shared by the real CLI adapters; the fake
        overrides it to mutate the worktree instead.
        """

        argv = self.build_argv(packet, workdir, prompt)
        returncode, stdout, stderr, timed_out = runner(
            argv, workdir, packet.timeout_seconds
        )
        # command_display omits the (potentially large) prompt body, showing
        # only the program + leading flags so logs stay readable and never echo
        # the full instruction text.
        display = " ".join(argv[: min(2, len(argv))]) + " <prompt>"
        return AdapterResult.create(
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            timed_out=timed_out,
            command_display=display,
        )


class ClaudeWorkerAdapter(WorkerAdapter):
    """Adapter for the Claude CLI in non-interactive print mode."""

    @property
    def name(self) -> str:
        return "claude"

    def is_available(self, which: Callable[[str], str | None] = shutil.which) -> bool:
        return which("claude") is not None

    def build_argv(self, packet, workdir: str, prompt: str) -> list[str]:
        # ``-p/--print`` runs a single non-interactive turn and exits.  We keep
        # flags minimal: no auto-approve of dangerous tools, no extra surface.
        return ["claude", "-p", prompt]


class CodexWorkerAdapter(WorkerAdapter):
    """Adapter for the Codex CLI (``codex exec``).

    For V0, availability is purely binary presence — we deliberately do NOT run
    any auth/whoami subcommand, since those can print credentials to stdout.
    """

    @property
    def name(self) -> str:
        return "codex"

    def is_available(self, which: Callable[[str], str | None] = shutil.which) -> bool:
        return which("codex") is not None

    def build_argv(self, packet, workdir: str, prompt: str) -> list[str]:
        return ["codex", "exec", prompt]


class FakeWorkerAdapter(WorkerAdapter):
    """Test-only adapter that mutates the worktree via an injected callable.

    ``apply(workdir)`` is responsible for whatever file changes the test wants
    to simulate (write a file, edit one, or do nothing).  ``run`` invokes it and
    returns a canned :class:`AdapterResult`.  Always reports as available.
    """

    def __init__(
        self,
        apply: Callable[[str], None],
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
    ) -> None:
        self._apply = apply
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._timed_out = timed_out

    @property
    def name(self) -> str:
        return "fake"

    def is_available(self, which: Callable[[str], str | None] = shutil.which) -> bool:
        return True

    def build_argv(self, packet, workdir: str, prompt: str) -> list[str]:
        return ["fake-worker", "<prompt>"]

    def run(
        self,
        packet,
        workdir: str,
        prompt: str,
        runner: Runner = default_subprocess_runner,
    ) -> AdapterResult:
        # Apply the simulated edits unless we are simulating a timeout (a real
        # timed-out coder may have made partial or no useful changes; we keep it
        # simple and skip the mutation so timeout tests see no/expected state).
        if not self._timed_out:
            self._apply(workdir)
        return AdapterResult.create(
            stdout=self._stdout,
            stderr=self._stderr,
            returncode=self._returncode,
            timed_out=self._timed_out,
            command_display="fake-worker <prompt>",
        )

"""Safety rails for the bounded backend coding worker.

This module is pure policy: it has no side effects and never touches the
filesystem or git.  It answers three questions for the controller:

1.  Which paths are *globally* off-limits to any worker?
    (:data:`DEFAULT_PROTECTED_GLOBS`)
2.  Is a given ``git`` invocation destructive and therefore forbidden?
    (:func:`is_destructive_git`)
3.  Do the files a worker actually changed stay inside the allow-list and clear
    of forbidden/protected surfaces?  (:func:`classify_changed_files`)

Keeping this logic isolated and dependency-free makes it cheap to unit-test and
easy to audit.
"""

from __future__ import annotations

from pathlib import PurePosixPath

# Globs that NO worker may ever modify, regardless of what its task packet's
# allow-list says.  These cover secrets, deployment surfaces, infra config and
# anything whose corruption could leak credentials or take down production.
# Defense in depth: even if a packet's allowed_files were mis-scoped, a change
# touching one of these is a hard policy block.
DEFAULT_PROTECTED_GLOBS: tuple[str, ...] = (
    "**/.env*",
    ".env*",
    "**/secret*",
    "**/*secret*",
    "**/config.yaml",
    "config.yaml",
    ".github/workflows/**",
    "**/*.service",
    "**/Dockerfile*",
    "Dockerfile*",
    "railway.*",
    "**/railway*",
    "**/migrations/**",
    "**/storage/**",
    "**/*.pem",
    "**/id_rsa*",
)


def match_any(path: str, globs) -> bool:
    """Return True if ``path`` matches any glob in ``globs``.

    Matching is done with :class:`pathlib.PurePosixPath.match`, which is
    glob-aware (``**`` spans directories) and anchored sensibly.  We test both
    the full POSIX path and the bare basename against each glob so that a glob
    like ``Dockerfile*`` matches ``services/api/Dockerfile`` and a glob like
    ``**/config.yaml`` matches a top-level ``config.yaml`` too.
    """

    if not globs:
        return False

    # Normalize to a POSIX path so backslashes (Windows-style) do not defeat the
    # matcher. Strip only a literal leading "./" prefix — NOT via lstrip("./"),
    # which would also eat the leading dot of dotfiles (".env" -> "env") and let
    # protected secrets slip past the matcher.
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized:
        normalized = path.replace("\\", "/")

    pp = PurePosixPath(normalized)
    basename = pp.name

    for glob in globs:
        # Full-path match (handles ``**`` and directory-spanning patterns).
        if pp.match(glob):
            return True
        # Basename match for patterns that have no slash (e.g. ``Dockerfile*``,
        # ``.env*``) — PurePosixPath.match already matches the trailing
        # component for slashless patterns, but matching the basename
        # explicitly keeps behaviour obvious and robust.
        if "/" not in glob and PurePosixPath(basename).match(glob):
            return True
    return False


# ---------------------------------------------------------------------------
# Destructive git detection
# ---------------------------------------------------------------------------

# Flags that turn an otherwise-fine git subcommand into a destructive one.
_FORCE_FLAGS = {"--force", "-f", "--force-with-lease"}


def is_destructive_git(argv: list[str]) -> bool:
    """Return True if ``argv`` is a destructive/history-rewriting git command.

    The controller routes *all* git through a guard that refuses anything this
    function flags.  The policy is conservative on the *dangerous* side: every
    listed dangerous form returns True, while genuinely unknown shapes return
    False (we do not want to accidentally block harmless plumbing).

    ``argv`` may or may not start with the literal ``"git"``; both are handled.
    """

    if not argv:
        return False

    # Drop a leading "git" so indexing is consistent.
    args = list(argv)
    if args and args[0] == "git":
        args = args[1:]
    if not args:
        return False

    # Find the subcommand: the first token that is not a leading global option
    # (e.g. ``git -C path push`` -> subcommand "push").  We keep this simple and
    # skip ``-C <path>`` and other leading dashed globals.
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if tok == "-C":
            idx += 2  # skip the flag and its path argument
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        break
    if idx >= len(args):
        return False

    sub = args[idx]
    rest = args[idx + 1 :]
    rest_set = set(rest)

    # Subcommands that are destructive in their entirety.
    if sub in {"filter-branch", "update-ref", "reflog"}:
        return True

    # rebase rewrites history.
    if sub == "rebase":
        return True

    # push --force / -f / --force-with-lease.
    if sub == "push":
        return bool(rest_set & _FORCE_FLAGS)

    # reset --hard discards working-tree and index state irreversibly.
    if sub == "reset":
        return "--hard" in rest_set

    # clean -f / -d / -x deletes untracked files.
    if sub == "clean":
        for tok in rest:
            if tok.startswith("-") and not tok.startswith("--"):
                # Combined short flags like -fdx.
                if any(ch in tok for ch in ("f", "d", "x")):
                    return True
            if tok in {"--force"}:
                return True
        return False

    # branch -D / -d deletes branches.
    if sub == "branch":
        return bool(rest_set & {"-D", "-d", "--delete", "-D="})

    # gc --prune can drop unreachable objects irreversibly.
    if sub == "gc":
        return any(tok.startswith("--prune") for tok in rest)

    # checkout/restore that discard working-tree changes.
    if sub == "checkout":
        # "git checkout -- <file>" discards changes to tracked files.
        return "--" in rest
    if sub == "restore":
        # ``git restore`` defaults to discarding worktree changes for the named
        # tracked paths.  Treat any restore with a pathspec as destructive.
        # ``--staged`` only unstages (less dangerous) but to stay conservative
        # we still flag restores that touch the worktree.
        if "--staged" in rest_set and "--worktree" not in rest_set:
            # Pure unstage — index only.  Still mildly mutating but reversible;
            # flag conservatively as destructive to keep the worker hands-off.
            return True
        return True

    # stash drop / clear lose stashed work.
    if sub == "stash":
        return bool(rest_set & {"drop", "clear"})

    # worktree remove --force can delete a worktree with changes.
    if sub == "worktree":
        return rest[:1] == ["remove"] and bool(rest_set & {"--force", "-f"})

    # Unknown / harmless shapes: not destructive.
    return False


# ---------------------------------------------------------------------------
# Changed-file classification
# ---------------------------------------------------------------------------


def classify_changed_files(
    changed: list[str],
    allowed_globs,
    forbidden_globs,
    protected_globs,
) -> dict:
    """Classify the worker's changed files against the safety boundaries.

    A changed file is a violation if it matches a forbidden or protected glob,
    OR if it does not match any allowed glob.  The result dict reports each
    bucket so the controller can attach precise evidence to a policy block.

    Returns a dict with keys:

    * ``all_allowed`` — True only when every changed file is inside the
      allow-list and clear of forbidden/protected surfaces.
    * ``disallowed`` — files that match no allowed glob.
    * ``forbidden_hits`` — files matching a packet-specific forbidden surface.
    * ``protected_hits`` — files matching a globally protected surface.
    """

    disallowed: list[str] = []
    forbidden_hits: list[str] = []
    protected_hits: list[str] = []

    for path in changed:
        if match_any(path, protected_globs):
            protected_hits.append(path)
        if match_any(path, forbidden_globs):
            forbidden_hits.append(path)
        # Anything that does not match an allowed glob is disallowed, even if it
        # happens to also be forbidden/protected (it is recorded in both).
        if not match_any(path, allowed_globs):
            disallowed.append(path)

    all_allowed = not (disallowed or forbidden_hits or protected_hits)

    return {
        "all_allowed": all_allowed,
        "disallowed": disallowed,
        "forbidden_hits": forbidden_hits,
        "protected_hits": protected_hits,
    }

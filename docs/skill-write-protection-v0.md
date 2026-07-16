# Skill Write Protection V0

## Why this exists

A normal reflective conversation silently patched local skill files under
`~/.hermes/skills/`. Those files live **outside version control**, and a cron
job would have later read the patched skill — a silent, persistent change to
the agent's procedural memory with no review and no easy undo. Baseline
recovery restored the modified `SKILL.md` files and quarantined the stray
reference files, but nothing stopped it from happening again.

The root cause: ordinary tool-heavy turns could trigger an automatic
background skill-review fork, and that fork was explicitly authorized to write.
Topical pasted content could therefore lead from research tools to a persistent
`SKILL.md` mutation without a user request or preview.

## What is blocked

All six mutating `skill_manage` actions — `create`, `edit`, `patch`,
`delete`, `write_file`, `remove_file` — **fail closed** when called from:

- a normal interactive conversation or operator session,
- a cron job,
- a reflective / planning answer,
- a subagent,
- any context that has not explicitly opted in.

A blocked call returns `success: false` with a message naming the action and
pointing at the recovery path. Nothing is written to disk.

Reads (`skills_list`, `skill_view`) are **never** gated.

## What is allowed

Normal conversation never writes a skill directly. A top-level interactive
request such as “update this skill” binds intent for that turn, but intent only
stages a proposed diff through the existing skills approval store. The skill
file remains unchanged until the user previews and approves that exact pending
change.

| Flow | Result |
|------|--------|
| Ordinary conversation, research, cron, subagent, or background review | blocked; no staging and no write |
| Explicit top-level interactive skill-change request | staged preview; no write |
| Curator review pass (`hermes curator run`) | explicit `allow_skill_writes()` context |
| `/skills` approval replay | applies the already-approved staged payload |

Automatic post-turn skill review is disabled. Background memory review remains
separate and has no skill-write authority.

The direct-write decision lives in
`tools.skill_provenance.skill_writes_allowed()`. Explicit request intent is a
separate per-turn signal and is deliberately ignored by raw file and terminal
guards. The single `skill_manage` chokepoint is
`tools/skill_manager_tool.py::_apply_skill_write_gate`.

To deliberately allow a curator write from new code, wrap it:

```python
from tools.skill_provenance import allow_skill_writes
with allow_skill_writes():
    skill_manage(action="patch", name="my-skill", ...)
```

## Backup / snapshot behaviour

Before **any allowed** skill write, the whole `~/.hermes/skills/` tree is
snapshotted (`_snapshot_before_skill_write` →
`agent.curator_backup.snapshot_skills`). This reuses the curator's existing
backup convention: a timestamped directory holding `skills.tar.gz` plus a
`manifest.json` (reason, time, size, file count) — enough to restore.

**Backup location:** `~/.hermes/skills/.curator_backups/<utc-timestamp>/`

The snapshot is best-effort: a backup failure is logged but does not block the
write, matching the curator's own pre-pass snapshot behaviour. Old snapshots
are pruned to the newest `curator.backup.keep` (default 5).

## How to recover from a bad skill write

```
hermes curator rollback --list      # show available snapshots
hermes curator rollback             # restore the newest snapshot
hermes curator rollback <id>        # restore a specific snapshot
```

Rollback takes its own safety snapshot first, so the rollback itself is
undoable.

## Raw-write guard (V0.1)

V0 gated `skill_manage` only, leaving a hole: an agent could still write skill
files directly via the generic `write_file` / `patch` tools or a `terminal`
shell command. V0.1 closes it with the **same** `skill_writes_allowed()` gate:

- **`write_file` / `patch`** — `tools/file_tools.py::_check_sensitive_path`
  (the shared pre-write check both tools route through) blocks any write whose
  resolved path lands under `~/.hermes/skills/` unless allowed.
- **`terminal`** — `tools/terminal_tool.py` fails closed, *before* the
  dangerous-command check and regardless of `force`, on commands that obviously
  write into the skills dir (redirect/`tee` into a skills path, or a mutating
  verb — `cp`/`mv`/`rm`/`sed -i`/`touch`/`dd`/… — touching one). This is a
  best-effort heuristic: variable expansion, symlinks, or obfuscation can evade
  it; the `write_file`/`patch` guard is the hard boundary.

Reads, lists, and searches of `~/.hermes/skills/` are never gated (the guards
run only on the write path). Allowed raw writes snapshot the skills tree first,
exactly like `skill_manage`.

## Scope

In scope: `skill_manage` (V0) plus raw `write_file` / `patch` / `terminal`
writes (V0.1) — the designed skill-write path and its bypasses.

Out of scope:

- The skills hub install/sync path, a human-initiated CLI action already vetted
  by the `skills_guard` security scanner.
- Adversarial shell obfuscation against the terminal heuristic (see the
  best-effort caveat above).

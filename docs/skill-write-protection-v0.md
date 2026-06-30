# Skill Write Protection V0

## Why this exists

A normal reflective conversation silently patched local skill files under
`~/.hermes/skills/`. Those files live **outside version control**, and a cron
job would have later read the patched skill — a silent, persistent change to
the agent's procedural memory with no review and no easy undo. Baseline
recovery restored the modified `SKILL.md` files and quarantined the stray
reference files, but nothing stopped it from happening again.

The root cause: `skill_manage` (the agent's skill-write tool) wrote
immediately on every call, with no gate distinguishing a deliberate
curator/self-improvement pass from an ordinary conversational, operator, or
cron run.

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

A skill write proceeds only inside an explicit curator/self-improvement flow:

| Flow | How it's recognized |
|------|--------------------|
| Background self-improvement review fork | `is_background_review()` — origin `background_review`, bound per-turn |
| Curator review pass (`hermes curator run`) | wrapped in `allow_skill_writes()` around its review agent |
| `/skills` approval replay | already bypasses via the staged-write replay path |

The decision lives in `tools.skill_provenance.skill_writes_allowed()`. The gate
itself is the single chokepoint `tools/skill_manager_tool.py::_apply_skill_write_gate`,
called at the top of every `skill_manage` invocation.

To deliberately allow a write from new code, wrap it:

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

## Scope (V0)

In scope: the `skill_manage` tool — the designed skill-write path and the one
the incident used.

Out of scope (separate, broader surfaces, tracked as follow-ups):

- Raw `file_write` / `terminal` writes that target `~/.hermes/skills/`
  directly (these bypass `skill_manage`).
- The skills hub install/sync path, which is a human-initiated CLI action
  already vetted by the `skills_guard` security scanner.

"""Skill Write Protection V0 — tests/test_skill_write_protection.py

Lives at the top of tests/ (NOT under tests/tools/) so the autouse
allow-context fixture in tests/tools/conftest.py does NOT apply: the
skill-write gate runs here exactly as it does in production.

Covers:
  - a normal conversation / self-improvement *suggestion* (foreground origin)
    cannot write to ~/.hermes/skills
  - an explicit curator flow (allow_skill_writes) can write when allowed
  - the background self-improvement review fork can write
  - an allowed write snapshots the skills tree first
  - a blocked write returns a clear error
  - a cron / reflective foreground run does not patch skills
  - existing skill *read* behaviour still works
"""

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_constants import get_hermes_home
from tools.skill_manager_tool import skill_manage
from tools.skills_tool import skill_view
from tools.skill_provenance import (
    allow_skill_writes,
    skill_writes_allowed,
    set_current_write_origin,
    reset_current_write_origin,
    BACKGROUND_REVIEW,
)


VALID_SKILL = """\
---
name: probe-skill
description: A skill used by the write-protection tests.
---

# Probe Skill

Step 1: original body.
"""


@contextmanager
def _skills_env():
    """Point both the writer and reader at <hermes_home>/skills so the gate,
    the writes, and the curator snapshot all operate on one tree."""
    skills_dir = get_hermes_home() / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    with patch("tools.skill_manager_tool.SKILLS_DIR", skills_dir), \
         patch("tools.skills_tool.SKILLS_DIR", skills_dir), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[skills_dir]), \
         patch("agent.skill_utils.get_external_skills_dirs", return_value=[]):
        yield skills_dir


def _seed_skill(skills_dir: Path) -> Path:
    """Create an existing skill on disk (bypassing the gate)."""
    skill_md = skills_dir / "probe-skill" / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(VALID_SKILL, encoding="utf-8")
    return skill_md


# ---------------------------------------------------------------------------
# Blocked: normal / conversational / cron / reflective foreground runs
# ---------------------------------------------------------------------------

def test_foreground_create_is_blocked():
    """A normal conversation (default foreground origin, no allow context)
    cannot create a skill."""
    with _skills_env() as skills_dir:
        result = json.loads(skill_manage(action="create", name="probe-skill", content=VALID_SKILL))
        assert result["success"] is False
        assert not (skills_dir / "probe-skill").exists()


def test_foreground_patch_is_blocked_and_file_untouched():
    """A self-improvement *suggestion* in a normal conversation cannot patch
    an existing skill — and the file on disk is unchanged."""
    with _skills_env() as skills_dir:
        skill_md = _seed_skill(skills_dir)
        before = skill_md.read_text(encoding="utf-8")

        result = json.loads(skill_manage(
            action="patch", name="probe-skill",
            old_string="original body.", new_string="SILENTLY PATCHED.",
        ))
        assert result["success"] is False
        assert skill_md.read_text(encoding="utf-8") == before
        assert "SILENTLY PATCHED" not in skill_md.read_text(encoding="utf-8")


def test_blocked_write_has_clear_error():
    """The block message names the action and points at the recovery path."""
    with _skills_env():
        result = json.loads(skill_manage(action="create", name="probe-skill", content=VALID_SKILL))
        err = result.get("error", "")
        assert "blocked" in err.lower()
        assert "curator" in err.lower()
        assert "probe-skill" in err


def test_cron_reflective_run_does_not_patch_skills():
    """A cron / reflective proposal flow runs as a normal (foreground) agent:
    its origin grants no skill-write permission."""
    with _skills_env() as skills_dir:
        skill_md = _seed_skill(skills_dir)
        before = skill_md.read_text(encoding="utf-8")
        # cron jobs and reflective answers carry no allow context.
        assert skill_writes_allowed() is False
        result = json.loads(skill_manage(
            action="edit", name="probe-skill",
            content=VALID_SKILL.replace("original body.", "cron-edited."),
        ))
        assert result["success"] is False
        assert skill_md.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Allowed: explicit curator / self-improvement flows
# ---------------------------------------------------------------------------

def test_curator_flow_can_create():
    """An explicit curator flow (allow_skill_writes) may write."""
    with _skills_env() as skills_dir:
        with allow_skill_writes():
            result = json.loads(skill_manage(action="create", name="probe-skill", content=VALID_SKILL))
        assert result["success"] is True
        assert (skills_dir / "probe-skill" / "SKILL.md").exists()


def test_background_review_fork_can_patch():
    """The self-improvement review fork (background_review origin) may write."""
    with _skills_env() as skills_dir:
        skill_md = _seed_skill(skills_dir)
        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            assert skill_writes_allowed() is True
            result = json.loads(skill_manage(
                action="patch", name="probe-skill",
                old_string="original body.", new_string="review-fork patched.",
            ))
        finally:
            reset_current_write_origin(token)
        assert result["success"] is True
        assert "review-fork patched." in skill_md.read_text(encoding="utf-8")


def test_allowed_write_snapshots_first():
    """Before an allowed mutation, the skills tree is snapshotted so the write
    is recoverable."""
    with _skills_env() as skills_dir:
        _seed_skill(skills_dir)
        backups = skills_dir / ".curator_backups"
        assert not backups.exists() or not any(backups.iterdir())

        with allow_skill_writes():
            result = json.loads(skill_manage(
                action="patch", name="probe-skill",
                old_string="original body.", new_string="patched with backup.",
            ))
        assert result["success"] is True
        # A timestamped snapshot dir with a restorable tarball + manifest exists.
        snaps = [d for d in backups.iterdir() if (d / "skills.tar.gz").exists()]
        assert snaps, "expected a curator snapshot before the allowed write"
        assert (snaps[0] / "manifest.json").exists()


# ---------------------------------------------------------------------------
# Reads are never gated
# ---------------------------------------------------------------------------

def test_read_still_works_in_foreground():
    """skill_view (read path) is unaffected by the write guard."""
    with _skills_env() as skills_dir:
        _seed_skill(skills_dir)
        assert skill_writes_allowed() is False  # plain foreground
        result = json.loads(skill_view("probe-skill"))
        assert result["success"] is True
        assert "original body." in result["content"]

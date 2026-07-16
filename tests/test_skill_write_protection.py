"""Skill Write Protection V0 — tests/test_skill_write_protection.py

Lives at the top of tests/ (NOT under tests/tools/) so the autouse
allow-context fixture in tests/tools/conftest.py does NOT apply: the
skill-write gate runs here exactly as it does in production.

Covers:
  - a normal conversation / self-improvement *suggestion* (foreground origin)
    cannot write to ~/.hermes/skills
  - an explicit curator flow (allow_skill_writes) can write when allowed
  - the background self-improvement review fork cannot write
  - an explicit top-level request stages a preview instead of writing
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
from tools.skill_manager_tool import apply_skill_pending, skill_manage
from tools.skills_tool import skill_view
from tools.skill_provenance import (
    allow_skill_writes,
    bind_explicit_skill_write_request,
    explicit_skill_write_requested,
    skill_writes_allowed,
    set_current_write_origin,
    reset_current_write_origin,
    BACKGROUND_REVIEW,
)


@pytest.fixture(autouse=True)
def _reset_explicit_skill_intent():
    bind_explicit_skill_write_request(
        "", origin="assistant_tool", platform="cron", parent_session_id=""
    )
    yield
    bind_explicit_skill_write_request(
        "", origin="assistant_tool", platform="cron", parent_session_id=""
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


def test_background_review_fork_cannot_patch():
    """Automatic background review has no skill-write authority."""
    with _skills_env() as skills_dir:
        skill_md = _seed_skill(skills_dir)
        before = skill_md.read_text(encoding="utf-8")
        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            assert skill_writes_allowed() is False
            result = json.loads(skill_manage(
                action="patch", name="probe-skill",
                old_string="original body.", new_string="review-fork patched.",
            ))
        finally:
            reset_current_write_origin(token)
        assert result["success"] is False
        assert skill_md.read_text(encoding="utf-8") == before


def test_explicit_top_level_skill_request_stages_preview_without_writing():
    with _skills_env() as skills_dir:
        skill_md = _seed_skill(skills_dir)
        before = skill_md.read_text(encoding="utf-8")
        assert bind_explicit_skill_write_request(
            "@ponytail full\nPlease update the probe skill with this verified rule.",
            origin="assistant_tool",
            platform="telegram",
            parent_session_id="",
        ) is True
        with patch("tools.write_approval.stage_write", return_value={"id": "pending1"}) as stage:
            result = json.loads(skill_manage(
                action="patch", name="probe-skill",
                old_string="original body.", new_string="approved later.",
            ))
        assert result["success"] is True
        assert result["staged"] is True
        assert result["pending_id"] == "pending1"
        stage.assert_called_once()
        assert skill_md.read_text(encoding="utf-8") == before


def test_explicit_skill_request_is_denied_for_subagent_cron_and_negation():
    assert bind_explicit_skill_write_request(
        "Update the probe skill.", origin="assistant_tool",
        platform="telegram", parent_session_id="parent-1",
    ) is False
    assert bind_explicit_skill_write_request(
        "Update the probe skill.", origin="assistant_tool",
        platform="cron", parent_session_id="",
    ) is False
    assert bind_explicit_skill_write_request(
        "Do not modify any skill or SKILL.md file.", origin="assistant_tool",
        platform="telegram", parent_session_id="",
    ) is False
    assert explicit_skill_write_requested() is False


def test_explicit_skill_request_fails_closed_when_approval_boundary_unavailable():
    with _skills_env() as skills_dir:
        skill_md = _seed_skill(skills_dir)
        before = skill_md.read_text(encoding="utf-8")
        bind_explicit_skill_write_request(
            "Update the probe skill.", origin="assistant_tool",
            platform="telegram", parent_session_id="",
        )
        with patch(
            "tools.skill_manager_tool._load_write_approval",
            side_effect=ImportError("unavailable"),
        ):
            result = json.loads(skill_manage(
                action="patch", name="probe-skill",
                old_string="original body.", new_string="must not land.",
            ))
        assert result["success"] is False
        assert "approval gate is unavailable" in result["error"]
        assert skill_md.read_text(encoding="utf-8") == before


def test_approved_pending_replay_is_the_only_foreground_application_path():
    with _skills_env() as skills_dir:
        skill_md = _seed_skill(skills_dir)
        result = json.loads(apply_skill_pending({
            "action": "patch",
            "name": "probe-skill",
            "old_string": "original body.",
            "new_string": "approved change.",
        }))
        assert result["success"] is True
        assert "approved change." in skill_md.read_text(encoding="utf-8")


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

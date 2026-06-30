"""Skill Write Protection V0.1 — raw write/terminal bypass.

Top-level tests/ (NOT tests/tools/) so the autouse allow-context fixture does
not apply: the guard runs as it does in production.

Covers the bypass V0 left open — raw file_write/patch and terminal writes into
~/.hermes/skills/ — plus that reads, allowed contexts, and snapshots still work.
"""

import json
from pathlib import Path

from hermes_constants import get_hermes_home
from tools.file_tools import write_file_tool, patch_tool, read_file_tool
from tools.terminal_tool import (
    _command_targets_skills_write,
    _skill_write_terminal_block,
)
from tools.skill_provenance import allow_skill_writes, path_targets_skills


SKILL_BODY = "---\nname: probe\ndescription: probe.\n---\n\n# Probe\n\nbody.\n"


def _skill_md() -> Path:
    return get_hermes_home() / "skills" / "probe" / "SKILL.md"


def _seed(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SKILL_BODY, encoding="utf-8")


# ---------------------------------------------------------------------------
# Raw file writes — blocked in foreground
# ---------------------------------------------------------------------------

def test_raw_write_to_skill_md_is_blocked():
    skill_md = _skill_md()
    result = json.loads(write_file_tool(str(skill_md), "MALICIOUS", task_id="default"))
    assert result.get("error")
    assert "skill" in result["error"].lower()
    assert not skill_md.exists()


def test_raw_patch_to_skill_is_blocked_and_unchanged():
    skill_md = _skill_md()
    _seed(skill_md)
    result = json.loads(patch_tool(
        mode="replace", path=str(skill_md),
        old_string="body.", new_string="PATCHED", task_id="default",
    ))
    assert result.get("error")
    assert skill_md.read_text(encoding="utf-8") == SKILL_BODY


def test_non_skill_write_is_unaffected(tmp_path):
    target = tmp_path / "notes.md"
    result = json.loads(write_file_tool(str(target), "hello", task_id="default"))
    assert not result.get("error")
    assert target.read_text(encoding="utf-8") == "hello"


# ---------------------------------------------------------------------------
# Reads still work
# ---------------------------------------------------------------------------

def test_read_from_skills_still_works():
    skill_md = _skill_md()
    _seed(skill_md)
    result = json.loads(read_file_tool(str(skill_md), task_id="default"))
    assert not result.get("error")
    assert "body." in (result.get("content") or "")


# ---------------------------------------------------------------------------
# Allowed context — write succeeds and snapshots first
# ---------------------------------------------------------------------------

def test_allowed_raw_write_succeeds_and_snapshots_first():
    skill_md = _skill_md()
    _seed(skill_md)
    backups = get_hermes_home() / "skills" / ".curator_backups"
    with allow_skill_writes():
        result = json.loads(write_file_tool(str(skill_md), SKILL_BODY + "more", task_id="default"))
    assert not result.get("error"), result
    assert skill_md.read_text(encoding="utf-8").endswith("more")
    snaps = [d for d in backups.iterdir() if (d / "skills.tar.gz").exists()]
    assert snaps, "expected a curator snapshot before the allowed raw write"
    assert (snaps[0] / "manifest.json").exists()


# ---------------------------------------------------------------------------
# Terminal heuristic
# ---------------------------------------------------------------------------

def test_terminal_write_patterns_detected():
    assert _command_targets_skills_write("echo x > ~/.hermes/skills/probe/SKILL.md")
    assert _command_targets_skills_write("echo x >> ~/.hermes/skills/probe/SKILL.md")
    assert _command_targets_skills_write("tee ~/.hermes/skills/probe/SKILL.md")
    assert _command_targets_skills_write("sed -i s/a/b/ ~/.hermes/skills/probe/SKILL.md")
    assert _command_targets_skills_write("rm ~/.hermes/skills/probe/SKILL.md")
    assert _command_targets_skills_write("cp /tmp/evil ~/.hermes/skills/probe/SKILL.md")


def test_terminal_read_patterns_not_detected():
    assert not _command_targets_skills_write("cat ~/.hermes/skills/probe/SKILL.md")
    assert not _command_targets_skills_write("ls -la ~/.hermes/skills")
    assert not _command_targets_skills_write("grep foo ~/.hermes/skills/probe/SKILL.md > /tmp/out")
    assert not _command_targets_skills_write("echo hi > /tmp/safe.txt")


def test_terminal_block_helper_blocks_in_foreground():
    msg = _skill_write_terminal_block("echo x > ~/.hermes/skills/probe/SKILL.md")
    assert msg and "curator" in msg.lower()


def test_terminal_block_helper_allows_in_context():
    with allow_skill_writes():
        assert _skill_write_terminal_block("echo x > ~/.hermes/skills/probe/SKILL.md") is None


def test_terminal_read_command_never_blocked():
    assert _skill_write_terminal_block("cat ~/.hermes/skills/probe/SKILL.md") is None


# ---------------------------------------------------------------------------
# path helper
# ---------------------------------------------------------------------------

def test_path_targets_skills():
    assert path_targets_skills(str(_skill_md()))
    assert path_targets_skills(str(get_hermes_home() / "skills"))
    assert not path_targets_skills(str(get_hermes_home() / "memories" / "x.md"))

"""Regression test: google-workspace SKILL.md must declare required_credential_files.

PR #9931 accidentally removed the required_credential_files header, which broke
credential file mounting in Docker/Modal remote backends (#16452). This test
prevents the regression from silently reappearing.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch


SKILL_MD = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/SKILL.md"
)

_EXPECTED_PATHS = {
    "secrets/google/google_token.json",
    "secrets/google/google_credentials.json",
}


def _parse_frontmatter(content: str) -> dict:
    from agent.skill_utils import parse_frontmatter

    fm, _ = parse_frontmatter(content)
    return fm


class TestGoogleWorkspaceCredentialFiles:
    def test_required_credential_files_present_in_skill_md(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        entries = fm.get("required_credential_files")
        assert entries, "required_credential_files missing from google-workspace SKILL.md"
        assert isinstance(entries, list), "required_credential_files must be a list"
        paths = {
            (e["path"] if isinstance(e, dict) else e)
            for e in entries
        }
        assert _EXPECTED_PATHS <= paths, (
            f"Missing entries in required_credential_files: {_EXPECTED_PATHS - paths}"
        )

    def test_entries_are_registered_when_files_exist(self, tmp_path):
        hermes_home = tmp_path / ".hermes"
        google_dir = hermes_home / "secrets/google"
        google_dir.mkdir(parents=True)
        (google_dir / "google_token.json").write_text("{}")
        (google_dir / "google_credentials.json").write_text("{}")

        from tools.credential_files import (
            clear_credential_files,
            get_credential_file_mounts,
            register_credential_files,
        )

        clear_credential_files()
        try:
            content = SKILL_MD.read_text(encoding="utf-8")
            fm = _parse_frontmatter(content)
            entries = fm.get("required_credential_files", [])

            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
                missing = register_credential_files(entries)

            assert missing == [], f"Unexpected missing files: {missing}"
            mounts = get_credential_file_mounts()
            container_paths = {m["container_path"] for m in mounts}
            assert "/root/.hermes/secrets/google/google_token.json" in container_paths
            assert "/root/.hermes/secrets/google/google_credentials.json" in container_paths
        finally:
            clear_credential_files()

    def test_missing_token_is_reported(self, tmp_path):
        """google_token.json absent (first-time setup) — reported as missing, client secret still mounts."""
        hermes_home = tmp_path / ".hermes"
        (hermes_home / "secrets/google").mkdir(parents=True)
        (hermes_home / "secrets/google/google_credentials.json").write_text("{}")

        from tools.credential_files import (
            clear_credential_files,
            get_credential_file_mounts,
            register_credential_files,
        )

        clear_credential_files()
        try:
            content = SKILL_MD.read_text(encoding="utf-8")
            fm = _parse_frontmatter(content)
            entries = fm.get("required_credential_files", [])

            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
                missing = register_credential_files(entries)

            assert "secrets/google/google_token.json" in missing
            mounts = get_credential_file_mounts()
            container_paths = {m["container_path"] for m in mounts}
            assert "/root/.hermes/secrets/google/google_credentials.json" in container_paths
            assert "/root/.hermes/secrets/google/google_token.json" not in container_paths
        finally:
            clear_credential_files()

"""Regression tests for the purchase-executor install package (Hermes #65).

Covers two Cal-gate failures:
  1. doctor.sh must treat a *static* systemd unit as inert, not as "enabled".
  2. staging synthetic credentials must be encrypted with the credential ID the
     unit requests (name-binding), while living in a distinct staging-*.cred file.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent / "packaging" / "purchase-executor"
STAGING_UNIT = PKG / "hermes-purchase-executor-staging.service"
STAGE_SCRIPT = PKG / "stage-synthetic-credentials.sh"
DOCTOR = PKG / "doctor.sh"


def _staging_credentials():
    """(credential_id, cred_file_basename) from the staging unit."""
    pairs = []
    for line in STAGING_UNIT.read_text().splitlines():
        m = re.match(r"\s*LoadCredentialEncrypted=([^:]+):(.+)", line)
        if m:
            pairs.append((m.group(1).strip(), os.path.basename(m.group(2).strip())))
    return pairs


def test_staging_credential_names_bind_to_requested_ids():
    pairs = _staging_credentials()
    assert pairs, "staging unit declares no encrypted credentials"
    script = STAGE_SCRIPT.read_text()

    # The staging file for id X must be staging-X.cred ...
    for cred_id, cred_file in pairs:
        assert cred_file == f"staging-{cred_id}.cred", (
            f"unit requests id {cred_id!r} from file {cred_file!r}; "
            f"expected staging-{cred_id}.cred"
        )

    # ... and the stage() helper must encrypt with --name=<id> (NOT the file
    # stem), writing to the staging-<id> file. This is the exact bug that broke
    # the gate ("credential name 'staging-card_name' does not match 'card_name'").
    assert '--name="$1"' in script
    assert 'staging-$1.cred' in script
    staged_ids = set(re.findall(r"^stage\s+(\S+)", script, flags=re.M))
    requested_ids = {cred_id for cred_id, _ in pairs}
    assert requested_ids <= staged_ids, (
        f"unit requests {requested_ids - staged_ids} that stage script never stages"
    )
    # And the script must NOT bind the staging- prefix into the credential name.
    assert '--name="staging-' not in script
    assert "stage staging-" not in script


def test_doctor_treats_static_unit_as_inert(tmp_path):
    # Stub `systemctl` reporting the production unit as static + inactive — the
    # exact state that made the real gate falsely report "ENABLED".
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "systemctl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        '  is-enabled) echo static; exit 0;;\n'   # static units: prints static, exits 0
        '  is-active) exit 3;;\n'                  # inactive
        '  *) exit 0;;\n'
        'esac\n'
    )
    stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    result = subprocess.run(
        ["bash", str(DOCTOR)], capture_output=True, text=True, env=env
    )
    out = result.stdout + result.stderr
    # The regression: a static unit must NOT be reported as bootable/enabled.
    assert "production unit is bootable" not in out
    assert "production unit not bootable" in out
    assert "production unit inactive" in out


def test_scripts_are_syntax_clean():
    for script in PKG.glob("*.sh"):
        r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert r.returncode == 0, f"{script.name}: {r.stderr}"


def test_install_does_not_disable_static_units():
    # `systemctl disable` errors on static units ("no [Install] section"); the
    # installer must not call it.
    code_lines = [
        line for line in (PKG / "install.sh").read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    assert not any("systemctl disable" in line for line in code_lines)

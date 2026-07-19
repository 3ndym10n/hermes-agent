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
PROD_UNIT = PKG / "hermes-purchase-executor.service"
STAGING_UNIT = PKG / "hermes-purchase-executor-staging.service"
STAGE_SCRIPT = PKG / "stage-synthetic-credentials.sh"
DOCTOR = PKG / "doctor.sh"


def _unit_values(unit_path, key):
    vals = []
    for line in unit_path.read_text().splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        if s.startswith(key + "="):
            vals.append(s.split("=", 1)[1].strip())
    return vals


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


@pytest.mark.parametrize("unit", [PROD_UNIT, STAGING_UNIT])
def test_workdir_and_interpreter_are_reachable_via_bind_mounts(unit):
    # The service user cannot traverse the human home (750/700, no ACL tools),
    # so the units hide the home (ProtectHome=tmpfs) and bind-mount ONLY the
    # needed subtrees read-only. Prove the WorkingDirectory and the actual Python
    # interpreter are each inside a bind-mounted subtree AND world-accessible on
    # disk (what makes them readable through the read-only bind).
    assert _unit_values(unit, "ProtectHome") == ["tmpfs"]
    binds = _unit_values(unit, "BindReadOnlyPaths")
    workdir = _unit_values(unit, "WorkingDirectory")[0]
    execstart = _unit_values(unit, "ExecStart")[0]

    def covered(path):
        return any(path == b or path.startswith(b.rstrip("/") + "/") for b in binds)

    assert covered(workdir), f"{workdir} not covered by a bind mount {binds}"

    # The interpreter in ExecStart, resolved through symlinks, must be covered.
    py = execstart.split()[0]
    real = os.path.realpath(py)
    assert covered(real), f"interpreter {real} not covered by bind mounts {binds}"

    # On this host the sources must be world-readable/executable for the bind to
    # grant access (files keep real ownership inside the mount).
    if os.path.exists(workdir):
        mode = os.stat(workdir).st_mode
        assert mode & stat.S_IROTH and mode & stat.S_IXOTH, f"{workdir} not world-rx"
    if os.path.exists(real):
        assert os.stat(real).st_mode & stat.S_IXOTH, f"{real} not world-executable"


def test_units_do_not_weaken_home_protection():
    # Regression against a lazy "fix" that broadly opens the home.
    for unit in (PROD_UNIT, STAGING_UNIT):
        text = unit.read_text()
        assert "ProtectHome=read-only" not in text
        assert "ProtectHome=no" not in text
        # No bind mount of the whole private home or its parent.
        for b in _unit_values(unit, "BindReadOnlyPaths"):
            assert b.rstrip("/") not in {"/home", "/home/v0id"}, f"too-broad bind: {b}"


def test_install_verifies_readability_and_drops_setfacl():
    install = (PKG / "install.sh").read_text()
    assert "setfacl" not in install  # ACL approach abandoned (tools may be absent)
    assert "verify_world_readable" in install  # fail-loud precondition check
    # The verification must not swallow errors (it exits non-zero on failure).
    assert "exit 1" in install


def test_cal_gate_stage_run_classifies_enabled_by_value():
    gate = (PKG / "cal-gate.sh").read_text()
    # The false "WARN prod ENABLED" came from an exit-code check on a static unit.
    assert "&& echo \"WARN prod ENABLED\"" not in gate
    assert 'prod_state="$(systemctl is-enabled' in gate
    assert "is-active --quiet" in gate


def test_cal_gate_staging_pass_detection_is_invocation_scoped_and_authoritative():
    gate = (PKG / "cal-gate.sh").read_text()
    # No truncated tail window (the false negative Cal hit); scope to THIS run.
    assert "-n 200" not in gate
    assert "_SYSTEMD_INVOCATION_ID=$INVOC" in gate
    # Validate the authoritative systemd exit fields, not just a log grep.
    assert 'RESULT="$(systemctl show' in gate and "Result" in gate
    assert "ExecMainStatus" in gate
    assert '[ "$RESULT" = "success" ]' in gate
    assert '[ "$MAINSTATUS" = "0" ]' in gate
    # And it must be able to fail the run (non-zero exit) on a genuine failure.
    assert "staging_failed=1" in gate
    assert 'exit "${staging_failed:-0}"' in gate


def test_install_does_not_disable_static_units():
    # `systemctl disable` errors on static units ("no [Install] section"); the
    # installer must not call it.
    code_lines = [
        line for line in (PKG / "install.sh").read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    assert not any("systemctl disable" in line for line in code_lines)

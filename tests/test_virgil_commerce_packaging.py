from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


PKG = Path(__file__).resolve().parent.parent / "packaging" / "virgil-commerce"
UNIT = PKG / "virgil-commerce.service"


def _values(key: str) -> list[str]:
    prefix = f"{key}="
    return [
        line.strip().split("=", 1)[1]
        for line in UNIT.read_text().splitlines()
        if line.strip().startswith(prefix)
    ]


def test_unit_reuses_hardened_virgil_service_boundary():
    assert _values("Restart") == ["on-failure"]
    assert _values("UMask") == ["0077"]
    assert _values("NoNewPrivileges") == ["yes"]
    assert _values("PrivateTmp") == ["yes"]
    assert _values("ProtectSystem") == ["strict"]
    assert _values("ProtectHome") == ["read-only"]
    assert _values("RestrictAddressFamilies") == ["AF_UNIX AF_INET AF_INET6"]
    assert _values("WorkingDirectory") == ["/home/v0id/.hermes/hermes-agent"]
    assert _values("ReadOnlyPaths") == ["/home/v0id/.hermes/hermes-agent"]
    assert _values("EnvironmentFile") == [
        "-/home/v0id/.hermes/.env",
        "-/home/v0id/.hermes/secrets/porkbun.env",
        "-/home/v0id/.hermes/secrets/shopify.env",
    ]

    command = shlex.split(_values("ExecStart")[0])
    assert command == [
        "/home/v0id/.hermes/hermes-agent/venv/bin/python",
        "/home/v0id/.hermes/hermes-agent/commerce_operator.py",
    ]


def test_socket_and_state_paths_are_private_and_writable():
    socket_dir = "/home/v0id/.hermes/commerce/ab"
    assert _values("Environment") == [f"AGENT_BROWSER_SOCKET_DIR={socket_dir}"]
    writable = _values("ReadWritePaths")
    assert "/home/v0id/.hermes/commerce" in writable
    assert "/home/v0id/.hermes/browser-profiles/commerce" in writable
    assert any(
        socket_dir == path or socket_dir.startswith(path.rstrip("/") + "/")
        for path in writable
    )

    install = (PKG / "install.sh").read_text()
    for path in (
        "$HERMES_HOME/commerce/ab",
        "$HERMES_HOME/commerce/evidence",
        "$HERMES_HOME/commerce/receipts",
        "$HERMES_HOME/browser-profiles/commerce",
    ):
        assert f'"{path}"' in install
    assert "install -d -m 0700" in install


def test_lifecycle_scripts_are_inert_and_preserve_state():
    install = (PKG / "install.sh").read_text()
    install_systemctl = [
        line.strip()
        for line in install.splitlines()
        if line.strip().startswith("systemctl ")
    ]
    assert install_systemctl == ["systemctl --user daemon-reload"]

    uninstall = (PKG / "uninstall.sh").read_text()
    assert "disable --now" in uninstall
    assert "/home/v0id/.hermes/commerce" not in uninstall
    assert "/home/v0id/.hermes/browser-profiles" not in uninstall
    assert "rm -rf" not in uninstall

    for script in PKG.glob("*.sh"):
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{script.name}: {result.stderr}"

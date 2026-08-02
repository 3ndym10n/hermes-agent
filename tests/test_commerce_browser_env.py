from __future__ import annotations

import subprocess

import commerce_browser as browser


def test_browser_subprocess_environment_is_allowlisted_and_secret_free(
    tmp_path, monkeypatch
):
    for name in browser._BROWSER_ENV_ALLOWLIST:
        monkeypatch.delenv(name, raising=False)

    allowed = {
        name: f"runtime-{name.casefold()}" for name in browser._BROWSER_ENV_ALLOWLIST
    }
    allowed["HOME"] = str(tmp_path / "home")
    allowed["PATH"] = "/usr/bin:/bin"
    for name, value in allowed.items():
        monkeypatch.setenv(name, value)

    secret_names = {
        "PORKBUN_API_KEY",
        "SHOPIFY_CLIENT_SECRET",
        "COGITATOR_BRIDGE_TOKEN",
        "OPENAI_API_KEY",
        "AGENT_BROWSER_ENCRYPTION_KEY",
        "AGENT_BROWSER_PROXY_PASSWORD",
        "NODE_OPTIONS",
    }
    for name in secret_names:
        monkeypatch.setenv(name, "sentinel-worker-secret")

    socket_dir = tmp_path / "socket"
    monkeypatch.setattr(browser, "SOCKET_DIR", socket_dir)
    expected = {
        **allowed,
        "AGENT_BROWSER_SOCKET_DIR": str(socket_dir),
    }
    assert browser.browser_env() == expected

    child_envs = []

    def fake_run(arguments, **kwargs):
        child_envs.append(kwargs["env"])
        return subprocess.CompletedProcess(arguments, 0, '{"success":true,"data":{}}')

    monkeypatch.setattr(browser, "browser_binary", lambda: "/safe/agent-browser")
    monkeypatch.setattr(subprocess, "run", fake_run)
    session = "commerce_cj_12345678_1234_1234_1234_123456789abc"

    browser.browser_json(session, "session", "list")
    browser.insert_text(session, "safe text")

    assert child_envs == [expected, expected]
    assert secret_names.isdisjoint(child_envs[0])
    assert "sentinel-worker-secret" not in child_envs[0].values()

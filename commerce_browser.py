"""Small, fail-closed wrapper around commerce agent-browser sessions."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SOCKET_DIR = Path.home() / ".hermes" / "commerce" / "ab"
PROFILE_ROOT = Path.home() / ".hermes" / "browser-profiles" / "commerce"
_JOB_ID_RE = re.compile(
    r"cj_[0-9a-f]{8}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{12}\Z"
)
_SESSION_RE = re.compile(
    r"commerce_cj_[0-9a-f]{8}_[0-9a-f]{4}_[0-9a-f]{4}_"
    r"[0-9a-f]{4}_[0-9a-f]{12}\Z"
)
_TAB_ID_RE = re.compile(r"t[1-9][0-9]{0,5}\Z")
_DNS_NAME_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_MAX_OUTPUT = 64 * 1024

_BROWSER_ENV_ALLOWLIST = (
    "HOME",
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LC_NUMERIC",
    "LC_TIME",
    "TZ",
    "TMPDIR",
    "TMP",
    "TEMP",
    "XDG_RUNTIME_DIR",
    "PLAYWRIGHT_BROWSERS_PATH",
    "AGENT_BROWSER_EXECUTABLE_PATH",
)


class BrowserLifecycleError(RuntimeError):
    """A stable browser error code with no subprocess output attached."""


def validate_browser_binding(job_id: str, session: str) -> None:
    if (
        not isinstance(job_id, str)
        or _JOB_ID_RE.fullmatch(job_id) is None
        or not isinstance(session, str)
        or _SESSION_RE.fullmatch(session) is None
        or session != f"commerce_{job_id}"
    ):
        raise BrowserLifecycleError("invalid_browser_session")


def validate_tab_id(tab_id: Any) -> str:
    if not isinstance(tab_id, str) or _TAB_ID_RE.fullmatch(tab_id) is None:
        raise BrowserLifecycleError("invalid_browser_tab")
    return tab_id


def validate_entry_url(value: Any) -> str:
    """Accept only re-enterable, credential-free browser entry URLs."""

    if not isinstance(value, str) or not value or len(value) > 2_048:
        raise BrowserLifecycleError("invalid_browser_entry_url")
    if value != value.strip() or "\\" in value or any(ord(char) < 33 for char in value):
        raise BrowserLifecycleError("invalid_browser_entry_url")
    if value == "about:blank":
        return value
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise BrowserLifecycleError("invalid_browser_entry_url")
    try:
        port = parsed.port
    except ValueError:
        raise BrowserLifecycleError("invalid_browser_entry_url") from None
    host = parsed.hostname.casefold()
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback:
            raise BrowserLifecycleError("invalid_browser_entry_url")
    elif host == "localhost":
        raise BrowserLifecycleError("invalid_browser_entry_url")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is None and host != "localhost" and _DNS_NAME_RE.fullmatch(host) is None:
        raise BrowserLifecycleError("invalid_browser_entry_url")
    if address is not None and parsed.scheme == "https" and not address.is_global:
        raise BrowserLifecycleError("invalid_browser_entry_url")
    if port is not None and not 1 <= port <= 65_535:
        raise BrowserLifecycleError("invalid_browser_entry_url")
    return value


def browser_binary() -> str:
    bundled = (
        Path(__file__).parent / "node_modules" / ".bin" / "agent-browser",
        Path.home() / ".hermes/hermes-agent/node_modules/.bin/agent-browser",
    )
    for candidate in bundled:
        if candidate.is_file():
            return str(candidate)
    executable = shutil.which("agent-browser")
    if not executable:
        raise BrowserLifecycleError("agent_browser_unavailable")
    return executable


def _secure_dir(path: Path) -> Path:
    if path.is_symlink():
        raise BrowserLifecycleError("unsafe_browser_directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise BrowserLifecycleError("unsafe_browser_directory")
    path.chmod(0o700)
    return path


def browser_env(socket_dir: Path | None = None) -> dict[str, str]:
    """Build the browser runtime environment without worker credentials."""

    stable_socket_dir = _secure_dir(socket_dir or SOCKET_DIR)
    env = {
        key: value for key in _BROWSER_ENV_ALLOWLIST if (value := os.environ.get(key))
    }
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("PATH", os.defpath)
    env["AGENT_BROWSER_SOCKET_DIR"] = str(stable_socket_dir)
    return env


def browser_json(
    session: str,
    *command: str,
    profile: Path | None = None,
    socket_dir: Path | None = None,
    timeout: int = 10,
) -> Any:
    if _SESSION_RE.fullmatch(session) is None:
        raise BrowserLifecycleError("invalid_browser_session")
    arguments = [browser_binary(), "--session", session]
    if profile is not None:
        arguments.extend(("--profile", str(profile)))
    arguments.extend(("--json", *command))
    try:
        result = subprocess.run(
            arguments,
            env=browser_env(socket_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BrowserLifecycleError("agent_browser_command_failed") from None
    if result.returncode != 0 or len(result.stdout) > _MAX_OUTPUT:
        raise BrowserLifecycleError("agent_browser_command_failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise BrowserLifecycleError("agent_browser_invalid_response") from None
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise BrowserLifecycleError("agent_browser_command_failed")
    return payload.get("data")


def _session_is_live(session: str, socket_dir: Path | None) -> bool:
    """Check the daemon registry before CDP; `get` auto-starts blank sessions."""

    listed = browser_json(session, "session", "list", socket_dir=socket_dir, timeout=5)
    sessions = listed.get("sessions") if isinstance(listed, dict) else None
    if (
        not isinstance(sessions, list)
        or len(sessions) > 256
        or any(not isinstance(item, str) for item in sessions)
    ):
        raise BrowserLifecycleError("agent_browser_invalid_response")
    if session not in sessions:
        return False
    cdp = browser_json(session, "get", "cdp-url", socket_dir=socket_dir, timeout=5)
    endpoint = cdp.get("cdpUrl") if isinstance(cdp, dict) else None
    parsed = urlparse(endpoint if isinstance(endpoint, str) else "")
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError:
        raise BrowserLifecycleError("browser_session_unavailable") from None
    if (
        parsed.scheme != "ws"
        or not address.is_loopback
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65_535
    ):
        raise BrowserLifecycleError("browser_session_unavailable")
    return True


def ensure_browser_session(
    job_id: str,
    session: str,
    entry_url: str,
    *,
    profile_root: Path | None = None,
    socket_dir: Path | None = None,
) -> dict[str, Any]:
    """Reattach without navigation, or relaunch a dead session at its entry URL."""

    validate_browser_binding(job_id, session)
    entry = validate_entry_url(entry_url)
    root = _secure_dir(profile_root or PROFILE_ROOT)
    profile = _secure_dir(root / job_id)
    reattached = _session_is_live(session, socket_dir)
    if not reattached:
        browser_json(
            session,
            "open",
            entry,
            profile=profile,
            socket_dir=socket_dir,
            timeout=30,
        )
        reattached = False
    stream = browser_json(session, "stream", "status", socket_dir=socket_dir, timeout=5)
    if not isinstance(stream, dict) or stream.get("enabled") is not True:
        browser_json(session, "stream", "enable", socket_dir=socket_dir, timeout=5)
        stream = browser_json(
            session, "stream", "status", socket_dir=socket_dir, timeout=5
        )
    port = stream.get("port") if isinstance(stream, dict) else None
    if (
        not isinstance(stream, dict)
        or stream.get("enabled") is not True
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65_535
    ):
        raise BrowserLifecycleError("browser_stream_unavailable")
    return {
        "profile": str(profile),
        "reattached": reattached,
        "session": session,
    }


def stream_url(session: str) -> str:
    if not _session_is_live(session, None):
        raise BrowserLifecycleError("browser_stream_unavailable")
    data = browser_json(session, "stream", "status", timeout=5)
    if not isinstance(data, dict) or data.get("enabled") is not True:
        browser_json(session, "stream", "enable", timeout=5)
        data = browser_json(session, "stream", "status", timeout=5)
    port = data.get("port") if isinstance(data, dict) else None
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
        raise BrowserLifecycleError("browser_stream_unavailable")
    return f"ws://127.0.0.1:{port}"


def browser_origin(session: str) -> str:
    if not _session_is_live(session, None):
        raise BrowserLifecycleError("browser_session_unavailable")
    data = browser_json(session, "get", "url", timeout=5)
    value = data.get("url") if isinstance(data, dict) else ""
    if value == "about:blank":
        return value
    parsed = urlparse(value if isinstance(value, str) else "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "Browser session"
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return "Browser session"
    return f"{parsed.scheme}://{host}{f':{port}' if port else ''}"


def insert_text(session: str, value: Any) -> None:
    if _SESSION_RE.fullmatch(session) is None:
        raise BrowserLifecycleError("invalid_browser_session")
    if not isinstance(value, str) or not value or len(value) > 8_192:
        raise ValueError("invalid_text_input")
    try:
        result = subprocess.run(
            [browser_binary(), "--session", session, "--json", "batch", "--bail"],
            input=json.dumps([["keyboard", "inserttext", value]]),
            env=browser_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BrowserLifecycleError("browser_text_input_failed") from None
    if result.returncode != 0:
        raise BrowserLifecycleError("browser_text_input_failed")


def switch_tab(session: str, tab_id: Any) -> None:
    browser_json(session, "tab", validate_tab_id(tab_id), timeout=5)


MOBILE_VIEWPORT = (390, 844)
MAX_PAGE_TEXT = 200_000


def set_viewport(session: str, width: int, height: int) -> None:
    """Pin a deterministic viewport before capturing layout evidence."""
    for value in (width, height):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= 4096
        ):
            raise BrowserLifecycleError("invalid_browser_viewport")
    browser_json(session, "viewport", str(width), str(height), timeout=10)


def open_url(session: str, url: Any) -> None:
    """Navigate the job's session to a validated, re-enterable entry URL."""
    browser_json(session, "open", validate_entry_url(url), timeout=30)


def page_text(session: str) -> str:
    """Return the rendered page text, bounded so a hostile page cannot flood."""
    data = browser_json(session, "get", "text", timeout=15)
    text = data.get("text") if isinstance(data, dict) else data
    if not isinstance(text, str):
        raise BrowserLifecycleError("browser_read_failed")
    return text[:MAX_PAGE_TEXT]


def screenshot(session: str, path: Path | str) -> Path:
    """Capture one 0600 screenshot into the job's evidence directory."""
    target = Path(path)
    if target.is_symlink():
        raise BrowserLifecycleError("unsafe_screenshot_path")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    browser_json(session, "screenshot", str(target), timeout=30)
    if not target.is_file():
        raise BrowserLifecycleError("browser_screenshot_failed")
    target.chmod(0o600)
    return target


def fill_field(session: str, selector: str, value: str) -> None:
    """Fill one selector. Never used for secrets -- those go through the gate."""
    if not isinstance(selector, str) or not 1 <= len(selector) <= 256:
        raise BrowserLifecycleError("invalid_browser_selector")
    if not isinstance(value, str) or not 1 <= len(value) <= 1_024:
        raise BrowserLifecycleError("invalid_browser_value")
    browser_json(session, "fill", selector, value, timeout=15)


def click_role(session: str, role: str, name: str) -> None:
    """Click by accessible role and name rather than a brittle CSS path."""
    for value in (role, name):
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise BrowserLifecycleError("invalid_browser_locator")
    browser_json(session, "find", "role", f"{role}={name}", "click", timeout=15)

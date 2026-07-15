"""Read one X source through the existing local burner-backed twitter CLI."""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

FEATURE_ENV = "ENABLE_AUTHENTICATED_X_FALLBACK"
COOKIE_PATH = Path("/home/v0id/.hermes/secrets/x-burner-cookies.txt")
TWITTER_BIN = Path("/home/v0id/.hermes/x-twitter-cli-venv/bin/twitter")
MAX_SOURCE_CHARS = 18_000
MAX_PROVIDER_OUTPUT_CHARS = 256_000
_X_STATUS_RE = re.compile(
    r"^https?://(?:www\.)?(?:x\.com|twitter\.com)/"
    r"(?:[A-Za-z0-9_]{1,15}|i)/status/(\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_TRUE = {"1", "true", "yes", "on"}


def _failed(status: str) -> dict[str, Any]:
    return {"status": status, "complete": False}


def _session_values(cookie_path: Path, *, now: float) -> tuple[str, str] | str:
    try:
        info = cookie_path.stat()
        geteuid = getattr(os, "geteuid", None)
        if (
            geteuid is None
            or info.st_uid != geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            return "login_required"
        jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError):
        return "login_required"
    found: dict[str, Any] = {}
    for cookie in jar:
        domain = cookie.domain.lstrip(".").lower()
        if cookie.name in {"auth_token", "ct0"} and (
            domain == "x.com" or domain.endswith(".x.com")
        ):
            found[cookie.name] = cookie
    if set(found) != {"auth_token", "ct0"}:
        return "login_required"
    if any(
        cookie.expires is not None and cookie.expires <= now
        for cookie in found.values()
    ):
        return "auth_session_expired"
    return found["auth_token"].value, found["ct0"].value


def _provider_failure(payload: Any, returncode: int) -> str:
    error = payload.get("error") if isinstance(payload, Mapping) else {}
    code = str(error.get("code") or "").lower() if isinstance(error, Mapping) else ""
    if any(part in code for part in ("auth", "unauthorized", "login")):
        return "auth_session_expired"
    if "forbidden" in code:
        return "forbidden_to_burner"
    if any(part in code for part in ("not_found", "notfound", "missing")):
        return "source_not_found"
    return "provider_unavailable" if returncode else "source_not_found"


def fetch_authenticated_x_source(
    url: str,
    *,
    environ: Mapping[str, str] | None = None,
    cookie_path: Path = COOKIE_PATH,
    binary_path: Path = TWITTER_BIN,
    run: Callable[..., Any] = subprocess.run,
    now: float | None = None,
) -> dict[str, Any]:
    """Return verified full text or one sanitized fail-closed status."""
    env_source = os.environ if environ is None else environ
    enabled = str(env_source.get(FEATURE_ENV, "")).strip().lower() in _TRUE
    if not enabled:
        return _failed("provider_unavailable")
    match = _X_STATUS_RE.fullmatch(str(url or "").strip())
    if not match:
        return _failed("unsupported_source_type")
    post_id = match.group(1)
    canonical_url = f"https://x.com/i/status/{post_id}"
    current_time = time.time() if now is None else now
    session = _session_values(cookie_path, now=current_time)
    if isinstance(session, str):
        return _failed(session)
    if not binary_path.is_file():
        return _failed("provider_unavailable")
    child_env = {
        "HOME": str(Path.home()),
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TWITTER_AUTH_TOKEN": session[0],
        "TWITTER_CT0": session[1],
    }
    try:
        completed = run(
            [
                str(binary_path),
                "tweet",
                canonical_url,
                "--max",
                "1",
                "--full-text",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=child_env,
        )
    except (OSError, subprocess.SubprocessError):
        return _failed("provider_unavailable")
    raw = str(getattr(completed, "stdout", "") or "")
    if len(raw) > MAX_PROVIDER_OUTPUT_CHARS:
        return _failed("partial_source_rejected")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return _failed("provider_unavailable")
    returncode = int(getattr(completed, "returncode", 1))
    if (
        returncode != 0
        or not isinstance(payload, Mapping)
        or payload.get("schema_version") != "1"
        or payload.get("ok") is not True
    ):
        return _failed(_provider_failure(payload, returncode))
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], Mapping):
        return _failed("source_not_found")
    root = data[0]
    if str(root.get("id") or "") != post_id:
        return _failed("identity_mismatch")
    author = root.get("author")
    handle = str(author.get("screenName") or "") if isinstance(author, Mapping) else ""
    if not _HANDLE_RE.fullmatch(handle):
        return _failed("identity_mismatch")
    article_title = str(root.get("articleTitle") or "").strip()
    article_text = str(root.get("articleText") or "").strip()
    root_text = str(root.get("text") or "").strip()
    if article_title and not article_text:
        return _failed("partial_source_rejected")
    if article_text:
        text = f"{article_title}\n\n{article_text}".strip()
        source_type = "article"
    else:
        text = root_text
        if re.fullmatch(r"https?://\S+", root_text):
            return _failed("partial_source_rejected")
        source_type = (
            "protected_post"
            if root.get("isSubscriberOnly") is True
            else "long_form_note"
            if len(root_text) > 280
            else "ordinary_post"
        )
    quoted = root.get("quotedTweet")
    if isinstance(quoted, Mapping) and str(quoted.get("text") or "").strip():
        quoted_author = quoted.get("author")
        quoted_handle = (
            str(quoted_author.get("screenName") or "")
            if isinstance(quoted_author, Mapping)
            else ""
        )
        text += f"\n\nQuoted post by @{quoted_handle}:\n{str(quoted['text']).strip()}"
    if not text or len(text) > MAX_SOURCE_CHARS:
        return _failed("partial_source_rejected")
    return {
        "status": "fetched_full_authenticated",
        "canonical_url": canonical_url,
        "post_id": post_id,
        "author_handle": handle,
        "source_type": source_type,
        "complete": True,
        "text": text,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "acquisition_mode": "authenticated_x_fallback",
    }

"""Shared least-privilege OAuth policy for the Google Workspace skill."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from _hermes_home import get_hermes_home


LINXIO_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events.owned",
)
SERVICE_PROFILES = {"linxio": LINXIO_SCOPES}


def oauth_client_path() -> Path:
    return Path(
        os.environ.get(
            "GOOGLE_OAUTH_CLIENT_FILE",
            get_hermes_home() / "secrets/google/google_credentials.json",
        )
    ).expanduser()


def oauth_token_path() -> Path:
    return Path(
        os.environ.get(
            "GOOGLE_OAUTH_TOKEN_FILE",
            get_hermes_home() / "secrets/google/google_token.json",
        )
    ).expanduser()


def private_state_path(name: str) -> Path:
    return oauth_token_path().parent / name


def ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("private directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def write_private_json(path: Path, payload: dict) -> None:
    ensure_private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(payload, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        Path(temporary).unlink(missing_ok=True)


def secure_existing_file(path: Path) -> None:
    ensure_private_directory(path.parent)
    path.chmod(0o600)

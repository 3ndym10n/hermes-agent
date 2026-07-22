"""Shared least-privilege OAuth policy for the Google Workspace skill."""

from __future__ import annotations

import json
import os
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
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def write_private_json(path: Path, payload: dict) -> None:
    ensure_private_directory(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(payload, stream, indent=2)
            stream.write("\n")
    finally:
        if fd >= 0:
            os.close(fd)


def secure_existing_file(path: Path) -> None:
    ensure_private_directory(path.parent)
    path.chmod(0o600)

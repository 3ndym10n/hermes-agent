"""Security and behavior contracts for the Linxio Workspace profile."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import stat
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills/productivity/google-workspace/scripts"
API_PATH = SCRIPTS / "google_api.py"
SETUP_PATH = SCRIPTS / "setup.py"
SMOKE_PATH = SCRIPTS / "smoke_test.py"
EXPECTED_SCOPES = {
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events.owned",
}


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_FILE", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_TOKEN_FILE", raising=False)
    module = _load(API_PATH, "google_api_linxio_test")
    module._gws_binary = lambda: "/usr/bin/gws"
    module._ensure_authenticated = lambda: None
    return module


def test_exact_scopes_and_named_profile(api):
    setup = _load(SETUP_PATH, "google_setup_scopes_test")
    assert set(api.SCOPES) == EXPECTED_SCOPES
    assert set(setup.SCOPES) == EXPECTED_SCOPES
    assert len(api.SCOPES) == 5
    assert set(api.SERVICE_PROFILES) == {"linxio"}
    assert set(api.SERVICE_PROFILES["linxio"]) == EXPECTED_SCOPES


def test_default_and_override_paths_are_private(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    auth = _load(SCRIPTS / "google_auth.py", "google_auth_paths_test")
    assert auth.oauth_client_path() == tmp_path / "home/secrets/google/google_credentials.json"
    assert auth.oauth_token_path() == tmp_path / "home/secrets/google/google_token.json"

    client = tmp_path / "outside/client.json"
    token = tmp_path / "outside/token.json"
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_FILE", str(client))
    monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_FILE", str(token))
    assert auth.oauth_client_path() == client
    assert auth.oauth_token_path() == token

    auth.write_private_json(token, {"token": "not-a-real-token"})
    assert stat.S_IMODE(token.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(token.stat().st_mode) == 0o600


def test_pending_oauth_state_is_private(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    setup = _load(SETUP_PATH, "google_setup_permissions_test")
    setup._save_pending_auth(state="state", code_verifier="verifier")
    assert stat.S_IMODE(setup.PENDING_AUTH_PATH.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(setup.PENDING_AUTH_PATH.stat().st_mode) == 0o600



@pytest.mark.parametrize("scopes", [
    ["https://www.googleapis.com/auth/gmail.readonly"],
    sorted(EXPECTED_SCOPES | {"https://www.googleapis.com/auth/drive"}),
])
def test_runtime_rejects_missing_or_extra_token_scopes(monkeypatch, tmp_path, scopes):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    module = _load(API_PATH, f"google_api_scope_reject_{len(scopes)}")
    module.TOKEN_PATH.parent.mkdir(parents=True)
    module.TOKEN_PATH.write_text(json.dumps({"scopes": scopes}))
    with pytest.raises(SystemExit):
        module._ensure_authenticated()


def test_cli_exposes_no_send_attendees_share_or_permanent_delete(tmp_path):
    env = {**os.environ, "HERMES_HOME": str(tmp_path / ".hermes")}
    forbidden = [
        ["gmail", "send"],
        ["gmail", "reply"],
        ["calendar", "create", "--summary", "x", "--start", "2026-01-01T00:00:00Z", "--end", "2026-01-01T00:10:00Z", "--attendees", "x@example.com"],
        ["drive", "share", "file"],
        ["drive", "delete", "file", "--permanent"],
        ["contacts", "list"],
        ["sheets", "get", "sheet", "A1"],
        ["docs", "get", "doc"],
    ]
    for argv in forbidden:
        result = subprocess.run(
            [sys.executable, str(API_PATH), *argv],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2, argv


def test_gmail_thread_reads_complete_messages(api, capsys):
    body = api.base64.urlsafe_b64encode(b"full customer body").decode()
    api._run_gws = lambda *a, **k: {
        "id": "thread-1",
        "messages": [{
            "id": "message-1",
            "threadId": "thread-1",
            "payload": {
                "headers": [{"name": "From", "value": "customer@example.com"}],
                "mimeType": "multipart/mixed",
                "parts": [{
                    "mimeType": "multipart/alternative",
                    "parts": [{"mimeType": "text/plain", "body": {"data": body}}],
                }],
            },
        }],
    }
    api.gmail_thread_get(api.argparse.Namespace(thread_id="thread-1"))
    result = json.loads(capsys.readouterr().out)
    assert result["messages"][0]["body"] == "full customer body"


def test_gmail_draft_create_and_delete_never_send(api, capsys):
    calls = []

    def run(parts, *, params=None, body=None):
        calls.append((parts, params, body))
        if parts[-1] == "create":
            return {"id": "draft-1", "message": {"id": "message-1"}}
        return {}

    api._run_gws = run
    api.gmail_draft_create(api.argparse.Namespace(
        to="recipient@example.com", subject="subject", body="body", html=False,
        cc="", from_header="", thread_id="",
    ))
    api.gmail_draft_delete(api.argparse.Namespace(draft_id="draft-1"))
    assert [call[0] for call in calls] == [
        ["gmail", "users", "drafts", "create"],
        ["gmail", "users", "drafts", "delete"],
    ]
    assert "drafted" in capsys.readouterr().out


def _event_args(api, **overrides):
    values = {
        "summary": "Approved review",
        "start": "2026-08-01T14:00:00Z",
        "end": "2026-08-01T14:30:00Z",
        "location": "",
        "description": "",
        "calendar": "primary",
        "dry_run": True,
        "approval_token": "",
    }
    values.update(overrides)
    return api.argparse.Namespace(**values)


def test_calendar_dry_run_is_private_and_approval_is_one_time(api, capsys):
    api._run_gws = MagicMock(return_value={"id": "event-1", "summary": "Approved review"})
    api.calendar_create(_event_args(api))
    preview = json.loads(capsys.readouterr().out)
    assert preview["plan"]["event"]["visibility"] == "private"
    assert "attendees" not in preview["plan"]["event"]
    assert stat.S_IMODE(api.APPROVAL_PATH.stat().st_mode) == 0o600
    api._run_gws.assert_not_called()

    approved = _event_args(
        api, dry_run=False, approval_token=preview["approval_token"]
    )
    api.calendar_create(approved)
    _, kwargs = api._run_gws.call_args
    assert kwargs["params"]["sendUpdates"] == "none"
    assert not api.APPROVAL_PATH.exists()
    with pytest.raises(SystemExit):
        api.calendar_create(approved)


def test_calendar_approval_rejects_expired_token_before_api_call(api, monkeypatch):
    args = api.argparse.Namespace(
        summary="Private review", start="2026-08-01T14:00:00Z",
        end="2026-08-01T14:30:00Z", location="", description="",
        dry_run=True, approval_token="", calendar="primary",
    )
    api.calendar_create(args)
    approval = json.loads(api.APPROVAL_PATH.read_text())
    approval["expires_at"] = 0
    api.write_private_json(api.APPROVAL_PATH, approval)
    args.dry_run = False
    args.approval_token = approval["token"]
    run = MagicMock()
    monkeypatch.setattr(api, "_run_gws", run)

    with pytest.raises(SystemExit):
        api.calendar_create(args)

    run.assert_not_called()


def test_calendar_approval_is_bound_to_exact_plan(api, capsys):
    api.calendar_create(_event_args(api))
    token = json.loads(capsys.readouterr().out)["approval_token"]
    with pytest.raises(SystemExit):
        api.calendar_create(_event_args(
            api, summary="Changed", dry_run=False, approval_token=token
        ))


def test_drive_create_file_marks_linxio_ownership(api, monkeypatch, capsys):
    media_module = types.ModuleType("googleapiclient.http")
    media_module.MediaInMemoryUpload = lambda data, **kwargs: (data, kwargs)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", media_module)
    create = MagicMock()
    create.return_value.execute.return_value = {"id": "file-1", "name": "Knowledge"}
    service = MagicMock()
    service.files.return_value.create = create
    api.build_service = lambda *a: service

    api.drive_create_file(api.argparse.Namespace(
        name="Knowledge", kind="linxio", content="text", content_file="", parent=""
    ))
    metadata = create.call_args.kwargs["body"]
    assert metadata["mimeType"] == "application/vnd.google-apps.document"
    assert metadata["appProperties"] == {
        "hermesServiceProfile": "linxio",
        "knowledgeKind": "linxio",
    }
    assert json.loads(capsys.readouterr().out)["status"] == "created"


def test_persistent_redaction_preserves_memory_content():
    from agent.redact import (
        PersistentRedactingFormatter,
        redact_persistent_text,
        redact_sensitive_text,
    )

    raw = (
        'customer@example.com +1 (555) 867-5309 '
        'https://localhost/callback?code=secret-code '
        '{"refresh_token":"refresh-secret","body":"raw customer request"} '
        '--auth-code 4/authorizationcode --approval-token approval-secret '
        '--body "another raw customer body" ya29.accesstoken'
    )
    assert "customer@example.com" in redact_sensitive_text(raw)
    persisted = redact_persistent_text(raw)
    for secret in (
        "customer@example.com", "555", "secret-code", "refresh-secret",
        "raw customer request", "authorizationcode", "approval-secret",
        "another raw customer body", "accesstoken",
    ):
        assert secret not in persisted
    record = logging.LogRecord("test", logging.INFO, "", 0, raw, (), None)
    assert "customer@example.com" not in PersistentRedactingFormatter("%(message)s").format(record)


def test_gitignore_covers_google_oauth_artifacts():
    ignore = (ROOT / ".gitignore").read_text()
    for pattern in (
        "**/secrets/google/",
        "google_credentials.json",
        "google_token*.json",
        "google_oauth_pending*.json",
        "google_calendar_approval*.json",
    ):
        assert pattern in ignore


def test_smoke_test_uses_drafts_no_attendees_and_always_cleans_up(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    sys.modules.pop("google_api", None)
    smoke = _load(SMOKE_PATH, "google_smoke_cleanup_test")

    media_module = types.ModuleType("googleapiclient.http")
    media_module.MediaInMemoryUpload = lambda data, **kwargs: (data, kwargs)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", media_module)

    gmail = MagicMock()
    gmail_users = gmail.users.return_value
    gmail_users.messages.return_value.list.return_value.execute.return_value = {
        "messages": []
    }
    gmail_users.getProfile.return_value.execute.return_value = {
        "emailAddress": "owner@example.com"
    }
    gmail_users.drafts.return_value.create.return_value.execute.return_value = {
        "id": "draft-1"
    }

    drive = MagicMock()
    drive.files.return_value.create.return_value.execute.return_value = {
        "id": "file-1"
    }

    calendar = MagicMock()
    calendar.events.return_value.list.return_value.execute.return_value = {}
    calendar.events.return_value.insert.return_value.execute.return_value = {
        "id": "event-1"
    }

    services = {"gmail": gmail, "drive": drive, "calendar": calendar}
    smoke.build_service = lambda name, version: services[name]
    smoke.dry_run()
    preview = json.loads(capsys.readouterr().out)
    assert "attendees" not in preview["plan"]["event"]

    smoke.run(preview["approval_token"])
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "passed"
    gmail_users.drafts.return_value.delete.assert_called_once_with(
        userId="me", id="draft-1"
    )
    drive.files.return_value.update.assert_called_once_with(
        fileId="file-1", body={"trashed": True}
    )
    calendar.events.return_value.delete.assert_called_once_with(
        calendarId="primary", eventId="event-1", sendUpdates="none"
    )
    event = calendar.events.return_value.insert.call_args.kwargs["body"]
    assert event["visibility"] == "private"
    assert "attendees" not in event
    assert gmail_users.messages.return_value.send.call_count == 0


def test_smoke_test_cleans_draft_and_drive_after_calendar_failure(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    sys.modules.pop("google_api", None)
    smoke = _load(SMOKE_PATH, "google_smoke_failure_cleanup_test")
    media_module = types.ModuleType("googleapiclient.http")
    media_module.MediaInMemoryUpload = lambda data, **kwargs: (data, kwargs)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", media_module)

    gmail = MagicMock()
    users = gmail.users.return_value
    users.messages.return_value.list.return_value.execute.return_value = {
        "messages": []
    }
    users.getProfile.return_value.execute.return_value = {
        "emailAddress": "owner@example.com"
    }
    users.drafts.return_value.create.return_value.execute.return_value = {
        "id": "draft-1"
    }
    drive = MagicMock()
    drive.files.return_value.create.return_value.execute.return_value = {
        "id": "file-1"
    }
    calendar = MagicMock()
    calendar.events.return_value.list.return_value.execute.return_value = {}
    calendar.events.return_value.insert.return_value.execute.side_effect = RuntimeError(
        "synthetic failure"
    )
    services = {"gmail": gmail, "drive": drive, "calendar": calendar}
    smoke.build_service = lambda name, version: services[name]

    smoke.dry_run()
    token = json.loads(capsys.readouterr().out)["approval_token"]
    with pytest.raises(SystemExit):
        smoke.run(token)
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed"
    assert result["checks"]["failure_type"] == "RuntimeError"
    users.drafts.return_value.delete.assert_called_once()
    drive.files.return_value.update.assert_called_once()
    calendar.events.return_value.delete.assert_not_called()

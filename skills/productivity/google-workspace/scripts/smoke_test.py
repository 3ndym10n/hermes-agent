#!/usr/bin/env python3
"""Reversible live smoke test for the Linxio Google Workspace profile."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from google_api import (
    APPROVAL_PATH,
    APPROVAL_TTL_SECONDS,
    _consume_approval,
    _issue_approval,
    build_service,
)


def _new_plan() -> dict:
    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        second=0, microsecond=0
    )
    end = start + timedelta(minutes=10)
    return {
        "operation": "smoke-test",
        "calendar": "primary",
        "event": {
            "summary": "Hermes Linxio smoke test",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "visibility": "private",
        },
    }


def dry_run() -> None:
    plan = _new_plan()
    print(json.dumps({
        "status": "approval_required",
        "plan": plan,
        "approval_token": _issue_approval(plan),
        "expires_in": APPROVAL_TTL_SECONDS,
        "guarantees": ["no email sending", "no attendees", "cleanup always attempted"],
    }, indent=2))


def run(approval_token: str) -> None:
    try:
        approval = json.loads(APPROVAL_PATH.read_text())
        plan = approval["plan"]
    except Exception:
        print("ERROR: no pending smoke-test approval; run --dry-run first", file=sys.stderr)
        raise SystemExit(2)
    if plan.get("operation") != "smoke-test":
        print("ERROR: pending approval is not for the smoke test", file=sys.stderr)
        raise SystemExit(2)
    _consume_approval(approval_token, plan)

    draft_id = drive_file_id = event_id = None
    checks = {}
    cleanup = {}
    try:
        gmail = build_service("gmail", "v1")
        found = gmail.users().messages().list(
            userId="me", q="newer_than:30d", maxResults=1
        ).execute().get("messages", [])
        checks["gmail_search"] = True
        if found:
            message = gmail.users().messages().get(
                userId="me", id=found[0]["id"], format="full"
            ).execute()
            gmail.users().threads().get(
                userId="me", id=message["threadId"], format="full"
            ).execute()
        checks["gmail_read"] = True

        own_email = gmail.users().getProfile(userId="me").execute()["emailAddress"]
        mime = MIMEText("Temporary Linxio smoke-test draft. This message is never sent.")
        mime["To"] = own_email
        mime["Subject"] = "Hermes Linxio smoke-test draft"
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        draft = gmail.users().drafts().create(
            userId="me", body={"message": {"raw": raw}}
        ).execute()
        draft_id = draft["id"]
        checks["gmail_draft_create"] = True

        from googleapiclient.http import MediaInMemoryUpload  # ty: ignore[unresolved-import]

        drive = build_service("drive", "v3")
        created = drive.files().create(
            body={
                "name": "Hermes Linxio smoke test",
                "mimeType": "application/vnd.google-apps.document",
                "appProperties": {
                    "hermesServiceProfile": "linxio",
                    "knowledgeKind": "linxio",
                },
            },
            media_body=MediaInMemoryUpload(
                b"Temporary Linxio smoke-test file. Safe to trash.",
                mimetype="text/plain",
                resumable=False,
            ),
            fields="id",
        ).execute()
        drive_file_id = created["id"]
        checks["drive_create"] = True

        calendar = build_service("calendar", "v3")
        calendar.events().list(
            calendarId=plan["calendar"],
            timeMin=datetime.now(timezone.utc).isoformat(),
            maxResults=1,
            singleEvents=True,
        ).execute()
        checks["calendar_read"] = True
        assert "attendees" not in plan["event"]
        created_event = calendar.events().insert(
            calendarId=plan["calendar"],
            body=plan["event"],
            sendUpdates="none",
        ).execute()
        event_id = created_event["id"]
        checks["calendar_private_create"] = True
    except Exception as exc:
        checks["failure_type"] = type(exc).__name__
    finally:
        if draft_id:
            try:
                gmail.users().drafts().delete(userId="me", id=draft_id).execute()
                cleanup["gmail_draft"] = True
            except Exception as exc:
                cleanup["gmail_draft"] = type(exc).__name__
        if drive_file_id:
            try:
                drive.files().update(
                    fileId=drive_file_id, body={"trashed": True}
                ).execute()
                cleanup["drive_file_trashed"] = True
            except Exception as exc:
                cleanup["drive_file_trashed"] = type(exc).__name__
        if event_id:
            try:
                calendar.events().delete(
                    calendarId=plan["calendar"], eventId=event_id, sendUpdates="none"
                ).execute()
                cleanup["calendar_event"] = True
            except Exception as exc:
                cleanup["calendar_event"] = type(exc).__name__

    passed = "failure_type" not in checks and all(value is True for value in cleanup.values())
    print(json.dumps({"status": "passed" if passed else "failed", "checks": checks, "cleanup": cleanup}, indent=2))
    if not passed:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--approval-token")
    args = parser.parse_args()
    dry_run() if args.dry_run else run(args.approval_token)


if __name__ == "__main__":
    main()

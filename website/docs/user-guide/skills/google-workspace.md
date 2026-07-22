---
sidebar_position: 2
sidebar_label: "Google Workspace"
title: "Google Workspace — Linxio Gmail, Calendar & Drive"
description: "Least-privilege Gmail drafts, owned Calendar events, and integration-created Drive files"
---

# Google Workspace Skill

The Linxio profile gives Hermes read access to Gmail and Calendar, draft-only Gmail composition, approval-gated owned Calendar events, and `drive.file` access to files the integration creates.

Email sending is not exposed.

## Scopes

Authorization requests exactly five scopes:

```text
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.compose
https://www.googleapis.com/auth/drive.file
https://www.googleapis.com/auth/calendar.readonly
https://www.googleapis.com/auth/calendar.events.owned
```

Credentials default to `${HERMES_HOME:-$HOME/.hermes}/secrets/google/`. Deployments can set `GOOGLE_OAUTH_CLIENT_FILE` and `GOOGLE_OAUTH_TOKEN_FILE` to absolute secret paths.

## Authorization

```bash
GSETUP="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/setup.py"
$GSETUP --service-profile linxio --check
$GSETUP --service-profile linxio --auth-url
$GSETUP --service-profile linxio --auth-code 'COMPLETE_LOCALHOST_REDIRECT_URL'
```

The browser consent and redirect step requires the user. Never print or copy credential/token contents.

## Gmail

```bash
GAPI="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/google_api.py"
$GAPI gmail search 'from:customer@example.com newer_than:30d' --max 10
$GAPI gmail get MESSAGE_ID
$GAPI gmail thread THREAD_ID
$GAPI gmail draft-create --to recipient@example.com --subject 'Subject' --body 'Body'
$GAPI gmail draft-reply MESSAGE_ID --body 'Reply body'
$GAPI gmail draft-delete DRAFT_ID
```

There is no send, reply-send, or draft-send command.

## Calendar

```bash
$GAPI calendar list
$GAPI calendar create --summary 'Review' --start 2026-08-01T14:00:00Z --end 2026-08-01T14:30:00Z --dry-run
$GAPI calendar create --summary 'Review' --start 2026-08-01T14:00:00Z --end 2026-08-01T14:30:00Z --approval-token TOKEN
$GAPI calendar delete EVENT_ID
```

Creation tokens are short-lived, one-time, and bound to the exact preview. Created events are private, attendees are unsupported, and notifications are disabled.

## Drive

```bash
$GAPI drive create-file --kind linxio --name 'Linxio Knowledge' --content 'Text'
$GAPI drive create-file --kind cogitator --name 'Cogitator Knowledge' --content-file /path/to/content.txt
$GAPI drive delete FILE_ID
```

Deletion moves files to trash. Sharing and permanent deletion are not exposed.

## Smoke test

```bash
GSMOKE="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/smoke_test.py"
$GSMOKE --dry-run
$GSMOKE --approval-token TOKEN
```

The smoke test never sends email or adds attendees and always attempts cleanup of its draft, Drive file, and private Calendar event.

---
name: google-workspace
description: "Least-privilege Gmail, Calendar, and Drive access for Linxio."
version: 2.0.0
author: Nous Research
license: MIT
platforms: [linux, macos, windows]
required_credential_files:
  - path: secrets/google/google_token.json
    description: Google OAuth2 token created by the setup script
  - path: secrets/google/google_credentials.json
    description: Google OAuth2 desktop client credentials
metadata:
  hermes:
    tags: [Google, Gmail, Calendar, Drive, Email, OAuth, Linxio]
    homepage: https://github.com/NousResearch/hermes-agent
---

# Google Workspace — Linxio

Gmail reading and draft composition, owned Calendar events, and integration-created Drive files through Hermes-managed OAuth. Normal use cannot send email. Calendar creation requires a short-lived, one-time approval token bound to the exact event preview.

## Required scopes

The named `linxio` service profile always requests exactly these scopes:

```text
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.compose
https://www.googleapis.com/auth/drive.file
https://www.googleapis.com/auth/calendar.readonly
https://www.googleapis.com/auth/calendar.events.owned
```

Do not add or substitute broader scopes.

## Paths

Defaults:

```text
${HERMES_HOME:-$HOME/.hermes}/secrets/google/google_credentials.json
${HERMES_HOME:-$HOME/.hermes}/secrets/google/google_token.json
```

Credential deployments may override them with secrets-only environment variables:

```bash
GOOGLE_OAUTH_CLIENT_FILE=/absolute/path/to/google_credentials.json
GOOGLE_OAUTH_TOKEN_FILE=/absolute/path/to/google_token.json
```

OAuth directories are mode `0700`; credentials, tokens, approval state, and pending authorization files are mode `0600`.

## Authorization

Define the scripts:

```bash
GSETUP="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/setup.py"
GAPI="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/google_api.py"
GSMOKE="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/smoke_test.py"
```

Check existing authorization:

```bash
$GSETUP --service-profile linxio --check
```

If the configured client file is already staged, do not copy or print it. Start authorization only after the user explicitly asks:

```bash
$GSETUP --service-profile linxio --auth-url
```

Send the URL to the user. They must sign in, approve consent, and paste the complete localhost redirect URL. Exchange it without logging or repeating it:

```bash
$GSETUP --service-profile linxio --auth-code 'COMPLETE_LOCALHOST_REDIRECT_URL'
```

## Gmail

```bash
# Search by customer, company, prospect, or any Gmail query
$GAPI gmail search 'from:customer@example.com newer_than:30d' --max 10
$GAPI gmail search '"Prospect Company"' --max 10

# Read one complete message or thread
$GAPI gmail get MESSAGE_ID
$GAPI gmail thread THREAD_ID

# Create drafts only
$GAPI gmail draft-create --to recipient@example.com --subject 'Subject' --body 'Body'
$GAPI gmail draft-reply MESSAGE_ID --body 'Reply body'
$GAPI gmail draft-delete DRAFT_ID
```

There is no `gmail send`, reply-send, or draft-send command. Never bypass that boundary through `gws`, raw API calls, or another tool in Virgil's normal workflow.

## Calendar

Reads are immediate:

```bash
$GAPI calendar list
$GAPI calendar list --start 2026-08-01T00:00:00Z --end 2026-08-08T00:00:00Z
```

Creation is two-step. First preview the exact private, attendee-free event:

```bash
$GAPI calendar create --summary 'Review' --start 2026-08-01T14:00:00Z --end 2026-08-01T14:30:00Z --dry-run
```

Show the preview to the user. Only after explicit approval, use the returned token with the identical event arguments:

```bash
$GAPI calendar create --summary 'Review' --start 2026-08-01T14:00:00Z --end 2026-08-01T14:30:00Z --approval-token TOKEN
```

Tokens expire after ten minutes, are one-time use, and fail if event arguments change. Attendees are unsupported and `sendUpdates` is always `none`.

Cleanup an integration-owned event:

```bash
$GAPI calendar delete EVENT_ID
```

## Drive

`drive.file` only exposes files created or explicitly opened by this integration.

```bash
$GAPI drive create-file --kind linxio --name 'Linxio Knowledge' --content 'Document text'
$GAPI drive create-file --kind cogitator --name 'Cogitator Knowledge' --content-file /path/to/content.txt
$GAPI drive search 'Knowledge' --max 10
$GAPI drive get FILE_ID
$GAPI drive delete FILE_ID
```

Drive deletion always moves a file to trash. Sharing and permanent deletion are not exposed.

## Reversible smoke test

Preview the private test event and request explicit approval:

```bash
$GSMOKE --dry-run
```

After approval:

```bash
$GSMOKE --approval-token TOKEN
```

The smoke test reads/searches Gmail, reads a full message and thread when available, creates then deletes a draft, creates then trashes a Drive document, reads Calendar, and creates then deletes one private event. It never sends email, never adds attendees, and attempts cleanup after success or failure.

## Privacy rules

- Requested message and thread content is returned in memory so Virgil can answer Cal.
- Persistent logs redact OAuth client secrets, access/refresh tokens, authorization codes, approval tokens, email addresses, phone numbers, and serialized email bodies/snippets.
- Do not redirect raw Gmail output into persistent files or logs.
- Never print, copy, upload, or commit credential/token files.

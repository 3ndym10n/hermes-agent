# Virgil Mobile operational sources V1

Virgil Mobile is a read-only operational projection over authoritative source
systems. Adapters produce bounded facts; deterministic Hermes policy maps those
facts into the existing Attention Queue. The worker may upsert, resolve, or
expire Attention records and deliver an already-approved Telegram notification.
It never mutates an external source.

## Factual source matrix

The baseline column describes the audited state before this V1 integration.

| Source | Baseline and authentication | Authority and V1 reads | External writes and mutation boundary | V1 health |
|---|---|---|---|---|
| Gmail | Connected and already Attention-integrated through the approved Google account and independent 60-second Gmail worker. | Gmail remains authoritative. V1 reads only protected worker health/checkpoint metadata; it does not replay mail. | No Gmail mutation: no draft creation or sending, read-state, archive, delete, move, or label change. V1 writes only source health; the existing adapter may upsert/resolve Attention items. | `active`, `paused`, `degraded`, or `failed`. |
| Calendar | Approved Calendar OAuth was connected but Calendar was not Attention-integrated before this PR. The primary account is verified against the existing Gmail account fingerprint. | Google Calendar is authoritative. V1 reads the primary calendar from now through seven days, including safely visible cancellations. | Read-only: no event create/update/delete, invitation response, or attendee change. Only Attention and source-status state changes. | `active`, `unavailable`, or `failed`. |
| Cogitator | The bearer-authenticated bridge was connected; no Cogitator item was Attention-integrated before this PR. | Cogitator's durable database and files remain authoritative. V1 reads only the bridge's bounded `operational_items` snapshot. | No approve, reject, promote, research start, routing change, or note write. Only Attention and source-status state changes. | `active`, `degraded`, or `failed`. |
| GitHub | Authenticated `gh` access and both local repositories were available; no operational Attention adapter existed. | GitHub is authoritative for PRs/checks/reviews; local Git is authoritative for checkout risk. Reads are limited to `3ndym10n/hermes-agent` and `3ndym10n/Cogitator`. | No PR merge/close/edit, issue change, branch change, reset, clean-up, push, or deployment. | `active`, `unavailable`, or `failed`. |
| Ecommerce | Trustworthy evidence was limited to GitHub/local work. No commerce runtime database was found during the audit. | GitHub/local state is used now. A configured private commerce DB is read-only if it later exists and has the supported `jobs`/`gates` schema. Provider and registrar systems remain authoritative. | No purchase, domain registration, DNS, Shopify, payment, commerce-job, credential, or PR mutation. | `active`, `blocked`, `unavailable`, or `failed`; absent trustworthy evidence reports “No trustworthy ecommerce runtime feed is currently available.” |
| System | Local service, timer, filesystem, SQLite, Tailscale, and authentication state were available. | Local systemd/Tailscale state and the Attention database are authoritative for health. | Inspection only. No service restart, repair, log mutation, database reset, or Tailscale change. Incidents only change Attention/source status. | `healthy` or `degraded`; adapter failure is recorded as `failed`. |
| HubSpot | Deliberately deferred: production access is unavailable. | No reads. HubSpot remains authoritative when a future approved adapter exists. | No writes and no V1 adapter. | Deferred/unavailable. |
| Softphone | Deliberately deferred: no production API is available. | No reads. The phone system remains authoritative when a future approved adapter exists. | No writes and no V1 adapter. | Deferred/unavailable. |
| Personal | Not connected. | No authoritative feed and no reads. | No writes and no adapter. A retained historical Attention item may still make the project filter visible. | `not_connected`. |

## Stored data boundary

Every fact is reduced to the existing queue contract: source type, stable opaque
record/event identity, deterministic kind, bounded title, bounded safe summary,
recommended action, optional domain, confidence, due/expiry times, and an
allowlisted deep link. Policy—not the adapter—sets project, item type, priority,
status, waiting-on, and reason code. `processing_version` is
`virgil-operational-sources-v1`.

Before storage, external text is normalized, control characters and angle
brackets are removed, secret-looking values, email addresses, URLs, and long
numbers are redacted, and title/summary/action are limited to 180/500/300
characters. Source-status messages are limited to 300 characters and failure
codes to 80. Raw provider responses are never stored.

Adapter-specific retained data:

- **Gmail:** existing opaque thread/message identity, bounded operational title,
  outcome/category/reason, confidence, timestamps, recommendation, and an
  allowlisted Gmail thread link. The health adapter reads only `mode`,
  `authentication_health`, `last_successful_poll`, `history_watermark`, verified
  account fingerprint, intervention flags, and shadow safety hold; it stores
  only the derived status/message/timestamps, never those values.
- **Calendar:** hashed event identity; title; start and end (`due_at` and
  `expires_at`); Australia/Sydney display time; attendee count; location/video
  presence booleans; event status; recommendation; and an allowlisted Calendar
  link. The API field selection excludes descriptions, attendee addresses,
  notes, and attachments.
- **Cogitator:** the bridge accepts exactly `source_id`, `title`, `item_type`,
  `created_at`, `review_status`, `current_action`, `evidence_quality`,
  `research_status`, `research_updated_at`, `research_stalled`,
  `research_has_artifact`, `promotion_candidate_ready`, `promotion_approved`,
  `high_risk`, and `blocked`. Hermes stores a hash of `source_id`, a bounded
  title, and a short status/evidence summary; it does not store the raw snapshot.
- **GitHub:** repository, PR number, bounded title, draft/merge/review/check
  outcome, unresolved-thread count, timestamps, and allowlisted GitHub URL.
  Local branch/head/dirty/divergence facts affect only a hashed event identity
  and generic checkout-risk summary; diffs and file contents are not stored.
- **Ecommerce:** relevant GitHub PR facts; if a supported DB exists, hashed
  job/gate identity, bounded current state or gate type, active flag, deadline,
  and recommendation. A source update time contributes only to the opaque event
  identity. Provider payloads and external commercial state are not inferred
  when the feed is absent.
- **System:** unit name and bounded active/result/restart findings, timer
  staleness, private Tailscale availability, database integrity, disk threshold,
  and derived Gmail/Calendar/source health. No raw logs, command output, or
  internal path is copied into Attention.

All adapters forbid raw email bodies/snippets/draft bodies, Calendar bodies or
attendee addresses, source documents, raw research, meeting notes, attachments,
OAuth/bridge tokens, cookies, passwords, payment data, phone numbers, private
provider payloads, terminal transcripts, and unbounded logs. Credentials and
checkpoints remain in their existing protected files.

## Future adapter contract

A future HubSpot or softphone adapter must emit a bounded `SourceFact` and use a
deterministic policy entry. The resulting Attention submission supports:

- source identity (`source_type`, stable record ID, stable event ID);
- project, item type, priority, and status;
- safe title, safe summary, and one recommended action;
- waiting-on, reason code, and optional confidence;
- optional due/expiry time and one allowlisted deep link; and
- processing version.

The adapter may not select side effects or Telegram interruption. Adding the
adapter requires an approved read credential and an explicit policy; it does not
require a new queue, network ingestion endpoint, dashboard, or chat surface.

## Worker operation

Run commands from the Hermes checkout with its venv Python. Controllable sources
are `gmail`, `calendar`, `cogitator`, `github`, `ecommerce`, and `system`.

```bash
/home/v0id/.hermes/hermes-agent/venv/bin/python -m virgil_operational_sources status
/home/v0id/.hermes/hermes-agent/venv/bin/python -m virgil_operational_sources doctor
/home/v0id/.hermes/hermes-agent/venv/bin/python -m virgil_operational_sources run
/home/v0id/.hermes/hermes-agent/venv/bin/python -m virgil_operational_sources reconcile
/home/v0id/.hermes/hermes-agent/venv/bin/python -m virgil_operational_sources reconcile --source calendar
/home/v0id/.hermes/hermes-agent/venv/bin/python -m virgil_operational_sources pause calendar
/home/v0id/.hermes/hermes-agent/venv/bin/python -m virgil_operational_sources resume calendar
/home/v0id/.hermes/hermes-agent/venv/bin/python -m virgil_operational_sources disable calendar
/home/v0id/.hermes/hermes-agent/venv/bin/python -m virgil_operational_sources enable calendar
```

`status` reads durable source state. `doctor` checks the Attention DB, GitHub
authentication, Tailscale CLI, Calendar credential file, and Cogitator bridge
configuration. `run` executes only due enabled/unpaused adapters. `reconcile`
forces the same bounded reads; repeat `--source` to restrict it. Controls change
only durable worker control state. The worker uses a non-overlapping private
lock, retries each adapter twice, isolates failures, and never lets one failed
adapter stop the rest.

Cadence is two minutes for Gmail health and system health, and five minutes for
Calendar, Cogitator, GitHub, and ecommerce. The existing Gmail ingestion worker
continues independently every 60 seconds.

## Initial reconciliation bounds

The first run is idempotent and uses the same limits as every forced reconcile:

- Gmail: existing Attention records and protected health only; no mailbox replay.
- Calendar: now through seven days.
- Cogitator: the current bounded operational snapshot only.
- GitHub: current relevant open PRs, local checkout risk, and merges from the
  last seven days.
- Ecommerce: current relevant GitHub PRs and, only when present, active/current
  jobs plus open gates and job updates from the last seven days.
- System: current degradation only.

Missing or expired source records are resolved in Attention; source systems are
untouched. No activity older than seven days is imported.

## Install the user units

These commands are deployment instructions; adding this document does not run
them or mutate the current runtime.

```bash
install -d -m 0700 /home/v0id/.config/systemd/user
install -m 0644 packaging/virgil-mobile/virgil-operational-sources.service /home/v0id/.config/systemd/user/
install -m 0644 packaging/virgil-mobile/virgil-operational-sources.timer /home/v0id/.config/systemd/user/
systemd-analyze verify /home/v0id/.config/systemd/user/virgil-operational-sources.service /home/v0id/.config/systemd/user/virgil-operational-sources.timer
systemctl --user daemon-reload
systemctl --user start virgil-operational-sources.service
systemctl --user enable --now virgil-operational-sources.timer
systemctl --user status virgil-operational-sources.timer --no-pager
```

The oneshot service reads the existing optional `/home/v0id/.hermes/.env`, has a
read-only home/system view, and may write only under
`/home/v0id/.hermes/attention`. `OnBootSec=45s` makes the timer eligible 45
seconds after boot; it then runs every two minutes with up to 15 seconds
randomized delay and is persistent across downtime.

# Virgil Mobile V0

Virgil Mobile is Cal's private, mobile-first operational Attention Queue. It
does not replace Telegram or any source system: Telegram captures and
interrupts, while Virgil keeps each situation durable and organised until it
is resolved.

## Proactive-attention policy

Every source outcome maps to exactly one policy result:

| Result | Queue status | Telegram |
| --- | --- | --- |
| Interrupt now | `needs_cal` or `safety_hold`, urgent/high | Immediate |
| Prepare and queue | `prepared` | Immediate only when high priority |
| Ask for a decision | `needs_cal` | Immediate when urgent/high |
| Monitor quietly | `monitoring` | None |
| Ignore | No visible item; aggregate metric only | None |

Source adapters cannot select arbitrary actions. They submit a closed,
validated record to `hermes_attention.upsert_attention`; Hermes applies the
interruption rule. An unchanged event never creates a second item or alert.
An update edits the prior Telegram message when one exists. V0 queue actions
change only Attention state.

## Data boundary

The queue may store sender display name, credible company name, email subject,
received time, closed category, confidence, reason code, opaque Gmail message
and thread IDs, and allowlisted deep links.

It rejects raw bodies, snippets, signatures, quoted threads, draft bodies,
attachments, phone numbers, email addresses, payment-card-shaped values,
secret-shaped values, HTML, commands, unallowlisted URLs, unknown keys and
unknown enum values. Source records remain authoritative.

Unresolved items remain while active. Resolved, dismissed and stale items are
pruned after 30 days; sanitized activity is pruned after 90 days. The Gmail
adapter applies retention on each source upsert. Operators can also run:

```bash
venv/bin/python hermes_attention.py prune
venv/bin/python hermes_attention.py backup
venv/bin/python hermes_attention.py delete ITEM_ID --confirm DELETE-ITEM_ID
```

The database is `${HERMES_HOME}/attention/attention.db`. The directory is
`0700`, database/WAL files are `0600`, migrations are additive, foreign keys
and a bounded busy timeout are enabled, writes use explicit immediate
transactions, activity rows are append-only, and UI changes require the
current `row_version`.

Before rollback, stop the service and create a consistent backup with the
command above. To restore, keep the failed database for diagnosis, copy the
selected backup to `attention.db` while the service and Gmail timer are
stopped, set mode `0600`, then restart. Never copy a live WAL database with
`cp`.

## Gmail V0 adapter

The existing Linxio history watermark, first-run baseline, shadow state,
account binding, failure pauses, approval gate, timer and no-send guarantee
remain authoritative.

- Shadow would-draft: one normal `prepared` item labelled
  `SHADOW ONLY — NO GMAIL DRAFT CREATED`.
- Decision-required: one high `needs_cal` item.
- Processing or cross-customer safety failure: one high/urgent `safety_hold`.
- Ignored: worker metric only.
- New external message: update/reopen the same thread item.
- Later Cal Sent message: resolve the same thread item.

The adapter reads Inbox and Sent `messageAdded` history events from the
existing checkpoint. It never marks read, archives, sends, labels, or otherwise
mutates Gmail.

## Private deployment

The application binds to `127.0.0.1:8788` and accepts Tailscale identity
headers only from loopback. State-changing requests require the exact
same-origin `Origin`, a process-scoped CSRF token, JSON content type and a
bounded body. One configured Tailscale login is accepted. Responses use a
strict CSP, frame denial, no-store API policy and per-minute read/write limits.
No cookies, CDN, analytics, fonts or third-party assets are used.

Set the two non-secret values in `~/.hermes/config.yaml`:

```yaml
attention:
  authorized_tailscale_user: "CAL_TAILSCALE_LOGIN"
  public_url: "https://TAILNET_HOST:8443"
```

Install and start the user service:

```bash
install -d -m 0700 ~/.hermes/attention ~/.config/systemd/user
install -m 0600 packaging/virgil-mobile/virgil-mobile.service \
  ~/.config/systemd/user/virgil-mobile.service
systemctl --user daemon-reload
systemctl --user enable --now virgil-mobile.service
curl --fail http://127.0.0.1:8788/healthz
```

Expose only the private Tailscale listener:

```bash
tailscale serve --https=8443 --bg http://127.0.0.1:8788
```

Do not use `tailscale funnel` for Virgil Mobile. On Cal's current host, port
443 already belongs to an independent Cogitator Railway bridge. Leave that
existing route unchanged; Virgil uses private port 8443 so deployment and
rollback do not affect it.

Rollback is reversible:

```bash
tailscale serve --https=8443 off
systemctl --user disable --now virgil-mobile.service
```

## PWA cache boundary

The service worker caches only `/`, CSS, JavaScript, the manifest and local
icons. It does not intercept `/api/` or `/item/`, and therefore never caches
Attention JSON, customer metadata, auth material or source deep links. Offline
shell startup displays: `Virgil is offline. Live operational items are
unavailable.`

## Upstream comparison

Read-only comparison was made against official
`NousResearch/hermes-agent` main at
`48b21acb90375e28082b944eb96bbd1a3759c02f` and release
`v2026.7.20` (Quicksilver).

- Already in Cal's fork: FastAPI/uvicorn dashboard stack, session storage,
  token-auth helpers, PTY dashboard, health APIs and desktop/TUI surfaces.
- Safely reusable: existing installed FastAPI/uvicorn runtime and local icon.
- Selective-port candidates, not needed in V0:
  `apps/desktop/src/store/goals.ts` (ephemeral desktop goal display),
  `web/src/lib/pty-mobile-input.ts` (mobile terminal input), and
  `hermes_cli/dashboard_auth/token_auth.py` (service bearer tokens).
- Incompatible with V0: proxying the full dashboard would grant broad Hermes
  management access and create a second chat/dashboard surface.
- Irrelevant: upstream desktop chat lifecycle and terminal-oriented mobile
  input. Upstream has no web manifest or service worker to reuse.

No upstream commit or file was ported, and no upstream merge/rebase was
performed.

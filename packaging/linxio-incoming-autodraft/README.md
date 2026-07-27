# Linxio incoming Gmail autodraft V1

Issue: #91. This package installs one isolated user-level one-shot worker and a
60-second timer. It never starts another gateway or Telegram poller.

## Architecture decision

Gmail history polling is the smallest sound trigger already supported by the
restricted OAuth token. First run calls `users.getProfile("me")`, verifies the
exact Linxio account, stores the returned current history watermark, and
processes nothing. Later runs request only `messageAdded` history filtered to
`INBOX`. A missing/expired history checkpoint stops without replay.

`collect_history_events()` is the trigger seam. A later `users.watch`/Pub/Sub
adapter can emit the same opaque message/history records into the unchanged
eligibility, thread, policy, state, drafting, and notification path.

The worker reuses:

- Gmail `readonly` + `compose`; there is no send operation;
- Hermes' auxiliary model client with `tools=None`;
- approved/promoted Cogitator fact retrieval and read-only promoted writing
  guidance;
- Hermes' existing one-shot Telegram sender;
- stdlib SQLite, MIME, file locks, and systemd.

No Pub/Sub, new cloud resource, service account, model tool, core command,
dependency, OAuth scope, second gateway, or second Telegram bot is added.

## Safety state

The policy starts disabled. Shadow mode can assess new post-baseline mail but
cannot write Gmail. Draft mode requires a current one-time preview approval
bound to:

- `caleb.bacon@linxio.com`;
- Inbox-only, external-human-only, reply-draft-only authority;
- never-send/no read-state/archive/delete/label/CC/BCC actions;
- the closed safe/blocked categories and 85% confidence threshold;
- 5 drafts/hour and 20 drafts/day;
- policy and processing versions.

Material policy changes alter the fingerprint and invalidate approval. The
SQLite database stores only opaque Gmail/history/draft IDs, closed state and
reason values, confidence buckets, hashes, counts, and timestamps. It never
stores bodies, subjects, addresses, names, customer facts, proposed replies,
tokens, attachments, or cookies.

## Install disabled

```bash
UNIT_DIR="$HOME/.config/systemd/user"
install -d -m 0700 "$UNIT_DIR"
install -m 0600 packaging/linxio-incoming-autodraft/linxio-incoming-autodraft.service "$UNIT_DIR/"
install -m 0600 packaging/linxio-incoming-autodraft/linxio-incoming-autodraft.timer "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now linxio-incoming-autodraft.timer
```

The timer runs while mode remains `disabled`; its first invocation only
verifies the account and establishes the no-history baseline.

Define the operator command:

```bash
AUTODRAFT="/home/v0id/.hermes/hermes-agent/venv/bin/python /home/v0id/.hermes/hermes-agent/skills/productivity/google-workspace/scripts/incoming_autodraft.py"
```

## Operate

```bash
$AUTODRAFT status
$AUTODRAFT doctor
$AUTODRAFT mode shadow
$AUTODRAFT mode pause
$AUTODRAFT mode resume
$AUTODRAFT mode disabled
$AUTODRAFT explain ignored
$AUTODRAFT explain decision
```

`mode shadow` starts a fresh bounded observation: at most 24 hours or 10 new
external-human candidates, whichever occurs first. It records only sanitized
outcomes and aggregate latency, cannot create drafts, and returns to disabled
mode at the bound. `status.shadow_test` reports the live counters. A
history-gap, stuck queue, account/auth fault, or cross-customer risk instead
creates a safety hold without advancing the checkpoint.

Draft mode is a separate two-step Cal gate:

```bash
$AUTODRAFT policy-preview
$AUTODRAFT policy-approve --token ONE_TIME_TOKEN --approver Cal
$AUTODRAFT mode draft
```

`mode disabled` is the immediate kill switch. `mode pause` remembers the prior
shadow/draft mode; `mode resume` restores it only when any required draft
policy approval remains current.
While ordinarily disabled or paused, each timer tick advances directly to the
current profile watermark without listing messages. Resuming therefore never
drains an old-email queue. Safety and history-gap holds are the exception: they
preserve the existing checkpoint until explicit operator intervention.

A wrong Gmail account latches mode to disabled even after the approved account
is restored. After investigating the substitution, explicitly verify and clear
the hold; mode remains disabled until a separate shadow/draft choice:

```bash
$AUTODRAFT account-reverify --confirm REVERIFY-CAL-LINXIO-GMAIL
```

An invalid Gmail history checkpoint never rebases itself. After operator
investigation, explicitly discard the gap without replaying it:

```bash
$AUTODRAFT baseline-reset --confirm RESET-TO-CURRENT-GMAIL-HISTORY
```

There is intentionally no historical replay command in V1.

## Monitoring

`status` is sanitized and reports mode, account/policy health, timer state,
poll/watermark time, pending age/count, daily funnel counts, closed ignore
reasons, duplicate suppression/prevention, stale drafts, Cogitator/Telegram
success times, auth health, failures, and bounded shadow counts with average and
maximum processing latency.

`doctor` performs read-only profile, configuration, permission, policy, and
timer checks. Routine healthy runs do not notify. High-signal account, OAuth,
Cogitator, history-gap, repeated-processing, polling-delay, daily-limit,
Telegram, and state-integrity faults notify through the existing Telegram
delivery path without message bodies or proposed replies.

## Rollback

```bash
systemctl --user disable --now linxio-incoming-autodraft.timer
rm "$HOME/.config/systemd/user/linxio-incoming-autodraft.service"
rm "$HOME/.config/systemd/user/linxio-incoming-autodraft.timer"
systemctl --user daemon-reload
systemctl --user reset-failed
```

Rollback leaves the private operational database in place for audit/recovery.
Deleting it would discard the checkpoint and requires a separate deliberate
operator decision.

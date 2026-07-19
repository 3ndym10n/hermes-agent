#!/usr/bin/env bash
# Root-owned one-shot launch helper for the Restricted Purchase Executor.
# Delivers a SHORT-LIVED execution ticket to the production unit via a root-only
# tmpfs credential file (never argv/env/logs/persistent disk), starts exactly one
# run, then shreds the token. The ticket is single-use governance-side; a failed
# launch leaves no usable token behind.
#
# Usage (ticket on stdin, never argv):
#   printf '%s' "$TICKET" | sudo /path/to/launch.sh
set -euo pipefail
UNIT=hermes-purchase-executor
RUNDIR=/run/${UNIT}
TOKEN_FILE=${RUNDIR}/ticket_token

[ "$(id -u)" -eq 0 ] || { echo "launch.sh must run as root" >&2; exit 1; }

cleanup() { [ -e "$TOKEN_FILE" ] && shred -u "$TOKEN_FILE" 2>/dev/null || rm -f "$TOKEN_FILE" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

ticket="$(cat)"
[ -n "$ticket" ] || { echo "empty ticket on stdin; refusing" >&2; exit 2; }

install -o root -g root -m 700 -d "$RUNDIR"
( umask 077; printf '%s' "$ticket" > "$TOKEN_FILE" )
unset ticket

echo "launching one purchase run..."
rc=0
systemctl start "$UNIT" || rc=$?
cleanup  # token consumed; remove immediately (also done by trap)
systemctl --no-pager --lines=0 status "$UNIT" >/dev/null 2>&1 || true
echo "executor unit finished (systemctl start rc=$rc); inspect: journalctl -u ${UNIT} -n 50"
exit "$rc"

#!/usr/bin/env bash
# Idempotent rollback. Disables + removes units, executor user, dirs, and config.
# Synthetic staging credentials are removed; real card credentials are left
# untouched (root-only) unless --purge-creds is given.
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
systemctl disable --now hermes-purchase-executor.service 2>/dev/null || true
systemctl disable --now hermes-purchase-executor-staging.service 2>/dev/null || true
rm -f /etc/systemd/system/hermes-purchase-executor.service /etc/systemd/system/hermes-purchase-executor-staging.service
systemctl daemon-reload
rm -f /etc/credstore.encrypted/staging-card_*.cred
rm -rf /run/hermes-purchase-executor
if [ "${1:-}" = "--purge-creds" ]; then rm -f /etc/credstore.encrypted/card_*.cred /etc/credstore.encrypted/cogitator_bridge_token.cred; fi
rm -rf /etc/hermes-purchase-executor /var/lib/hermes-purchase-executor
id -u hermes-purchase-executor >/dev/null 2>&1 && userdel hermes-purchase-executor 2>/dev/null || true
echo "uninstalled. Real card credentials $([ "${1:-}" = "--purge-creds" ] && echo removed || echo left in place, root-only)."

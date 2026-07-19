#!/usr/bin/env bash
# Read-only health check. No sudo required for most checks; never decrypts or
# prints credential contents. Exits non-zero on any failure (fail-loud).
set -uo pipefail
UNIT=/etc/systemd/system/hermes-purchase-executor.service
STAGING=/etc/systemd/system/hermes-purchase-executor-staging.service
ETC=/etc/hermes-purchase-executor
fail=0
ok(){ echo "PASS: $1"; }
bad(){ echo "FAIL: $1"; fail=1; }

[ -f "$UNIT" ] && ok "production unit installed" || bad "production unit missing"
[ -f "$STAGING" ] && ok "staging unit installed" || bad "staging unit missing"
id -u hermes-purchase-executor >/dev/null 2>&1 && ok "executor user exists" || bad "executor user missing"

if systemctl is-enabled hermes-purchase-executor >/dev/null 2>&1; then bad "production unit is ENABLED (must be disabled)"; else ok "production unit disabled"; fi
if systemctl is-active hermes-purchase-executor >/dev/null 2>&1; then bad "production unit is ACTIVE (must be inactive)"; else ok "production unit inactive"; fi

if [ -f "$ETC/config.yaml" ]; then
  grep -q "sandbox_bypass: never" "$ETC/config.yaml" && ok "config sandbox_bypass=never" || bad "config sandbox_bypass not never"
  grep -q "cloud_provider: local" "$ETC/config.yaml" && ok "config cloud_provider=local" || bad "config cloud not local"
  grep -q "record_sessions: false" "$ETC/config.yaml" && ok "config record_sessions=false" || bad "config recording not false"
else bad "config.yaml missing"; fi

if [ -f "$ETC/executor.env" ]; then
  if grep -iqE "card|cvv|pan|token|secret|password" "$ETC/executor.env"; then bad "executor.env may contain a secret"; else ok "executor.env is non-secret"; fi
else bad "executor.env missing"; fi

# Credential presence by NAME only (never decrypt). Production card creds are
# optional here (real staging is a separate Cal step).
for n in staging-card_number staging-card_expiry staging-card_cvv staging-card_name; do
  [ -f "/etc/credstore.encrypted/$n.cred" ] && ok "synthetic cred present: $n" || echo "INFO: synthetic cred not staged yet: $n"
done

# v0id must NOT be able to read the credstore.
if sudo -n true 2>/dev/null; then :; fi
if [ -r /etc/credstore.encrypted ] && su -s /bin/sh v0id -c 'cat /etc/credstore.encrypted/*.cred' >/dev/null 2>&1; then
  bad "v0id can read credstore (must be root-only)"
else ok "credstore not readable by v0id"; fi

if command -v systemd-analyze >/dev/null; then
  echo "--- systemd-analyze security (production) ---"; systemd-analyze security hermes-purchase-executor 2>/dev/null | tail -3 || true
fi

[ "$fail" -eq 0 ] && echo "DOCTOR: PASS" || echo "DOCTOR: FAIL"
exit "$fail"

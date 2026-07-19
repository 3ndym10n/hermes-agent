#!/usr/bin/env bash
# ============================================================================
# CONSOLIDATED CAL SUDO GATE — inert install + synthetic systemd staging test.
#
# This is the ONE script Cal runs with sudo. It is idempotent and re-runnable.
# It does NOT: decrypt or stage real card credentials, enable the production
# executor or operator bridge, enable any Railway flag, start the production
# unit, or make any purchase. It installs the inert package, stages SYNTHETIC
# (public 4242 test) credentials, and runs ONE synthetic loopback staging pass
# under the real systemd hardening.
#
#   sudo ./cal-gate.sh install   https://worker-production-42f3.up.railway.app
#   sudo ./cal-gate.sh stage-run
#   sudo ./cal-gate.sh rollback
# ============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ETC=/etc/hermes-purchase-executor
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

case "${1:-}" in
  install)
    BRIDGE_URL="${2:-}"
    [ -n "$BRIDGE_URL" ] || { echo "usage: cal-gate.sh install <COGITATOR_BRIDGE_URL>" >&2; exit 2; }
    "$HERE/install.sh"
    printf 'COGITATOR_BRIDGE_URL=%s\n' "$BRIDGE_URL" > "$ETC/executor.env"
    chmod 644 "$ETC/executor.env"
    "$HERE/stage-synthetic-credentials.sh"
    echo "--- doctor (must show inert + hardened) ---"
    "$HERE/doctor.sh" || { echo "doctor reported problems; not proceeding to stage-run" >&2; exit 1; }
    echo
    echo "INSTALL COMPLETE (inert). Production unit is DISABLED, no real creds staged,"
    echo "no flags enabled. Next: sudo ./cal-gate.sh stage-run"
    ;;
  stage-run)
    echo "--- running ONE synthetic loopback staging pass under systemd hardening ---"
    systemctl start hermes-purchase-executor-staging.service || true
    echo "--- staging journal (tail) ---"
    journalctl -u hermes-purchase-executor-staging -n 40 --no-pager || true
    if journalctl -u hermes-purchase-executor-staging -n 200 --no-pager 2>/dev/null | grep -q '"fake_e2e": "PASS"'; then
      echo "STAGING: PASS"
    else
      echo "STAGING: did not observe PASS — inspect the journal above." >&2
    fi
    echo "--- production unit still inert? ---"
    # Classify by STATE VALUE, not exit code: a static unit's is-enabled exits 0
    # while printing "static" (inert). Only bootable states are a problem.
    prod_state="$(systemctl is-enabled hermes-purchase-executor 2>/dev/null || true)"
    case "$prod_state" in
      enabled|enabled-runtime|alias|linked|linked-runtime) echo "WARN prod BOOTABLE ($prod_state)";;
      *) echo "prod unit not bootable (${prod_state:-unknown}) (good)";;
    esac
    if systemctl is-active --quiet hermes-purchase-executor; then echo "WARN prod ACTIVE"; else echo "prod unit inactive (good)"; fi
    ;;
  rollback)
    "$HERE/uninstall.sh" "${2:-}"
    ;;
  *)
    echo "usage: cal-gate.sh {install <bridge-url>|stage-run|rollback [--purge-creds]}" >&2
    exit 2 ;;
esac

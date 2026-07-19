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
    UNIT=hermes-purchase-executor-staging.service
    echo "--- running ONE synthetic loopback staging pass under systemd hardening ---"
    systemctl start "$UNIT" || true
    # Authoritative outcome from THIS exact invocation, not a truncated tail.
    INVOC="$(systemctl show "$UNIT" -p InvocationID --value)"
    RESULT="$(systemctl show "$UNIT" -p Result --value)"
    MAINSTATUS="$(systemctl show "$UNIT" -p ExecMainStatus --value)"
    echo "--- staging journal (this invocation, complete) ---"
    journalctl "_SYSTEMD_INVOCATION_ID=$INVOC" --no-pager 2>/dev/null | tail -60 || true
    marker=no
    if [ -n "$INVOC" ] && journalctl "_SYSTEMD_INVOCATION_ID=$INVOC" --no-pager 2>/dev/null | grep -q '"fake_e2e": "PASS"'; then
      marker=yes
    fi
    echo "--- staging result: Result=$RESULT ExecMainStatus=$MAINSTATUS fake_e2e_marker=$marker ---"
    if [ "$RESULT" = "success" ] && [ "$MAINSTATUS" = "0" ] && [ "$marker" = "yes" ]; then
      echo "STAGING: PASS"
    else
      echo "STAGING: FAIL (need Result=success, ExecMainStatus=0, and the fake_e2e PASS marker in this invocation)" >&2
      staging_failed=1
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
    exit "${staging_failed:-0}"
    ;;
  rollback)
    "$HERE/uninstall.sh" "${2:-}"
    ;;
  *)
    echo "usage: cal-gate.sh {install <bridge-url>|stage-run|rollback [--purge-creds]}" >&2
    exit 2 ;;
esac

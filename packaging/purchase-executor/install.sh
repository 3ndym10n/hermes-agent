#!/usr/bin/env bash
# Idempotent installer for the Restricted Purchase Executor (Hermes issue #65).
# Installs INERT unit/config/scripts only. Does NOT stage real credentials, does
# NOT enable or start anything, does NOT enable any production flag.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "install.sh must run as root" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
CHECKOUT=/home/v0id/.hermes/hermes-agent
USER_NAME=hermes-purchase-executor
ETC=/etc/hermes-purchase-executor

id -u "$USER_NAME" >/dev/null 2>&1 || \
  useradd --system --no-create-home --shell /usr/sbin/nologin "$USER_NAME"

install -o root -g root -m 755 -d "$ETC"
install -o root -g root -m 644 "$HERE/config.yaml" "$ETC/config.yaml"
[ -f "$ETC/executor.env" ] || install -o root -g root -m 644 "$HERE/executor.env.example" "$ETC/executor.env"

install -o "$USER_NAME" -g "$USER_NAME" -m 700 -d /var/lib/hermes-purchase-executor

# The service reaches its runtime NOT via ACLs on the human home (fragile, and
# the acl package may be absent) but via systemd BindReadOnlyPaths in the units.
# That only works if the bind sources exist and are world-readable, since the
# files keep their real ownership inside the mount. Verify that here and FAIL the
# install loudly if not — no silent 2>/dev/null.
PY_REAL="$(readlink -f "$CHECKOUT/venv/bin/python")"
verify_world_readable() {
  local path="$1"
  [ -e "$path" ] || { echo "MISSING required runtime path: $path" >&2; exit 1; }
  # 'others' must be able to read (dirs: read+traverse) — that is what lets the
  # dedicated service user read it through the read-only bind mount.
  if [ -d "$path" ]; then
    [ "$(stat -c '%A' "$path" | cut -c8-9)" = "r-" ] || \
      { echo "NOT world-readable (needed for bind mount): $path" >&2; exit 1; }
  else
    [ -r "$path" ] && [ "$(stat -c '%A' "$path" | cut -c8)" = "r" ] || \
      { echo "NOT world-readable (needed for bind mount): $path" >&2; exit 1; }
  fi
}
verify_world_readable "$CHECKOUT"
verify_world_readable "$CHECKOUT/purchase_executor.py"
verify_world_readable "$PY_REAL"
[ -x "$PY_REAL" ] || { echo "interpreter not executable: $PY_REAL" >&2; exit 1; }
verify_world_readable /home/v0id/.cache/ms-playwright
echo "verified: service runtime is reachable via bind mounts (world-readable sources)."

install -o root -g root -m 644 "$HERE/hermes-purchase-executor.service" /etc/systemd/system/
install -o root -g root -m 644 "$HERE/hermes-purchase-executor-staging.service" /etc/systemd/system/
systemctl daemon-reload
# Both units are STATIC (no [Install] section) — inherently inert: they cannot be
# enabled to run at boot and only run when explicitly started. `systemctl disable`
# is a no-op error on static units, so we don't call it. Belt-and-braces: ensure
# neither is currently active.
systemctl stop hermes-purchase-executor.service 2>/dev/null || true
systemctl stop hermes-purchase-executor-staging.service 2>/dev/null || true

echo "installed inert. Units are static (not bootable) and inactive. No credentials staged. Next:"
echo "  1. edit $ETC/executor.env (COGITATOR_BRIDGE_URL)"
echo "  2. stage-synthetic-credentials.sh   (staging only)"
echo "  3. doctor.sh                         (verify)"
echo "  4. real card creds are a SEPARATE explicit step, not done here."

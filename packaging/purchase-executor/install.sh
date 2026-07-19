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

# Read/execute ACLs for the unprivileged user on exactly what it needs.
if command -v setfacl >/dev/null; then
  setfacl -R -m u:"$USER_NAME":rX "$CHECKOUT" "$CHECKOUT/venv" 2>/dev/null || true
  [ -d /home/v0id/.cache/ms-playwright ] && \
    setfacl -R -m u:"$USER_NAME":rX /home/v0id/.cache/ms-playwright 2>/dev/null || true
  # Traverse into the home path (execute bit only, not read).
  setfacl -m u:"$USER_NAME":--x /home/v0id /home/v0id/.hermes /home/v0id/.cache 2>/dev/null || true
fi

install -o root -g root -m 644 "$HERE/hermes-purchase-executor.service" /etc/systemd/system/
install -o root -g root -m 644 "$HERE/hermes-purchase-executor-staging.service" /etc/systemd/system/
systemctl daemon-reload
# Explicitly keep both units disabled + stopped.
systemctl disable hermes-purchase-executor.service 2>/dev/null || true
systemctl disable hermes-purchase-executor-staging.service 2>/dev/null || true

echo "installed inert. Units are disabled. No credentials staged. Next:"
echo "  1. edit $ETC/executor.env (COGITATOR_BRIDGE_URL)"
echo "  2. stage-synthetic-credentials.sh   (staging only)"
echo "  3. doctor.sh                         (verify)"
echo "  4. real card creds are a SEPARATE explicit step, not done here."

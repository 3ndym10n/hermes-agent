#!/usr/bin/env bash
# Remove the user unit while preserving all commerce state and browser profiles.
set -euo pipefail

readonly UNIT_DIR=/home/v0id/.config/systemd/user
readonly UNIT=virgil-commerce.service

[ "$(id -un)" = "v0id" ] || {
  echo "uninstall.sh must run as v0id" >&2
  exit 1
}

systemctl --user disable --now "$UNIT" 2>/dev/null || true
rm -f -- "$UNIT_DIR/$UNIT"
systemctl --user daemon-reload

echo "Uninstalled $UNIT; commerce state and browser profiles were preserved."

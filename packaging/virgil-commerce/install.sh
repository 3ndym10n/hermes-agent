#!/usr/bin/env bash
# Install the user unit and private runtime directories without starting it.
set -euo pipefail

readonly HERMES_HOME=/home/v0id/.hermes
readonly UNIT_DIR=/home/v0id/.config/systemd/user
readonly UNIT=virgil-commerce.service
readonly HERE="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -un)" = "v0id" ] || {
  echo "install.sh must run as v0id" >&2
  exit 1
}

install -d -m 0700 \
  "$HERMES_HOME/commerce" \
  "$HERMES_HOME/commerce/ab" \
  "$HERMES_HOME/commerce/evidence" \
  "$HERMES_HOME/commerce/receipts" \
  "$HERMES_HOME/browser-profiles" \
  "$HERMES_HOME/browser-profiles/commerce" \
  "$UNIT_DIR"
install -m 0600 "$HERE/$UNIT" "$UNIT_DIR/$UNIT"
systemctl --user daemon-reload

echo "Installed $UNIT inert; enable and start it only as an explicit deployment step."

#!/usr/bin/env bash
# Stage SYNTHETIC (public 4242 test) payment credentials for the STAGING unit.
# Never real card data. Idempotent. Uses the same field NAMES as production
# (prefixed staging-) to exercise systemd credential name-binding.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
DST=/etc/credstore.encrypted
install -o root -g root -m 700 -d "$DST"
stage() { printf '%s' "$2" | systemd-creds encrypt --name="$1" - "$DST/$1.cred"; chmod 600 "$DST/$1.cred"; }
stage staging-card_number 4242424242424242
stage staging-card_expiry 12/29
stage staging-card_cvv 123
stage staging-card_name "Staging Synthetic"
echo "staged synthetic staging-card_* credentials (public test PAN only)."

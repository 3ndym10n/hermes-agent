#!/usr/bin/env bash
# Stage SYNTHETIC (public 4242 test) payment credentials for the STAGING unit.
# Never real card data. Idempotent.
#
# systemd binds the credential NAME (from --name) into the encrypted blob and,
# at load time, requires it to equal the credential ID requested in the unit's
# LoadCredentialEncrypted=ID:PATH. The staging unit requests the SAME ids as
# production (card_number, ...), so we encrypt with --name=<id> — the exact
# production name (that IS the name-binding we're proving) — while writing to a
# distinct staging-<id>.cred FILE so the real production blobs are never touched.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
DST=/etc/credstore.encrypted
install -o root -g root -m 700 -d "$DST"
# stage <credential-id> <value>: name = id, file = staging-<id>.cred
stage() { printf '%s' "$2" | systemd-creds encrypt --name="$1" - "$DST/staging-$1.cred"; chmod 600 "$DST/staging-$1.cred"; }
stage card_number 4242424242424242
stage card_expiry 12/29
stage card_cvv 123
stage card_name "Staging Synthetic"
echo "staged synthetic staging-*.cred credentials bound to production names (public test PAN only)."

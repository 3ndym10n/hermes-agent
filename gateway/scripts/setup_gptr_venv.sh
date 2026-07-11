#!/usr/bin/env bash
# Create the isolated, pinned GPT Researcher venv for POST /research_gather.
#
# NEVER installed into the gateway's main venv or Railway (Cogitator #1012
# Phase 1 doctrine). The lock is the exact freeze of the venv that passed the
# Phase 0 B3 proof-of-fit (gpt-researcher==0.15.1, Python 3.11).
#
# Usage: bash gateway/scripts/setup_gptr_venv.sh
set -euo pipefail

VENV="${HERMES_GPTR_VENV:-$HOME/.hermes/gptr-venv}"
LOCK="$(cd "$(dirname "$0")/.." && pwd)/gptr-requirements.lock"

command -v uv >/dev/null || { echo "uv is required (https://docs.astral.sh/uv/)"; exit 1; }
[ -f "$LOCK" ] || { echo "lock file missing: $LOCK"; exit 1; }

uv venv "$VENV" --python 3.11
uv pip install --python "$VENV/bin/python" -r "$LOCK"

"$VENV/bin/python" -c "import gpt_researcher" \
  && echo "gptr venv ready: $VENV ($(du -sh "$VENV" | cut -f1), $(wc -l < "$LOCK") pinned packages)"

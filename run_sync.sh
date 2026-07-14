#!/bin/bash
# Runs sync.py once per calendar day. Safe to invoke multiple times (e.g. on
# login/wake as a catch-up in addition to the scheduled time) — it no-ops if
# today's sync already ran.
set -euo pipefail

cd "$(dirname "$0")"

MARKER_FILE=".last_sync_date"
TODAY="$(date +%Y-%m-%d)"

if [[ -f "$MARKER_FILE" ]] && [[ "$(cat "$MARKER_FILE")" == "$TODAY" ]]; then
  exit 0
fi

./.venv/bin/python sync.py
echo "$TODAY" > "$MARKER_FILE"

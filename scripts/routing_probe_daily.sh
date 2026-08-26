#!/bin/bash
# Daily routing probe: ask <gateway>/auto the same questions and record who answers.
#
# Built for launchd, which gives a job almost no environment: no shell profile,
# no PATH beyond the basics, no cwd. Everything here is therefore absolute and
# explicit, and the script fails loudly rather than half-running.
#
# Install: see scripts/routing_probe_daily.plist
#
# Two deliberate choices:
#
# 1. The trace store lives OUTSIDE the repo, under ~/Library/Application Support.
#    The repo sits in an iCloud-synced folder, and sync has already corrupted an
#    editable install here (see tests/test_environment_hygiene.py). A SQLite file
#    being written while a sync daemon copies it is a corrupted database, and
#    this one accumulates evidence over weeks -- losing it means starting the
#    clock again.
#
# 2. The API key is never copied, printed, or passed on a command line (where it
#    would sit in the process table). It is read from a file into an exported
#    variable and nothing else.

set -euo pipefail

REPO="${TRACEGUARD_REPO:-$HOME/Desktop/APP/traceguard}"
PKG="$REPO/packages/traceguard"
KEY_FILE="${ORCAROUTER_API_KEY_FILE:-$HOME/Desktop/APP/Keys/orcarouter_API_KEY.txt}"

DATA_DIR="${TRACEGUARD_DATA_DIR:-$HOME/Library/Application Support/traceguard}"
DB_PATH="$DATA_DIR/routing_probe.db"
LOG="$DATA_DIR/routing_probe.log"

GATEWAY="${TRACEGUARD_GATEWAY:-orcarouter}"
REPEATS="${TRACEGUARD_REPEATS:-2}"
MAX_USD="${TRACEGUARD_MAX_USD:-0.75}"

mkdir -p "$DATA_DIR"

log() { printf '%s  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$LOG"; }

fail() { log "ABORT: $*"; exit 1; }

[ -d "$PKG" ] || fail "package dir not found: $PKG"
[ -x "$PKG/.venv/bin/python" ] || fail "venv python not found; run: cd $PKG && uv sync --extra openai"
[ -f "$KEY_FILE" ] || fail "key file not found: $KEY_FILE"

# Pull only the key itself out of the file, whatever else it contains (labels,
# blank lines, sync conflict debris). A file with no sk- token is a
# configuration error, not something to send to the API and get a 401 for.
KEY="$(grep -o 'sk-[A-Za-z0-9_-]*' "$KEY_FILE" | head -1 || true)"
[ -n "$KEY" ] && [ ${#KEY} -ge 20 ] || fail "no plausible sk- key found in $KEY_FILE"

log "run start: gateway=$GATEWAY repeats=$REPEATS db=$DB_PATH"

# PYTHONPATH=src, and .venv/bin/python directly rather than `uv run`: this
# repo's editable install does not put src on sys.path (verified 2026-08-26 --
# both `.venv/bin/python` and `uv run python` reported it missing), so the
# console scripts and `python -m` both fail without this. Least indirection
# wins in a job nobody is watching.
cd "$PKG"
set +e
PYTHONPATH="src" ORCAROUTER_API_KEY="$KEY" OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}" \
    "$PKG/.venv/bin/python" -m traceguard.routing_integrity.conformance \
    --gateway "$GATEWAY" \
    --db "sqlite:///$DB_PATH" \
    --repeats "$REPEATS" \
    --max-usd "$MAX_USD" >>"$LOG" 2>&1
status=$?
set -e

if [ $status -ne 0 ]; then
    log "run FAILED with exit $status"
    exit $status
fi

log "run ok; auditing"
PYTHONPATH="src" "$PKG/.venv/bin/python" -m traceguard.routing_integrity \
    --db "sqlite:///$DB_PATH" --all >>"$LOG" 2>&1 || true

log "done"

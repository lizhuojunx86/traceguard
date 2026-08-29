#!/bin/bash
# Daily routing probe: ask each gateway's /auto alias the same questions and
# record who answers.
#
# Built for launchd, which gives a job almost no environment: no shell profile,
# no PATH beyond the basics, no cwd. Everything here is therefore absolute and
# explicit, and the script fails loudly rather than half-running.
#
# Install: see scripts/routing_probe_daily.plist
#
# Three deliberate choices:
#
# 1. The trace store lives OUTSIDE the repo, under ~/Library/Application Support.
#    The repo sits in an iCloud-synced folder, and sync has already corrupted an
#    editable install here (see tests/test_environment_hygiene.py). A SQLite file
#    being written while a sync daemon copies it is a corrupted database, and
#    this one accumulates evidence over weeks -- losing it means starting the
#    clock again.
#
# 2. The API keys are never copied, printed, or passed on a command line (where
#    they would sit in the process table). Each is read from its file into an
#    exported variable and nothing else.
#
# 3. One gateway failing does not stop the others. A day where OpenRouter's key
#    expired should still sample OrcaRouter: the comparison needs both series
#    aligned in time, and a gap is far more expensive than a logged error.

set -uo pipefail

REPO="${TRACEGUARD_REPO:-$HOME/dev/traceguard}"
PKG="$REPO/packages/traceguard"
KEY_DIR="${TRACEGUARD_KEY_DIR:-$HOME/Desktop/APP/Keys}"

DATA_DIR="${TRACEGUARD_DATA_DIR:-$HOME/Library/Application Support/traceguard}"
DB_PATH="$DATA_DIR/routing_probe.db"
LOG="$DATA_DIR/routing_probe.log"

REPEATS="${TRACEGUARD_REPEATS:-2}"
MAX_USD="${TRACEGUARD_MAX_USD:-0.75}"

# gateway:key-filename. Add a gateway by adding a line, provided it is also a
# preset in traceguard.gateways.
GATEWAYS=(
    "orcarouter:orcarouter_API_KEY.txt"
    "openrouter:OpenRouter_API_Key.txt"
)

mkdir -p "$DATA_DIR"

log() { printf '%s  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$LOG"; }

[ -d "$PKG" ] || { log "ABORT: package dir not found: $PKG"; exit 1; }
[ -x "$PKG/.venv/bin/python" ] || {
    log "ABORT: venv python not found; run: cd $PKG && uv sync --extra openai"
    exit 1
}

cd "$PKG" || exit 1

# The probe needs the openai SDK, which sits behind an optional extra. A plain
# `uv sync` in this package uninstalls it (verified 2026-08-29: `Uninstalled 11
# packages`, openai among them), so a routine dependency sync silently disarms
# this job -- every gateway then dies with exit 2 before sending a single call,
# and two days of comparison data went missing before anyone looked. `uv run`
# does not prune, which is why the nightly ingest never noticed. Self-heal with
# --inexact (leaves the rest of the venv untouched), then fail loudly.
if ! "$PKG/.venv/bin/python" -c 'import openai' >/dev/null 2>&1; then
    log "openai SDK missing -- self-healing with: uv sync --extra openai --inexact"
    UV="${TRACEGUARD_UV:-$HOME/.local/bin/uv}"
    if [ -x "$UV" ]; then
        env -u VIRTUAL_ENV "$UV" sync --extra openai --inexact >>"$LOG" 2>&1
    else
        log "uv not found at $UV; cannot self-heal"
    fi
    "$PKG/.venv/bin/python" -c 'import openai' >/dev/null 2>&1 || {
        log "ABORT: openai SDK not importable; run: cd $PKG && uv sync --extra openai"
        exit 1
    }
    log "self-heal ok: openai importable again"
fi

failures=0
attempted=0
succeeded=0

for entry in "${GATEWAYS[@]}"; do
    gateway="${entry%%:*}"
    key_file="$KEY_DIR/${entry#*:}"

    if [ ! -f "$key_file" ]; then
        log "SKIP $gateway: key file not found: $key_file"
        failures=$((failures + 1))
        continue
    fi

    # Pull only the key itself out of the file, whatever else it contains
    # (labels, blank lines, sync conflict debris). A file with no sk- token is
    # a configuration error, not something to send and collect a 401 for.
    key="$(grep -o 'sk-[A-Za-z0-9_-]*' "$key_file" | head -1)"
    if [ -z "$key" ] || [ ${#key} -lt 20 ]; then
        log "SKIP $gateway: no plausible sk- key in $key_file"
        failures=$((failures + 1))
        continue
    fi

    attempted=$((attempted + 1))
    log "run start: gateway=$gateway repeats=$REPEATS"

    # PYTHONPATH=src, and .venv/bin/python directly rather than `uv run`: this
    # repo's editable install does not put src on sys.path (verified
    # 2026-08-26 -- both .venv/bin/python and `uv run python` reported it
    # missing), so console scripts and `python -m` both fail without it. Least
    # indirection wins in a job nobody is watching.
    PYTHONPATH="src" \
    ORCAROUTER_API_KEY="$key" OPENROUTER_API_KEY="$key" \
        "$PKG/.venv/bin/python" -m traceguard.routing_integrity.conformance \
        --gateway "$gateway" \
        --db "sqlite:///$DB_PATH" \
        --repeats "$REPEATS" \
        --max-usd "$MAX_USD" >>"$LOG" 2>&1
    status=$?

    if [ $status -ne 0 ]; then
        log "run FAILED for $gateway with exit $status"
        failures=$((failures + 1))
    else
        log "run ok for $gateway"
        succeeded=$((succeeded + 1))
    fi
done

if [ $attempted -gt 0 ]; then
    log "auditing the store"
    PYTHONPATH="src" "$PKG/.venv/bin/python" -m traceguard.routing_integrity.timeline \
        --db "sqlite:///$DB_PATH" >>"$LOG" 2>&1
fi

log "done: $attempted gateway(s) attempted, $succeeded ok, $failures problem(s)"
[ $failures -eq 0 ] || exit 1

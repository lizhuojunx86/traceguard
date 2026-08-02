#!/usr/bin/env bash
# End-to-end check: do tokscale's historical totals survive a Claude Code
# resume/compact rewrite, or are they recomputed from live files?
#
#   ./run_check.sh [--version <npm-version>] [--bin <path>] [--work <dir>]
#                  [--drop <n>] [--keep]
#
# Installs the published tokscale npm package (pinned version) — or, with
# --bin, uses a binary you built yourself, which is how an unreleased branch
# or main gets verified. Generates a synthetic Claude Code corpus with a
# known-exact manifest under an isolated fake $HOME, runs tokscale three
# times (cold / warm / after an in-place rewrite that removes the last N
# assistant messages from one transcript), and compares totals.
#
# Exit codes: 0 = history frozen (no drift), 1 = drift confirmed, 2 = unexpected.
#
# Requires: node >= 18 + npm on PATH, python3. Network for npm install and
# tokscale's LiteLLM pricing fetch. Writes only under the work directory;
# touches no real ~/.claude data.

set -euo pipefail

VERSION="4.7.0"   # npm dist-tag "latest" at 2026-07-31
BIN=""
WORK=""
DROP=6
KEEP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --bin) BIN="$2"; shift 2 ;;
    --work) WORK="$2"; shift 2 ;;
    --drop) DROP="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$WORK" ]]; then
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/tokscale-drift-check.XXXXXX")"
  trap '[[ $KEEP -eq 1 ]] || rm -rf "$WORK"' EXIT
fi
mkdir -p "$WORK"
HOME_DIR="$WORK/fake-home"

echo "==> work dir: $WORK"

if [[ -n "$BIN" ]]; then
  [[ -x "$BIN" ]] || { echo "--bin is not executable: $BIN" >&2; exit 2; }
  TOKSCALE="$(cd "$(dirname "$BIN")" && pwd)/$(basename "$BIN")"
  echo "==> using binary: $TOKSCALE ($("$TOKSCALE" --version 2>/dev/null || echo 'version unknown'))"
else
  if [[ ! -x "$WORK/node_modules/.bin/tokscale" ]]; then
    echo "==> installing tokscale@$VERSION"
    (cd "$WORK" && npm init -y >/dev/null 2>&1 \
      && npm install --no-save --silent "tokscale@$VERSION" >/dev/null)
  fi
  TOKSCALE="$WORK/node_modules/.bin/tokscale"
fi

echo "==> generating corpus"
rm -rf "$HOME_DIR"
python3 "$HERE/gen_corpus.py" --home "$HOME_DIR"

run_tokscale() {  # $1 = output json path
  HOME="$HOME_DIR" XDG_CONFIG_HOME= XDG_CACHE_HOME= \
    "$TOKSCALE" models --json > "$1" 2>/dev/null
}

cd "$WORK"
cp "$HOME_DIR/manifest.json" manifest.json

echo "==> run 1: cold start on the intact corpus"
run_tokscale run1.json
echo "==> run 2: warm re-run, corpus unchanged"
run_tokscale run2.json

echo "==> simulating resume/compact rewrite (drop last $DROP messages)"
python3 "$HERE/simulate_rewrite.py" "$HOME_DIR/.claude/projects" \
  --drop "$DROP" --manifest drift-manifest.json

echo "==> run 3: after the rewrite"
run_tokscale run3.json

echo
set +e
python3 "$HERE/compare_totals.py"
rc=$?
set -e
[[ $KEEP -eq 1 ]] && echo "==> kept: $WORK"
exit $rc

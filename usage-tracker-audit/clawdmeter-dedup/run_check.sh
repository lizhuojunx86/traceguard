#!/usr/bin/env bash
# End-to-end check: does Clawdmeter count each Claude Code assistant message
# once, or once per content-block record?
#
#   ./run_check.sh [--commit <sha>] [--work <dir>] [--keep]
#
# Clones Clawdmeter (pinned commit by default), generates a synthetic corpus
# with a known-exact manifest, and runs Clawdmeter's own transcript functions
# over it. No PySide6 install needed — the checker stubs QtCore, and the token
# paths are stdlib-only.
#
# Requires: git, python3. Network access for the clone.
# Writes only under the work directory. Touches no real ~/.claude data.

set -euo pipefail

REPO_URL="https://github.com/weltern/Clawdmeter.git"
COMMIT="7dd0b7b00b9937b426830ec30e3e142a45fa32d0"   # main HEAD 2026-08-03 (v3.0.0)
WORK=""
KEEP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --commit) COMMIT="$2"; shift 2 ;;
    --work) WORK="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEN="$HERE/../cct-dedup-check/gen_corpus.py"
if [[ ! -f "$GEN" ]]; then
  echo "missing corpus generator: $GEN" >&2
  exit 2
fi

if [[ -z "$WORK" ]]; then
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/clawdmeter-dedup-check.XXXXXX")"
  trap '[[ $KEEP -eq 1 ]] || rm -rf "$WORK"' EXIT
fi
mkdir -p "$WORK"

REPO="$WORK/Clawdmeter"
HOME_DIR="$WORK/fake-home"

echo "==> work dir: $WORK"

if [[ ! -d "$REPO/.git" ]]; then
  echo "==> cloning Clawdmeter at ${COMMIT:0:12}"
  git init -q "$REPO"
  git -C "$REPO" remote add origin "$REPO_URL"
  git -C "$REPO" fetch -q --depth 1 origin "$COMMIT"
  git -C "$REPO" checkout -q FETCH_HEAD
fi

echo "==> generating corpus"
rm -rf "$HOME_DIR"
python3 "$GEN" --home "$HOME_DIR" --seed 20260805

echo "==> running Clawdmeter's own token paths"
set +e
python3 "$HERE/check_clawdmeter.py" --clawdmeter "$REPO" --home "$HOME_DIR"
rc=$?
set -e

[[ $KEEP -eq 1 ]] && echo "==> kept: $WORK"
exit $rc

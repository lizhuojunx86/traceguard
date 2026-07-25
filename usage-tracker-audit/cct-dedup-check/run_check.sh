#!/usr/bin/env bash
# End-to-end check: does claude-code-templates' analytics count each assistant
# message once, or once per content-block line?
#
#   ./run_check.sh [--commit <sha>] [--work <dir>] [--keep]
#
# Clones the upstream repo (pinned commit by default), installs only the two
# runtime deps ConversationAnalyzer requires, generates a synthetic corpus with
# a known-exact manifest, and runs the analyzer over it.
#
# Requires: git, node >= 18, npm, python3. Network access for the clone + npm.
# Writes only under the work directory. Touches no real ~/.claude data.

set -euo pipefail

REPO_URL="https://github.com/davila7/claude-code-templates.git"
COMMIT="e54d3cdad4896d904442becec570028ba7e878f1"   # 2026-07-24
WORK=""
KEEP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --commit) COMMIT="$2"; shift 2 ;;
    --work) WORK="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$WORK" ]]; then
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/cct-dedup-check.XXXXXX")"
  trap '[[ $KEEP -eq 1 ]] || rm -rf "$WORK"' EXIT
fi
mkdir -p "$WORK"

REPO="$WORK/claude-code-templates"
HOME_DIR="$WORK/fake-home"

echo "==> work dir: $WORK"

if [[ ! -d "$REPO/.git" ]]; then
  echo "==> cloning upstream at ${COMMIT:0:12}"
  git init -q "$REPO"
  git -C "$REPO" remote add origin "$REPO_URL"
  git -C "$REPO" fetch -q --depth 1 origin "$COMMIT"
  git -C "$REPO" checkout -q FETCH_HEAD
fi

# ConversationAnalyzer.js requires only chalk, fs-extra and node's path.
if [[ ! -d "$REPO/cli-tool/node_modules/fs-extra" ]]; then
  echo "==> installing chalk + fs-extra"
  (cd "$REPO/cli-tool" && npm install --silent --no-audit --no-fund --no-save chalk@4 fs-extra >/dev/null)
fi

echo "==> generating corpus"
rm -rf "$HOME_DIR"
python3 "$HERE/gen_corpus.py" --home "$HOME_DIR"

echo "==> running upstream analyzer"
set +e
node "$HERE/run_check.js" "$REPO" "$HOME_DIR" "$HOME_DIR/manifest.json"
rc=$?
set -e

if [[ $rc -eq 0 ]]; then
  echo "==> RESULT: totals match the manifest."
else
  echo "==> RESULT: totals do not match the manifest (exit $rc)."
fi
[[ $KEEP -eq 1 ]] && echo "==> kept: $WORK"
exit $rc

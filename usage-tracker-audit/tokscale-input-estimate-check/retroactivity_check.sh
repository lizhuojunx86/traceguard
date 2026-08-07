#!/usr/bin/env bash
# Does the #1037 parser fix reach numbers that were already cached?
#
#   ./retroactivity_check.sh <pre-fix-binary> <post-fix-binary> <workdir>
#
# The parser fix stopped char-estimating tool_result input tokens, but
# parser_version(Claude) was deliberately not bumped — bumping it discards the
# RetainObserved turns that #994 exists to keep. So a cached entry written by a
# pre-fix build is never re-parsed on upgrade.
#
# Three legs against one HOME, so the cache carries between them:
#
#   1. pre-fix binary  — builds the cache, inflated
#   2. post-fix binary — same cache: does upgrading change anything?
#   3. post-fix binary — after transcripts change on disk: does it self-heal,
#                        and does anything other than `input` move?
#
# Leg 3 appends a blank line, which changes the file without adding records.
# messageCount must not move; if it does, healing is retiring turns.
#
# Requires: a real ~/.claude/projects copy in <workdir>/home. Uses an isolated
# HOME so no real data is read.

set -euo pipefail

PRE="${1:?usage: retroactivity_check.sh <pre-fix-bin> <post-fix-bin> <workdir>}"
POST="${2:?usage: retroactivity_check.sh <pre-fix-bin> <post-fix-bin> <workdir>}"
WORK="${3:?usage: retroactivity_check.sh <pre-fix-bin> <post-fix-bin> <workdir>}"

PRE="$(cd "$(dirname "$PRE")" && pwd)/$(basename "$PRE")"
POST="$(cd "$(dirname "$POST")" && pwd)/$(basename "$POST")"
WORK="$(cd "$WORK" && pwd)"
HOME_DIR="$WORK/home"
PROJECTS="$HOME_DIR/.claude/projects"

[[ -d "$PROJECTS" ]] || { echo "no corpus at $PROJECTS" >&2; exit 2; }

measure() {  # $1 = binary, $2 = label
  local out
  out="$(HOME="$HOME_DIR" XDG_CONFIG_HOME= XDG_CACHE_HOME= "$1" models --json 2>/dev/null)"
  printf '%s' "$out" | python3 -c "
import json, sys
entries = json.load(sys.stdin)['entries']
agg = {}
for row in entries:
    for key, value in row.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            agg[key] = agg.get(key, 0) + value
print('%-28s input=%-12s output=%-12s msgs=%-8s cost=%.2f' % (
    '$2',
    format(agg['input'], ','),
    format(agg['output'], ','),
    format(agg['messageCount'], ','),
    agg['cost'],
))
"
}

touch_newest() {  # $1 = how many, most recently modified first
  python3 - "$PROJECTS" "$1" <<'PY'
import os, sys
root, n = sys.argv[1], int(sys.argv[2])
files = []
for d, _, names in os.walk(root):
    for name in names:
        if name.endswith('.jsonl'):
            p = os.path.join(d, name)
            try:
                files.append((os.path.getmtime(p), p))
            except OSError:
                pass
files.sort(reverse=True)
for _, p in files[:n]:
    with open(p, 'a') as fh:
        fh.write('\n')
print(f'  (touched {min(n, len(files))} of {len(files)} transcripts)')
PY
}

echo "==> leg 1: pre-fix build populates the cache"
"$PRE" --version
measure "$PRE" "pre-fix, cold cache"

echo
echo "==> leg 2: post-fix build, same cache, nothing else changed"
"$POST" --version
measure "$POST" "post-fix, inherited cache"
echo "    (identical to leg 1 means the fix is not retroactive)"

echo
echo "==> leg 3: transcripts change on disk, post-fix build re-reads them"
for n in 1 30 150 400; do
  touch_newest "$n"
  measure "$POST" "post-fix, $n newest touched"
done

echo
echo "Compare against a cold post-fix cache (run_ab.sh on a fresh HOME) for the"
echo "true value. Everything except input must match across every leg."

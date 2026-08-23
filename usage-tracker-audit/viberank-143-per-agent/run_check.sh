#!/bin/sh
# Verify viberank #143 against a real corpus: the per-agent split reconciles,
# and a Claude drift verdict no longer takes another tool's high-water mark
# with it.
#
#   ./run_check.sh [cc-byagent.json]
#
# With no argument it generates the report itself with the same command the
# published CLI runs (`ccusage daily --by-agent --json`, viberank-cli 1.10.0
# cli.js:57), which reads ~/.claude and the other tools' local histories.
#
# Needs git and node >= 22 (for TypeScript stripping). Clones viberank into
# .work/ and checks out the merge commit and its parent. Writes nothing
# outside this directory and submits nothing anywhere.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
WORK="$HERE/.work"
POST=15da384          # fix: apply a drift verdict to the tool it is evidence about
PRE=15da384^          # 7a6b8cf

REPORT=${1:-"$WORK/cc-byagent.json"}

mkdir -p "$WORK"
if [ ! -d "$WORK/repo/.git" ]; then
  git clone -q https://github.com/sculptdotfun/viberank.git "$WORK/repo"
fi
git -C "$WORK/repo" fetch -q origin
git -C "$WORK/repo" checkout -q "$POST"
[ -d "$WORK/pre" ] || git -C "$WORK/repo" worktree add -q "$WORK/pre" "$PRE"

if [ ! -f "$REPORT" ]; then
  echo "generating $REPORT ..."
  npx -y ccusage@latest daily --by-agent --json > "$REPORT"
fi

echo "=============================================================="
echo "1 . the split reconciles with the day it divides"
echo "=============================================================="
python3 "$HERE/reconcile.py" "$REPORT"

echo
echo "=============================================================="
echo "2 . merge A/B, their code, $PRE vs $POST"
echo "=============================================================="
printf '%-28s %14s %14s\n' "incoming non-claude slice" "pre" "post"
for keep in 1 0.7 0; do
  pre=$(node "$HERE/merge_ab.ts" "$WORK/pre/src/lib/ccusage.ts" "$REPORT" "$keep" 2>/dev/null |
    awk '/non-claude after merge/ {print $4}')
  post=$(node "$HERE/merge_ab.ts" "$WORK/repo/src/lib/ccusage.ts" "$REPORT" "$keep" 2>/dev/null |
    awk '/non-claude after merge/ {print $4}')
  case $keep in
    1) label="re-reported unchanged" ;;
    0) label="absent" ;;
    *) label="$keep of observed" ;;
  esac
  printf '%-28s %14s %14s\n' "$label" "$pre" "$post"
  echo "$post" > "$WORK/.post_$keep"
done

echo
observed=$(node "$HERE/merge_ab.ts" "$WORK/repo/src/lib/ccusage.ts" "$REPORT" 1 2>/dev/null |
  awk '/non-claude observed/ {print $3}')
fail=0
for keep in 1 0.7 0; do
  got=$(cat "$WORK/.post_$keep")
  [ "$got" = "$observed" ] || { echo "FAIL: post lost non-claude money at nc_keep=$keep ($got vs $observed)"; fail=1; }
done
[ "$fail" -eq 0 ] && echo "PASS: after #143 every non-claude slice survives a Claude verdict ($observed)"
exit "$fail"

#!/usr/bin/env bash
# Cross-check the compaction half of deepseek-harness#1886 across three trees.
# Nothing is reimplemented: apply() and view() come from each tree's own
# usage-projection.ts, transpiled by esbuild and otherwise untouched.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${WORK:-$HERE/.work}"
FIXTURE="${FIXTURE:-$HERE/../dsh-conformance/fixture/sessions}"

UP_REPO=https://github.com/deepseek-ai/deepseek-harness.git
UP_SHA=${UP_SHA:-b150a551b8d465e31e418e1b2eaf5e79bbb7d28e}      # dsh-v0.1.1-rc.2
YHA_REPO=https://github.com/yha9806/deepseek-harness.git
YHA_SHA=${YHA_SHA:-63688b0ef57d7911ea748820dc056892be04adae}     # compaction + retry
LXY_REPO=https://github.com/a137460387/deepseek-harness.git
LXY_SHA=${LXY_SHA:-64ee978ad1d0c868f3a8d65734dbc456e4273e94}     # compaction only

SRC=packages/llm/token-meter/src/usage-projection.ts
mkdir -p "$WORK/src" "$WORK/build"

fetch() { # name repo sha
  local d="$WORK/repo-$1"
  [ -d "$d" ] || git clone --quiet --filter=blob:none --no-checkout "$2" "$d"
  git -C "$d" fetch --quiet --filter=blob:none origin "$3" 2>/dev/null || true
  git -C "$d" show "$3:$SRC" > "$WORK/src/$1.ts"
}
fetch up  "$UP_REPO"  "$UP_SHA"
fetch yha "$YHA_REPO" "$YHA_SHA"
fetch lxy "$LXY_REPO" "$LXY_SHA"

# Ablation: strip yha's attempt-boundary branch, leaving only its compaction
# branch. If the two patches agree on compaction, this must equal lxy exactly.
python3 - "$WORK/src/yha.ts" "$WORK/src/yha_nb.ts" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
s = open(src).read()
branch = """    if (event.type === 'assistant/chunk' && event.data.chunk.type === 'finish'
      && (event.data.chunk.reason.kind === 'error' || event.data.chunk.reason.kind === 'aborted')) {
      return state.last?.turn === event.data.turn && state.last.step === event.data.step
        ? { ...state, last: null }
        : state
    }

"""
assert branch in s, "attempt-boundary branch not found verbatim -- ablation aborted"
open(dst, 'w').write(s.replace(branch, '', 1))
PY

[ -d "$HERE/node_modules" ] || (cd "$HERE" && npm install --silent --no-audit --no-fund)
for v in up lxy yha yha_nb; do
  cp "$HERE/surface-projection.ts" "$WORK/src/surface-projection.ts"
  "$HERE/node_modules/.bin/esbuild" "$WORK/src/$v.ts" --bundle --format=esm \
    --platform=node --external:zod --log-level=error --outfile="$WORK/build/$v.js"
done

run() { BUILD="$WORK/build" VARIANT="$1" node "$HERE/fold.js" "$2"; }

echo "conformance fixture -- $FIXTURE"
for v in up lxy yha; do printf '  %-8s %s\n' "$v" "$(run $v "$FIXTURE")"; done
echo
echo "ablation -- 63688b0 minus its attempt-boundary branch vs 64ee978"
A=$(run lxy "$FIXTURE"); B=$(run yha_nb "$FIXTURE")
printf '  %-8s %s\n  %-8s %s\n' 64ee978 "$A" yha-nb "$B"
[ "$A" = "$B" ] && echo "  IDENTICAL on the compaction dimension" \
                || { echo "  DIFFER"; exit 1; }
echo
for p in nousage midstep; do
  echo "probe/$p"
  for v in up lxy yha; do printf '  %-8s %s\n' "$v" "$(run $v "$HERE/probe/$p")"; done
done

#!/usr/bin/env bash
# A/B for tokscale PR #1162 (DSH compaction/summary fold).
#   pre  = 86126c2^ (7fc3634e, the main commit the branch left from)
#   post = 522027d  (PR #1162 head)
# One corpus, isolated HOME per leg (cold cache), DSH_HOME pinned at a frozen copy.
set -uo pipefail

OUT=/tmp/ab1162
CORPUS=/tmp/dsh-corpus-frozen

run_leg() {
  local name="$1" bin="$2"
  local home="$OUT/home-$name"
  rm -rf "$home"; mkdir -p "$home"
  env -i \
    HOME="$home" \
    PATH=/usr/bin:/bin:/usr/sbin:/sbin \
    DSH_HOME="$CORPUS" \
    TZ=Asia/Shanghai \
    "$bin" --json > "$OUT/$name.json" 2> "$OUT/$name.err"
  echo "[$name] exit=$? bytes=$(wc -c < "$OUT/$name.json")"
}

run_leg pre  "$OUT/tokscale-pre"
run_leg post "$OUT/tokscale-post"

echo "=== top-level keys ==="
/usr/bin/python3 - <<'PY'
import json
for leg in ("pre", "post"):
    try:
        d = json.load(open(f"/tmp/ab1162/{leg}.json"))
    except Exception as e:
        print(leg, "UNPARSEABLE", e)
        continue
    print(leg, type(d).__name__, list(d)[:20] if isinstance(d, dict) else len(d))
PY

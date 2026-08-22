#!/usr/bin/env bash
# The cache leg of the #1162 A/B: does the parser_version 1 -> 2 bump actually
# reach an existing cache written by the pre-fix binary?
#
#   leg 1  pre  on a fresh HOME      -> writes a v1 cache, undercounted
#   leg 2  post on the SAME HOME     -> must land on the cold post figure
#   leg 3  post on the same HOME     -> must be identical to leg 2 (idempotent)
set -uo pipefail

OUT=/tmp/ab1162
CORPUS=/tmp/dsh-corpus-frozen
HOME_W="$OUT/home-warm"

rm -rf "$HOME_W"; mkdir -p "$HOME_W"

leg() {
  local name="$1" bin="$2"
  env -i HOME="$HOME_W" PATH=/usr/bin:/bin DSH_HOME="$CORPUS" TZ=Asia/Shanghai \
    "$bin" --json > "$OUT/$name.json" 2>"$OUT/$name.err"
}

leg warm1_pre  "$OUT/tokscale-pre"
leg warm2_post "$OUT/tokscale-post"
leg warm3_post "$OUT/tokscale-post"

/usr/bin/python3 - <<'PY'
import json
rows = [
    ("cold pre      ", "pre"),
    ("cold post     ", "post"),
    ("warm1 pre     ", "warm1_pre"),
    ("warm2 post    ", "warm2_post"),
    ("warm3 post    ", "warm3_post"),
]
keys = ("totalInput", "totalOutput", "totalCacheRead", "totalCacheWrite", "totalMessages")
print(f"{'leg':16s}" + "".join(f"{k:>16s}" for k in keys) + f"{'total':>12s}")
for label, f in rows:
    d = json.load(open(f"/tmp/ab1162/{f}.json"))
    tot = sum(d[k] for k in keys[:4])
    print(f"{label:16s}" + "".join(f"{d[k]:>16,}" for k in keys) + f"{tot:>12,}")
PY

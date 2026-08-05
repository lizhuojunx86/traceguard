#!/usr/bin/env bash
# Run tokscale against the intact and emptied corpora and diff the totals.
#
#   ./run_ab.sh <tokscale-binary> <workdir>
#
# Each run gets its own isolated HOME so no real ~/.claude data is read and no
# cache carries between the two legs.

set -euo pipefail

BIN="${1:?usage: run_ab.sh <tokscale-binary> <workdir>}"
WORK="${2:?usage: run_ab.sh <tokscale-binary> <workdir>}"

BIN="$(cd "$(dirname "$BIN")" && pwd)/$(basename "$BIN")"
WORK="$(cd "$WORK" && pwd)"

echo "==> binary: $BIN"
"$BIN" --version || true

run_leg() {  # $1 = fake home, $2 = output json
  echo "==> running against $1"
  HOME="$1" XDG_CONFIG_HOME= XDG_CACHE_HOME= \
    "$BIN" models --json > "$2" 2>"$2.err" || {
      echo "tokscale failed; stderr:" >&2
      tail -20 "$2.err" >&2
      exit 2
    }
}

run_leg "$WORK/home-intact"  "$WORK/intact.json"
run_leg "$WORK/home-emptied" "$WORK/emptied.json"

python3 - "$WORK/intact.json" "$WORK/emptied.json" <<'PY'
import json, sys

FIELDS = ["input", "output", "cacheRead", "cacheWrite", "reasoning", "messageCount", "cost"]

def totals(path):
    # `tokscale models --json` emits {"groupBy": ..., "entries": [...]}; each
    # entry is one (client, model) row. Sum the scalar fields across rows and
    # skip the nested `performance` object.
    entries = json.load(open(path))["entries"]
    agg = {}
    for row in entries:
        for key, value in row.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                agg[key] = agg.get(key, 0) + value
    return agg

a, b = totals(sys.argv[1]), totals(sys.argv[2])
width = max(len(k) for k in FIELDS)

print()
print(f"{'field'.ljust(width)}  {'intact':>18}  {'emptied':>18}  {'delta':>18}  {'ratio':>8}")
print("-" * (width + 72))
for k in FIELDS:
    av, bv = a.get(k, 0), b.get(k, 0)
    delta = av - bv
    ratio = f"{av / bv:.2f}x" if bv else "-"
    fmt = (lambda v: f"{v:,.2f}") if k == "cost" else (lambda v: f"{v:,.0f}")
    print(f"{k.ljust(width)}  {fmt(av):>18}  {fmt(bv):>18}  {fmt(delta):>18}  {ratio:>8}")

inp_a, inp_b = a["input"], b["input"]
print()
print(f"estimate share of reported input : {(inp_a - inp_b) / inp_a:.1%}")
print(f"inflation factor                 : {inp_a / inp_b:.2f}x")
cost_delta = a["cost"] - b["cost"]
print(f"cost impact                      : ${cost_delta:,.2f} ({cost_delta / a['cost']:.2%})")
print()
print("Compare the `input` delta against the prediction printed by")
print("prepare_ab_corpus.py — they should agree to well under a percent.")
print()
PY

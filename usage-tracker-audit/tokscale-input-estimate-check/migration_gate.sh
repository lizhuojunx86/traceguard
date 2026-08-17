#!/usr/bin/env bash
# Regression gate for a fix that has to reach already-cached entries (#1011).
#
#   ./migration_gate.sh <pre-fix-binary> <fixed-binary> <corpus-dir>
#
# Written for a targeted, non-bumping cache migration. That migration turned out
# not to be needed — the retention-provenance rebuild (#1085) already re-parses
# every markerless Claude entry, and every entry that can still hold a char
# estimate is markerless. The gate is unchanged: point it at a build that is
# supposed to clear a stale cache, migration branch or release candidate.
#
# retroactivity_check.sh measures how much inflation a cache is still carrying.
# This is the pass/fail form of the same three legs, for pointing at a migration
# branch: it exits 0 only if the migration clears the cached inflation AND
# retires nothing while doing it.
#
# What it asserts
#
#   1. `input` on an inherited (pre-fix) cache, after the migration runs, equals
#      `input` from a cold parse by the same binary. That is the migration
#      working: today leg 2 is byte-identical to leg 1, which is what "the fix
#      is not retroactive" means.
#
#   2. output, cacheRead, cacheWrite and messageCount are identical across all
#      three legs. That is the property the migration must not break. A
#      parser_version bump would discard RetainObserved turns the live
#      transcript no longer contains, and it shows up here as messageCount
#      dropping rather than as a number nobody notices.
#
# Assertion 2 is the one worth having. A migration that fixes `input` and quietly
# retires assistant turns reintroduces the drift #994 fixed, and every field it
# damages moves in the direction that looks like an improvement.
#
#   <corpus-dir> must contain .claude/projects — a copy of a real tree. The
#   estimate only fires on tool_result blocks in real sessions, so no synthetic
#   corpus stands in for it (see README). Copies are made with `cp -al`, so a
#   large tree costs inodes rather than bytes, and nothing is written to it:
#   this script never appends to a transcript.
#
# Read-only with respect to the corpus and to $HOME. Both legs run under
# isolated HOMEs; your real ~/.claude is never opened.

set -euo pipefail

PRE="${1:?usage: migration_gate.sh <pre-fix-bin> <migrated-bin> <corpus-dir>}"
MIG="${2:?usage: migration_gate.sh <pre-fix-bin> <migrated-bin> <corpus-dir>}"
CORPUS="${3:?usage: migration_gate.sh <pre-fix-bin> <migrated-bin> <corpus-dir>}"

PRE="$(cd "$(dirname "$PRE")" && pwd)/$(basename "$PRE")"
MIG="$(cd "$(dirname "$MIG")" && pwd)/$(basename "$MIG")"
CORPUS="$(cd "$CORPUS" && pwd)"

[[ -d "$CORPUS/.claude/projects" ]] || {
  echo "no corpus at $CORPUS/.claude/projects" >&2
  exit 2
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

link_home() {  # $1 = destination HOME
  mkdir -p "$1/.claude"
  cp -al "$CORPUS/.claude/projects" "$1/.claude/projects"
}

measure() {  # $1 = binary, $2 = HOME — prints one JSON object
  HOME="$2" XDG_CONFIG_HOME= XDG_CACHE_HOME= "$1" models --json 2>/dev/null | python3 -c "
import json, sys
entries = json.load(sys.stdin)['entries']
agg = {}
for row in entries:
    for key, value in row.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            agg[key] = agg.get(key, 0) + value
print(json.dumps(agg))
"
}

INHERITED="$WORK/inherited"
COLD="$WORK/cold"
link_home "$INHERITED"
link_home "$COLD"

echo "==> leg 1: pre-fix build populates the cache"
"$PRE" --version
LEG1="$(measure "$PRE" "$INHERITED")"

echo "==> leg 2: migrated build, same cache"
"$MIG" --version
LEG2="$(measure "$MIG" "$INHERITED")"

echo "==> leg 3: migrated build, cold cache — the true value"
LEG3="$(measure "$MIG" "$COLD")"

python3 - "$LEG1" "$LEG2" "$LEG3" <<'PY'
import json, sys

leg1, leg2, leg3 = (json.loads(a) for a in sys.argv[1:4])
names = ("leg 1 pre-fix, cold", "leg 2 migrated, inherited", "leg 3 migrated, cold")

fields = ["input", "output", "cacheRead", "cacheWrite", "messageCount"]
fields = [f for f in fields if f in leg3]

print()
print("%-26s %s" % ("", "  ".join("%14s" % f for f in fields)))
for name, leg in zip(names, (leg1, leg2, leg3)):
    print("%-26s %s" % (name, "  ".join("%14s" % format(leg.get(f, 0), ",") for f in fields)))
print()

failures = []

if leg2["input"] != leg3["input"]:
    carried = leg2["input"] - leg3["input"]
    ratio = leg2["input"] / leg3["input"] if leg3["input"] else float("inf")
    failures.append(
        "input on the inherited cache is %s against a true %s — %s still carried, %.2fx. "
        "The migration did not reach entries written before it."
        % (format(leg2["input"], ","), format(leg3["input"], ","), format(carried, ","), ratio)
    )

for f in fields:
    if f == "input":
        continue
    values = {name: leg.get(f) for name, leg in zip(names, (leg1, leg2, leg3))}
    if len(set(values.values())) != 1:
        failures.append(
            "%s is not identical across the legs: %s. Only input may move; anything "
            "else means the migration retired records rather than recomputing them."
            % (f, ", ".join("%s=%s" % (n, format(v, ",")) for n, v in values.items()))
        )

cleared = leg1["input"] - leg2["input"]
if cleared:
    print("cleared from the inherited cache: %s tokens" % format(cleared, ","))

if failures:
    for line in failures:
        print("FAIL: %s" % line)
    sys.exit(1)

print("PASS: the migration reaches cached entries, and only input moved.")
PY

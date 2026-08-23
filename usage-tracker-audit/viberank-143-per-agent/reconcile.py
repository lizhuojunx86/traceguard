#!/usr/bin/env python3
"""Does a per-agent split reconcile with the day it claims to divide?

viberank's server keeps a `--by-agent` split only when the slices add up to
their row (`normalizeCcData`, src/lib/ccusage.ts). That gate is the reason a
malformed or hostile split cannot inflate one agent while the headline total
stays believable, so it is worth checking independently of their tests.

Also prints the mixed-day census, which is what decides how much money the
per-(machine, agent) keying in #143 is protecting on a given tree.

    python3 reconcile.py cc-byagent.json

Generate the input with the same command the CLI runs:

    npx ccusage@latest daily --by-agent --json > cc-byagent.json

Stdlib only. Reads nothing but the file you name.
"""
from __future__ import annotations

import json
import sys
from collections import Counter

FIELDS = [
    "inputTokens",
    "outputTokens",
    "cacheCreationTokens",
    "cacheReadTokens",
    "totalTokens",
]


def is_claude(agent: str) -> bool:
    return agent.startswith("claude")


def main(path: str) -> int:
    rows = json.load(open(path))["daily"]

    split_days = 0
    mismatches: list[tuple] = []
    worst_cost = 0.0
    census: Counter[str] = Counter()
    grand = mixed_cost = mixed_nonclaude = 0.0
    mixed_days = nonclaude_slices = 0

    for row in rows:
        grand += row.get("totalCost", 0.0)
        slices = row.get("agents") or []
        if not slices:
            census["no split"] += 1
            continue
        split_days += 1

        for field in FIELDS:
            got = sum(s.get(field, 0) for s in slices)
            if got != row.get(field, 0):
                mismatches.append((row["period"], field, got, row.get(field, 0)))
        worst_cost = max(
            worst_cost,
            abs(sum(s.get("totalCost", 0.0) for s in slices) - row.get("totalCost", 0.0)),
        )

        names = {s["agent"] for s in slices}
        if any(is_claude(n) for n in names) and len(names) > 1:
            census["claude + other"] += 1
            mixed_days += 1
            mixed_cost += row.get("totalCost", 0.0)
            for s in slices:
                if not is_claude(s["agent"]):
                    mixed_nonclaude += s.get("totalCost", 0.0)
                    nonclaude_slices += 1
        elif any(is_claude(n) for n in names):
            census["claude only"] += 1
        else:
            census["no claude"] += 1

    print(f"days in report        {len(rows)}")
    print(f"days carrying split   {split_days}")
    print(f"token mismatches      {len(mismatches)}")
    print(f"worst cost residual   {worst_cost:.10f}")
    print()
    for name, count in sorted(census.items()):
        print(f"{name:<16}      {count}")
    print()
    print(f"board cost            {grand:,.2f}")
    print(f"mixed-day cost        {mixed_cost:,.2f}")
    print(
        f"non-claude on mixed   {mixed_nonclaude:,.2f}"
        f"  ({100 * mixed_nonclaude / mixed_cost if mixed_cost else 0:.2f}% of mixed,"
        f" {100 * mixed_nonclaude / grand if grand else 0:.2f}% of board)"
    )
    print(f"non-claude slices     {nonclaude_slices} across {mixed_days} days")

    if mismatches:
        print()
        print("MISMATCHED, first five:")
        for period, field, got, want in mismatches[:5]:
            print(f"  {period} {field}: slices {got} vs row {want}")
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))

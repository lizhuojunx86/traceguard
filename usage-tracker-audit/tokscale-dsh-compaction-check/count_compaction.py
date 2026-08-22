#!/usr/bin/env python3
"""Count the corpus-side facts the A/B delta has to match.

The point: the difference between the two binaries should be exactly the
`compaction/summary` events' own usage and nothing else. This reads the corpus
directly, so the expected delta is computed without going through either
binary.

    python3 count_compaction.py [--root <DSH_HOME>]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os

BUCKETS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens",
           "reasoningTokens")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/.dsh"))
    args = ap.parse_args()

    types = collections.Counter()
    usage = collections.Counter()
    with_usage = 0
    without_usage = 0

    pattern = os.path.join(args.root, "sessions", "*", "*", "session.jsonl")
    files = sorted(glob.glob(pattern))
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                etype = event.get("type", "")
                types[etype] += 1
                if etype != "compaction/summary":
                    continue
                u = (event.get("data") or {}).get("usage")
                if not u:
                    without_usage += 1
                    continue
                with_usage += 1
                for key in BUCKETS:
                    value = u.get(key)
                    if isinstance(value, int):
                        usage[key] += value

    print(f"files                 {len(files)}")
    print(f"events                {sum(types.values())}")
    print(f"assistant/message     {types['assistant/message']}")
    print(f"assistant/chunk       {types['assistant/chunk']}")
    print(f"compaction/summary    {types['compaction/summary']}"
          f"  (with usage {with_usage}, without {without_usage})")
    print()
    print("expected A/B delta, from the corpus rather than either binary:")
    for key in BUCKETS:
        if usage[key]:
            print(f"  {key:20s} {usage[key]:>10,}")
    print(f"  {'TOTAL':20s} {sum(usage.values()):>10,}")
    print(f"  {'messageCount':20s} {with_usage:>10,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

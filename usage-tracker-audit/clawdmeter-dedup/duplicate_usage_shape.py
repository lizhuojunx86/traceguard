#!/usr/bin/env python3
"""What shape does a repeated `usage` block take across one message's records?

Claude Code writes one assistant message as several JSONL records, one per
content block, each repeating `message.id`. Clawdmeter #21 was about counting
those records instead of the messages. This measures the follow-up question the
maintainer raised there: whether the repeated `usage` objects are identical, or
whether some of them are partial.

They are not always identical, and the discriminator is not the Claude Code
version. It is `isSidechain`. Main-path groups repeat a finished `usage`;
sidechain (subagent) groups repeat a running total, so the last record carries
the real figure and keep-first loses the difference.

Grouping discipline: records are grouped per `(file, message.id)`, never by
`message.id` alone. A global grouping splices records from unrelated files into
one sequence and manufactures non-monotonic groups that do not exist. That
artifact is reported too, because it is easy to hit.

    ./duplicate_usage_shape.py [--root ~/.claude/projects]

Reads only. Needs no dependencies beyond the standard library.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
from pathlib import Path

USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def signature(usage: dict) -> tuple:
    return tuple(usage.get(key) for key in USAGE_KEYS)


def collect(root: Path):
    """(file, message.id) -> [(line_no, usage, is_sidechain)], in file order."""
    per_file = collections.defaultdict(list)
    by_id = collections.defaultdict(set)
    transcripts = 0

    for path in root.rglob("*.jsonl"):
        transcripts += 1
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line_no, line in enumerate(handle):
                if '"usage"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = record.get("message")
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                usage = message.get("usage")
                message_id = message.get("id")
                if not isinstance(usage, dict) or not message_id:
                    continue
                per_file[(str(path), message_id)].append(
                    (line_no, usage, bool(record.get("isSidechain")))
                )
                by_id[message_id].add(str(path))

    return per_file, by_id, transcripts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("~/.claude/projects"))
    args = ap.parse_args()
    root = args.root.expanduser()
    if not root.is_dir():
        print(f"not a directory: {root}")
        return 2

    per_file, by_id, transcripts = collect(root)
    records = sum(len(v) for v in per_file.values())
    ids = len(by_id)

    print(f"transcripts                     : {transcripts:,}")
    print(f"usage-bearing assistant records : {records:,}")
    print(f"distinct message.id             : {ids:,}")
    print(f"records per id                  : {records / ids:.3f}x")

    multi = {k: sorted(v) for k, v in per_file.items() if len(v) > 1}
    main_groups = [v for v in multi.values() if not any(s for _, _, s in v)]
    side_groups = [v for v in multi.values() if any(s for _, _, s in v)]

    def identical(groups):
        return sum(1 for g in groups if len({signature(u) for _, u, _ in g}) == 1)

    print()
    print("repeated usage, grouped per (file, message.id)")
    for label, groups in (("main path", main_groups), ("sidechain", side_groups)):
        if not groups:
            continue
        same = identical(groups)
        print(
            f"  {label:<10} {len(groups):>7,} groups   "
            f"byte-identical {same:>7,} ({100 * same / len(groups):5.2f}%)"
        )

    differing = [g for g in multi.values() if len({signature(u) for _, u, _ in g}) > 1]
    if differing:
        outs = [[(u.get("output_tokens") or 0) for _, u, _ in g] for g in differing]
        nondec = sum(1 for o in outs if all(b >= a for a, b in zip(o, o[1:])))
        last_max = sum(1 for o in outs if o[-1] == max(o))
        print()
        print(f"groups whose usage differs      : {len(differing):,}")
        print(f"  output_tokens non-decreasing  : {nondec:,} ({100 * nondec / len(differing):.2f}%)")
        print(f"  last record carries the max   : {last_max:,} ({100 * last_max / len(differing):.2f}%)")
        zeroed = sum(
            1
            for g in differing
            if any(all((u.get(k) or 0) == 0 for k in USAGE_KEYS) for _, u, _ in g)
        )
        print(f"  containing an all-zero copy   : {zeroed:,}")

    keep_first = sum((g[0][1].get("output_tokens") or 0) for g in per_file.values())
    per_bucket_max = sum(
        max((u.get("output_tokens") or 0) for _, u, _ in g) for g in per_file.values()
    )
    print()
    print(f"output tokens, per-bucket max   : {per_bucket_max:,}")
    print(f"output tokens, keep-first       : {keep_first:,}")
    print(
        f"  keep-first loses              : {per_bucket_max - keep_first:,} "
        f"({100 * (per_bucket_max - keep_first) / per_bucket_max:.1f}%)"
    )

    cross = {mid: paths for mid, paths in by_id.items() if len(paths) > 1}
    after_file_dedup = sum(len(paths) for paths in by_id.values())
    print()
    print(f"ids appearing in >1 transcript  : {len(cross):,} ({100 * len(cross) / ids:.2f}%)")
    print(f"residual after per-file dedup   : {after_file_dedup / ids:.4f}x")

    # The artifact: group by message.id alone and the same corpus grows
    # non-monotonic groups out of nothing.
    spliced = collections.defaultdict(list)
    for (path, mid), rows in per_file.items():
        for line_no, usage, _ in rows:
            spliced[mid].append((path, line_no, usage))
    false_nonmono = 0
    for mid, rows in spliced.items():
        if len(rows) < 2:
            continue
        rows.sort()
        o = [(u.get("output_tokens") or 0) for _, _, u in rows]
        if len(set(o)) > 1 and not all(b >= a for a, b in zip(o, o[1:])):
            false_nonmono += 1
    print()
    print(f"non-monotonic groups if you group by message.id alone: {false_nonmono:,}")
    print("(zero of them survive per-(file, id) grouping — a splicing artifact)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

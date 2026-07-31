#!/usr/bin/env python3
"""Measure the streaming-record shape of a Claude Code corpus.

The measurement posted in Maciek-roboblog/Claude-Code-Usage-Monitor#158:
group assistant lines by (message.id, requestId) within each file, compare
first vs last usage per group, split by writer path (main transcripts vs
subagents/**). Also intended for re-verification of a fixed reader branch:
its per-key output should equal this script's last-wins totals.

Usage:
    python3 measure_streaming_shape.py [--projects ~/.claude/projects]

Reads transcripts, prints aggregates only. Stdlib only.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from collections import defaultdict

FIELDS = ("input_tokens", "output_tokens",
          "cache_creation_input_tokens", "cache_read_input_tokens")


def ut(u: dict) -> tuple:
    return tuple(int(u.get(k) or 0) for k in FIELDS)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"))
    args = ap.parse_args()

    files = glob.glob(os.path.join(args.projects, "**", "*.jsonl"), recursive=True)
    buckets = {"main": defaultdict(list), "subagent": defaultdict(list)}
    lines_total = miss_mid = miss_rid = 0
    mid_rids: dict[str, set] = defaultdict(set)
    group_files: dict[tuple, set] = defaultdict(set)

    for f in files:
        b = "subagent" if os.sep + "subagents" + os.sep in f else "main"
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in fh:
            if '"assistant"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue
            lines_total += 1
            mid, rid = msg.get("id"), rec.get("requestId")
            if not mid:
                miss_mid += 1
            if not rid:
                miss_rid += 1
            if not mid or not rid:
                continue
            buckets[b][(mid, rid)].append(ut(u))
            mid_rids[mid].add(rid)
            group_files[(mid, rid)].add(f)

    print(f"files={len(files):,} lines_with_usage={lines_total:,} "
          f"missing_mid={miss_mid:,} missing_rid={miss_rid:,}")
    print(f"mids with >1 requestId: {sum(1 for s in mid_rids.values() if len(s) > 1):,}")
    print(f"(mid,rid) groups spanning files: "
          f"{sum(1 for s in group_files.values() if len(s) > 1):,}")

    for b in ("main", "subagent"):
        g = buckets[b]
        multi = {k: v for k, v in g.items() if len(v) > 1}
        ident = sum(1 for v in multi.values() if len(set(v)) == 1)
        fw = sum(v[0][1] for v in g.values())
        lw = sum(v[-1][1] for v in g.values())
        mx = sum(max(x[1] for x in v) for v in g.values())
        print(f"[{b}] groups={len(g):,} multi-line={len(multi):,} "
              f"identical={ident:,} ({100 * ident / max(len(multi), 1):.1f}%)")
        print(f"[{b}] output: first-wins={fw:,} last-wins={lw:,} max-wins={mx:,} "
              f"first-wins loss={100 * (lw - fw) / max(lw, 1):.2f}%")


if __name__ == "__main__":
    main()

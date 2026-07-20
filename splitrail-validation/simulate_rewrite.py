#!/usr/bin/env python3
"""Simulate a Claude Code resume/compact rewrite on a SNAPSHOT tree.

Picks the largest *main* transcript (``projects/<slug>/<sessionId>.jsonl``,
subagent files excluded), removes ALL lines belonging to the last N distinct
assistant ``message.id`` groups, and rewrites the file in place — exactly the
observable effect of a real resume/compact: messages vanish, mtime bumps.

Writes a manifest with the expected per-model deltas under BOTH streaming
conventions (a message is written as multiple partial-usage lines):

- ``last_line``  : usage of the last line per message id (max snapshot)
- ``sum_lines``  : sum across that message id's lines

Whichever convention splitrail's totals move by tells us empirically how it
accounts streaming partials.

Usage: simulate_rewrite.py SNAPSHOT_ROOT [--drop 5] [--manifest out.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path, help="snapshot projects root (will be modified!)")
    ap.add_argument("--drop", type=int, default=5)
    ap.add_argument("--manifest", type=Path, default=Path("drift-manifest.json"))
    args = ap.parse_args()

    mains = sorted(args.root.glob("*/*.jsonl"), key=lambda p: p.stat().st_size, reverse=True)
    if not mains:
        print("no main transcripts found under", args.root, file=sys.stderr)
        return 1
    target = mains[0]

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    order: list[str] = []          # distinct assistant message ids, file order
    by_id: dict[str, list[dict]] = {}
    for raw in lines:
        if '"assistant"' not in raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        mid = msg.get("id")
        if not mid or not isinstance(msg.get("usage"), dict):
            continue
        if mid not in by_id:
            order.append(mid)
            by_id[mid] = []
        by_id[mid].append(msg)

    if len(order) <= args.drop:
        print(f"target has only {len(order)} messages; need > {args.drop}", file=sys.stderr)
        return 1

    drop_ids = set(order[-args.drop:])

    def usage_tuple(u: dict) -> dict:
        return {
            "input_tokens": int(u.get("input_tokens") or 0),
            "output_tokens": int(u.get("output_tokens") or 0),
            "cache_read_input_tokens": int(u.get("cache_read_input_tokens") or 0),
            "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens") or 0),
        }

    expected: dict[str, dict] = {}
    for mid in drop_ids:
        msgs = by_id[mid]
        model = msgs[-1].get("model") or "unknown"
        slot = expected.setdefault(
            model,
            {"messages": 0, "lines": 0,
             "last_line": {k: 0 for k in ("input_tokens", "output_tokens",
                                          "cache_read_input_tokens",
                                          "cache_creation_input_tokens")},
             "sum_lines": {k: 0 for k in ("input_tokens", "output_tokens",
                                          "cache_read_input_tokens",
                                          "cache_creation_input_tokens")}},
        )
        slot["messages"] += 1
        slot["lines"] += len(msgs)
        last = usage_tuple(msgs[-1].get("usage") or {})
        for k, v in last.items():
            slot["last_line"][k] += v
        for m in msgs:
            for k, v in usage_tuple(m.get("usage") or {}).items():
                slot["sum_lines"][k] += v

    kept, removed_lines = [], 0
    for raw in lines:
        keep = True
        if '"assistant"' in raw:
            try:
                rec = json.loads(raw)
                if rec.get("type") == "assistant" and (rec.get("message") or {}).get("id") in drop_ids:
                    keep = False
            except json.JSONDecodeError:
                pass
        if keep:
            kept.append(raw)
        else:
            removed_lines += 1

    target.write_text("".join(kept), encoding="utf-8")

    manifest = {
        "target_file": str(target),
        "dropped_message_ids": sorted(drop_ids),
        "removed_lines": removed_lines,
        "expected_delta_by_model": expected,
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    total_msgs = sum(v["messages"] for v in expected.values())
    print(f"rewrote {target.name}: -{total_msgs} messages ({removed_lines} lines); "
          f"manifest -> {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

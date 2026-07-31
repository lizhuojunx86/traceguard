#!/usr/bin/env python3
"""Simulate a Claude Code resume/compact rewrite on the fake-home corpus.

Picks the largest *main* transcript (``projects/<slug>/<sessionId>.jsonl``,
subagent files excluded), removes ALL lines belonging to the last N distinct
assistant ``message.id`` groups, and rewrites the file in place — the
observable effect of a real resume/compact: messages vanish, mtime bumps.

Adapted from splitrail-validation/simulate_rewrite.py (same repo). The
synthetic corpus writes byte-identical usage on every line of a message, so
the ``last_line`` and per-message conventions coincide; the manifest still
records both so the checker can state its expectation precisely.

Usage: simulate_rewrite.py PROJECTS_ROOT [--drop 6] [--manifest out.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FIELDS = ("input_tokens", "output_tokens",
          "cache_read_input_tokens", "cache_creation_input_tokens")


def usage_tuple(u: dict) -> dict:
    return {k: int(u.get(k) or 0) for k in FIELDS}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path, help="fake-home .claude/projects root (will be modified!)")
    ap.add_argument("--drop", type=int, default=6)
    ap.add_argument("--manifest", type=Path, default=Path("drift-manifest.json"))
    args = ap.parse_args()

    mains = sorted(args.root.glob("*/*.jsonl"), key=lambda p: p.stat().st_size, reverse=True)
    if not mains:
        print("no main transcripts found under", args.root, file=sys.stderr)
        return 1
    target = mains[0]

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    order: list[str] = []
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

    expected = {"messages": 0, "lines": 0,
                "last_line": {k: 0 for k in FIELDS},
                "sum_lines": {k: 0 for k in FIELDS}}
    for mid in drop_ids:
        msgs = by_id[mid]
        expected["messages"] += 1
        expected["lines"] += len(msgs)
        for k, v in usage_tuple(msgs[-1].get("usage") or {}).items():
            expected["last_line"][k] += v
        for m in msgs:
            for k, v in usage_tuple(m.get("usage") or {}).items():
                expected["sum_lines"][k] += v

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
        "expected_delta": expected,
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"rewrote {target.name}: -{expected['messages']} messages ({removed_lines} lines); "
          f"manifest -> {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

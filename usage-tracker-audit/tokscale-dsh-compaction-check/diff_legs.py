#!/usr/bin/env python3
"""Diff the two legs of the #1162 A/B: every numeric leaf that moved."""
import json
import sys

OUT = "/tmp/ab1162"


def leaves(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from leaves(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from leaves(v, f"{path}[{i}]")
    else:
        yield path, node


def main():
    pre = json.load(open(f"{OUT}/pre.json"))
    post = json.load(open(f"{OUT}/post.json"))
    a = dict(leaves(pre))
    b = dict(leaves(post))

    moved, same_num, only = [], 0, []
    for k in sorted(set(a) | set(b)):
        va, vb = a.get(k, "<absent>"), b.get(k, "<absent>")
        if va == vb:
            if isinstance(va, (int, float)) and not isinstance(va, bool):
                same_num += 1
            continue
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            moved.append((k, va, vb, vb - va))
        else:
            only.append((k, va, vb))

    print(f"numeric leaves identical across the pair: {same_num}")
    print(f"\n=== moved ({len(moved)}) ===")
    for k, va, vb, d in moved:
        print(f"{k:60s} {va:>15} -> {vb:>15}  delta {d:+}")
    print(f"\n=== structural / non-numeric differences ({len(only)}) ===")
    for k, va, vb in only[:40]:
        print(f"{k:60s} {va!r} -> {vb!r}")


if __name__ == "__main__":
    sys.exit(main())

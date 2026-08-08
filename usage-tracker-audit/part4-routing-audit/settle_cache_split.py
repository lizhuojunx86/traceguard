#!/usr/bin/env python3
"""Settle the 1-hour cache premium: is it $88.75 or $1,393.49?

Two figures disagree by 15.7x about what reading the flat
``cache_creation_5m`` / ``cache_creation_1h`` keys is worth:

* **$1,393.49** — claimed in the ``pricing.py`` docstring.
* **$88.75** — what the ``rp-20260808T041211Z-e92e55`` recompute batch actually
  wrote across 3,918 rows.

The recompute runs over ``traces``, and that table stores only ``tokens_in``,
``tokens_out`` and ``cost_usd`` — not the raw ``usage`` block. So it cannot see
``cache_creation_1h`` at all, and $88.75 may be measuring something else
entirely. The transcripts are the only place the evidence lives.

METHOD. This does not reimplement the pricing rules. It calls
``compute_cost_usd`` twice per message: once with the record's usage as
written, and once with the flat cache keys stripped out. Stripping them
reproduces the pre-fix behaviour exactly, because the old code read only the
nested shape and fell back to "all cache creation is 5-minute TTL" when it was
absent. The difference between the two totals is the fix, priced, with no
second implementation to disagree with.

Deduplicates by ``message.id`` first. One assistant message is written as
several JSONL records carrying byte-identical usage, so summing per record
would inflate this the same way it inflates everything else.

Read-only. Touches no database and writes nothing.

    cd packages/traceguard && uv run python \\
        ../../usage-tracker-audit/part4-routing-audit/settle_cache_split.py
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "packages" / "traceguard" / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))  # uv run treats a script outside the project as standalone

from traceguard.routing_audit.pricing import (  # noqa: E402
    cache_creation_split,
    compute_cost_usd,
)

FLAT_KEYS = ("cache_creation_5m", "cache_creation_1h")


def parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def as_written_and_pre_fix(usage: dict) -> tuple[dict, dict]:
    """The record's usage, and the same usage as the old code would have seen.

    The old splitter read only ``cache_creation.ephemeral_*``. Dropping the
    flat keys makes the fixed splitter take the same fallback the old one took
    on every real record: the whole cache-creation total as 5-minute TTL.
    """
    pre_fix = {k: v for k, v in usage.items() if k not in FLAT_KEYS}
    return usage, pre_fix


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "source",
        nargs="?",
        type=Path,
        default=Path.home() / ".claude" / "projects",
        help="transcript root (default: ~/.claude/projects)",
    )
    args = ap.parse_args(argv)
    root = args.source.expanduser()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    seen: set[str] = set()
    files = records = messages = dupes = 0
    unpriced: dict[str, int] = defaultdict(int)
    tokens_1h = 0
    total_now = Decimal("0")
    total_pre = Decimal("0")
    by_model: dict[str, list] = defaultdict(lambda: [0, Decimal("0")])  # [1h tokens, delta]

    for path in sorted(root.rglob("*.jsonl")):
        files += 1
        try:
            fh = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                model = msg.get("model")
                if not isinstance(usage, dict) or not model:
                    continue
                records += 1
                key = msg.get("id") or rec.get("uuid")
                if key is not None:
                    if key in seen:
                        dupes += 1
                        continue
                    seen.add(key)
                messages += 1

                invoked_at = parse_ts(rec.get("timestamp"))
                now_usage, pre_usage = as_written_and_pre_fix(usage)

                cost_now = compute_cost_usd(model, now_usage, invoked_at)
                cost_pre = compute_cost_usd(model, pre_usage, invoked_at)
                if cost_now is None or cost_pre is None:
                    unpriced[model] += 1
                    continue

                _, one_hour = cache_creation_split(now_usage)
                tokens_1h += one_hour
                delta = Decimal(str(cost_now)) - Decimal(str(cost_pre))
                total_now += Decimal(str(cost_now))
                total_pre += Decimal(str(cost_pre))
                if one_hour or delta:
                    slot = by_model[model]
                    slot[0] += one_hour
                    slot[1] += delta

    delta = total_now - total_pre
    print(f"source                : {root}")
    print(f"files                 : {files:,}")
    print(f"records with usage    : {records:,}")
    print(f"distinct messages     : {messages:,}   (dropped {dupes:,} duplicate records)")
    print(f"1-hour cache tokens   : {tokens_1h:,}")
    print()
    print(f"total, as written     : ${total_now:,.2f}")
    print(f"total, pre-fix        : ${total_pre:,.2f}")
    print(f"the fix is worth      : ${delta:,.2f}")
    print()
    print("  claimed in pricing.py docstring : $1,393.49")
    print("  written by rp-...041211         : $88.75")
    print()
    if by_model:
        print("by model (1h tokens, delta):")
        for model, (tok, d) in sorted(by_model.items(), key=lambda kv: -kv[1][1]):
            print(f"  {model:<34} {tok:>14,}  ${d:>10,.2f}")
    if unpriced:
        print()
        print("unpriced models (excluded from both totals):")
        for model, n in sorted(unpriced.items(), key=lambda kv: -kv[1]):
            print(f"  {model:<34} {n:>8,} messages")
    return 0


if __name__ == "__main__":
    sys.exit(main())

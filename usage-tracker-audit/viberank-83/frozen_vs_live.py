#!/usr/bin/env python3
"""Frozen append-only totals vs a fresh recompute from live transcripts.

This is the viberank submit model in miniature: one side is what was recorded
when the messages happened, the other is what you get by re-reading the local
JSONLs today. Both sides use the SAME parser (routing_audit's collect_records),
so any difference is drift, not two implementations disagreeing.

Months that have already ended are the interesting rows: nothing legitimately
grows there, so a shortfall is purely what live files no longer show.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "packages" / "traceguard" / "src"))

from traceguard.routing_audit.ingest_claude_code import collect_records  # noqa: E402

SOURCE = Path.home() / ".claude" / "projects"
DB = _REPO / "traces_routing_audit.db"


def main() -> int:
    records, stats = collect_records(SOURCE)
    live = defaultdict(lambda: {"n": 0})
    for rec in records.values():
        live[rec.invoked_at.strftime("%Y-%m")]["n"] += 1

    conn = sqlite3.connect(DB)
    frozen = {
        month: {"n": n}
        for month, n in conn.execute(
            "SELECT strftime('%Y-%m', invoked_at), COUNT(*) FROM traces GROUP BY 1"
        )
    }

    months = sorted(set(live) | set(frozen))
    print(f"live files scanned : {stats.files_main} main + {stats.files_subagent} subagent")
    print(f"live records       : {len(records):,}")
    print(f"frozen records     : {sum(v['n'] for v in frozen.values()):,}")
    print()
    print(f"{'month':<9} {'frozen':>9} {'live now':>9} {'delta':>9} {'shortfall':>10}")
    print("-" * 50)
    for m in months:
        f = frozen.get(m, {"n": 0})["n"]
        lv = live.get(m, {"n": 0})["n"]
        d = lv - f
        pct = f"{(f - lv) / f:.1%}" if f else "-"
        print(f"{m:<9} {f:>9,} {lv:>9,} {d:>+9,} {pct:>10}")

    total_f = sum(v["n"] for v in frozen.values())
    total_l = sum(v["n"] for v in live.values())
    print("-" * 50)
    print(f"{'TOTAL':<9} {total_f:>9,} {total_l:>9,} {total_l - total_f:>+9,} "
          f"{(total_f - total_l) / total_f:>10.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

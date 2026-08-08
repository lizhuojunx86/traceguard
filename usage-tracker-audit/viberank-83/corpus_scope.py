#!/usr/bin/env python3
"""At what period granularity is `corpus.{files,bytes}` well defined?

The usage-drift-log spec keys a record by `month` but counts `corpus.files`
over the whole corpus. viberank (#112) decides per `(day, machine)`, so the
question is whether the corpus counters can be scoped to the period they are
meant to describe without double counting.

A transcript file is only attributable to one period if all of its records
fall inside it. This measures that, per month and per day, over the live
~/.claude/projects tree, using routing_audit's parser (same one used by
frozen_vs_live.py).
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "packages" / "traceguard" / "src"))

from traceguard.routing_audit.ingest_claude_code import collect_records  # noqa: E402

SOURCE = Path.home() / ".claude" / "projects"


def _spread(records, fmt: str) -> tuple[dict, dict, dict]:
    periods_of_file: dict[str, set[str]] = defaultdict(set)
    recs_of_file: dict[str, int] = defaultdict(int)
    files_of_period: dict[str, set[str]] = defaultdict(set)
    for rec in records.values():
        period = rec.invoked_at.strftime(fmt)
        periods_of_file[rec.source_file].add(period)
        recs_of_file[rec.source_file] += 1
        files_of_period[period].add(rec.source_file)
    return periods_of_file, recs_of_file, files_of_period


def _report(label: str, records, fmt: str) -> None:
    periods_of_file, recs_of_file, files_of_period = _spread(records, fmt)
    total_files = len(periods_of_file)
    multi = [f for f, ps in periods_of_file.items() if len(ps) > 1]
    multi_recs = sum(recs_of_file[f] for f in multi)
    summed = sum(len(v) for v in files_of_period.values())
    print(f"\n── scoped by {label} ── ({total_files:,} files carry records)")
    print(f"files spanning >1 {label:<5}: {len(multi):>5} of {total_files:,} "
          f"({len(multi) / total_files:.1%})")
    print(f"records in those files    : {multi_recs:>5,} of {len(records):,} "
          f"({multi_recs / len(records):.1%})")
    print(f"sum of per-{label} counts   : {summed:,} vs {total_files:,} global "
          f"(+{summed / total_files - 1:.1%} double counted)")


def _churn(records) -> None:
    """How fast new files mask a deletion in an older month.

    A whole-tree `corpus.files` count only detects removal if it is not
    outrun by ordinary work elsewhere in the tree.
    """
    first_seen: dict[str, str] = {}
    for rec in records.values():
        day = rec.invoked_at.strftime("%Y-%m-%d")
        prior = first_seen.get(rec.source_file)
        if prior is None or day < prior:
            first_seen[rec.source_file] = day
    per_day = Counter(first_seen.values())
    per_month = Counter(day[:7] for day in first_seen.values())
    recent = sorted(per_day)[-28:]
    rates = sorted(per_day[d] for d in recent)
    median = rates[len(rates) // 2]
    print("\n── new files per active day ──")
    print(f"last {len(recent)} active days: median {median}, "
          f"mean {sum(rates) / len(rates):.0f}")
    print("footprint of each month (files first seen in it):")
    for month in sorted(per_month):
        print(f"  {month}: {per_month[month]:>5,}")


def main() -> int:
    records, stats = collect_records(SOURCE)
    print(f"corpus: {stats.files_main} main + {stats.files_subagent} subagent "
          f"= {stats.files_main + stats.files_subagent} files "
          f"(a non-recursive glob sees {stats.files_main})")
    print(f"records: {len(records):,}")
    for label, fmt in (("month", "%Y-%m"), ("day", "%Y-%m-%d")):
        _report(label, records, fmt)
    _churn(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

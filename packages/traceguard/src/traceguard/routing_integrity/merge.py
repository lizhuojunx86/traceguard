"""Merge one trace store into another without losing or duplicating evidence.

Probe runs accumulate across stores more easily than anyone plans for: a run
lands in the working directory before the daily job exists, the job then writes
somewhere durable, and the early runs — often the most interesting ones,
because they were taken before anything was tuned — sit in a file nobody reads
again. Comparing across two files is not evidence; it is two anecdotes.

    python -m traceguard.routing_integrity.merge \\
        --from sqlite:///old.db --into sqlite:///accumulating.db --dry-run

Three properties this needs and a naive INSERT does not have:

**Identity is rebuilt, not copied.** ``trace_id`` is an autoincrement key, so
the same id means different rows in two stores. Rows are inserted without an
id and ``parent_trace_id`` is remapped to whatever the destination assigned.

**Running it twice does nothing the second time.** Rows are matched on
``(project, component, invoked_at, input_hash)`` — the closest thing a trace
has to a natural key. Merging is something people retry after an interruption,
and a merge that silently doubles the evidence is worse than one that fails.

**Nothing is written until everything can be.** One transaction; an error
leaves the destination as it was. The destination here is a store being filled
over weeks, and losing it means restarting the clock.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard.store.models import Trace

#: The columns that carry evidence. ``trace_id`` is deliberately absent.
_COPIED = (
    "project", "component", "operation", "correlation_id",
    "input_hash", "input_summary",
    "model_id", "prompt_template_id", "prompt_template_hash",
    "output_parsed", "parse_status",
    "latency_ms", "tokens_in", "tokens_out", "cost_usd",
    "feature_as_of", "invoked_at",
    "error_class", "error_message",
)


@dataclass
class MergeResult:
    inserted: int = 0
    skipped: int = 0
    considered: int = 0

    def render(self, *, dry_run: bool) -> str:
        verb = "would insert" if dry_run else "inserted"
        lines = [
            f"{self.considered} trace(s) considered",
            f"  {verb} {self.inserted}",
            f"  skipped {self.skipped} already present",
        ]
        if dry_run:
            lines.append("\ndry run: the destination was not modified.")
        return "\n".join(lines)


def _natural_key(trace: Trace) -> tuple:
    """What identifies a trace independently of which store it lives in."""
    return (trace.project, trace.component, trace.invoked_at, trace.input_hash)


def _existing_keys(session: Session) -> set[tuple]:
    return {_natural_key(t) for t in session.scalars(select(Trace))}


def merge_traces(
    source: Engine,
    destination: Engine,
    *,
    project: str | None = None,
    dry_run: bool = False,
) -> MergeResult:
    """Copy traces from ``source`` into ``destination``, skipping duplicates."""
    result = MergeResult()

    with Session(source) as src_sess:
        stmt = select(Trace).order_by(Trace.trace_id)
        if project:
            stmt = stmt.where(Trace.project == project)
        incoming = list(src_sess.scalars(stmt))
        # Detach the values now; the source session closes below.
        payloads = [
            ({col: getattr(t, col) for col in _COPIED}, t.trace_id, t.parent_trace_id)
            for t in incoming
        ]
        keys = [_natural_key(t) for t in incoming]

    result.considered = len(payloads)
    if not payloads:
        return result

    with Session(destination) as dst_sess:
        present = _existing_keys(dst_sess)
        id_map: dict[int, int] = {}

        for (values, old_id, old_parent), key in zip(payloads, keys):
            if key in present:
                result.skipped += 1
                continue
            row = Trace(**values)
            dst_sess.add(row)
            # flush per row so autoincrement ids exist for parent remapping
            dst_sess.flush()
            if old_id is not None:
                id_map[old_id] = row.trace_id
            present.add(key)
            result.inserted += 1

        # Second pass: parents, now that every new id is known. A parent that
        # did not come along is left null rather than pointed at a stranger.
        for (values, old_id, old_parent) in payloads:
            if old_parent is None or old_id is None or old_id not in id_map:
                continue
            new_parent = id_map.get(old_parent)
            if new_parent is not None:
                dst_sess.get(Trace, id_map[old_id]).parent_trace_id = new_parent

        if dry_run:
            dst_sess.rollback()
        else:
            dst_sess.commit()

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m traceguard.routing_integrity.merge",
        description="Merge one trace store into another, idempotently.",
    )
    parser.add_argument("--from", dest="source", required=True, help="source DB URL")
    parser.add_argument("--into", dest="destination", required=True, help="destination DB URL")
    # Defaults to every project. A merge tool that silently leaves rows behind
    # is the same failure as one that silently doubles them: you find out weeks
    # later, from a gap you cannot explain.
    parser.add_argument(
        "--project", default=None,
        help="restrict to one project (default: merge every project)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    from traceguard.store.models import make_engine

    if args.source == args.destination:
        print("refusing to merge a store into itself")
        return 2

    result = merge_traces(
        make_engine(args.source),
        make_engine(args.destination),
        project=args.project or None,
        dry_run=args.dry_run,
    )
    print(result.render(dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""CLI for the Claude Code session backfill.

Default mode is a dry-run: parse the source tree and print statistics
without touching any database. Pass ``--write`` to persist.

Examples::

    python -m traceguard.routing_audit.ingest                       # dry-run
    python -m traceguard.routing_audit.ingest --write \
        --db sqlite:///traces_routing_audit.db
    python -m traceguard.routing_audit.ingest --rollback cc-...-abc \
        --db sqlite:///traces_routing_audit.db
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone

from traceguard.routing_audit.ingest_claude_code import (
    DEFAULT_SOURCE,
    append_run_log,
    format_report,
    ingest,
    rollback_batch,
)

DEFAULT_DB = "sqlite:///traces_routing_audit.db"


def _parse_since(value: str) -> datetime:
    """Parse ``--since`` as ``<N>d`` (N days ago) or an ISO date/datetime."""
    m = re.fullmatch(r"(\d+)d", value.strip())
    if m:
        return datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--since must be '<N>d' or an ISO date/datetime, got {value!r}"
        ) from exc
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m traceguard.routing_audit.ingest",
        description="Backfill local Claude Code session history into traceguard traces.",
    )
    parser.add_argument(
        "--source",
        default=str(DEFAULT_SOURCE),
        help=f"Claude Code projects root (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"SQLAlchemy DB URL (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="actually write traces (default: dry-run that only prints statistics)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="force dry-run even if --write is passed; with --rollback, preview without deleting",
    )
    parser.add_argument(
        "--rollback",
        metavar="BATCH_ID",
        help="delete every trace written by BATCH_ID from --db, then exit",
    )
    parser.add_argument(
        "--no-subagents",
        action="store_true",
        help="ingest only main transcripts, skip subagents/agent-*.jsonl",
    )
    parser.add_argument(
        "--since",
        type=_parse_since,
        metavar="WHEN",
        help="incremental: skip files whose mtime predates WHEN "
        "('<N>d' for N days ago, or an ISO date/datetime). Full scan if omitted.",
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        help="append a JSON-line run record (date, new rows, new cost, errors) to PATH",
    )
    args = parser.parse_args(argv)

    if args.rollback:
        n_traces, n_log = rollback_batch(args.rollback, args.db, dry_run=args.dry_run)
        verb = "would delete" if args.dry_run else "deleted"
        print(f"rollback {args.rollback}: {verb} {n_traces} traces, {n_log} log rows")
        return 0

    write = args.write and not args.dry_run
    error: str | None = None
    try:
        stats = ingest(
            args.source,
            args.db,
            write=write,
            include_subagents=not args.no_subagents,
            since=args.since,
        )
    except Exception as exc:  # log the failure for the scheduled job, then re-raise
        if args.log_file:
            from traceguard.routing_audit.ingest_claude_code import IngestStats

            append_run_log(args.log_file, IngestStats(), wrote=write, error=repr(exc))
        raise
    print(format_report(stats, wrote=write))
    if args.log_file:
        append_run_log(args.log_file, stats, wrote=write, error=error)
    if write:
        print(f"\ndb: {args.db} (private usage metadata — keep out of version control)")
    else:
        print("\n(dry-run — re-run with --write to persist)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

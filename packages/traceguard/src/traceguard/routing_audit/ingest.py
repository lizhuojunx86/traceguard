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
import sys

from traceguard.routing_audit.ingest_claude_code import (
    DEFAULT_SOURCE,
    format_report,
    ingest,
    rollback_batch,
)

DEFAULT_DB = "sqlite:///traces_routing_audit.db"


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
    args = parser.parse_args(argv)

    if args.rollback:
        n_traces, n_log = rollback_batch(args.rollback, args.db, dry_run=args.dry_run)
        verb = "would delete" if args.dry_run else "deleted"
        print(f"rollback {args.rollback}: {verb} {n_traces} traces, {n_log} log rows")
        return 0

    write = args.write and not args.dry_run
    stats = ingest(
        args.source,
        args.db,
        write=write,
        include_subagents=not args.no_subagents,
    )
    print(format_report(stats, wrote=write))
    if write:
        print(f"\ndb: {args.db} (private usage metadata — keep out of version control)")
    else:
        print("\n(dry-run — re-run with --write to persist)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

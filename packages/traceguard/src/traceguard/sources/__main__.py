"""CLI for the source-snapshot extension (mirrors the audit CLI pattern).

::

    python -m traceguard.sources --db sqlite:///traces.db enable
    python -m traceguard.sources --db sqlite:///traces.db list [--source-uri PATTERN]
                                 [--since ISO] [--verdict V] [--limit N] [--json]
    python -m traceguard.sources --db sqlite:///traces.db drift [--source-uri PATTERN]
                                 [--since ISO] [--json]

``--db`` is a top-level option and MUST precede the subcommand (argparse hands
everything after the subcommand to the subparser, which does not define it).
It falls back to ``TRACEGUARD_DB_URL`` then the make_engine default.
``list`` exits 1 when any listed snapshot carries an actionable verdict
(``anachronistic`` / ``unverifiable``), so it can gate a CI step. ``drift``
exits 1 when at least one source changed its content between retrievals.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.sources.drift import compute_drift, drift_to_dict
from traceguard.sources.models import SourceSnapshotRow, ensure_source_tables
from traceguard.sources.validate import SourceVerdict
from traceguard.store.models import make_engine


def _parse_iso(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _row_dict(row: SourceSnapshotRow) -> dict:
    """Stable key order, so `--json` output diffs cleanly between runs."""
    return {
        "snapshot_id": row.snapshot_id,
        "trace_id": row.trace_id,
        "source_uri": row.source_uri,
        "source_kind": row.source_kind,
        "content_hash": row.content_hash,
        "content_encoding": row.content_encoding,
        "normalized_hash": row.normalized_hash,
        "normalizer_id": row.normalizer_id,
        "retrieved_at": row.retrieved_at.isoformat() if row.retrieved_at else None,
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "effective_at": row.effective_at.isoformat() if row.effective_at else None,
        "source_version": row.source_version,
        "mcp_server_id": row.mcp_server_id,
        "tool_name": row.tool_name,
        "cache_status": row.cache_status,
        "verdict": row.verdict,
        "strict": bool(row.strict),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m traceguard.sources",
        description=(
            "Point-in-time correctness for retrieved data (experimental, SPEC v1.2 §6.6). "
            "Records digests and metadata only — never the retrieved content."
        ),
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLAlchemy DB URL (default: TRACEGUARD_DB_URL / traceguard.db)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("enable", help="create the source_snapshots table (idempotent)")

    p_list = sub.add_parser("list", help="list recorded snapshots, newest retrieval first")
    p_list.add_argument(
        "--source-uri",
        default=None,
        metavar="PATTERN",
        help="SQL LIKE pattern against source_uri (use %% as the wildcard)",
    )
    p_list.add_argument("--since", default=None, metavar="ISO", help="retrieved_at >= this time")
    p_list.add_argument(
        "--verdict",
        default=None,
        choices=[v.value for v in SourceVerdict],
        help="only snapshots carrying this verdict",
    )
    p_list.add_argument("--limit", type=int, default=50, help="max rows (default 50)")
    p_list.add_argument("--json", action="store_true", help="JSON lines instead of a table")

    p_drift = sub.add_parser(
        "drift",
        help="which sources changed their content between retrievals (with a Wilson 95% CI)",
    )
    p_drift.add_argument(
        "--source-uri",
        default=None,
        metavar="PATTERN",
        help="SQL LIKE pattern against source_uri (use %% as the wildcard)",
    )
    p_drift.add_argument("--since", default=None, metavar="ISO", help="retrieved_at >= this time")
    p_drift.add_argument("--json", action="store_true", help="one JSON object instead of a table")

    args = parser.parse_args(argv)
    engine = make_engine(args.db)

    if args.command == "enable":
        ensure_source_tables(engine)
        print(
            "source_snapshots ready. Digests and metadata only — retrieved content "
            "is never stored (SPEC v1.2 §6.6)."
        )
        return 0

    if args.command == "list":
        stmt = select(SourceSnapshotRow).order_by(SourceSnapshotRow.retrieved_at.desc())
        if args.source_uri:
            stmt = stmt.where(SourceSnapshotRow.source_uri.like(args.source_uri))
        if args.since:
            try:
                stmt = stmt.where(SourceSnapshotRow.retrieved_at >= _parse_iso(args.since))
            except ValueError as exc:
                print(f"--since must be ISO 8601: {exc}", file=sys.stderr)
                return 2
        if args.verdict:
            stmt = stmt.where(SourceSnapshotRow.verdict == args.verdict)
        stmt = stmt.limit(max(1, args.limit))

        with Session(engine) as sess:
            rows = list(sess.scalars(stmt))

        actionable = 0
        for row in rows:
            verdict = SourceVerdict(row.verdict)
            if verdict.actionable:
                actionable += 1
            if args.json:
                print(json.dumps(_row_dict(row), sort_keys=False, separators=(",", ":")))
            else:
                published = row.published_at.isoformat() if row.published_at else "-"
                print(
                    f"  [{row.verdict}] trace={row.trace_id} "
                    f"retrieved={row.retrieved_at.isoformat()} published={published} "
                    f"{row.content_hash[:12]} {row.source_uri}"
                )
        if not args.json:
            print(
                f"{len(rows)} snapshot(s), {actionable} actionable "
                "(anachronistic / unverifiable)"
            )
        return 1 if actionable else 0

    if args.command == "drift":
        since = None
        if args.since:
            try:
                since = _parse_iso(args.since)
            except ValueError as exc:
                print(f"--since must be ISO 8601: {exc}", file=sys.stderr)
                return 2
        report = compute_drift(engine, since=since, source_uri=args.source_uri)

        if args.json:
            print(json.dumps(drift_to_dict(report), sort_keys=False, indent=2))
            return 1 if report.sources_drifted else 0

        print(report.summary())
        for src in report.sources:
            if not src.comparable:
                continue
            marker = "CHANGED" if src.drifted else "stable "
            print(
                f"  [{marker}] {src.source_uri}  "
                f"{src.observations} retrieval(s), {src.distinct_hashes} distinct "
                f"digest(s), {src.changes} change(s)"
            )
            if src.drifted:
                for r in src.retrievals:
                    print(
                        f"      {r.retrieved_at.isoformat()}  {r.content_hash[:16]}  "
                        f"[{r.verdict}] trace={r.trace_id}"
                    )
        if report.sources_single_observation:
            # Named, never silently folded away: a source seen once cannot have
            # drifted and cannot be shown not to have.
            print(
                f"  ({report.sources_single_observation} source(s) retrieved only once "
                "are excluded from the rate — one observation is not a comparison)"
            )
        if report.snapshots_unchecked:
            print(
                f"  ({report.snapshots_unchecked} snapshot(s) with verdict 'unchecked' "
                "are not observations and were not counted)"
            )
        return 1 if report.sources_drifted else 0

    return 2  # pragma: no cover - argparse enforces the subcommand set


if __name__ == "__main__":
    sys.exit(main())

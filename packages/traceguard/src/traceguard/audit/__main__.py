"""CLI for the audit evidence layer (mirrors the routing_audit CLI pattern).

::

    python -m traceguard.audit enable  --db sqlite:///traces.db [--chain-only] [--no-backfill] [--strict]
    python -m traceguard.audit disable --db sqlite:///traces.db
    python -m traceguard.audit verify  --db sqlite:///traces.db [--anchor '<json>']
    python -m traceguard.audit anchor  --db sqlite:///traces.db

``verify`` exits 1 on BREAK findings (tamper evidence), 0 otherwise.
``--db`` falls back to ``TRACEGUARD_DB_URL`` then the make_engine default.
"""
from __future__ import annotations

import argparse
import sys

from traceguard.audit.chain import disable, enable
from traceguard.audit.verify import ChainAnchor, export_anchor, verify_chain
from traceguard.store.models import make_engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m traceguard.audit",
        description="Opt-in tamper-evident audit trail for traceguard traces (experimental).",
    )
    parser.add_argument(
        "--db", default=None, help="SQLAlchemy DB URL (default: TRACEGUARD_DB_URL / traceguard.db)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_enable = sub.add_parser("enable", help="enable audit (tables + settings + backfill + attach)")
    p_enable.add_argument(
        "--chain-only",
        action="store_true",
        help="enable the hash chain WITHOUT the ORM append-only guard",
    )
    p_enable.add_argument(
        "--no-backfill",
        action="store_true",
        help="do not chain pre-existing traces (they will show as coverage gaps)",
    )
    p_enable.add_argument(
        "--strict",
        action="store_true",
        help="fail closed on chain failures in this process (TRACEGUARD_AUDIT_STRICT=1)",
    )

    sub.add_parser("disable", help="flip the DB flag off (guard lifts, chaining stops)")
    p_verify = sub.add_parser("verify", help="recompute the whole chain; exit 1 on BREAKs")
    p_verify.add_argument(
        "--anchor",
        default=None,
        metavar="JSON",
        help="previously exported anchor JSON: ADDS an anchor-consistency check "
        "(truncation/rewrite since the export) on top of the full walk",
    )
    sub.add_parser("anchor", help="print the chain head digest (store it OUTSIDE the DB)")

    args = parser.parse_args(argv)
    engine = make_engine(args.db)

    if args.command == "enable":
        backfilled = enable(
            engine,
            append_only=not args.chain_only,
            backfill=not args.no_backfill,
            strict=args.strict,
        )
        print(
            f"audit enabled (append_only={not args.chain_only}); "
            f"backfilled {backfilled} pre-existing trace(s)"
        )
        return 0

    if args.command == "disable":
        disable(engine)
        print("audit disabled: guard lifted, new writes are no longer chained "
              "(the existing chain stays verifiable)")
        return 0

    if args.command == "verify":
        anchor = ChainAnchor.from_json(args.anchor) if args.anchor else None
        result = verify_chain(engine, from_anchor=anchor)
        print(result.summary())
        for finding in result.findings:
            loc = f"seq={finding.seq}" if finding.seq is not None else ""
            tid = f"trace_id={finding.trace_id}" if finding.trace_id is not None else ""
            where = " ".join(x for x in (loc, tid) if x)
            print(f"  [{finding.severity}] {finding.kind} {where}: {finding.detail}")
        return 0 if result.ok else 1

    if args.command == "anchor":
        print(export_anchor(engine).to_json())
        return 0

    return 2  # pragma: no cover - argparse enforces the subcommand set


if __name__ == "__main__":
    sys.exit(main())

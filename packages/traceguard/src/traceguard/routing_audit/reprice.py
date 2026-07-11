"""Backfill ``traces.cost_usd`` for rows that were NULL when first ingested.

A model can be observed before its price is known (e.g. sonnet-5 subagent
traces landed cost=NULL until :mod:`pricing` gained an entry). This tool
recomputes cost for rows where ``cost_usd IS NULL`` and the model now has a
price, using the SAME list-price function as ingest (era-aware via
``invoked_at`` for Sonnet 5). SPEC §3.1: cost_usd is list price at write time;
this is a deferred first write, not a post-hoc reconciliation of billed cost.

Idempotent (already-priced rows are skipped), independent batch, reversible
(:func:`rollback_reprice` restores the logged old value). No API calls.

CLI::

    python -m traceguard.routing_audit.reprice            # dry-run stats
    python -m traceguard.routing_audit.reprice --write --db sqlite:///...
    python -m traceguard.routing_audit.reprice --rollback rp-... --db sqlite:///...
"""
from __future__ import annotations

import argparse
import sys
import uuid as uuid_mod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path  # noqa: F401  (kept for parity with sibling CLIs)

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from traceguard.routing_audit.models import RoutingAuditRepriceLog, ensure_tables
from traceguard.routing_audit.pricing import compute_cost_usd, price_for
from traceguard.store.models import Trace, make_engine

DEFAULT_DB = "sqlite:///traces_routing_audit.db"
_WRITE_CHUNK = 500


@dataclass
class RepriceStats:
    null_rows: int = 0  # rows with cost_usd IS NULL
    priced: int = 0  # of those, now have a price
    unpriced: int = 0  # still no price (skipped)
    no_usage: int = 0  # NULL because usage/model made cost genuinely None
    written: int = 0
    new_cost_total: Decimal = Decimal("0")
    batch_id: str | None = None
    by_model: dict[str, int] = field(default_factory=dict)


def _batch_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"rp-{stamp}-{uuid_mod.uuid4().hex[:6]}"


def reprice_null_costs(
    db_url: str | None = None, *, write: bool = False, batch_id: str | None = None
) -> RepriceStats:
    """Recompute cost for NULL-cost rows whose model now has a price."""
    stats = RepriceStats()
    engine = make_engine(db_url)
    ensure_tables(engine)
    stats.batch_id = batch_id or _batch_id()

    with Session(engine) as sess:
        rows = list(
            sess.execute(
                select(Trace.trace_id, Trace.model_id, Trace.output_parsed, Trace.invoked_at)
                .where(Trace.cost_usd.is_(None))
            )
        )
        stats.null_rows = len(rows)
        pending: list[tuple[int, str, Decimal]] = []
        for trace_id, model_id, output_parsed, invoked_at in rows:
            if model_id is None or price_for(model_id, invoked_at) is None:
                stats.unpriced += 1
                continue
            usage = (output_parsed or {}).get("usage")
            new_cost = compute_cost_usd(model_id, usage, invoked_at)
            if new_cost is None:
                # model is priced but usage was absent → genuinely None, leave it
                stats.no_usage += 1
                continue
            stats.priced += 1
            stats.new_cost_total += new_cost
            stats.by_model[model_id] = stats.by_model.get(model_id, 0) + 1
            pending.append((trace_id, model_id, new_cost))

        if not write:
            return stats

        for start in range(0, len(pending), _WRITE_CHUNK):
            chunk = pending[start : start + _WRITE_CHUNK]
            for trace_id, model_id, new_cost in chunk:
                sess.execute(
                    update(Trace).where(Trace.trace_id == trace_id).values(cost_usd=new_cost)
                )
                sess.add(
                    RoutingAuditRepriceLog(
                        batch_id=stats.batch_id,
                        trace_id=trace_id,
                        model_id=model_id,
                        old_cost_usd=None,
                        new_cost_usd=new_cost,
                    )
                )
            sess.commit()
            stats.written += len(chunk)
    return stats


def rollback_reprice(batch_id: str, db_url: str | None = None) -> int:
    """Restore the logged old cost for every trace in ``batch_id``. Returns count."""
    engine = make_engine(db_url)
    ensure_tables(engine)
    with Session(engine) as sess:
        logs = list(
            sess.scalars(
                select(RoutingAuditRepriceLog).where(
                    RoutingAuditRepriceLog.batch_id == batch_id
                )
            )
        )
        for log in logs:
            sess.execute(
                update(Trace).where(Trace.trace_id == log.trace_id).values(
                    cost_usd=log.old_cost_usd
                )
            )
            sess.delete(log)
        sess.commit()
    return len(logs)


def format_report(stats: RepriceStats, *, wrote: bool) -> str:
    mode = f"WROTE batch={stats.batch_id}" if wrote else "DRY-RUN (no writes)"
    lines = [
        f"== routing_audit: reprice NULL costs — {mode} ==",
        f"NULL-cost rows: {stats.null_rows} | now priceable: {stats.priced} "
        f"| still unpriced: {stats.unpriced} | priced-but-no-usage: {stats.no_usage}",
        f"new cost total: ${stats.new_cost_total:.4f} | written: {stats.written}",
    ]
    for model, n in sorted(stats.by_model.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {model:<28} {n}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m traceguard.routing_audit.reprice",
        description="Backfill NULL traces.cost_usd for now-priced models (no API calls).",
    )
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--write", action="store_true", help="persist (default: dry-run)")
    parser.add_argument("--rollback", metavar="BATCH_ID", help="restore a reprice batch, then exit")
    args = parser.parse_args(argv)

    if args.rollback:
        n = rollback_reprice(args.rollback, args.db)
        print(f"rollback {args.rollback}: restored {n} rows to their prior cost")
        return 0

    stats = reprice_null_costs(args.db, write=args.write)
    print(format_report(stats, wrote=args.write))
    if not args.write:
        print("\n(dry-run — re-run with --write to persist)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

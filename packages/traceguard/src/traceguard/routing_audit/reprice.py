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
from typing import Any, Callable

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
    db_url: str | None = None,
    *,
    write: bool = False,
    batch_id: str | None = None,
    on_cost_write: Callable[..., Any] | None = None,
) -> RepriceStats:
    """Recompute cost for NULL-cost rows whose model now has a price.

    ``on_cost_write`` (optional, default None = exactly the old behavior) is
    invoked once per persisted row AFTER each chunk commits, with keyword args
    ``trace_id, event_type='deferred_first_write', old_value, new_value,
    batch_id, reason`` — the audit evidence layer wires this to
    ``traceguard.audit.record_cost_event`` (CLI: ``--audit``). Post-commit on
    purpose: events are only recorded for changes that actually landed; a
    crash in between at worst under-records (verify then flags cost_mismatch).
    Hook errors propagate — this is an explicit opt-in path, not a hot path.
    """
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
            if on_cost_write is not None:
                for trace_id, model_id, new_cost in chunk:
                    on_cost_write(
                        trace_id=trace_id,
                        event_type="deferred_first_write",
                        old_value=None,
                        new_value=new_cost,
                        batch_id=stats.batch_id,
                        reason=f"reprice backfill ({model_id})",
                    )
    return stats


@dataclass
class RecomputeStats:
    """Rows whose ALREADY-SET cost changes when the pricing rules are corrected."""

    scanned: int = 0  # rows with a non-NULL cost that were re-derived
    changed: int = 0  # of those, the ones whose recomputed cost differs
    unchanged: int = 0
    unpriceable: int = 0  # priced before, not derivable now — never silently zeroed
    written: int = 0
    old_total: Decimal = Decimal("0")  # summed over CHANGED rows only
    new_total: Decimal = Decimal("0")
    batch_id: str | None = None
    by_model: dict[str, list[Any]] = field(default_factory=dict)  # model -> [rows, delta]

    @property
    def delta(self) -> Decimal:
        return self.new_total - self.old_total


def recompute_costs(
    db_url: str | None = None,
    *,
    write: bool = False,
    batch_id: str | None = None,
    on_cost_write: Callable[..., Any] | None = None,
) -> RecomputeStats:
    """Re-derive cost for rows that ALREADY have one, logging the true old value.

    :func:`reprice_null_costs` cannot do this. It filters on ``cost_usd IS
    NULL`` and hard-codes ``old_cost_usd=None`` in the log, because it only
    ever performs a deferred FIRST write. Correcting a pricing rule is a
    different operation: the rows already hold a value, that value is wrong,
    and the log must carry what it was or the change is irreversible.

    Written for the 2026-08-08 cache-TTL correction — 1-hour cache writes had
    been billed at the 5-minute 1.25x rate on every row in the store, because
    the TTL split was only ever read from the nested usage shape and local data
    only ever uses the flat one. Nothing here is specific to that: it re-derives
    every non-NULL cost under the current rules and writes back only what
    differs.

    A row that was priced and is no longer derivable is counted under
    ``unpriceable`` and LEFT ALONE. Overwriting a real number with NULL because
    the rules regressed would destroy data on the strength of a bug.

    Batch/rollback semantics match the NULL path, so the two together read as
    one ordered history per trace: the deferred first write, then each
    correction, each reversible on its own batch id.
    """
    stats = RecomputeStats()
    engine = make_engine(db_url)
    ensure_tables(engine)
    stats.batch_id = batch_id or _batch_id()

    with Session(engine) as sess:
        rows = list(
            sess.execute(
                select(
                    Trace.trace_id, Trace.model_id, Trace.output_parsed,
                    Trace.invoked_at, Trace.cost_usd,
                ).where(Trace.cost_usd.is_not(None))
            )
        )
        stats.scanned = len(rows)
        pending: list[tuple[int, str, Decimal, Decimal]] = []
        for trace_id, model_id, output_parsed, invoked_at, old_cost in rows:
            usage = (output_parsed or {}).get("usage")
            new_cost = compute_cost_usd(model_id, usage, invoked_at)
            if new_cost is None:
                stats.unpriceable += 1
                continue
            if new_cost == old_cost:
                stats.unchanged += 1
                continue
            stats.changed += 1
            stats.old_total += old_cost
            stats.new_total += new_cost
            agg = stats.by_model.setdefault(model_id, [0, Decimal("0")])
            agg[0] += 1
            agg[1] += new_cost - old_cost
            pending.append((trace_id, model_id, old_cost, new_cost))

        if not write:
            return stats

        for start in range(0, len(pending), _WRITE_CHUNK):
            chunk = pending[start : start + _WRITE_CHUNK]
            for trace_id, model_id, old_cost, new_cost in chunk:
                sess.execute(
                    update(Trace).where(Trace.trace_id == trace_id).values(cost_usd=new_cost)
                )
                sess.add(
                    RoutingAuditRepriceLog(
                        batch_id=stats.batch_id,
                        trace_id=trace_id,
                        model_id=model_id,
                        old_cost_usd=old_cost,  # the real prior value — what makes it reversible
                        new_cost_usd=new_cost,
                    )
                )
            sess.commit()
            stats.written += len(chunk)
            if on_cost_write is not None:
                for trace_id, model_id, old_cost, new_cost in chunk:
                    on_cost_write(
                        trace_id=trace_id,
                        event_type="pricing_rule_correction",
                        old_value=old_cost,
                        new_value=new_cost,
                        batch_id=stats.batch_id,
                        reason=f"cost recomputed under current pricing rules ({model_id})",
                    )
    return stats


def format_recompute_report(stats: RecomputeStats, *, wrote: bool) -> str:
    mode = f"WROTE batch={stats.batch_id}" if wrote else "DRY-RUN (no writes)"
    sign = "+" if stats.delta >= 0 else "-"
    lines = [
        f"== routing_audit: recompute existing costs — {mode} ==",
        f"scanned: {stats.scanned} priced rows | changed: {stats.changed} "
        f"| unchanged: {stats.unchanged} | no longer derivable (left alone): "
        f"{stats.unpriceable}",
        # A delta never prints without the base it moves from.
        f"over the {stats.changed} changed rows: ${stats.old_total:.4f} -> "
        f"${stats.new_total:.4f}  ({sign}${abs(stats.delta):.4f})",
        f"written: {stats.written}",
    ]
    if stats.by_model:
        lines += ["", f"  {'model':<28} {'rows':>7} {'delta_usd':>12}"]
        for model, (n, delta) in sorted(stats.by_model.items(), key=lambda kv: -kv[1][1]):
            lines.append(f"  {model:<28} {n:>7} {delta:>12.4f}")
    return "\n".join(lines)


def rollback_reprice(
    batch_id: str,
    db_url: str | None = None,
    *,
    on_cost_write: Callable[..., Any] | None = None,
) -> int:
    """Restore the logged old cost for every trace in ``batch_id``. Returns count.

    ``on_cost_write`` mirrors :func:`reprice_null_costs`: called post-commit
    per restored row with ``event_type='rollback'`` (old_value = the reprice
    value being undone, new_value = the restored prior value).
    """
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
        restored = [(log.trace_id, log.new_cost_usd, log.old_cost_usd) for log in logs]
        for log in logs:
            sess.execute(
                update(Trace).where(Trace.trace_id == log.trace_id).values(
                    cost_usd=log.old_cost_usd
                )
            )
            sess.delete(log)
        sess.commit()
    if on_cost_write is not None:
        for trace_id, undone_value, restored_value in restored:
            on_cost_write(
                trace_id=trace_id,
                event_type="rollback",
                old_value=undone_value,
                new_value=restored_value,
                batch_id=batch_id,
                reason="reprice rollback",
            )
    return len(restored)


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
    parser.add_argument(
        "--recompute",
        action="store_true",
        help="re-derive costs for rows that ALREADY have one and rewrite those that "
        "differ under the current pricing rules (default path only fills NULLs)",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="record each cost write as a chained traceguard.audit cost event "
        "(requires audit enabled on --db)",
    )
    args = parser.parse_args(argv)

    on_cost_write = None
    if args.audit:
        # Lazy import + explicit wiring only when asked for: reprice never
        # auto-detects the audit layer (importing it must stay side-effect
        # free and opt-in must stay visible at the call site).
        from traceguard.audit import is_enabled, record_cost_event

        audit_engine = make_engine(args.db)
        # Pre-flight BEFORE any write: the hooks fire post-commit, so a
        # not-enabled failure discovered mid-run would leave cost writes
        # committed (and a rollback's reprice-log rows deleted) with no
        # recorded events.
        if not is_enabled(audit_engine):
            print(
                "--audit requires the audit layer to be enabled on this DB first: "
                f"python -m traceguard.audit enable --db {args.db}"
            )
            return 2

        def on_cost_write(**kwargs: Any) -> None:
            record_cost_event(audit_engine, **kwargs)

    if args.rollback:
        n = rollback_reprice(args.rollback, args.db, on_cost_write=on_cost_write)
        print(f"rollback {args.rollback}: restored {n} rows to their prior cost")
        return 0

    if args.recompute:
        rstats = recompute_costs(args.db, write=args.write, on_cost_write=on_cost_write)
        print(format_recompute_report(rstats, wrote=args.write))
        if not args.write:
            print("\n(dry-run — re-run with --write to persist)")
        return 0

    stats = reprice_null_costs(args.db, write=args.write, on_cost_write=on_cost_write)
    print(format_report(stats, wrote=args.write))
    if not args.write:
        print("\n(dry-run — re-run with --write to persist)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

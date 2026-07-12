"""Policy-deviation audit over backfilled Claude Code traces.

Not a routing *diary* — a *deviation audit*. The declarable tiered routing
policy (``routing_policy.yaml``) states which tier each
(project, component, task_type) should use; this module compares the observed
traces against it and records, per ``(tagging unit, component)``, whether the
actual model crossed a tier boundary.

Why (unit, component) grain: the policy is layered by role (main thread vs
Explore vs workflow-subagent) and by task_type, and a single unit's time
window contains several components (a main-thread stretch plus the subagents
it spawned). Each (unit, component) gets one verdict; the *dominant* model
(most traces) is the ``actual_model``.

Deviation = a TIER mismatch. Same-tier substitutions (opus-4-8 ↔ fable-5) are
not deviations — the audit is about frontier/mid/cheap layering, not exact
model identity. Records with no actual model (API errors) and traces outside
any tagging unit are skipped and counted, never guessed.

Manual loop (mirrors task_tags): ``export`` writes the deviation rows to
``routing_deviations.csv``; fill ``reason`` (≤200 chars) and ``outcome``
(adopted/rework/discarded/unknown), then ``import`` marks them
``source="manual"`` — regeneration never overwrites a manual row.

Timing note: unit attribution uses the DB's ``invoked_at`` only; the mutable
source tree is never re-read here (see ingest_claude_code data caveats).

CLI::

    python -m traceguard.routing_audit.routing_decisions generate [--write]
    python -m traceguard.routing_audit.routing_decisions export --csv devs.csv
    python -m traceguard.routing_audit.routing_decisions import --csv devs.csv
    python -m traceguard.routing_audit.routing_decisions report
"""
from __future__ import annotations

import argparse
import csv
import sys
import uuid as uuid_mod
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.routing_audit.models import RoutingDecision, ensure_tables
from traceguard.routing_audit.task_tags import load_unit_index
from traceguard.store.models import Trace, make_engine

DEFAULT_DB = "sqlite:///traces_routing_audit.db"
DEFAULT_POLICY = Path(__file__).with_name("routing_policy.yaml")
OUTCOMES = ("adopted", "rework", "discarded", "unknown")
_REASON_MAX = 200
_PENDING_NOTE = "(heuristic task tags — pending manual review)"


@dataclass
class Policy:
    tiers: dict[str, list[str]]
    default_tier: str
    rules: list[dict[str, Any]]
    _model_to_tier: dict[str, str] = field(default_factory=dict)

    def tier_of(self, model_id: str | None) -> str:
        if model_id is None:
            return "unknown"
        return self._model_to_tier.get(model_id, "unknown")

    def match(
        self, project: str, component: str, task_type: str
    ) -> tuple[str, str | None]:
        """Return (expected_tier, expected_model) for a (project, component, task_type).

        The applicable rule with the most specified-and-matching keys wins;
        ties go to the earlier rule in the file. No match → default_tier.
        """
        best_score: tuple[int, int] | None = None
        best: tuple[str, str | None] = (self.default_tier, None)
        for i, rule in enumerate(self.rules):
            spec = 0
            applicable = True
            for key, val in (
                ("project", project),
                ("component", component),
                ("task_type", task_type),
            ):
                if key in rule:
                    if rule[key] != val:
                        applicable = False
                        break
                    spec += 1
            if not applicable:
                continue
            score = (spec, -i)  # more specific, then earlier in file
            if best_score is None or score > best_score:
                best_score = score
                best = (rule.get("expected_tier", self.default_tier), rule.get("expected_model"))
        return best


def load_policy(path: Path | str | None = None) -> Policy:
    p = Path(path) if path else DEFAULT_POLICY
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    tiers = data.get("tiers", {}) or {}
    model_to_tier = {m: tier for tier, models in tiers.items() for m in (models or [])}
    return Policy(
        tiers=tiers,
        default_tier=data.get("default_tier", "frontier"),
        rules=data.get("rules", []) or [],
        _model_to_tier=model_to_tier,
    )


@dataclass
class _Agg:
    unit_id: str
    component: str
    session_id: str
    project: str
    task_type: str
    ts: datetime
    model_counts: Counter = field(default_factory=Counter)
    cost: Decimal = Decimal("0")
    n: int = 0


@dataclass
class DecisionStats:
    decisions: int = 0
    deviations: int = 0
    inserted: int = 0
    updated: int = 0
    manual_kept: int = 0
    skipped_api_error: int = 0
    skipped_untagged: int = 0
    deviation_cost: Decimal = Decimal("0")
    batch_id: str | None = None
    policy_path: str = ""
    # task_type -> [decisions, deviations, dev_cost]
    by_task_type: dict[str, list[Any]] = field(default_factory=dict)


def _batch_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"dec-{stamp}-{uuid_mod.uuid4().hex[:6]}"


def _aggregate(
    engine: Any, stats: DecisionStats, *, as_of: datetime | None = None
) -> dict[tuple[str, str], _Agg]:
    index = load_unit_index(engine)
    agg: dict[tuple[str, str], _Agg] = {}
    with Session(engine) as sess:
        stmt = select(
            Trace.output_parsed,
            Trace.invoked_at,
            Trace.project,
            Trace.component,
            Trace.model_id,
            Trace.cost_usd,
        )
        if as_of is not None:
            stmt = stmt.where(Trace.invoked_at <= as_of)
        rows = sess.execute(stmt)
        for output_parsed, invoked_at, project, component, model_id, cost_usd in rows:
            if model_id is None:  # API error — no actual model to judge
                stats.skipped_api_error += 1
                continue
            session_id = (output_parsed or {}).get("session_id")
            hit = index.lookup(session_id, invoked_at)
            if hit is None:  # trace outside any tagging unit
                stats.skipped_untagged += 1
                continue
            unit_id, task_type, _unit_project = hit
            key = (unit_id, component)
            a = agg.get(key)
            if a is None:
                a = _Agg(
                    unit_id=unit_id,
                    component=component,
                    session_id=session_id or "unknown",
                    project=project,
                    task_type=task_type,
                    ts=invoked_at,
                )
                agg[key] = a
            a.model_counts[model_id] += 1
            a.cost += cost_usd or Decimal("0")
            a.n += 1
            if invoked_at < a.ts:
                a.ts = invoked_at
    return agg


def generate_decisions(
    db_url: str | None = None,
    *,
    write: bool = False,
    policy_path: Path | str | None = None,
    as_of: datetime | None = None,
) -> DecisionStats:
    """Build one decision per (unit, component); optionally upsert.

    ``source="manual"`` rows are never regenerated. Returns stats even in
    dry-run (``write=False``) so the report can be previewed. ``as_of`` freezes
    the trace snapshot (only ``invoked_at <= as_of``).
    """
    policy = load_policy(policy_path)
    stats = DecisionStats(policy_path=str(policy_path or DEFAULT_POLICY))
    engine = make_engine(db_url)
    ensure_tables(engine)
    agg = _aggregate(engine, stats, as_of=as_of)

    stats.batch_id = _batch_id()
    sess: Session | None = Session(engine) if write else None
    try:
        for (unit_id, component), a in agg.items():
            actual_model = a.model_counts.most_common(1)[0][0]
            actual_tier = policy.tier_of(actual_model)
            expected_tier, expected_model = policy.match(a.project, component, a.task_type)
            deviation = actual_tier != expected_tier

            stats.decisions += 1
            bucket = stats.by_task_type.setdefault(a.task_type, [0, 0, Decimal("0")])
            bucket[0] += 1
            if deviation:
                stats.deviations += 1
                stats.deviation_cost += a.cost
                bucket[1] += 1
                bucket[2] += a.cost

            if sess is None:
                continue
            decision_id = f"{unit_id}#{component}"
            existing = sess.get(RoutingDecision, decision_id)
            if existing is not None and existing.source == "manual":
                stats.manual_kept += 1
                continue
            values = dict(
                ts=a.ts,
                unit_id=unit_id,
                session_id=a.session_id,
                project=a.project,
                component=component,
                task_type=a.task_type,
                expected_tier=expected_tier,
                expected_model=expected_model,
                actual_model=actual_model,
                actual_tier=actual_tier,
                deviation=deviation,
                n_traces=a.n,
                cost_usd=a.cost,
                batch_id=stats.batch_id,
            )
            if existing is None:
                sess.add(RoutingDecision(decision_id=decision_id, source="generated", **values))
                stats.inserted += 1
            else:
                for k, v in values.items():
                    setattr(existing, k, v)
                stats.updated += 1
        if sess is not None:
            sess.commit()
    finally:
        if sess is not None:
            sess.close()
    return stats


_DEV_FIELDS = [
    "decision_id",
    "project",
    "component",
    "task_type",
    "expected_tier",
    "expected_model",
    "actual_model",
    "actual_tier",
    "n_traces",
    "cost_usd",
    "reason",
    "outcome",
]


def export_deviations_csv(csv_path: Path | str, db_url: str | None = None) -> int:
    """Export deviation rows (cost-desc) for manual reason/outcome entry."""
    engine = make_engine(db_url)
    ensure_tables(engine)
    with Session(engine) as sess:
        decisions = list(
            sess.scalars(
                select(RoutingDecision)
                .where(RoutingDecision.deviation.is_(True))
                .order_by(RoutingDecision.cost_usd.desc())
            )
        )
        rows = [
            {
                "decision_id": d.decision_id,
                "project": d.project,
                "component": d.component,
                "task_type": d.task_type,
                "expected_tier": d.expected_tier,
                "expected_model": d.expected_model or "",
                "actual_model": d.actual_model,
                "actual_tier": d.actual_tier,
                "n_traces": d.n_traces,
                "cost_usd": f"{d.cost_usd:.6f}" if d.cost_usd is not None else "",
                "reason": d.reason or "",
                "outcome": d.outcome,
            }
            for d in decisions
        ]
    with Path(csv_path).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_DEV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def import_decisions_csv(csv_path: Path | str, db_url: str | None = None) -> DecisionStats:
    """Re-import edited deviations: set reason/outcome, mark source="manual"."""
    stats = DecisionStats(batch_id=_batch_id())
    engine = make_engine(db_url)
    ensure_tables(engine)
    skipped_missing = 0
    skipped_bad = 0
    with Path(csv_path).open(encoding="utf-8", newline="") as fh, Session(engine) as sess:
        for row in csv.DictReader(fh):
            decision_id = (row.get("decision_id") or "").strip()
            outcome = (row.get("outcome") or "unknown").strip() or "unknown"
            if outcome not in OUTCOMES:
                skipped_bad += 1
                continue
            existing = sess.get(RoutingDecision, decision_id)
            if existing is None:
                skipped_missing += 1
                continue
            reason = (row.get("reason") or "").strip()[:_REASON_MAX] or None
            existing.reason = reason
            existing.outcome = outcome
            existing.source = "manual"
            existing.batch_id = stats.batch_id
            stats.updated += 1
        sess.commit()
    stats.decisions = stats.updated + skipped_missing + skipped_bad
    stats.skipped_untagged = skipped_missing
    return stats


def format_report(db_url: str | None = None) -> str:
    """Deviation-rate report: by task_type, plus a task_type × actual_model pivot."""
    engine = make_engine(db_url)
    ensure_tables(engine)
    with Session(engine) as sess:
        decisions = list(sess.scalars(select(RoutingDecision)))

    if not decisions:
        return "no routing_decisions rows — run `generate --write` first."

    total = len(decisions)
    dev = [d for d in decisions if d.deviation]
    manual = sum(1 for d in decisions if d.source == "manual")
    dev_cost = sum((d.cost_usd or Decimal("0") for d in dev), Decimal("0"))

    # by task_type
    by_tt: dict[str, list[Any]] = defaultdict(lambda: [0, 0, Decimal("0")])
    for d in decisions:
        b = by_tt[d.task_type]
        b[0] += 1
        if d.deviation:
            b[1] += 1
            b[2] += d.cost_usd or Decimal("0")

    lines = [
        f"== routing deviations {_PENDING_NOTE} ==",
        f"decisions: {total} | deviations: {len(dev)} "
        f"({len(dev) / total:.1%}) | manual-reviewed: {manual}",
        f"total deviation cost: ${dev_cost:.4f}",
        "",
        "deviation rate by task_type:",
        f"{'task_type':<20} {'decisions':>9} {'deviations':>10} {'rate':>7} {'dev_cost_usd':>13}",
        "-" * 62,
    ]
    for tt in sorted(by_tt, key=lambda t: by_tt[t][2], reverse=True):
        n, nd, c = by_tt[tt]
        rate = f"{nd / n:.1%}" if n else "—"
        lines.append(f"{tt:<20} {n:>9} {nd:>10} {rate:>7} {c:>13.4f}")

    # deviation pivot: task_type × actual_model
    lines += [
        "",
        "deviations by task_type × actual_model (tier mismatch):",
        f"{'task_type':<20} {'expected':>9} {'actual_model':<28} {'actual':>8} "
        f"{'units':>6} {'cost_usd':>11}",
        "-" * 86,
    ]
    cells: dict[tuple[str, str, str, str], list[Any]] = defaultdict(lambda: [0, Decimal("0")])
    for d in dev:
        cell = cells[(d.task_type, d.expected_tier, d.actual_model, d.actual_tier)]
        cell[0] += 1
        cell[1] += d.cost_usd or Decimal("0")
    for (tt, exp_tier, actual_model, act_tier), (units, cost) in sorted(
        cells.items(), key=lambda kv: kv[1][1], reverse=True
    ):
        lines.append(
            f"{tt:<20} {exp_tier:>9} {actual_model:<28} {act_tier:>8} "
            f"{units:>6} {cost:>11.4f}"
        )
    lines.append("-" * 86)
    lines.append(
        f"note: same-tier substitutions are not deviations; "
        f"{total - len(dev)} decisions were on-policy."
    )
    return "\n".join(lines)


def _format_gen_stats(stats: DecisionStats, *, wrote: bool) -> str:
    mode = f"WROTE batch={stats.batch_id}" if wrote else "DRY-RUN (no writes)"
    lines = [
        f"== routing_audit: decisions generate — {mode} {_PENDING_NOTE} ==",
        f"policy: {stats.policy_path}",
        f"decisions: {stats.decisions} | deviations: {stats.deviations} "
        f"({stats.deviations / stats.decisions:.1%} of decisions)"
        if stats.decisions
        else "decisions: 0",
        f"inserted: {stats.inserted} | updated: {stats.updated} | manual kept: {stats.manual_kept}",
        f"deviation cost: ${stats.deviation_cost:.4f}",
        f"skipped: {stats.skipped_api_error} api-error, {stats.skipped_untagged} untagged traces",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m traceguard.routing_audit.routing_decisions",
        description="Policy-deviation audit over backfilled routing traces.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="build decisions from policy vs actual (dry-run unless --write)")
    p_gen.add_argument("--db", default=DEFAULT_DB)
    p_gen.add_argument("--policy", default=None, help=f"policy YAML (default: {DEFAULT_POLICY})")
    p_gen.add_argument("--write", action="store_true")
    p_gen.add_argument("--as-of", default=None, help="freeze: only traces invoked_at <= this")

    p_export = sub.add_parser("export", help="export deviation rows to CSV for manual review")
    p_export.add_argument("--db", default=DEFAULT_DB)
    p_export.add_argument("--csv", default="routing_deviations.csv")

    p_import = sub.add_parser("import", help="re-import edited deviations as manual")
    p_import.add_argument("--db", default=DEFAULT_DB)
    p_import.add_argument("--csv", required=True)

    p_report = sub.add_parser("report", help="deviation-rate report + pivot")
    p_report.add_argument("--db", default=DEFAULT_DB)

    args = parser.parse_args(argv)
    if args.command == "generate":
        from traceguard.routing_audit.counterfactual import parse_as_of

        stats = generate_decisions(
            args.db, write=args.write, policy_path=args.policy, as_of=parse_as_of(args.as_of)
        )
        print(_format_gen_stats(stats, wrote=args.write))
        if not args.write:
            print("\n(dry-run — re-run with --write to persist)")
    elif args.command == "export":
        n = export_deviations_csv(args.csv, args.db)
        print(f"exported {n} deviation rows to {args.csv} (fill reason/outcome, then import)")
    elif args.command == "import":
        stats = import_decisions_csv(args.csv, args.db)
        print(f"imported {stats.updated} manual rows (skipped {stats.skipped_untagged} unknown ids)")
    elif args.command == "report":
        print(format_report(args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())

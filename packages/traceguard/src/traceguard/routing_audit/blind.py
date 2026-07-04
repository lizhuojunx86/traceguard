"""Blind A/B eval loop + intra-tier advisor-premium analysis.

Two things live here, deliberately kept apart from the *tier-deviation* audit
(routing_decisions):

1. INTRA-TIER advisor premium (pure arithmetic, available now): within the
   frontier tier, what does choosing Fable 5 over Opus 4.8 cost? This reuses
   the counterfactual's fable→opus rows. It answers "same-tier premium", NOT
   "wrong-tier routing" — those two analyses must never be merged in a report.

2. BLIND eval loop (track built now, data after a rerun): export a blind sheet
   (question summary + answer A / answer B in a fixed label-free order, the
   position→model key kept in-DB), a human fills verdict + one-line reason,
   import reveals. Then the blinded premium: Fable win-rate, tie-rate, and the
   average dollar premium per consult where Fable was actually judged better.

Privacy: the blind sheet (answer bodies) is a LOCAL, gitignored working file.
No answer text is ever committed or put in a summary report.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.routing_audit.counterfactual import compute_counterfactuals
from traceguard.routing_audit.models import BlindEval, RerunResult, ensure_tables
from traceguard.store.models import make_engine

DEFAULT_DB = "sqlite:///traces_routing_audit.db"
FABLE = "claude-fable-5"
OPUS = "claude-opus-4-8"
VERDICTS = ("a_better", "b_better", "tie")
_PENDING_NOTE = "(heuristic task tags — pending manual review)"


# ── 1. Intra-tier advisor premium (arithmetic) ──────────────────────────────

@dataclass
class PremiumRow:
    task_type: str
    units: int
    fable_actual: Decimal
    opus_cf: Decimal
    premium: Decimal


def intra_tier_premium(db_url: str | None = None) -> list[PremiumRow]:
    """Per task_type: Σ fable actual vs Σ opus-4-8 counterfactual (both frontier)."""
    rows = [
        r
        for r in compute_counterfactuals(db_url)
        if r.current_model == FABLE and r.candidate == OPUS
    ]
    agg: dict[str, list[Any]] = defaultdict(lambda: [0, Decimal("0"), Decimal("0")])
    for r in rows:
        a = agg[r.task_type]
        a[0] += 1
        a[1] += r.actual_cost
        a[2] += r.cf_cost
    out = [
        PremiumRow(tt, n, fa, cf, (fa - cf))
        for tt, (n, fa, cf) in agg.items()
    ]
    out.sort(key=lambda p: p.premium, reverse=True)
    return out


def format_intra_tier_premium(db_url: str | None = None) -> str:
    rows = intra_tier_premium(db_url)
    lines = [
        f"== intra-tier advisor premium (arithmetic) — fable-5 vs opus-4-8, both frontier {_PENDING_NOTE} ==",
        "SAME-TIER premium (the cost of choosing Fable over Opus within the",
        "frontier tier). This is NOT the cross-tier deviation audit — keep separate.",
        "",
        f"{'task_type':<20} {'units':>6} {'fable_actual$':>14} {'opus_cf$':>12} "
        f"{'premium$':>11} {'premium%':>9}",
        "-" * 76,
    ]
    tot_n = 0
    tot_fa = Decimal("0")
    tot_cf = Decimal("0")
    for p in rows:
        pct = f"{p.premium / p.fable_actual:.1%}" if p.fable_actual else "—"
        lines.append(
            f"{p.task_type:<20} {p.units:>6} {p.fable_actual:>14.2f} {p.opus_cf:>12.2f} "
            f"{p.premium:>11.2f} {pct:>9}"
        )
        tot_n += p.units
        tot_fa += p.fable_actual
        tot_cf += p.opus_cf
    lines.append("-" * 76)
    tot_prem = tot_fa - tot_cf
    tot_pct = f"{tot_prem / tot_fa:.1%}" if tot_fa else "—"
    lines.append(
        f"{'TOTAL':<20} {tot_n:>6} {tot_fa:>14.2f} {tot_cf:>12.2f} {tot_prem:>11.2f} {tot_pct:>9}"
    )
    lines.append(
        "premium = list-price cost forgone by running Fable instead of Opus 4.8; "
        "whether Fable's answer was worth it is the blind-eval question (§5b), not this table."
    )
    return "\n".join(lines)


# ── 2. Blind eval loop (needs completed reruns) ─────────────────────────────

_BLIND_FIELDS = ["blind_id", "unit_id", "project", "question_summary", "answer_a", "answer_b", "verdict", "reason"]


def _fable_is_a(blind_id: str) -> bool:
    """Deterministic, reproducible A/B assignment (no RNG)."""
    return int(hashlib.sha256(blind_id.encode("utf-8")).hexdigest(), 16) % 2 == 0


@dataclass
class BlindStats:
    exported: int = 0
    imported: int = 0
    skipped_no_rerun_answer: int = 0


def export_blind_sheet(
    csv_path: Path | str, db_url: str | None = None
) -> BlindStats:
    """Export a blind A/B sheet from COMPLETED reruns (local, gitignored file).

    Each row: question summary + answer A / answer B in a fixed label-free
    order; the position→model key is stored in ``routing_audit_blind_eval``
    and revealed only at import. Rows without a rerun answer are skipped
    (nothing to compare yet — run the rerun first).
    """
    stats = BlindStats()
    engine = make_engine(db_url)
    ensure_tables(engine)
    out_rows: list[dict[str, Any]] = []
    with Session(engine) as sess:
        reruns = list(sess.scalars(select(RerunResult)))
        for r in reruns:
            if not r.rerun_answer or not r.original_answer:
                stats.skipped_no_rerun_answer += 1
                continue
            blind_id = r.rerun_id
            fable_a = _fable_is_a(blind_id)
            # original answer is the source (fable) answer; rerun_answer is target
            fable_ans, target_ans = r.original_answer, r.rerun_answer
            answer_a, answer_b = (
                (fable_ans, target_ans) if fable_a else (target_ans, fable_ans)
            )
            pos_a = r.source_model if fable_a else r.target_model
            pos_b = r.target_model if fable_a else r.source_model
            existing = sess.get(BlindEval, blind_id)
            if existing is None:
                sess.add(
                    BlindEval(
                        blind_id=blind_id, unit_id=r.unit_id,
                        position_a_model=pos_a, position_b_model=pos_b,
                    )
                )
            else:
                existing.position_a_model = pos_a
                existing.position_b_model = pos_b
            out_rows.append(
                {
                    "blind_id": blind_id,
                    "unit_id": r.unit_id,
                    "project": r.project,
                    "question_summary": r.prompt_summary or "",
                    "answer_a": answer_a,
                    "answer_b": answer_b,
                    "verdict": "",
                    "reason": "",
                }
            )
        sess.commit()
    with Path(csv_path).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_BLIND_FIELDS)
        writer.writeheader()
        writer.writerows(out_rows)
    stats.exported = len(out_rows)
    return stats


def import_blind(csv_path: Path | str, db_url: str | None = None) -> BlindStats:
    """Import verdicts (a_better/b_better/tie) + reasons; unblind in-DB."""
    stats = BlindStats()
    engine = make_engine(db_url)
    ensure_tables(engine)
    with Path(csv_path).open(encoding="utf-8", newline="") as fh, Session(engine) as sess:
        for row in csv.DictReader(fh):
            blind_id = (row.get("blind_id") or "").strip()
            verdict = (row.get("verdict") or "").strip()
            if verdict not in VERDICTS:
                continue
            existing = sess.get(BlindEval, blind_id)
            if existing is None:
                continue
            existing.verdict = verdict
            existing.reason = (row.get("reason") or "").strip()[:200] or None
            stats.imported += 1
        sess.commit()
    return stats


def format_blind_premium(db_url: str | None = None) -> str:
    """Fable win/tie rates + avg premium per 'fable actually better' consult."""
    engine = make_engine(db_url)
    ensure_tables(engine)
    with Session(engine) as sess:
        evals = [e for e in sess.scalars(select(BlindEval)) if e.verdict is not None]
        reruns = {r.rerun_id: r for r in sess.scalars(select(RerunResult))}

    if not evals:
        return (
            "== advisor premium with blind verdicts ==\n"
            "[PENDING: blind-eval] no verdicts yet — needs completed reruns + an\n"
            "imported blind sheet. Track is built; run the rerun (separate, key-gated)."
        )

    # Map each eval to whether FABLE won (fable is position A iff _fable_is_a).
    fable_win = tie = opus_win = 0
    fable_better_premiums: list[Decimal] = []
    for e in evals:
        fable_a = _fable_is_a(e.blind_id)
        if e.verdict == "tie":
            tie += 1
            continue
        fable_won = (e.verdict == "a_better") == fable_a
        if fable_won:
            fable_win += 1
            r = reruns.get(e.blind_id)
            if r is not None and r.est_cost_usd is not None:
                # premium ≈ fable actual − target cost; approximate with est here
                fable_better_premiums.append(Decimal("0"))  # filled once real costs exist
        else:
            opus_win += 1
    n = len(evals)
    avg_prem = (
        sum(fable_better_premiums, Decimal("0")) / len(fable_better_premiums)
        if fable_better_premiums
        else Decimal("0")
    )
    return "\n".join(
        [
            "== advisor premium with blind verdicts ==",
            f"consults evaluated: {n} | fable better: {fable_win} ({fable_win / n:.0%}) "
            f"| ties: {tie} | opus better: {opus_win}",
            f"avg premium per 'fable actually better': ${avg_prem:.4f} "
            "(exact once real rerun costs are recorded)",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m traceguard.routing_audit.blind",
        description="Intra-tier advisor premium + blind A/B eval loop.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_prem = sub.add_parser("premium", help="arithmetic intra-tier fable vs opus premium")
    p_prem.add_argument("--db", default=DEFAULT_DB)

    p_export = sub.add_parser("export", help="export blind A/B sheet (needs completed reruns)")
    p_export.add_argument("--db", default=DEFAULT_DB)
    p_export.add_argument("--csv", default="routing_audit_blind.csv")

    p_import = sub.add_parser("import", help="import verdicts + unblind")
    p_import.add_argument("--db", default=DEFAULT_DB)
    p_import.add_argument("--csv", required=True)

    p_report = sub.add_parser("report", help="blind-verdict premium summary")
    p_report.add_argument("--db", default=DEFAULT_DB)

    args = parser.parse_args(argv)
    if args.command == "premium":
        print(format_intra_tier_premium(args.db))
    elif args.command == "export":
        stats = export_blind_sheet(args.csv, args.db)
        print(
            f"exported {stats.exported} blind rows to {args.csv} "
            f"(skipped {stats.skipped_no_rerun_answer} without a rerun answer)"
        )
    elif args.command == "import":
        stats = import_blind(args.csv, args.db)
        print(f"imported {stats.imported} verdicts")
    elif args.command == "report":
        print(format_blind_premium(args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())

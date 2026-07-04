"""Cost counterfactual engine — pure arithmetic, zero API calls.

Re-prices every ``(tagging unit, current model)`` group against cheaper
candidate models, holding the token composition fixed, to answer: *if the same
work had run on a cheaper model, what would it have cost?* This is a
COST-side hypothetical only. Whether quality would survive is a separate
question answered by the next step's rerun/labelling — so every figure here is
framed "**if quality holds**, save $X", never an unconditional "save $X".

Candidates per current model:
- always: Claude Sonnet 5 (both price eras — see below) and Claude Haiku 4.5.
- current == Claude Fable 5: additionally Claude Opus 4.8 (same "frontier"
  tier, half the price).
A candidate equal to the current model is skipped.

Sonnet 5 dual pricing (verified against the platform pricing page 2026-07-02),
reported side by side:
- introductory (through 2026-08-31): $2 / $10 per MTok in/out
- standard (from 2026-09-01):        $3 / $15 per MTok in/out
Cache multipliers are the standard 0.1× read / 1.25× 5m-write / 2× 1h-write on
input for every model.

Tokenizer conversion (pricing page: "Opus 4.7 and later, Fable 5, Mythos 5,
Sonnet 5 use a newer tokenizer … approximately 30% more tokens for the same
text; Sonnet 4.6 and earlier use the previous tokenizer"). Haiku 4.5 is NOT in
the newer-tokenizer list, so it uses the older one. Token counts are therefore
converted by tokenizer generation, not copied blindly:
- newer → newer (e.g. opus/fable → sonnet-5): ×1  (1:1)
- newer → Haiku 4.5 (older):                  ÷1.3 (per the ~30% note)
- Haiku 4.5 (older) → newer:                  ×1.3 (symmetric)
The task brief states "÷1.3 when the target is Haiku"; that is exactly the
newer→Haiku case (all frontier source models are newer-tokenizer). The
old→new ×1.3 case only arises for the handful of already-Haiku units and is
handled symmetrically so their numbers aren't silently wrong.

Unit attribution uses the DB's ``invoked_at`` only; the mutable source tree is
never re-read (see ingest_claude_code data caveats). The current cost is the
recorded list-price ``cost_usd`` (so a fast-mode unit's premium is part of its
baseline and shows up as extra headroom against a standard-speed candidate).

CLI::

    python -m traceguard.routing_audit.counterfactual matrix
    python -m traceguard.routing_audit.counterfactual top --n 10
    python -m traceguard.routing_audit.counterfactual candidates [--csv path]
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.routing_audit.ingest_claude_code import DEFAULT_SOURCE
from traceguard.routing_audit.models import ensure_tables
from traceguard.routing_audit.pricing import PRICES, ModelPrice
from traceguard.routing_audit.task_tags import (
    DEFAULT_GAP_MINUTES,
    iter_session_units,
    load_unit_index,
    redact_summary,
)
from traceguard.store.models import Trace, make_engine

DEFAULT_DB = "sqlite:///traces_routing_audit.db"
_MTOK = Decimal(1_000_000)
_COST_QUANTUM = Decimal("0.000001")
_HAIKU_DIVISOR = Decimal("1.3")
_PENDING_NOTE = "(heuristic task tags — pending manual review)"

# Candidate price sheets. Sonnet 5 appears twice (two price eras); haiku/opus
# reuse the observed-model prices so there is a single source of truth.
CANDIDATE_PRICES: dict[str, ModelPrice] = {
    "claude-sonnet-5-intro": ModelPrice(Decimal("2"), Decimal("10")),
    "claude-sonnet-5-standard": ModelPrice(Decimal("3"), Decimal("15")),
    "claude-haiku-4-5-20251001": PRICES["claude-haiku-4-5-20251001"],
    "claude-opus-4-8": PRICES["claude-opus-4-8"],
}
_ALWAYS = ("claude-sonnet-5-intro", "claude-sonnet-5-standard", "claude-haiku-4-5-20251001")


def parse_as_of(value: str | None) -> datetime | None:
    """Parse an ``--as-of`` freeze point: ISO date (→ end of that UTC day) or datetime."""
    if not value:
        return None
    from datetime import time, timezone

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"--as-of must be an ISO date/datetime, got {value!r}") from exc
    if dt.tzinfo is None:
        # a bare date means "through the end of that day"
        if len(value.strip()) <= 10:
            dt = datetime.combine(dt.date(), time.max)
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _base_model(candidate: str) -> str:
    """Strip the ``-intro``/``-standard`` price-era suffix for identity checks."""
    for suffix in ("-intro", "-standard"):
        if candidate.endswith(suffix):
            return candidate[: -len(suffix)]
    return candidate


def candidates_for(current_model: str) -> list[str]:
    cands = list(_ALWAYS)
    if current_model == "claude-fable-5":
        cands = ["claude-opus-4-8", *cands]
    return [c for c in cands if _base_model(c) != current_model]


def _tokenizer_gen(model_id: str) -> str:
    # Haiku 4.5 uses the previous tokenizer; opus-4.7+/fable/sonnet-5 the newer.
    return "old" if model_id.startswith("claude-haiku") else "new"


def token_factor(current_model: str, target_model: str) -> Decimal:
    cur, tgt = _tokenizer_gen(current_model), _tokenizer_gen(_base_model(target_model))
    if cur == tgt:
        return Decimal(1)
    return Decimal(1) / _HAIKU_DIVISOR if cur == "new" else _HAIKU_DIVISOR


def _tokens_of(usage: dict[str, Any] | None) -> dict[str, int]:
    if not usage:
        return {"base_input": 0, "cache_read": 0, "cache_5m": 0, "cache_1h": 0, "output": 0}
    c5 = usage.get("cache_creation_5m")
    c1 = usage.get("cache_creation_1h")
    if c5 is None and c1 is None:
        c5, c1 = int(usage.get("cache_creation_input_tokens") or 0), 0
    return {
        "base_input": int(usage.get("input_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "cache_5m": int(c5 or 0),
        "cache_1h": int(c1 or 0),
        "output": int(usage.get("output_tokens") or 0),
    }


def _price_tokens(price: ModelPrice, tokens: dict[str, int], factor: Decimal) -> Decimal:
    def scaled(kind: str) -> Decimal:
        return Decimal(tokens[kind]) * factor

    cost = (
        scaled("base_input") * price.input_per_mtok
        + scaled("cache_read") * price.input_per_mtok * price.cache_read_mult
        + scaled("cache_5m") * price.input_per_mtok * price.cache_write_5m_mult
        + scaled("cache_1h") * price.input_per_mtok * price.cache_write_1h_mult
        + scaled("output") * price.output_per_mtok
    ) / _MTOK
    return cost.quantize(_COST_QUANTUM)


@dataclass
class UnitModelAgg:
    unit_id: str
    task_type: str
    project: str
    current_model: str
    tokens: dict[str, int] = field(default_factory=lambda: {
        "base_input": 0, "cache_read": 0, "cache_5m": 0, "cache_1h": 0, "output": 0
    })
    actual_cost: Decimal = Decimal("0")
    n_traces: int = 0


def aggregate_unit_models(
    db_url: str | None = None, *, as_of: datetime | None = None
) -> list[UnitModelAgg]:
    """Sum token composition + actual cost per (unit, current model).

    ``as_of`` freezes the snapshot: only traces with ``invoked_at <= as_of``
    are counted (daily ingest keeps writing, but a pinned report stays stable).
    """
    engine = make_engine(db_url)
    ensure_tables(engine)
    index = load_unit_index(engine)
    aggs: dict[tuple[str, str], UnitModelAgg] = {}
    with Session(engine) as sess:
        stmt = select(
            Trace.output_parsed, Trace.invoked_at, Trace.project, Trace.model_id, Trace.cost_usd
        )
        if as_of is not None:
            stmt = stmt.where(Trace.invoked_at <= as_of)
        rows = sess.execute(stmt)
        for output_parsed, invoked_at, project, model_id, cost_usd in rows:
            if model_id is None:
                continue
            session_id = (output_parsed or {}).get("session_id")
            hit = index.lookup(session_id, invoked_at)
            if hit is None:
                continue
            unit_id, task_type, _proj = hit
            key = (unit_id, model_id)
            a = aggs.get(key)
            if a is None:
                a = UnitModelAgg(unit_id, task_type, project, model_id)
                aggs[key] = a
            for kind, val in _tokens_of((output_parsed or {}).get("usage")).items():
                a.tokens[kind] += val
            a.actual_cost += cost_usd or Decimal("0")
            a.n_traces += 1
    return list(aggs.values())


@dataclass
class CounterfactualRow:
    unit_id: str
    task_type: str
    project: str
    current_model: str
    candidate: str
    actual_cost: Decimal
    cf_cost: Decimal
    saving: Decimal


def compute_counterfactuals(
    db_url: str | None = None, *, as_of: datetime | None = None
) -> list[CounterfactualRow]:
    rows: list[CounterfactualRow] = []
    for a in aggregate_unit_models(db_url, as_of=as_of):
        for candidate in candidates_for(a.current_model):
            price = CANDIDATE_PRICES[candidate]
            factor = token_factor(a.current_model, candidate)
            cf_cost = _price_tokens(price, a.tokens, factor)
            rows.append(
                CounterfactualRow(
                    unit_id=a.unit_id,
                    task_type=a.task_type,
                    project=a.project,
                    current_model=a.current_model,
                    candidate=candidate,
                    actual_cost=a.actual_cost,
                    cf_cost=cf_cost,
                    saving=(a.actual_cost - cf_cost).quantize(_COST_QUANTUM),
                )
            )
    return rows


def format_matrix(db_url: str | None = None, *, as_of: datetime | None = None) -> str:
    """task_type × current_model → candidate potential-saving matrix."""
    rows = compute_counterfactuals(db_url, as_of=as_of)
    if not rows:
        return "no counterfactual rows — ingest + tag first."
    candidates = list(CANDIDATE_PRICES.keys())
    # (task_type, current_model) -> candidate -> Σ saving
    cells: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for r in rows:
        cells[(r.task_type, r.current_model)][r.candidate] += r.saving

    short = {
        "claude-sonnet-5-intro": "sonnet5-intro",
        "claude-sonnet-5-standard": "sonnet5-std",
        "claude-haiku-4-5-20251001": "haiku-4.5",
        "claude-opus-4-8": "opus-4.8",
    }
    header = f"{'task_type':<18} {'current_model':<16} " + " ".join(
        f"{short[c]:>14}" for c in candidates
    )
    lines = [
        f"== counterfactual saving matrix — IF QUALITY HOLDS {_PENDING_NOTE} ==",
        "values = Σ(actual − candidate) list-price cost per cell; blank = N/A "
        "(candidate == current or not offered)",
        "",
        header,
        "-" * len(header),
    ]
    for (task_type, current_model) in sorted(cells, key=lambda k: (k[0], k[1])):
        offered = cells[(task_type, current_model)]
        cols = " ".join(
            (f"{offered[c]:>14.2f}" if c in offered else f"{'—':>14}") for c in candidates
        )
        lines.append(f"{task_type:<18} {_short_model(current_model):<16} {cols}")
    lines.append("-" * len(header))
    lines.append(
        "reading: a positive value is the cost that WOULD be saved IF the "
        "cheaper model produced acceptable quality — quality is not assessed here."
    )
    return "\n".join(lines)


def _short_model(model_id: str) -> str:
    return model_id.replace("claude-", "").replace("-20251001", "")


def format_top(
    db_url: str | None = None, *, n: int = 10, as_of: datetime | None = None
) -> str:
    """Top-N (unit, candidate) rows by absolute saving — the rerun candidate pool."""
    rows = [r for r in compute_counterfactuals(db_url, as_of=as_of) if r.saving > 0]
    rows.sort(key=lambda r: r.saving, reverse=True)
    top = rows[:n]
    if not top:
        return "no positive-saving counterfactuals found."
    header = (
        f"{'unit_id':<28} {'task_type':<17} {'current':<10} {'candidate':<14} "
        f"{'actual$':>9} {'cf$':>9} {'save$':>9}"
    )
    lines = [
        f"== top {n} counterfactual savings — IF QUALITY HOLDS {_PENDING_NOTE} ==",
        header,
        "-" * len(header),
    ]
    for r in top:
        lines.append(
            f"{r.unit_id:<28} {r.task_type:<17} {_short_model(r.current_model):<10} "
            f"{_short_model(r.candidate):<14} {r.actual_cost:>9.3f} {r.cf_cost:>9.3f} "
            f"{r.saving:>9.3f}"
        )
    lines.append("-" * len(header))
    lines.append(
        "these are COST candidates only; the rerun/quality decision is the next step."
    )
    return "\n".join(lines)


# ── Task 4: quality-counterfactual candidate shortlist (prepare only) ──

# A unit's first "prompt" is only a real self-contained consult if it isn't a
# system-injected block (task-notification, local-command caveat, reminder) or
# a bare continuation/approval ("请继续", "同意", "开始 Phase 3"). Those are
# links in a tool chain, not standalone questions.
_CONFIRM_RE = re.compile(
    r"^(请?\s*继续|同意.{0,14}|批准.{0,14}|好的?|ok(ay)?|go|approved?|"
    r"开始.{0,24}|done|完成.{0,14}|\d+\s*已完成.{0,24})[。.!！\s]*$",
    re.IGNORECASE,
)


def _is_substantive_consult(text: str) -> bool:
    t = text.strip()
    if not t or t.startswith("<") or t.startswith("Caveat:") or "task-notification" in t[:40]:
        return False
    if len(t) < 8:
        return False
    return _CONFIRM_RE.match(t) is None


_CANDIDATE_FIELDS = [
    "unit_id",
    "project",
    "task_type",
    "n_turns",
    "current_model",
    "current_tokens_in",
    "current_tokens_out",
    "current_cost_usd",
    "summary",
]


def quality_candidates(
    db_url: str | None = None,
    source: Path | str = DEFAULT_SOURCE,
    *,
    gap_minutes: int = DEFAULT_GAP_MINUTES,
    max_turns: int = 8,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """Shortlist self-contained Fable units for a possible quality rerun.

    Heuristic filter (approximate — a human confirms self-containment and
    advisor-nature before any rerun): the unit used Fable 5 and is reasonably
    self-contained (``n_turns <= max_turns``). Advisor-tagged units
    (``decision-advisor``) sort first because they are the intended target,
    but the (still heuristic, pending-review) tags may misfile a real advisor
    consult — so other task_types are included and each row keeps its
    ``task_type`` for the human to judge. Within those groups, fewer turns
    (more self-contained) then higher cost rank first. NO API calls are made;
    summaries are redacted (≤100 chars).
    """
    aggs = [a for a in aggregate_unit_models(db_url) if a.current_model == "claude-fable-5"]
    fable_units = {a.unit_id: a for a in aggs}

    # Pull first-prompt + n_turns live from the source (redacted for the CSV).
    meta: dict[str, Any] = {}
    for unit in iter_session_units(Path(source).expanduser(), gap_minutes=gap_minutes):
        if unit.unit_id in fable_units:
            meta[unit.unit_id] = unit

    rows: list[dict[str, Any]] = []
    for unit_id, a in fable_units.items():
        u = meta.get(unit_id)
        if u is None or u.n_turns > max_turns or not _is_substantive_consult(u.first_prompt):
            continue
        rows.append(
            {
                "unit_id": unit_id,
                "project": a.project,
                "task_type": a.task_type,
                "n_turns": u.n_turns,
                "current_model": a.current_model,
                "current_tokens_in": a.tokens["base_input"]
                + a.tokens["cache_read"]
                + a.tokens["cache_5m"]
                + a.tokens["cache_1h"],
                "current_tokens_out": a.tokens["output"],
                "current_cost_usd": f"{a.actual_cost:.4f}",
                "summary": redact_summary(u.first_prompt),
            }
        )
    # advisor-tagged first, then fewer turns, then higher cost.
    rows.sort(
        key=lambda r: (
            r["task_type"] != "decision-advisor",
            r["n_turns"],
            -Decimal(r["current_cost_usd"]),
        )
    )
    return rows[:limit]


def format_candidates(rows: list[dict[str, Any]], csv_path: Path | str | None = None) -> str:
    if csv_path is not None:
        with Path(csv_path).open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=_CANDIDATE_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    lines = [
        f"== quality-counterfactual candidates (prepare only, NO reruns) {_PENDING_NOTE} ==",
        "filter: current=fable-5, self-contained (n_turns<=8), substantive first "
        "prompt (no continuations/system blocks); decision-advisor sorted first "
        "— all approximate, confirm advisor-nature + tool-independence by hand",
        f"{len(rows)} candidates" + (f" → {csv_path}" if csv_path else ""),
        "",
        f"{'unit_id':<28} {'project':<16} {'turns':>5} {'tok_in':>10} "
        f"{'tok_out':>8} {'cost$':>8}  summary",
        "-" * 100,
    ]
    for r in rows:
        lines.append(
            f"{r['unit_id']:<28} {r['project']:<16} {r['n_turns']:>5} "
            f"{r['current_tokens_in']:>10} {r['current_tokens_out']:>8} "
            f"{r['current_cost_usd']:>8}  {r['summary'][:48]}"
        )
    lines.append("-" * 100)
    lines.append("next step: pair this with the top-N cost savings and your CSV corrections to pick a rerun set.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m traceguard.routing_audit.counterfactual",
        description="Cost counterfactual engine (pure arithmetic, no API calls).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_matrix = sub.add_parser("matrix", help="task_type × current → candidate saving matrix")
    p_matrix.add_argument("--db", default=DEFAULT_DB)
    p_matrix.add_argument("--as-of", default=None, help="freeze: only traces invoked_at <= this")

    p_top = sub.add_parser("top", help="top-N units by potential saving")
    p_top.add_argument("--db", default=DEFAULT_DB)
    p_top.add_argument("--n", type=int, default=10)
    p_top.add_argument("--as-of", default=None)

    p_cand = sub.add_parser("candidates", help="shortlist self-contained fable advisor units")
    p_cand.add_argument("--db", default=DEFAULT_DB)
    p_cand.add_argument("--source", default=str(DEFAULT_SOURCE))
    p_cand.add_argument("--csv", default=None)
    p_cand.add_argument("--limit", type=int, default=15)

    args = parser.parse_args(argv)
    if args.command == "matrix":
        print(format_matrix(args.db, as_of=parse_as_of(args.as_of)))
    elif args.command == "top":
        print(format_top(args.db, n=args.n, as_of=parse_as_of(args.as_of)))
    elif args.command == "candidates":
        rows = quality_candidates(args.db, args.source, limit=args.limit)
        print(format_candidates(rows, args.csv))
    return 0


if __name__ == "__main__":
    sys.exit(main())

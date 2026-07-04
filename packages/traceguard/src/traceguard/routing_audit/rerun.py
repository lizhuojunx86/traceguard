"""Quality-rerun harness — builds the track, does not depart the station.

Replays self-contained advisor consults on a target model (default Opus 4.8)
so a later blind eval can weigh Fable's in-tier premium against a cheaper
frontier model. This module is SAFE BY DEFAULT: ``--dry-run`` (the default)
makes ZERO API calls — it only estimates per-consult cost and persists the
plan. A real run requires an explicit ``--execute`` plus an Anthropic key and
is intended to be triggered separately, after the candidate list is confirmed.

Candidate pool (no manual input needed): the task-4 self-contained Fable
shortlist (≤15) intersected with the counterfactual top-10 units, then topped
up from the shortlist to 10–12.

Safety valves:
- ``--dry-run`` default: estimate only, no calls.
- ``--max-cost`` hard ceiling (default $30): if the batch's estimated total
  exceeds it, the WHOLE batch is rejected (dry-run flags it; execute refuses).
- every rerun writes a ``routing_audit_ingest_log`` audit row, same as ingest.

Self-audit: a real rerun's own API call is itself instrumented into ``traces``
as ``project="traceguard", component="rerun-harness"`` — the audit tool's
overhead must be audited by the audit tool.

Privacy: consult prompts and answers are read from the (local) source and kept
in the local, gitignored DB only. No export, CSV, or report ever contains
answer bodies — only hashes, token counts, and costs.
"""
from __future__ import annotations

import argparse
import sys
import uuid as uuid_mod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from traceguard.routing_audit.counterfactual import (
    CANDIDATE_PRICES,
    quality_candidates,
)
from traceguard.routing_audit.ingest_claude_code import DEFAULT_SOURCE, _parse_ts
from traceguard.routing_audit.models import RerunResult, ensure_tables
from traceguard.routing_audit.pricing import PRICES
from traceguard.routing_audit.task_tags import _human_prompt, load_unit_index, redact_summary
from traceguard.sdk.normalizer import input_hash
from traceguard.store.models import make_engine

DEFAULT_DB = "sqlite:///traces_routing_audit.db"
DEFAULT_TARGET = "claude-opus-4-8"
DEFAULT_MAX_COST = Decimal("30")
SOURCE_MODEL = "claude-fable-5"
_POOL_MIN, _POOL_MAX = 10, 12


@dataclass
class RerunCandidate:
    unit_id: str
    session_id: str
    project: str
    task_type: str
    source_model: str
    ts_start: datetime
    ts_end: datetime | None


@dataclass
class RerunEstimate:
    cand: RerunCandidate
    prompt: str | None
    original_answer: str | None
    est_cost_usd: Decimal
    tokens_in: int
    tokens_out: int


@dataclass
class RerunStats:
    batch_id: str | None = None
    target_model: str = DEFAULT_TARGET
    candidates: int = 0
    with_prompt: int = 0
    est_total: Decimal = Decimal("0")
    max_cost: Decimal = DEFAULT_MAX_COST
    rejected: bool = False
    written: int = 0
    estimates: list[RerunEstimate] = field(default_factory=list)


def _batch_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"rerun-{stamp}-{uuid_mod.uuid4().hex[:6]}"


def select_candidates(
    db_url: str | None = None, source: Path | str = DEFAULT_SOURCE
) -> list[RerunCandidate]:
    """task-4 shortlist ∩ counterfactual top-10, topped up to 10–12 units."""
    shortlist = quality_candidates(db_url, source, limit=15)
    q_units = [r["unit_id"] for r in shortlist]
    src_model = {r["unit_id"]: r["current_model"] for r in shortlist}

    from traceguard.routing_audit.counterfactual import compute_counterfactuals

    cf = [r for r in compute_counterfactuals(db_url) if r.saving > 0]
    cf.sort(key=lambda r: r.saving, reverse=True)
    top_units: list[str] = []
    seen: set[str] = set()
    for r in cf:
        if r.unit_id not in seen:
            seen.add(r.unit_id)
            top_units.append(r.unit_id)
        if len(top_units) >= 10:
            break
    top_set = set(top_units)
    # intersection first (best of both signals), then the rest of the shortlist
    ordered = [u for u in q_units if u in top_set] + [u for u in q_units if u not in top_set]
    chosen_ids = ordered[:_POOL_MAX]

    # Resolve unit metadata from the task_tags windows.
    engine = make_engine(db_url)
    ensure_tables(engine)
    index = load_unit_index(engine)
    by_unit: dict[str, tuple[str, datetime, datetime | None, str]] = {}
    for session_id, spans in index.spans.items():
        for ts_start, ts_end, unit_id, _task, project in spans:
            by_unit[unit_id] = (session_id, ts_start, ts_end, project)
    # task_type from the shortlist rows
    task_by_unit = {r["unit_id"]: r["task_type"] for r in shortlist}

    out: list[RerunCandidate] = []
    for unit_id in chosen_ids:
        meta = by_unit.get(unit_id)
        if meta is None:
            continue
        session_id, ts_start, ts_end, project = meta
        out.append(
            RerunCandidate(
                unit_id=unit_id,
                session_id=session_id,
                project=project,
                task_type=task_by_unit.get(unit_id, "unknown"),
                source_model=src_model.get(unit_id, SOURCE_MODEL),
                ts_start=ts_start,
                ts_end=ts_end,
            )
        )
    return out


def _assistant_text(rec: dict[str, Any]) -> str | None:
    if rec.get("type") != "assistant":
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        if parts:
            return "\n".join(parts)
    return None


def extract_consult(
    cand: RerunCandidate, source_root: Path
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Read the first human prompt + next assistant answer (and its usage).

    Returns (prompt, original_answer, first_answer_usage); any may be None if
    the source file was rewritten away. The usage gives the original consult's
    real token footprint (context included), the realistic size of a
    self-contained replay. Bodies stay in memory / local DB only.
    """
    import json

    session_file = None
    if source_root.is_dir():
        for proj in source_root.iterdir():
            candidate = proj / f"{cand.session_id}.jsonl"
            if candidate.exists():
                session_file = candidate
                break
    if session_file is None:
        return None, None, None

    prompt: str | None = None
    answer: str | None = None
    usage: dict[str, Any] | None = None
    with session_file.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"user"' not in line and '"assistant"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            ts = _parse_ts(rec.get("timestamp"))
            if ts is None or ts < cand.ts_start:
                continue
            if cand.ts_end is not None and ts >= cand.ts_end:
                break
            if prompt is None:
                p = _human_prompt(rec)
                if p is not None:
                    prompt = p
                continue
            a = _assistant_text(rec)
            if a is not None:
                answer = a
                u = (rec.get("message") or {}).get("usage")
                usage = u if isinstance(u, dict) else None
                break
    return prompt, answer, usage


_MTOK = Decimal(1_000_000)
_CHARS_PER_TOKEN = Decimal("2.5")  # coarse mixed CN/EN estimate; see note


def _est_tokens(text: str | None) -> int:
    """Very rough token estimate from character count (mixed CN/EN).

    A replayed self-contained consult is a fresh single call — no cache reuse,
    no accumulated history — so its size is the prompt + expected answer, NOT
    the unit's aggregate token footprint. This char-based figure is only a
    magnitude; the real count is known after execution.
    """
    if not text:
        return 0
    return int(Decimal(len(text)) / _CHARS_PER_TOKEN)


def estimate_costs(
    candidates: list[RerunCandidate], target_model: str, db_url: str | None, source_root: Path
) -> list[RerunEstimate]:
    """Dry-run per-consult cost: a fresh single call on the target model.

    Sized from the original consult's real usage (input incl. the context the
    consult needed to be self-contained, output = its answer), billed at
    target rates with NO cache (a cold replay). Falls back to a char estimate
    of prompt+answer when the source usage is gone. Magnitude only — the true
    count is known after execution.
    """
    price = CANDIDATE_PRICES.get(target_model) or PRICES.get(target_model)
    estimates: list[RerunEstimate] = []
    for cand in candidates:
        prompt, answer, usage = extract_consult(cand, source_root)
        if usage is not None:
            tin = (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0)
            )
            tout = int(usage.get("output_tokens") or 0)
        else:
            tin = _est_tokens(prompt)
            tout = _est_tokens(answer) or 1500
        if price is None:
            est_cost = Decimal("0")
        else:
            est_cost = (
                (Decimal(tin) * price.input_per_mtok + Decimal(tout) * price.output_per_mtok)
                / _MTOK
            ).quantize(Decimal("0.000001"))
        estimates.append(
            RerunEstimate(
                cand=cand,
                prompt=prompt,
                original_answer=answer,
                est_cost_usd=est_cost,
                tokens_in=tin,
                tokens_out=tout,
            )
        )
    return estimates


def plan_reruns(
    db_url: str | None = None,
    source: Path | str = DEFAULT_SOURCE,
    *,
    target_model: str = DEFAULT_TARGET,
    max_cost: Decimal = DEFAULT_MAX_COST,
    write: bool = True,
) -> RerunStats:
    """Dry-run: select candidates, estimate cost, persist the plan. No API calls.

    If the estimated total exceeds ``max_cost`` the batch is flagged rejected
    and (when writing) rows are stored with status ``skipped`` so nothing looks
    ready to execute.
    """
    source_root = Path(source).expanduser()
    candidates = select_candidates(db_url, source_root)
    estimates = estimate_costs(candidates, target_model, db_url, source_root)
    stats = RerunStats(
        target_model=target_model, max_cost=max_cost, estimates=estimates,
        candidates=len(candidates),
        with_prompt=sum(1 for e in estimates if e.prompt is not None),
        est_total=sum((e.est_cost_usd for e in estimates), Decimal("0")),
    )
    stats.rejected = stats.est_total > max_cost
    if not write:
        return stats

    engine = make_engine(db_url)
    ensure_tables(engine)
    stats.batch_id = _batch_id()
    status = "skipped" if stats.rejected else "estimated"
    with Session(engine) as sess:
        for e in estimates:
            rerun_id = f"{e.cand.unit_id}#{target_model}"
            prompt_hash = input_hash(e.prompt) if e.prompt is not None else input_hash(None)
            existing = sess.get(RerunResult, rerun_id)
            values = dict(
                batch_id=stats.batch_id,
                unit_id=e.cand.unit_id,
                project=e.cand.project,
                task_type=e.cand.task_type,
                source_model=e.cand.source_model,
                target_model=target_model,
                prompt_hash=prompt_hash,
                prompt_summary=redact_summary(e.prompt, limit=100) if e.prompt else None,
                est_cost_usd=e.est_cost_usd,
                original_answer=e.original_answer,  # local-only
                status=status,
            )
            if existing is None:
                sess.add(RerunResult(rerun_id=rerun_id, **values))
            elif existing.status not in ("completed",):  # never clobber a real run
                for k, v in values.items():
                    setattr(existing, k, v)
            stats.written += 1
        sess.commit()
    return stats
    # NOTE: the routing_audit_ingest_log audit row (message_id → trace_id) is
    # written per rerun at EXECUTE time, alongside the self-instrumented
    # traceguard/rerun-harness trace — not during this dry-run plan.


def format_estimate_table(stats: RerunStats) -> str:
    verdict = (
        f"REJECTED — est ${stats.est_total:.2f} > cap ${stats.max_cost:.2f}"
        if stats.rejected
        else f"within cap (est ${stats.est_total:.2f} <= ${stats.max_cost:.2f})"
    )
    lines = [
        f"== rerun harness — DRY-RUN (no API calls) — {verdict} ==",
        f"target model: {stats.target_model} | candidates: {stats.candidates} "
        f"| with extractable prompt: {stats.with_prompt}",
        "",
        f"{'unit_id':<44} {'project':<15} {'task_type':<17} {'est_in':>9} "
        f"{'est_out':>8} {'est_cost$':>10}",
        "-" * 106,
    ]
    for e in sorted(stats.estimates, key=lambda x: x.est_cost_usd, reverse=True):
        flag = "" if e.prompt is not None else "  [no prompt]"
        lines.append(
            f"{e.cand.unit_id:<44} {e.cand.project:<15} {e.cand.task_type:<17} "
            f"{e.tokens_in:>9} {e.tokens_out:>8} {e.est_cost_usd:>10.4f}{flag}"
        )
    lines.append("-" * 106)
    lines.append(
        f"estimated batch total: ${stats.est_total:.4f} (cap ${stats.max_cost:.2f}). "
        "NO API calls were made. To execute, confirm the list and re-run with "
        "--execute (separate, key-gated)."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m traceguard.routing_audit.rerun",
        description="Quality-rerun harness (dry-run only by default; no API calls).",
    )
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--model", default=DEFAULT_TARGET, help="target model to rerun on")
    parser.add_argument("--max-cost", type=Decimal, default=DEFAULT_MAX_COST)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="ACTUALLY call the API (requires an Anthropic key). Not the default; "
        "run only after confirming the candidate list.",
    )
    args = parser.parse_args(argv)

    if args.execute:
        print(
            "refusing to execute: real reruns are gated to a separate, "
            "explicitly-confirmed step. This build ships the dry-run plan only."
        )
        return 2

    stats = plan_reruns(
        args.db, args.source, target_model=args.model, max_cost=args.max_cost, write=True
    )
    print(format_estimate_table(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())

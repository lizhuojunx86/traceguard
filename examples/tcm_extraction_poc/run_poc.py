#!/usr/bin/env python3
"""End-to-end PoC: generic reverse-calculation detection in traceguard.

Exercises all three framework extension points through traceguard's real
library surface (no mocks):

  * E2 — pluggable check registry (reverse_calc dispatched by config presence)
  * E1 — generic reverse_calc structural check (domain params from YAML)
  * E3 — audit-flag semantics (a suspicion is quarantined from pass_rate)

It uses a synthetic cross-batch yield signature as the positive case
(sigma ~= 0.022 pp, all hugging the 90.0% floor) and a natural-variance series
as the negative control. Runs structural-only (DEGRADED mode); no LLM required.

Usage:
    uv run python examples/tcm_extraction_poc/run_poc.py

Exits non-zero if any expectation is unmet, so it doubles as an integration
check. Domain knowledge (spec edges, the sigma_floor) lives entirely in
configs/examples/tcm_extraction.yaml — this script and guardian core contain no
domain-specific thresholds.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from guardian.core.config import load_pipeline
from guardian.core.guardian_node import evaluate
from guardian.core.step import StepOutput
from guardian.store.reader import TraceReader
from guardian.store.writer import TraceWriter

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs" / "examples" / "tcm_extraction.yaml"
PIPELINE = "tcm-extraction-qa"
STEP = "cross_batch_yield"
PRODUCT = "extract-alpha"

# Synthetic cross-batch yields: sigma ~= 0.022 pp, all hugging the 90.0 floor.
POSITIVE = [
    ("B1", 90.27),
    ("B2", 90.26),
    ("B3", 90.30),
    ("B4", 90.28),
    ("B5", 90.32),
    ("B6", 90.30),
]
# Natural-variance control: sigma ~1.4 pp -> not reverse-calculated.
NEGATIVE = [
    ("N20260201", 89.1),
    ("N20260202", 91.8),
    ("N20260203", 90.3),
    ("N20260204", 92.5),
    ("N20260205", 88.7),
    ("N20260206", 91.0),
]


def _payload(batch_id: str, yield_pct: float) -> str:
    return json.dumps(
        {"product": PRODUCT, "batch_id": batch_id, "yield_pct": yield_pct},
        ensure_ascii=False,
    )


def run_scenario(db_url: str, batches: list[tuple[str, float]]):
    """Seed all but the last batch as prior history, then evaluate the last."""
    writer = TraceWriter(db_url)
    for batch_id, y in batches[:-1]:
        writer.write(
            PIPELINE, STEP, action="pass", passed=True, score=1.0,
            issues=[], attempt=1, output_preview=_payload(batch_id, y),
        )

    last_id, last_y = batches[-1]
    reader = TraceReader(db_url)
    step_cfg = next(s for s in load_pipeline(str(CONFIG)).steps if s.name == STEP)
    output = StepOutput(
        step_name=STEP,
        output_data={"product": PRODUCT, "batch_id": last_id, "yield_pct": last_y},
    )
    # History-aware but stateless: the reader supplies prior batches.
    decision = evaluate(
        output, step_cfg.guardian,
        reader=reader, pipeline_name=PIPELINE, step_name=STEP,
    )
    writer.write(
        PIPELINE, STEP, action=decision.action, passed=decision.action == "pass",
        score=decision.score, issues=decision.issues, attempt=1,
        output_preview=_payload(last_id, last_y), flag_type=decision.flag_type,
    )
    stats = reader.get_step_stats(PIPELINE, STEP, days=3650)
    return decision, stats


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        print("=" * 68)
        print("traceguard PoC — generic reverse-calc (E1/E2/E3), DEGRADED")
        print("=" * 68)

        # --- positive: the 6-batch signature should fire a suspicion ---
        pos_db = f"sqlite:///{tmp}/positive.db"
        decision, stats = run_scenario(pos_db, POSITIVE)
        print("\n[POSITIVE] synthetic yields", [y for _, y in POSITIVE])
        print(f"  action      = {decision.action}")
        print(f"  flag_type   = {decision.flag_type}")
        print(f"  issues      = {decision.issues}")
        print(f"  stats       = total={stats['total']} "
              f"pass_rate={stats['pass_rate']} "
              f"suspicion_count={stats['suspicion_count']}")
        pos_ok = (
            decision.action == "alert"
            and decision.flag_type == "suspicion"
            and stats["suspicion_count"] == 1
            and stats["pass_rate"] == 1.0  # 5/5 standard, NOT 5/6 (E3 quarantine)
        )
        print(f"  EXPECT fire + pass_rate unpolluted -> {'PASS' if pos_ok else 'FAIL'}")
        ok = ok and pos_ok

        # --- negative control: natural variance should NOT fire ---
        neg_db = f"sqlite:///{tmp}/negative.db"
        decision, stats = run_scenario(neg_db, NEGATIVE)
        print("\n[NEGATIVE] natural-variance yields", [y for _, y in NEGATIVE])
        print(f"  action      = {decision.action}")
        print(f"  flag_type   = {decision.flag_type}")
        print(f"  stats       = total={stats['total']} "
              f"pass_rate={stats['pass_rate']} "
              f"suspicion_count={stats['suspicion_count']}")
        neg_ok = (
            decision.action == "pass"
            and decision.flag_type == "standard"
            and stats["suspicion_count"] == 0
        )
        print(f"  EXPECT no fire -> {'PASS' if neg_ok else 'FAIL'}")
        ok = ok and neg_ok

    print("\n" + "=" * 68)
    print(f"PoC RESULT: {'ALL PASS' if ok else 'FAILURE'}")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

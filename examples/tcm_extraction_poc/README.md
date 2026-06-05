# Extraction — Reverse-Calculation Detection PoC

A worked example of the generic **reverse-calculation** audit check running on
synthetic cross-batch data, exercising three framework extension points added
to traceguard core:

- **E1** — generic `reverse_calc` structural check (`guardian/validators/structural.py`,
  `ReverseCalcConfig` in `guardian/core/config.py`).
- **E2** — pluggable structural-check registry (`register_structural_check`,
  dispatch by config presence).
- **E3** — audit-flag semantics (`EvalTrace.flag_type`): a *suspicion* is
  `action=alert` / `passed=False` but is quarantined from `pass_rate` in
  `get_step_stats`.

All domain knowledge (spec edges, the expert-estimated `sigma_floor`) lives in
`configs/examples/tcm_extraction.yaml`. Guardian core contains **no** domain-specific
values — the same check serves any "threshold + self-reported metric" scenario.

## What it detects

A reported metric that is *too stable AND hugging a specification boundary*
across a rolling window — the signature of values back-solved to just clear a
threshold rather than independently measured. Here: cross-batch extraction
**yield** with σ ≈ 0.02 pp, all hugging the 90.0% lower spec limit, against a
natural spread (`sigma_floor`) of ~0.5 pp from a domain-expert estimate.

## Run

```bash
# In-process demo (positive signature + natural-variance control). DEGRADED — no LLM.
uv run python examples/tcm_extraction_poc/run_poc.py

# Full CLI path: seed prior batches, then check batch 6.
uv run python - <<'PY'
import json
from guardian.store.writer import TraceWriter
w = TraceWriter("sqlite:///poc.db")
for bid, y in [("B1",90.27),("B2",90.26),("B3",90.30),
               ("B4",90.28),("B5",90.32)]:
    w.write("tcm-extraction-qa","cross_batch_yield",action="pass",passed=True,
            score=1.0,issues=[],attempt=1,
            output_preview=json.dumps({"product":"extract-alpha","batch_id":bid,"yield_pct":y},
                                      ensure_ascii=False))
PY
uv run guardian check \
  --pipeline configs/examples/tcm_extraction.yaml \
  --step cross_batch_yield \
  --input examples/tcm_extraction_poc/batches/B6.json \
  --db sqlite:///poc.db
```

Expected: `action=alert`, an issue noting `reverse-calc suspicion ... sigma=0.0203
< floor/20=0.0250 ... within 1.0 of interval_low edge 90.0`. In `get_step_stats`,
`pass_rate` stays `1.0` (suspicion quarantined) with `suspicion_count=1`.

## Stateless × cross-batch

The check is history-aware but the guardian node stays stateless: prior batch
yields are read from eval_store via `TraceReader.query_traces` and the σ is
computed in memory. Prior scalars are recovered by parsing each trace's
`output_preview` JSON — see the E4 note in the PoC results report.

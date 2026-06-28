# TraceGuard dogfood — Phase 0 acceptance #7

A **real consumer** that writes **≥100 traces** on traceguard via `wrap_openai`,
to satisfy Phase 0 acceptance #7 (`TRACEGUARD_ROADMAP.md` §3.3) and to flush out
the adoption friction that only real use surfaces — the kind that produced the
`0.8.1` wrapper-deepcopy fix.

## Run it

```bash
cd packages/traceguard

# Real OpenAI (or any OpenAI-compatible endpoint):
OPENAI_API_KEY=sk-... uv run --with openai python ../../examples/dogfood/run_dogfood.py

# Local Ollama / LM Studio:
OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1 \
  DOGFOOD_MODEL=llama3.1 uv run --with openai python ../../examples/dogfood/run_dogfood.py

# Credential-free (canned model, real plumbing):
uv run python ../../examples/dogfood/run_dogfood.py
```

Knobs: `DOGFOOD_MODEL` (default `gpt-4o-mini`), `DOGFOOD_N` (default `120`),
`DOGFOOD_DB` (default `./dogfood_traces.db`, recreated each run — git-ignored).

The workload is a real task: classify the market sentiment (`bullish` /
`bearish` / `neutral`) of business headlines, one trace per call.

## Result — real run, 2026-06-28 (`gpt-4o-mini`, N=120)

| metric | value |
|---|---|
| traces written | **120** |
| parse success / failed | 120 / 0 |
| distinct `input_hash` | 120 (every call a unique point-in-time input) |
| `model_id` recorded | 120 / 120 |
| tokens in / out | 4 886 / 236 |
| latency ms (min / p50 / p95 / max) | 477 / 717 / 1 068 / 2 245 |
| label distribution | bearish 64, bullish 52, neutral 4 |
| `copy.copy(wrapped)` | OK |
| `copy.deepcopy(wrapped)` | `TypeError` — **identical to the raw client** (transparent; pre-0.8.1 this was `RecursionError`) |

✅ **Acceptance #7 met**: a real consumer wrote 120 real traces on traceguard.

## Adoption findings (the point of dogfooding)

1. **Copy transparency holds in the wild (0.8.1).** On a real httpx-backed
   `openai.OpenAI`, `copy.deepcopy` fails with the *same* `TypeError` as the raw
   client (the httpx `_thread.RLock` is not deep-copyable for anyone), and
   `copy.copy` works. The wrapper no longer *adds* a failure. Note this also
   means frameworks do not deep-copy a *live* client in normal operation — so
   the realistic integration path is direct `wrap_openai` instrumentation, which
   this harness exercises.

2. **Wrapper traces don't yet carry the point-in-time linkage.** `wrap_openai`
   records input/model/output/tokens, but the resulting rows have
   `feature_as_of = NULL` and no model-registry link, so a consumer using *only*
   the wrapper gets tracing but **not** traceguard's differentiator — the
   look-ahead invariants (SPEC §3, invariants 1–4) need `feature_as_of` + a
   registered model. Roadmap signal: give `wrap_openai` / `wrap_anthropic` an
   opt-in hook to stamp `feature_as_of` and a `model_id` registry lookup so
   instrumented calls are invariant-checkable. Tracked as dogfood follow-up.

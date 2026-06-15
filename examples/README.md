# TraceGuard examples

All examples are synthetic — no API keys, no network. Run them from the SDK
package directory so the editable install resolves:

```bash
cd packages/traceguard
uv run python ../../examples/<name>.py
```

(Each script also injects `packages/traceguard/src` onto `sys.path`, so
`uv run python examples/<name>.py` from the repo root works too.)

## Runnable demos

| File | Shows | Look-ahead kind |
|------|-------|-----------------|
| [quickstart/run_quickstart.py](quickstart/run_quickstart.py) | The full tour: registries, point-in-time `select_model`, prompt hashing, tracing, invariants 1 & 2 | Harness (kind 2) |
| [model_anachronism.py](model_anachronism.py) | `strict` mode blocks a 2026 model from touching 2021 data; loose mode flags `is_anachronistic` and warns | Harness (kind 2) |
| [prompt_drift.py](prompt_drift.py) | Two prompt versions → two hashes the tracer records; invariant 3 rejects a prompt used before its `introduced_at` | Harness (kind 2) |
| [anthropic_call.py](anthropic_call.py) | `wrap_anthropic` around a real-or-fake client; one traced call; invariant 2 | Harness (kind 2) |

## Stubs (illustrative — point at later phases)

| File | Sketches | Enabled by |
|------|----------|------------|
| [training_contamination.py](training_contamination.py) | The model *recalls* resolved events from pretraining — estimated, not refused | `traceguard[contamination]` (P4) |
| [loop_self_contamination.py](loop_self_contamination.py) | A self-improving loop cites its own output as fact; evidence-gating rejects it | `traceguard.loop` (P5) |

See [../docs/POSITIONING.md](../docs/POSITIONING.md) for the two-kinds-of-look-ahead
framing and [../docs/case-studies/fmp-revision.md](../docs/case-studies/fmp-revision.md)
for a real-world harness-leakage case study.

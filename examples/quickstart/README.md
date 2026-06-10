# Quickstart

A fully synthetic, self-contained tour of TraceGuard's core guarantees.
No API keys, no network, in-memory SQLite.

```bash
cd packages/traceguard
uv sync
uv run python ../../examples/quickstart/run_quickstart.py
```

What it demonstrates, in order:

1. **Model registry** — two models registered with `released_at` /
   `available_to_us_at` timestamps.
2. **Point-in-time selection** — `select_model(..., strict=True)` at a 2025
   backtest date returns the 2024 model and refuses to select anything at a
   2023 date where no model was available yet.
3. **Invariant 2 validator** — `validate_model_timing` raises
   `InvariantViolation` when a feature timestamped 2025 claims to use a model
   that only became available in 2026.
4. **Versioned prompts** — `load_prompt` reads a git-tracked YAML template and
   pins its content hash.
5. **Tracing** — `tracer.span()` records input hash, model id, prompt version,
   output, and perf as one row in the `traces` table.
6. **Invariant 1 validator** — `validate_feature_as_of` accepts a consistent
   feature timestamp and rejects a forward-dated one.

Expected output ends with:

```
quickstart OK — every guard fired exactly where it should.
```

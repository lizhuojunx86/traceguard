# traceguard

**Point-in-time correct LLM instrumentation — the time-integrity layer for
LLM pipelines.**

When you run LLMs over historical data — backtesting a signal, replaying a
pipeline, re-scoring an archive — TraceGuard makes it structurally hard to
accidentally use a model or prompt that did not yet exist at the point in
time you are simulating.

It is not a dashboard or a gateway; it is the lower layer that guarantees the
timeline underneath one. It interoperates with observability stacks
(Langfuse / Phoenix via the optional `traceguard[otel]` exporter) rather than
competing with them — see the
[OpenTelemetry → Langfuse/Phoenix guide](https://github.com/lizhuojunx86/traceguard/blob/main/docs/integrations/otel-langfuse-phoenix.md)
and [docs/POSITIONING.md](https://github.com/lizhuojunx86/traceguard/blob/main/docs/POSITIONING.md).

- `traceguard.registry.models` — model registry with `released_at` /
  `available_to_us_at`; `select_model(..., strict=...)` with mandatory
  explicit mode (no default), so anachronistic choices fail loudly.
- `traceguard.registry.prompts` — git-tracked YAML prompt templates;
  `load_prompt` pins the content hash into every trace.
- `traceguard.sdk.tracer` — `@tracer.trace` decorator and `tracer.span()`
  context manager recording input hash, model/prompt versions, output, and
  perf into SQLAlchemy (SQLite by default).
- `traceguard.sdk.normalizer` — the single canonical `normalize_input` /
  `input_hash` (sorted keys, fixed float precision, normalized whitespace).
- `traceguard.sdk.wrappers.anthropic` — `wrap_anthropic` auto-instruments an
  Anthropic SDK client (extra: `traceguard[anthropic]`).
- `traceguard.sdk.wrappers.openai` — `wrap_openai` auto-instruments an OpenAI
  SDK client's `chat.completions` and `responses` calls (extra:
  `traceguard[openai]`).
- `traceguard.validators.lookahead` — pure-function invariant validators
  (`validate_feature_as_of`, `validate_model_timing`,
  `validate_reference_timing`) that raise `InvariantViolation`; call them in
  pytest/CI.

## Install

```bash
pip install traceguard
```

Requires Python 3.11+. Optional extras:
`pip install "traceguard[anthropic]"` / `pip install "traceguard[openai]"`
(Anthropic / OpenAI client wrappers) and
`pip install "traceguard[otel]"` (OpenTelemetry / OpenInference export to
Langfuse, Phoenix, or any OTLP backend).

## Example

```python
from datetime import datetime, timezone
from traceguard.registry.models import register_model, select_model
from traceguard.store.models import make_engine

engine = make_engine("sqlite:///traceguard.db")

register_model("demo-llm-2024", model_family="internal-ml",
               capability_class="general-llm",
               released_at=datetime(2024, 1, 10, tzinfo=timezone.utc),
               available_to_us_at=datetime(2024, 2, 1, tzinfo=timezone.utc),
               engine=engine)

# Backtesting as of mid-2025: models that arrived later are invisible.
model_id = select_model("general-llm",
                        available_at=datetime(2025, 6, 30, tzinfo=timezone.utc),
                        strict=True, engine=engine)
```

A complete runnable tour (synthetic data, no API keys) lives in
[examples/quickstart](https://github.com/lizhuojunx86/traceguard/tree/main/examples/quickstart).

## Contract

The binding interface contract — table schemas, SDK signatures, the four
look-ahead invariants, SemVer rules — is in
[docs/SPEC.md](https://github.com/lizhuojunx86/traceguard/blob/main/docs/SPEC.md).

Phase 0 scope: tracer, model/prompt registries, normalizer, invariants 1–3,
Anthropic + OpenAI wrappers. Not yet: drift checks, replay sets (invariant 4),
CLI, Postgres/TimescaleDB, Voyage wrapper — see
[TRACEGUARD_ROADMAP.md](https://github.com/lizhuojunx86/traceguard/blob/main/TRACEGUARD_ROADMAP.md).

## Development

```bash
cd packages/traceguard
uv sync
uv run pytest        # 136 tests
```

## License

Apache-2.0.

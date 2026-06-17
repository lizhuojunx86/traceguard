# Sending TraceGuard traces to Langfuse / Phoenix (or any OTLP backend)

> **Keep your dashboard. TraceGuard guarantees the trace you feed it is honest
> *in time*.** TraceGuard is not a dashboard, a UI, or a hosted service — it is
> the time-integrity layer that sits *underneath* one. Langfuse, Phoenix, and
> LangSmith answer *"what happened, and how much did it cost?"*. TraceGuard
> answers a different, lower-level question — *"could this have happened at the
> point in time you're simulating?"* — and then hands the answer up to your
> dashboard as ordinary OpenTelemetry spans. You interoperate; you don't
> migrate. See [../POSITIONING.md](../POSITIONING.md) for the full framing.

This is a concrete how-to: after you wire up the OpenTelemetry exporter, each
traced call flows into Langfuse / Phoenix / any OTLP collector as a span
carrying the point-in-time attributes that make look-ahead auditing possible —
`input_hash`, the model's `available_to_us_at`, the pinned prompt hash, and the
`feature_as_of` the run is simulating.

## Install

The exporter lives behind the `otel` extra (it stays out of the core install so
TraceGuard's runtime dependency footprint remains SQLAlchemy + Pydantic + PyYAML):

```bash
pip install "traceguard[otel]"
```

That pulls `opentelemetry-api`, `opentelemetry-sdk`, and the OTLP/HTTP exporter
(`opentelemetry-exporter-otlp-proto-http`). The SQLite/SQLAlchemy store stays
the **source of truth** (SPEC §6.1); OTel export is purely additive.

## Two ways to export

| Path | Call | When |
|------|------|------|
| **Real-time dual-write** | `tracer.enable_otel(...)` | Live runs — emit one OTLP span as each trace closes, alongside the SQLite write |
| **Batch / backfill** | `export_traces(rows, ...)` | Ship history you already recorded in SQLite up to a freshly-connected dashboard |

Both produce **byte-identical** spans for the same row, so you can mix them and
deduplicate downstream on the stable `traceguard.trace_id` attribute (each path
mints fresh OTel span ids).

### Real-time dual-write

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from traceguard.sdk.tracer import tracer  # the module-level default tracer

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint="...")))

tracer.enable_otel(
    tracer_provider=provider,
    # internal model_id -> the vendor name your dashboard should display
    model_name_map={"earnings-llm-2024": "gpt-4o"},
)
# every tracer.span(...) / @tracer.trace now ALSO emits an OTLP span on close
```

`enable_otel` mutates the tracer in place (like `configure`), so the
module-level singleton and any already-bound `@tracer.trace` decorators start
dual-writing without re-instantiation. It is idempotent (call again to
reconfigure) and `disable_otel()` restores the default-off path. Emitter
failures are **isolated** — a broken or slow collector can never break tracing,
the SQLite write, or your business call.

> **Production note:** prefer `BatchSpanProcessor` (async/queued). With
> `SimpleSpanProcessor` the OTLP send runs synchronously on the traced call's
> exit, so a slow collector adds latency to your call (the error is still
> isolated, just not the latency).

### Batch / backfill

```python
from traceguard.exporters.otel import export_traces

# `rows` is any iterable of traceguard Trace rows read back from the store.
n = export_traces(
    rows,
    tracer_provider=provider,
    engine=engine,                       # enables the available_to_us_at enrichment
    model_name_map={"earnings-llm-2024": "gpt-4o"},
)
```

`export_traces` prefetches model availability in a single query for the whole
batch. For a single row there is also `export_trace(trace, ...)`.

## Run it offline first (no key, no network)

Before pointing at a real backend, prove the span shape with the console
exporter. The full, self-checking script is
[`examples/otel_console_export.py`](../../examples/otel_console_export.py); its core:

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from traceguard.sdk.tracer import Tracer

provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

tracer = Tracer(engine)
tracer.enable_otel(tracer_provider=provider,
                   model_name_map={"earnings-llm-2024": "gpt-4o"})

with tracer.span("earnings-backtest", "scorer", "llm_score",
                 correlation_id="AAPL-2024Q2", feature_as_of=backtest_date) as span:
    span.record_input({"ticker": "AAPL", "release": "2024 Q2 earnings"})
    span.record_model_prompt(model_id="earnings-llm-2024",
                             prompt_template_id="earnings/scorer/v3",
                             prompt_template_hash="sha256:3f1c0de")
    span.record_output(parsed={"signal": "long", "score": 0.71})
    span.record_perf(latency_ms=83, tokens_in=210, tokens_out=14)
```

Run it:

```bash
cd packages/traceguard && uv run python ../../examples/otel_console_export.py
```

The console prints one span whose attributes include:

```text
gen_ai.request.model                = 'gpt-4o'
traceguard.model_id                 = 'earnings-llm-2024'
traceguard.model.available_to_us_at = '2024-02-01T00:00:00+00:00'
traceguard.feature_as_of            = '2024-06-30T00:00:00+00:00'
traceguard.input_hash               = '78eb93bc...e8ef5eef1'
traceguard.prompt_template_hash     = 'sha256:3f1c0de'
openinference.span.kind             = 'LLM'
```

## Point it at Langfuse / Phoenix

The only change from the offline snippet is the exporter — swap
`ConsoleSpanExporter` for the OTLP exporter and give it your backend's endpoint
and auth (never hard-code secrets; read them from the environment):

```python
import os
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# Langfuse (cloud): OTLP/HTTP ingest, Basic auth = base64("<public>:<secret>")
exporter = OTLPSpanExporter(
    endpoint="https://cloud.langfuse.com/api/public/otel/v1/traces",
    headers={"Authorization": f"Basic {os.environ['LANGFUSE_BASIC_AUTH']}"},
)

# Phoenix (local): OpenInference-native, no auth by default
# exporter = OTLPSpanExporter(endpoint="http://localhost:6006/v1/traces")

provider.add_span_processor(BatchSpanProcessor(exporter))
```

Endpoints and auth are backend-specific — check your Langfuse project settings
or Phoenix deployment for the exact OTLP URL and headers. TraceGuard emits
standard OTLP; anything that speaks OTLP/HTTP receives it unchanged.

## What lands in the dashboard

Each span mixes three vocabularies so both generic and TraceGuard-specific
consumers get what they need:

| Attribute | Vocabulary | What the dashboard does with it |
|-----------|-----------|---------------------------------|
| `openinference.span.kind` = `"LLM"` | OpenInference | **Phoenix** renders the span as an LLM call |
| `gen_ai.request.model` | GenAI semconv | Native **model** column/label (the vendor name from `model_name_map`) |
| `gen_ai.usage.input_tokens` / `output_tokens` | GenAI semconv | Native **token usage** columns |
| `traceguard.model_id` | `traceguard.*` | The internal model id, always preserved alongside the vendor name |
| `traceguard.model.available_to_us_at` | `traceguard.*` | **When you could first call that model** — the single most decision-relevant fact for look-ahead auditing |
| `traceguard.feature_as_of` | `traceguard.*` | The point in time the run is simulating |
| `traceguard.input_hash` | `traceguard.*` | Canonical input fingerprint — group/diff identical inputs across runs |
| `traceguard.prompt_template_id` / `traceguard.prompt_template_hash` | `traceguard.*` | The exact prompt version, pinned into the trace |
| `traceguard.trace_id` | `traceguard.*` | Stable id for **deduplication** if you both dual-write and batch-export |
| `traceguard.correlation_id`, `traceguard.project`, `traceguard.component` | `traceguard.*` | Group spans by run / pipeline stage |

The standard `gen_ai.*` fields populate the dashboard's native model and
token-usage views, so cost and latency look exactly as you expect. The
`traceguard.*` fields arrive as custom span attributes (Langfuse surfaces them
as span metadata; Phoenix shows them in the span's attributes panel) — which is
where the time-integrity story lives. The pair that does the real work is
`traceguard.feature_as_of` next to `traceguard.model.available_to_us_at`: with
both on the span, you (or a saved dashboard view / filter) can spot a model that
was used *before it was available to you* — the anachronism a normal
cost-and-latency view renders invisible.

Span timing is derived from the trace's `invoked_at` and `latency_ms`, so span
duration reflects the real call; errors map to `Status(ERROR)` plus
`exception.type` / `exception.message`.

## What this does and doesn't replace

- **Does:** make the spans your dashboard already understands carry the
  point-in-time provenance it otherwise has no way to know.
- **Doesn't:** replace your dashboard, store, or eval harness. SQLite stays the
  source of truth; the dashboard stays your dashboard. TraceGuard just guarantees
  the timeline underneath both.

For the structural side of look-ahead — refusing an anachronistic model or a
not-yet-existing prompt *before* the call — see the registries and invariants in
[../SPEC.md](../SPEC.md) §§4–5. This exporter is how the resulting time-correct
traces become visible in the tools you already run.

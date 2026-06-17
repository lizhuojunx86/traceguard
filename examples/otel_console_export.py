"""Example: export a TraceGuard trace as an OpenTelemetry span.

This shows the *interoperate, not compete* path (look-ahead kind 2 tooling, see
docs/POSITIONING.md): you keep your dashboard — Langfuse, Phoenix, or any OTLP
backend — and TraceGuard guarantees the trace you feed it is honest *in time*.
One traced call becomes one OTLP span carrying the point-in-time attributes that
matter for look-ahead auditing:

  * traceguard.input_hash                  — canonical input fingerprint
  * gen_ai.request.model                   — the vendor model name (dashboard label)
  * traceguard.model_id                    — the internal id (always preserved)
  * traceguard.model.available_to_us_at    — when we could first call that model
  * traceguard.feature_as_of               — the moment the run is simulating
  * traceguard.prompt_template_hash        — the exact prompt version, pinned

Here the span is printed to the console (offline). To ship it to Langfuse /
Phoenix, swap ConsoleSpanExporter for the OTLP exporter from the `otel` extra —
see docs/integrations/otel-langfuse-phoenix.md.

Everything here is synthetic — no API keys, no network, in-memory SQLite. It
needs only `opentelemetry-sdk` (the dev group has it; end users get it via
`pip install "traceguard[otel]"`).

Run (from the repo root)::

    uv run python examples/otel_console_export.py

or, from the SDK package::

    cd packages/traceguard && uv run python ../../examples/otel_console_export.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# Workaround: some Python builds (e.g. Homebrew 3.14) skip _-prefixed .pth
# files, breaking uv's editable install. Add the SDK source dir directly so the
# demo runs regardless of how the env was set up.
_PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "traceguard" / "src"
if _PKG_SRC.is_dir() and str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import (  # noqa: E402
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from traceguard.registry.models import register_model  # noqa: E402
from traceguard.sdk.tracer import Tracer  # noqa: E402
from traceguard.store.models import make_engine  # noqa: E402

UTC = timezone.utc


def main() -> int:
    engine = make_engine("sqlite:///:memory:")

    # A model we could only call from 2024-02 onward. `available_to_us_at` is the
    # fact look-ahead auditing turns on — it rides into the span unchanged.
    register_model(
        "earnings-llm-2024",
        model_family="internal-ml",
        capability_class="general-llm",
        released_at=datetime(2024, 1, 10, tzinfo=UTC),
        available_to_us_at=datetime(2024, 2, 1, tzinfo=UTC),
        engine=engine,
    )

    # An OTel provider with two exporters: Console prints each span for humans;
    # InMemory captures it so this demo can self-check the attributes. In
    # production you would register an OTLP exporter pointed at Langfuse/Phoenix.
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    memory = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(memory))

    # Opt in to real-time dual-write. `model_name_map` translates the internal
    # model_id to the vendor name your dashboard should display; the internal id
    # is still preserved under `traceguard.model_id`.
    tracer = Tracer(engine)
    tracer.enable_otel(
        tracer_provider=provider,
        model_name_map={"earnings-llm-2024": "gpt-4o"},
    )

    # One traced call, as of a mid-2024 backtest date. On span close TraceGuard
    # writes the SQLite row (source of truth) AND emits the OTLP span.
    backtest_date = datetime(2024, 6, 30, tzinfo=UTC)
    with tracer.span(
        "earnings-backtest",
        "scorer",
        "llm_score",
        correlation_id="AAPL-2024Q2",
        feature_as_of=backtest_date,
    ) as span:
        span.record_input({"ticker": "AAPL", "release": "2024 Q2 earnings"})
        span.record_model_prompt(
            model_id="earnings-llm-2024",
            prompt_template_id="earnings/scorer/v3",
            prompt_template_hash="sha256:3f1c0de",  # synthetic; the registry pins the real one
        )
        span.record_output(parsed={"signal": "long", "score": 0.71})
        span.record_perf(latency_ms=83, tokens_in=210, tokens_out=14)

    # Inspect the emitted span and assert the PIT attributes survived the trip.
    spans = memory.get_finished_spans()
    assert len(spans) == 1, f"expected one span, got {len(spans)}"
    attrs = dict(spans[0].attributes)
    print("\nEmitted span:", spans[0].name)
    for key in (
        "traceguard.input_hash",
        "gen_ai.request.model",
        "traceguard.model_id",
        "traceguard.model.available_to_us_at",
        "traceguard.feature_as_of",
        "traceguard.prompt_template_hash",
        "openinference.span.kind",
    ):
        print(f"  {key} = {attrs.get(key)!r}")

    # The two facts that make this trace time-honest:
    assert attrs["gen_ai.request.model"] == "gpt-4o", attrs.get("gen_ai.request.model")
    assert attrs["traceguard.model_id"] == "earnings-llm-2024"
    assert attrs["traceguard.model.available_to_us_at"] == "2024-02-01T00:00:00+00:00"
    assert attrs["traceguard.feature_as_of"] == "2024-06-30T00:00:00+00:00"
    assert attrs["traceguard.input_hash"], "input_hash must be set"
    assert attrs["openinference.span.kind"] == "LLM"

    print(
        "\notel_console_export OK — the span carries available_to_us_at and "
        "feature_as_of, so a dashboard can audit time-integrity, not just cost."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Smoke tests for the OpenTelemetry / OpenInference exporter (extra: otel).

Uses the SDK's in-memory exporter so no network/collector is needed. Verifies a
span is emitted and the time-integrity attributes (input_hash, model version +
available_to_us_at, prompt hash, feature_as_of) all land on it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("opentelemetry")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from traceguard.exporters.otel import (  # noqa: E402
    export_trace,
    export_traces,
    trace_to_attributes,
)
from traceguard.registry.models import register_model  # noqa: E402
from traceguard.sdk.tracer import Tracer  # noqa: E402
from traceguard.store.models import Trace  # noqa: E402

UTC = timezone.utc
MODEL_ID = "demo-llm-2024"


@pytest.fixture
def otel():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _register(engine) -> None:
    register_model(
        MODEL_ID,
        model_family="internal-ml",
        capability_class="general-llm",
        released_at=datetime(2024, 1, 1, tzinfo=UTC),
        available_to_us_at=datetime(2024, 2, 1, tzinfo=UTC),
        engine=engine,
    )


def _write_ok_trace(engine) -> Trace:
    _register(engine)
    tracer = Tracer(engine)
    with tracer.span(
        "proj",
        "comp",
        "llm_complete",
        correlation_id="c1",
        feature_as_of=datetime(2024, 6, 1, tzinfo=UTC),
    ) as span:
        span.record_input({"text": "hello"})
        span.record_model_prompt(
            model_id=MODEL_ID,
            prompt_template_id="proj/comp/v1",
            prompt_template_hash="deadbeef",
        )
        span.record_output(parsed={"ok": True}, parse_status="success")
        span.record_perf(latency_ms=42, tokens_in=10, tokens_out=5, cost_usd=0.001)
    with Session(engine) as sess:
        return sess.execute(select(Trace).order_by(Trace.trace_id)).scalars().all()[-1]


def _write_error_trace(engine) -> Trace:
    _register(engine)
    tracer = Tracer(engine)
    try:
        with tracer.span("proj", "comp", "llm_complete") as span:
            span.record_input({"text": "hello"})
            span.record_model_prompt(model_id=MODEL_ID)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with Session(engine) as sess:
        return sess.execute(select(Trace).order_by(Trace.trace_id)).scalars().all()[-1]


def test_export_trace_carries_time_integrity_attributes(engine, otel):
    provider, exporter = otel
    trace = _write_ok_trace(engine)

    export_trace(trace, tracer_provider=provider, engine=engine)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    s = spans[0]
    a = s.attributes

    assert s.name == "llm_complete"
    assert a["openinference.span.kind"] == "LLM"
    # model version + availability — the decision-relevant pair
    assert a["gen_ai.request.model"] == MODEL_ID
    assert a["traceguard.model.available_to_us_at"].startswith("2024-02-01")
    # input hash (64 hex) and prompt hash
    assert len(a["traceguard.input_hash"]) == 64
    assert a["traceguard.prompt_template_hash"] == "deadbeef"
    # business as-of time
    assert a["traceguard.feature_as_of"].startswith("2024-06-01")
    # gen_ai usage + perf
    assert a["gen_ai.usage.input_tokens"] == 10
    assert a["gen_ai.usage.output_tokens"] == 5
    assert s.status.status_code.name == "OK"
    # latency maps to span duration (42 ms -> 42_000_000 ns)
    assert s.end_time - s.start_time == 42 * 1_000_000


def test_export_error_trace_sets_error_status(engine, otel):
    provider, exporter = otel
    trace = _write_error_trace(engine)

    export_trace(trace, tracer_provider=provider, engine=engine)

    s = exporter.get_finished_spans()[0]
    assert s.status.status_code.name == "ERROR"
    assert s.attributes["exception.type"] == "RuntimeError"
    assert s.attributes["traceguard.parse_status"] == "failed"


def test_export_traces_returns_count(engine, otel):
    provider, exporter = otel
    trace = _write_ok_trace(engine)

    n = export_traces([trace, trace], tracer_provider=provider, engine=engine)

    assert n == 2
    assert len(exporter.get_finished_spans()) == 2


def test_attributes_omit_none_and_availability_without_engine(engine):
    trace = _write_ok_trace(engine)

    # No engine -> availability cannot be looked up, attribute omitted.
    attrs = trace_to_attributes(trace)
    assert "traceguard.model.available_to_us_at" not in attrs
    # OTel forbids None attribute values.
    assert all(v is not None for v in attrs.values())

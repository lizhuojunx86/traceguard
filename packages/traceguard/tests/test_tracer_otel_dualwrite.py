"""Real-OTLP tests for OTel dual-write (extra: otel).

Uses the SDK's in-memory exporter so no collector is needed. Verifies that
enabling dual-write emits one span per trace at close time, that the span is
byte-identical to what the batch export_trace would produce for the same row,
and that error traces and disable() behave correctly. Pure isolation/degradation
is covered (without a real SDK) in test_tracer_otel_isolation.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

pytest.importorskip("opentelemetry")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from traceguard.exporters.otel import export_trace  # noqa: E402
from traceguard.registry.models import register_model  # noqa: E402
from traceguard.sdk.tracer import Tracer  # noqa: E402
from traceguard.store.models import Trace  # noqa: E402

UTC = timezone.utc
MODEL_ID = "demo-llm-2024"
VENDOR_NAME = "gpt-4o-2024-08-06"


def _provider():
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


def _run_ok_span(tracer) -> None:
    with tracer.span(
        "proj", "comp", "llm_complete",
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


def test_dual_write_emits_span_with_time_integrity_attributes(engine):
    provider, exporter = _provider()
    _register(engine)
    tracer = Tracer(engine=engine)
    tracer.enable_otel(tracer_provider=provider, model_name_map={MODEL_ID: VENDOR_NAME})

    _run_ok_span(tracer)

    # SQLite write still happened.
    with Session(engine) as sess:
        assert len(sess.scalars(select(Trace)).all()) == 1

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    a = spans[0].attributes
    assert spans[0].name == "llm_complete"
    assert a["openinference.span.kind"] == "LLM"
    # Plan-A vendor mapping at emit time; internal id preserved separately.
    assert a["gen_ai.request.model"] == VENDOR_NAME
    assert a["traceguard.model_id"] == MODEL_ID
    assert a["traceguard.model.available_to_us_at"].startswith("2024-02-01")
    assert len(a["traceguard.input_hash"]) == 64
    assert a["traceguard.prompt_template_hash"] == "deadbeef"
    assert a["traceguard.feature_as_of"].startswith("2024-06-01")
    assert a["gen_ai.usage.input_tokens"] == 10
    assert spans[0].status.status_code.name == "OK"
    # latency reconstructed as span duration (42 ms -> 42_000_000 ns)
    assert spans[0].end_time - spans[0].start_time == 42 * 1_000_000
    # trace_id present as the downstream dedup key
    assert a["traceguard.trace_id"] == 1


def test_dual_write_span_matches_batch_export(engine):
    """Parity: the live span equals what export_trace produces for the same row."""
    live_provider, live_exporter = _provider()
    batch_provider, batch_exporter = _provider()
    _register(engine)
    tracer = Tracer(engine=engine)
    tracer.enable_otel(tracer_provider=live_provider, model_name_map={MODEL_ID: VENDOR_NAME})

    _run_ok_span(tracer)

    # Batch-export the persisted row with the same vendor mapping.
    with Session(engine) as sess:
        row = sess.scalars(select(Trace).order_by(Trace.trace_id)).all()[-1]
    export_trace(row, tracer_provider=batch_provider, engine=engine, model_name=VENDOR_NAME)

    live = live_exporter.get_finished_spans()[0]
    batch = batch_exporter.get_finished_spans()[0]
    assert dict(live.attributes) == dict(batch.attributes)
    assert live.name == batch.name
    assert live.end_time - live.start_time == batch.end_time - batch.start_time
    assert live.start_time == batch.start_time
    assert live.status.status_code == batch.status.status_code


def test_parity_holds_across_db_normalization_boundaries(engine):
    """Parity must survive the DB round-trip: non-UTC tz and >6dp cost.

    The committed row normalizes feature_as_of to UTC (UTCDateTime) and truncates
    cost_usd to Numeric(12, 6). The dual-write snapshot must reflect those same
    normalized values, or the live span would diverge from a batch export_trace.
    """
    live_provider, live_exporter = _provider()
    batch_provider, batch_exporter = _provider()
    _register(engine)
    tracer = Tracer(engine=engine)
    tracer.enable_otel(tracer_provider=live_provider, model_name_map={MODEL_ID: VENDOR_NAME})

    east8 = timezone(timedelta(hours=8))
    with tracer.span(
        "proj", "comp", "llm_complete",
        feature_as_of=datetime(2024, 6, 1, 9, 0, tzinfo=east8),  # non-UTC tz
    ) as span:
        span.record_input({"text": "hello"})
        span.record_model_prompt(model_id=MODEL_ID)
        span.record_output(parsed={"ok": True})
        span.record_perf(latency_ms=42, cost_usd=Decimal("0.00123456789"))  # >6 dp

    with Session(engine) as sess:
        row = sess.scalars(select(Trace).order_by(Trace.trace_id)).all()[-1]
    export_trace(row, tracer_provider=batch_provider, engine=engine, model_name=VENDOR_NAME)

    live = live_exporter.get_finished_spans()[0]
    batch = batch_exporter.get_finished_spans()[0]
    assert dict(live.attributes) == dict(batch.attributes)
    # And the normalized values actually landed (UTC + 6dp), not the raw inputs.
    assert live.attributes["traceguard.feature_as_of"] == "2024-06-01T01:00:00+00:00"
    assert live.attributes["traceguard.cost_usd"] == pytest.approx(0.001235)


def test_dual_write_error_trace_sets_error_status(engine):
    provider, exporter = _provider()
    _register(engine)
    tracer = Tracer(engine=engine)
    tracer.enable_otel(tracer_provider=provider)

    with pytest.raises(RuntimeError, match="boom"):
        with tracer.span("proj", "comp", "llm_complete") as span:
            span.record_input({"text": "hi"})
            span.record_model_prompt(model_id=MODEL_ID)
            raise RuntimeError("boom")

    s = exporter.get_finished_spans()[0]
    assert s.status.status_code.name == "ERROR"
    assert s.attributes["exception.type"] == "RuntimeError"
    assert s.attributes["traceguard.parse_status"] == "failed"


def test_disable_otel_stops_emitting(engine):
    provider, exporter = _provider()
    _register(engine)
    tracer = Tracer(engine=engine)
    tracer.enable_otel(tracer_provider=provider)

    _run_ok_span(tracer)
    assert len(exporter.get_finished_spans()) == 1

    tracer.disable_otel()
    _run_ok_span(tracer)
    assert len(exporter.get_finished_spans()) == 1  # no new span after disable

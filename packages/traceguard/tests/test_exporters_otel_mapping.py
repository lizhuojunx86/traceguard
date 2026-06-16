"""OTel exporter 0.4.0 additions: vendor model-name mapping + batched lookup.

New file (does not touch the existing test_exporters_otel.py smoke tests).
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
from sqlalchemy import event  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from traceguard.exporters.otel import export_trace, export_traces  # noqa: E402
from traceguard.registry.models import register_model  # noqa: E402
from traceguard.store.models import Trace  # noqa: E402

UTC = timezone.utc
MODEL_ID = "demo-llm-2024"


def _provider():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _register(engine, model_id: str = MODEL_ID) -> None:
    register_model(
        model_id,
        model_family="internal-ml",
        capability_class="general-llm",
        released_at=datetime(2024, 1, 1, tzinfo=UTC),
        available_to_us_at=datetime(2024, 2, 1, tzinfo=UTC),
        engine=engine,
    )


def _insert_trace(engine, model_id: str | None) -> Trace:
    with Session(engine) as sess:
        row = Trace(
            project="p",
            component="c",
            operation="llm",
            input_hash="0" * 64,
            parse_status="success",
            model_id=model_id,
            latency_ms=5,
            invoked_at=datetime(2024, 5, 1, tzinfo=UTC),
        )
        sess.add(row)
        sess.commit()
        tid = row.trace_id
    with Session(engine) as sess:  # fresh load so attributes survive detachment
        return sess.get(Trace, tid)


def test_export_trace_model_name_sets_vendor_and_preserves_id(engine):
    _register(engine)
    trace = _insert_trace(engine, MODEL_ID)
    provider, exporter = _provider()

    export_trace(trace, tracer_provider=provider, engine=engine, model_name="claude-opus-4")

    a = exporter.get_finished_spans()[0].attributes
    assert a["gen_ai.request.model"] == "claude-opus-4"   # vendor name for Phoenix/Langfuse
    assert a["traceguard.model_id"] == MODEL_ID            # internal id preserved


def test_default_export_keeps_model_id_for_gen_ai_model(engine):
    _register(engine)
    trace = _insert_trace(engine, MODEL_ID)
    provider, exporter = _provider()

    export_trace(trace, tracer_provider=provider, engine=engine)  # no model_name

    a = exporter.get_finished_spans()[0].attributes
    assert a["gen_ai.request.model"] == MODEL_ID            # unchanged default behaviour
    assert a["traceguard.model_id"] == MODEL_ID


def test_export_traces_model_name_map_with_fallback(engine):
    _register(engine, MODEL_ID)
    t1 = _insert_trace(engine, MODEL_ID)
    t2 = _insert_trace(engine, "unmapped-model")
    provider, exporter = _provider()

    n = export_traces(
        [t1, t2],
        tracer_provider=provider,
        engine=engine,
        model_name_map={MODEL_ID: "claude-opus-4"},
    )

    assert n == 2
    by_id = {
        s.attributes["traceguard.model_id"]: s.attributes
        for s in exporter.get_finished_spans()
    }
    assert by_id[MODEL_ID]["gen_ai.request.model"] == "claude-opus-4"
    assert by_id["unmapped-model"]["gen_ai.request.model"] == "unmapped-model"  # fallback


def test_export_traces_batches_registry_lookup_into_one_query(engine):
    # Regression guard for the N+1: availability is fetched once per batch, not
    # once per trace.
    _register(engine)
    traces = [_insert_trace(engine, MODEL_ID) for _ in range(3)]
    provider, _ = _provider()

    seen: list[str] = []

    def _capture(conn, cursor, statement, params, context, executemany):
        if "model_registry" in statement:
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        n = export_traces(traces, tracer_provider=provider, engine=engine)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert n == 3
    assert len(seen) == 1  # single batched SELECT, not 3

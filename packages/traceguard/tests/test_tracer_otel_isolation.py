"""Error-isolation and optional-dependency tests for OTel dual-write (0.5.0).

These deliberately need NO real opentelemetry SDK: they inject a stub sink via
the private ``_otel_sink=`` seam (proving hot-path isolation) and simulate the
extra being missing (proving graceful degradation). The real-OTLP behaviour is
covered in test_tracer_otel_dualwrite.py.
"""
from __future__ import annotations

import logging
import sys

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.sdk.tracer import Tracer
from traceguard.store.models import Trace


class _RecordingSink:
    """Stub OTel sink. Records each emit; optionally raises to test isolation."""

    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[Trace] = []
        self._raises = raises

    def emit(self, trace, *, engine=None) -> None:
        self.calls.append(trace)
        if self._raises:
            raise RuntimeError("exporter down")


def test_default_off_never_emits(engine):
    """No enable_otel -> no sink -> the hot path is byte-for-byte the old one."""
    tracer = Tracer(engine=engine)
    assert tracer._otel_sink is None
    with tracer.span("p", "c", "op") as sp:
        sp.record_input({"x": 1})
        sp.record_output(parsed={"ok": True})
        assert sp._snapshot is None  # no snapshot built when dual-write is off

    with Session(engine) as sess:
        rows = sess.scalars(select(Trace)).all()
    assert len(rows) == 1  # SQLite still the source of truth


def test_emit_failure_does_not_break_success_path(engine, caplog):
    sink = _RecordingSink(raises=True)
    tracer = Tracer(engine=engine, _otel_sink=sink)

    with caplog.at_level(logging.WARNING, logger="traceguard.otel"):
        # No exception must escape even though the sink raises.
        with tracer.span("p", "c", "op") as sp:
            sp.record_input({"x": 1})
            sp.record_model_prompt(model_id="m")
            sp.record_output(parsed={"ok": True})

    # Source of truth intact: exactly one row committed.
    with Session(engine) as sess:
        rows = sess.scalars(select(Trace)).all()
    assert len(rows) == 1
    assert rows[0].model_id == "m"
    # The sink was actually invoked, and the failure was logged, not raised.
    assert len(sink.calls) == 1
    assert any("otel dual-write failed" in r.message for r in caplog.records)


def test_emit_failure_does_not_mask_business_exception(engine, caplog):
    sink = _RecordingSink(raises=True)
    tracer = Tracer(engine=engine, _otel_sink=sink)

    with caplog.at_level(logging.WARNING, logger="traceguard.otel"):
        # The ORIGINAL ValueError must propagate, NOT the sink's RuntimeError.
        with pytest.raises(ValueError, match="business boom"):
            with tracer.span("p", "c", "op") as sp:
                sp.record_input({"x": 1})
                raise ValueError("business boom")

    # Error row still persisted, and the sink still fired on the error path.
    with Session(engine) as sess:
        row = sess.scalars(select(Trace)).one()
    assert row.error_class == "ValueError"
    assert row.parse_status == "failed"
    assert len(sink.calls) == 1
    assert any("otel dual-write failed" in r.message for r in caplog.records)


def test_snapshot_carries_trace_id_and_fields(engine):
    """The detached snapshot handed to the sink mirrors the committed row."""
    sink = _RecordingSink()
    tracer = Tracer(engine=engine, _otel_sink=sink)

    with tracer.span("p", "c", "op", correlation_id="c1") as sp:
        sp.record_input({"x": 1})
        sp.record_model_prompt(model_id="m", prompt_template_hash="dead")
        sp.record_output(parsed={"ok": True})
        sp.record_perf(latency_ms=42)

    snap = sink.calls[0]
    with Session(engine) as sess:
        row = sess.scalars(select(Trace)).one()
    assert snap.trace_id == row.trace_id  # PK copied -> traceguard.trace_id dedup key
    assert snap.model_id == "m"
    assert snap.correlation_id == "c1"
    assert snap.latency_ms == 42
    assert snap.invoked_at == row.invoked_at  # same timestamp -> identical span timing


def test_decorator_path_also_dual_writes(engine):
    sink = _RecordingSink()
    tracer = Tracer(engine=engine, _otel_sink=sink)

    @tracer.trace("demo", "fn", "parse")
    def add(a, b):
        return a + b

    assert add(2, 3) == 5
    assert len(sink.calls) == 1
    assert sink.calls[0].output_parsed == 5


def test_enable_otel_without_extra_raises(monkeypatch):
    """Simulate the otel extra missing: enable_otel must fail fast and point to it."""
    # Force a fresh import of the exporter with opentelemetry unavailable, so its
    # module-top guard raises the canonical traceguard[otel] ImportError.
    monkeypatch.delitem(sys.modules, "traceguard.exporters.otel", raising=False)
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", None)

    with pytest.raises(ImportError, match=r"traceguard\[otel\]"):
        Tracer().enable_otel()


def test_disable_otel_clears_sink(engine):
    sink = _RecordingSink()
    tracer = Tracer(engine=engine, _otel_sink=sink)
    tracer.disable_otel()
    assert tracer._otel_sink is None

    with tracer.span("p", "c", "op") as sp:
        sp.record_input({"x": 1})
    assert len(sink.calls) == 0  # nothing emitted after disable

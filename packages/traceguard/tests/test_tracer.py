"""Tests for tracer decorator + context manager (SPEC §4.1)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.sdk.tracer import Tracer
from traceguard.store.models import Trace


UTC = timezone.utc


@pytest.fixture
def tg_tracer(engine):
    return Tracer(engine=engine)


def test_span_writes_one_row(tg_tracer, engine):
    with tg_tracer.span("demo", "extractor", "llm_complete") as sp:
        sp.record_input({"q": "hello"})
        sp.record_model_prompt(model_id="claude-x", prompt_template_id="demo/extractor/v1")
        sp.record_output(parsed={"answer": 42}, parse_status="success")
        sp.record_perf(latency_ms=120, tokens_in=10, tokens_out=20, cost_usd=0.0012)

    with Session(engine) as sess:
        rows = sess.scalars(select(Trace)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.project == "demo"
    assert row.model_id == "claude-x"
    assert row.output_parsed == {"answer": 42}
    assert row.parse_status == "success"
    assert row.latency_ms == 120
    assert row.tokens_in == 10
    assert row.input_hash and len(row.input_hash) == 64


def test_span_records_error_on_exception(tg_tracer, engine):
    with pytest.raises(RuntimeError):
        with tg_tracer.span("demo", "x", "llm_complete") as sp:
            sp.record_input({"q": "boom"})
            raise RuntimeError("oops")

    with Session(engine) as sess:
        rows = sess.scalars(select(Trace)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.error_class == "RuntimeError"
    assert row.error_message == "oops"
    assert row.parse_status == "failed"


def test_decorator_records_input_and_output(tg_tracer, engine):
    @tg_tracer.trace("demo", "fn", "parse")
    def add(a, b):
        return a + b

    assert add(2, 3) == 5

    with Session(engine) as sess:
        rows = sess.scalars(select(Trace)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.output_parsed == 5
    assert row.parse_status == "success"


def test_decorator_correlation_and_feature_as_of(tg_tracer, engine):
    @tg_tracer.trace(
        "demo",
        "fn",
        "parse",
        correlation_from=lambda x, **_: f"item:{x}",
        feature_as_of_from=lambda x, **_: datetime(2025, 1, 1, tzinfo=UTC),
    )
    def identity(x):
        return x

    identity(42)

    with Session(engine) as sess:
        row = sess.scalars(select(Trace)).one()
    assert row.correlation_id == "item:42"
    assert row.feature_as_of == datetime(2025, 1, 1, tzinfo=UTC)


def test_decorator_records_exception(tg_tracer, engine):
    @tg_tracer.trace("demo", "fn", "parse")
    def bad():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        bad()

    with Session(engine) as sess:
        row = sess.scalars(select(Trace)).one()
    assert row.error_class == "ValueError"
    assert row.parse_status == "failed"

"""Span.record_source: persistence, the same-transaction rule, and isolation.

The load-bearing claims under test:

1. Snapshot rows land in the SAME transaction as their trace, carrying the
   trace's real primary key.
2. A snapshot failure never costs the host its trace or its call (SPEC §4.1),
   even under strict_persistence.
3. Strict invariant-3 refusal happens at record_source time, on the caller's
   stack — NOT at flush, where the fail-open tracer would swallow it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard import sources
from traceguard.sdk.tracer import Tracer
from traceguard.sources.models import SourceSnapshotRow
from traceguard.store.models import Trace, make_engine
from traceguard.validators.lookahead import InvariantViolation

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def src_engine(engine):
    sources.enable(engine)
    return engine


@pytest.fixture
def tg(src_engine):
    return Tracer(engine=src_engine)


def _snapshot(**kwargs):
    base = dict(
        source_uri="https://vendor.example/eps/AAPL",
        source_kind="vendor_api",
        content_hash="a" * 64,
        retrieved_at=NOW,
    )
    base.update(kwargs)
    return sources.SourceSnapshot(**base)


def _rows(engine) -> list[SourceSnapshotRow]:
    with Session(engine) as sess:
        return list(sess.scalars(select(SourceSnapshotRow)))


# ── enable ──────────────────────────────────────────────────────────────────

def test_enable_is_idempotent(engine):
    sources.enable(engine)
    sources.enable(engine)
    assert sources.source_tables_exist(engine)


def test_importing_sources_creates_no_tables(engine):
    """Import must have zero side effects; enable() is explicit (audit precedent)."""
    assert not sources.source_tables_exist(engine)


# ── the happy path: same transaction, real trace_id ─────────────────────────

def test_snapshot_row_lands_with_the_traces_real_primary_key(tg, src_engine):
    with tg.span("proj", "comp", "llm_complete", feature_as_of=NOW) as span:
        span.record_input({"q": "hi"})
        span.record_source(_snapshot(published_at=NOW - timedelta(days=1)), strict=False)

    with Session(src_engine) as sess:
        trace = sess.scalars(select(Trace)).one()
    rows = _rows(src_engine)
    assert len(rows) == 1
    assert rows[0].trace_id == trace.trace_id == span.trace_id
    assert rows[0].verdict == "verified"
    assert rows[0].strict is False


def test_every_field_round_trips(tg, src_engine):
    snap = _snapshot(
        content_encoding="utf-8",
        normalized_hash="b" * 64,
        normalizer_id="cdn-stripper@2.1",
        published_at=NOW - timedelta(days=2),
        effective_at=NOW - timedelta(days=1),
        source_version='W/"abc"',
        mcp_server_id="srv",
        tool_name="get_filing",
        cache_status="MISS",
    )
    with tg.span("proj", "comp", "llm_complete", feature_as_of=NOW) as span:
        span.record_source(snap, strict=True)

    row = _rows(src_engine)[0]
    assert row.source_uri == snap.source_uri
    assert row.source_kind == "vendor_api"
    assert row.content_hash == snap.content_hash
    assert row.content_encoding == "utf-8"
    assert row.normalized_hash == snap.normalized_hash
    assert row.normalizer_id == "cdn-stripper@2.1"
    assert row.retrieved_at == NOW
    assert row.published_at == snap.published_at
    assert row.effective_at == snap.effective_at
    assert row.source_version == 'W/"abc"'
    assert row.mcp_server_id == "srv"
    assert row.tool_name == "get_filing"
    assert row.cache_status == "MISS"
    assert row.strict is True


def test_several_sources_on_one_trace(tg, src_engine):
    with tg.span("proj", "comp", "llm_complete", feature_as_of=NOW) as span:
        for i in range(3):
            span.record_source(
                _snapshot(source_uri=f"https://v.example/{i}", content_hash=f"{i}" * 64),
                strict=False,
            )
    rows = _rows(src_engine)
    assert len(rows) == 3
    assert {r.trace_id for r in rows} == {span.trace_id}


def test_record_source_returns_the_verdict(tg):
    with tg.span("proj", "comp", "llm_complete", feature_as_of=NOW) as span:
        verdict = span.record_source(_snapshot(), strict=False)
    assert verdict is sources.SourceVerdict.UNVERIFIABLE


def test_record_source_accepts_a_mapping_of_fields(tg, src_engine):
    with tg.span("proj", "comp", "llm_complete", feature_as_of=NOW) as span:
        span.record_source(
            {
                "source_uri": "https://v.example/x",
                "source_kind": "http",
                "content_hash": "c" * 64,
                "retrieved_at": NOW,
            },
            strict=False,
        )
    assert _rows(src_engine)[0].source_uri == "https://v.example/x"


def test_record_source_rejects_a_non_snapshot(tg):
    with tg.span("proj", "comp", "llm_complete") as span:
        with pytest.raises(TypeError, match="SourceSnapshot or a mapping"):
            span.record_source("https://v.example/x", strict=False)


# ── strict happens at record time, not at flush ─────────────────────────────

def test_strict_anachronistic_raises_at_the_call_site(tg, src_engine):
    """The raise must reach the host. If this were deferred to _flush, the
    fail-open tracer would swallow it and strict would be silently defeated."""
    with pytest.raises(InvariantViolation) as excinfo:
        with tg.span("proj", "comp", "llm_complete", feature_as_of=NOW) as span:
            span.record_source(_snapshot(published_at=NOW + timedelta(days=1)), strict=True)
    assert excinfo.value.invariant == 3


def test_strict_unknown_published_at_raises_at_the_call_site(tg):
    with pytest.raises(InvariantViolation, match="cannot establish"):
        with tg.span("proj", "comp", "llm_complete", feature_as_of=NOW) as span:
            span.record_source(_snapshot(), strict=True)


def test_a_strict_refusal_still_leaves_the_trace_recorded(tg, src_engine):
    """The refusal is the host's error; the span still closes and writes its
    trace (with the error recorded), exactly like any other business exception."""
    with pytest.raises(InvariantViolation):
        with tg.span("proj", "comp", "llm_complete", feature_as_of=NOW) as span:
            span.record_input({"q": "hi"})
            span.record_source(_snapshot(), strict=True)
    with Session(src_engine) as sess:
        trace = sess.scalars(select(Trace)).one()
    assert trace.error_class == "InvariantViolation"
    # The refused snapshot was never buffered, so nothing was written.
    assert _rows(src_engine) == []


def test_loose_records_the_anachronistic_verdict_instead_of_refusing(tg, src_engine):
    with tg.span("proj", "comp", "llm_complete", feature_as_of=NOW) as span:
        span.record_source(_snapshot(published_at=NOW + timedelta(days=1)), strict=False)
    assert _rows(src_engine)[0].verdict == "anachronistic"


def test_unchecked_is_recorded_when_the_span_has_no_feature_as_of(tg, src_engine):
    with tg.span("proj", "comp", "llm_complete") as span:
        span.record_source(_snapshot(published_at=NOW), strict=True)
    assert _rows(src_engine)[0].verdict == "unchecked"


# ── isolation: a snapshot failure never costs the trace (SPEC §4.1) ─────────

def test_record_source_before_enable_fails_open_with_a_pointed_warning(caplog):
    """No table, so the write cannot land. The trace must still be written, the
    business call unaffected, and the warning must name the fix."""
    engine = make_engine("sqlite:///:memory:", create_all=True)  # sources NOT enabled
    tg = Tracer(engine=engine)
    with caplog.at_level(logging.WARNING, logger="traceguard.sources"):
        with tg.span("proj", "comp", "llm_complete", feature_as_of=NOW) as span:
            span.record_input({"q": "hi"})
            span.record_source(_snapshot(published_at=NOW), strict=False)

    with Session(engine) as sess:
        assert sess.scalars(select(Trace)).one() is not None  # the trace survived
    messages = [r.getMessage() for r in caplog.records if r.name == "traceguard.sources"]
    assert any("traceguard.sources.enable(engine)" in m for m in messages)


def test_snapshot_write_failure_does_not_take_the_trace_down(tg, src_engine, monkeypatch):
    """The savepoint is what makes 'same transaction' and 'fail-open' coexist:
    the snapshot inserts roll back, the trace still commits."""
    import traceguard.sources.models as models_mod

    class _Exploding(models_mod.SourceSnapshotRow):
        pass

    def _boom(*args, **kwargs):
        raise RuntimeError("snapshot insert exploded")

    monkeypatch.setattr(models_mod, "SourceSnapshotRow", _boom)

    with tg.span("proj", "comp", "llm_complete", feature_as_of=NOW) as span:
        span.record_input({"q": "hi"})
        span.record_source(_snapshot(published_at=NOW), strict=False)

    with Session(src_engine) as sess:
        trace = sess.scalars(select(Trace)).one()
    assert trace.trace_id == span.trace_id
    assert _rows(src_engine) == []  # snapshot rolled back, trace kept


def test_snapshot_failure_is_fail_open_even_under_strict_persistence(
    src_engine, monkeypatch
):
    """strict_persistence is about losing the TRACE. The invariant-3 decision
    already happened at record time; losing bookkeeping must not lose the trace
    it describes."""
    import traceguard.sources.models as models_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("snapshot insert exploded")

    monkeypatch.setattr(models_mod, "SourceSnapshotRow", _boom)
    tg = Tracer(engine=src_engine, strict_persistence=True)

    with tg.span("proj", "comp", "llm_complete", feature_as_of=NOW) as span:
        span.record_source(_snapshot(published_at=NOW), strict=False)

    with Session(src_engine) as sess:
        assert sess.scalars(select(Trace)).one() is not None


def test_business_call_unaffected_when_snapshots_cannot_be_written(monkeypatch):
    engine = make_engine("sqlite:///:memory:", create_all=True)
    tg = Tracer(engine=engine)

    @tg.trace("proj", "fn", "parse")
    def add(a, b):
        return a + b

    assert add(2, 3) == 5


def test_a_trace_with_no_sources_writes_exactly_as_before(tg, src_engine):
    """The flush()+savepoint branch is skipped entirely when nothing was
    recorded, so the default path is unchanged."""
    with tg.span("proj", "comp", "llm_complete") as span:
        span.record_input({"q": "hi"})
    assert span.trace_id is not None
    assert _rows(src_engine) == []


# ── the frozen surface is untouched ─────────────────────────────────────────

def test_record_source_is_a_new_method_not_a_changed_signature():
    """SPEC §6.3: adding a method is minor. record_output/record_perf etc. keep
    their exact signatures, and traceguard.__all__ does not grow."""
    import inspect

    import traceguard
    from traceguard.sdk.tracer import Span

    assert hasattr(Span, "record_source")
    assert len(traceguard.__all__) == 29
    assert "sources" not in traceguard.__all__

    param = inspect.signature(Span.record_source).parameters["strict"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty

    # The pre-1.6.0 record_* signatures are byte-identical.
    assert list(inspect.signature(Span.record_output).parameters) == [
        "self", "parsed", "parse_status",
    ]
    assert list(inspect.signature(Span.record_perf).parameters) == [
        "self", "latency_ms", "tokens_in", "tokens_out", "cost_usd",
    ]

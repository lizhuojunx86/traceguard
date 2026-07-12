"""Persistence fail-open isolation (SPEC §4.1 failure-mode MUST).

Tracing MUST NOT break or mask the instrumented business call. By default a
persistence failure is swallowed (logged at WARNING); ``strict_persistence``
opts into fail-closed propagation. We trigger a *real* persistence failure by
pointing the tracer at an engine whose schema was never created, so the row
INSERT raises ``OperationalError`` ("no such table: traces").
"""
from __future__ import annotations

import logging

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from traceguard.sdk.tracer import Tracer, _env_truthy
from traceguard.store.models import Trace, make_engine


@pytest.fixture
def broken_engine():
    """A SQLite engine with NO schema — any row INSERT raises OperationalError."""
    return make_engine("sqlite:///:memory:", create_all=False)


def test_flush_failure_swallowed_on_success_path(broken_engine):
    """Default fail-open: a persistence failure never reaches the caller and the
    business value is returned unaffected."""
    tg = Tracer(engine=broken_engine)  # strict_persistence defaults to False

    @tg.trace("demo", "fn", "parse")
    def add(a, b):
        return a + b

    # The DB write WILL fail, but the call must still return cleanly.
    assert add(2, 3) == 5


def test_flush_failure_does_not_mask_business_exception(broken_engine):
    """Error path: the caller's own exception propagates, NOT the DB error."""
    tg = Tracer(engine=broken_engine)

    with pytest.raises(ValueError) as excinfo:
        with tg.span("demo", "x", "llm_complete") as sp:
            sp.record_input({"q": "boom"})
            raise ValueError("business failure")

    # The original business exception is what surfaced — never replaced by the
    # OperationalError from the failed flush.
    assert str(excinfo.value) == "business failure"
    assert not isinstance(excinfo.value, OperationalError)


def test_flush_failure_logs_warning(broken_engine, caplog):
    tg = Tracer(engine=broken_engine)
    with caplog.at_level(logging.WARNING, logger="traceguard.tracer"):
        with tg.span("demo", "x", "llm_complete") as sp:
            sp.record_input({"q": "hi"})
    assert any(
        rec.name == "traceguard.tracer" and rec.levelno == logging.WARNING
        for rec in caplog.records
    )


def test_strict_persistence_propagates_on_success_path(broken_engine):
    """Fail-closed: a persistence failure surfaces as an error to the caller."""
    tg = Tracer(engine=broken_engine, strict_persistence=True)
    with pytest.raises(OperationalError):
        with tg.span("demo", "x", "llm_complete") as sp:
            sp.record_input({"q": "hi"})


def test_strict_persistence_fails_closed_on_error_path(broken_engine):
    """Fail-closed on the error path: a persistence failure surfaces (it is the
    explicit opt-in to NOT silently drop a trace). The business exception is not
    swallowed either — it stays reachable in the exception chain."""
    tg = Tracer(engine=broken_engine, strict_persistence=True)
    with pytest.raises(OperationalError) as excinfo:
        with tg.span("demo", "x", "llm_complete") as sp:
            sp.record_input({"q": "hi"})
            raise ValueError("business failure")
    # Walk the implicit-chaining links; the original business error is somewhere
    # in the chain (its exact position depends on the DBAPI driver's wrapping).
    seen, cur = [], excinfo.value
    while cur is not None and cur not in seen:
        seen.append(cur)
        cur = cur.__context__
    assert any(isinstance(e, ValueError) and str(e) == "business failure" for e in seen)


def test_strict_persistence_runtime_toggle_is_per_span(broken_engine):
    """The flag is read when a span is created, so toggling it on the tracer
    affects subsequent spans."""
    tg = Tracer(engine=broken_engine)
    # fail-open span: no raise
    with tg.span("demo", "x", "llm_complete") as sp:
        sp.record_input({"q": "a"})
    # flip to fail-closed
    tg.strict_persistence = True
    with pytest.raises(OperationalError):
        with tg.span("demo", "x", "llm_complete") as sp:
            sp.record_input({"q": "b"})


def test_healthy_engine_unaffected_by_isolation(engine):
    """The fail-open wrapper must not change the happy path: a good engine still
    persists exactly one row and exposes trace_id."""
    tg = Tracer(engine=engine)
    with tg.span("demo", "x", "llm_complete") as sp:
        sp.record_input({"q": "ok"})
    assert sp.trace_id is not None
    with Session(engine) as sess:
        rows = sess.scalars(select(Trace)).all()
    assert len(rows) == 1


def test_env_truthy_helper():
    assert _env_truthy.__name__ == "_env_truthy"  # symbol exists
    import os

    for val in ("1", "true", "TRUE", "Yes", "on"):
        os.environ["TG_TEST_FLAG"] = val
        assert _env_truthy("TG_TEST_FLAG") is True
    for val in ("0", "false", "no", "", "maybe"):
        os.environ["TG_TEST_FLAG"] = val
        assert _env_truthy("TG_TEST_FLAG") is False
    del os.environ["TG_TEST_FLAG"]

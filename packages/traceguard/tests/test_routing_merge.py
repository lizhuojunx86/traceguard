"""Tests for merging trace stores."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.routing_integrity.merge import main, merge_traces
from traceguard.sdk.tracer import Tracer
from traceguard.sdk.wrappers._base import routing_detail
from traceguard.store.models import Trace, make_engine

UTC = timezone.utc
T0 = datetime(2026, 8, 26, 15, 55, tzinfo=UTC)


class _Resp:
    def __init__(self, model: str | None) -> None:
        if model is not None:
            self.model = model


def _write(engine, stamp, served, *, project="routing-conformance", n=1):
    tracer = Tracer(engine)
    for i in range(n):
        with tracer.span(project, "orcarouter", "llm_complete", feature_as_of=stamp) as span:
            span.record_input({"probe": f"{served}-{i}"})
            span.record_model_prompt(model_id="orcarouter/auto")
            parsed: dict[str, Any] = {"content_text": "ok"}
            routing = routing_detail("orcarouter/auto", _Resp(served))
            if routing:
                parsed["routing"] = routing
            span.record_perf(tokens_in=10, tokens_out=20)
            span.record_output(parsed=parsed, parse_status="success")


def _count(engine) -> int:
    with Session(engine) as s:
        return len(list(s.scalars(select(Trace))))


@pytest.fixture()
def src():
    return make_engine("sqlite:///:memory:")


@pytest.fixture()
def dst():
    return make_engine("sqlite:///:memory:")


def test_the_missing_run_arrives(src, dst) -> None:
    _write(src, T0, "qwen3.7-plus", n=12)
    _write(dst, T0 + timedelta(minutes=20), "deepseek-v4-pro", n=12)

    result = merge_traces(src, dst)
    assert result.inserted == 12
    assert result.skipped == 0
    assert _count(dst) == 24


def test_merging_twice_changes_nothing(src, dst) -> None:
    """People retry a merge after an interruption; doubling the evidence is
    worse than failing."""
    _write(src, T0, "qwen3.7-plus", n=12)
    merge_traces(src, dst)
    second = merge_traces(src, dst)

    assert second.inserted == 0
    assert second.skipped == 12
    assert _count(dst) == 12


def test_dry_run_reports_without_writing(src, dst) -> None:
    _write(src, T0, "qwen3.7-plus", n=5)
    result = merge_traces(src, dst, dry_run=True)

    assert result.inserted == 5
    assert _count(dst) == 0, "dry run must not touch the destination"
    assert "was not modified" in result.render(dry_run=True)


def test_ids_are_rebuilt_not_copied(src, dst) -> None:
    """Both stores autoincrement from 1, so copied ids would collide."""
    _write(dst, T0, "deepseek-v4-pro", n=3)   # dst now owns ids 1..3
    _write(src, T0 + timedelta(hours=1), "qwen3.7-plus", n=3)  # src also 1..3

    merge_traces(src, dst)
    with Session(dst) as s:
        ids = sorted(t.trace_id for t in s.scalars(select(Trace)))
    assert ids == [1, 2, 3, 4, 5, 6]


def test_the_evidence_survives_the_trip(src, dst) -> None:
    _write(src, T0, "qwen3.7-plus", n=1)
    merge_traces(src, dst)

    with Session(dst) as s:
        trace = s.scalars(select(Trace)).one()
    assert trace.feature_as_of == T0
    assert trace.model_id == "orcarouter/auto"
    assert trace.output_parsed["routing"]["served_model"] == "qwen3.7-plus"
    assert trace.tokens_in == 10 and trace.tokens_out == 20


def test_failed_calls_come_too(src, dst) -> None:
    """The 401 that opened the log is part of the record."""
    tracer = Tracer(src)
    with pytest.raises(RuntimeError):
        with tracer.span("routing-conformance", "orcarouter", "llm_complete",
                         feature_as_of=T0) as span:
            span.record_input({"x": 1})
            span.record_model_prompt(model_id="orcarouter/auto")
            raise RuntimeError("401 Invalid API key")

    merge_traces(src, dst)
    with Session(dst) as s:
        trace = s.scalars(select(Trace)).one()
    assert trace.error_class is not None


def test_everything_comes_across_unless_you_narrow_it(src, dst) -> None:
    """Default is every project: a merge that silently drops rows is the same
    failure as one that silently doubles them, found out just as late."""
    _write(src, T0, "qwen3.7-plus", n=2, project="real-work")
    _write(src, T0, "glm-5.2", n=2)

    assert merge_traces(src, dst).inserted == 4


def test_narrowing_to_one_project_works(src, dst) -> None:
    _write(src, T0, "qwen3.7-plus", n=2, project="real-work")
    _write(src, T0, "glm-5.2", n=2)

    assert merge_traces(src, dst, project="routing-conformance").inserted == 2
    assert _count(dst) == 2


def test_a_partial_failure_leaves_the_destination_alone(src, dst, monkeypatch) -> None:
    """The destination accumulates for weeks; half a merge is not acceptable."""
    _write(dst, T0, "deepseek-v4-pro", n=2)
    _write(src, T0 + timedelta(hours=1), "qwen3.7-plus", n=5)

    real_flush = Session.flush
    calls = {"n": 0}

    def exploding_flush(self, *a, **k):
        calls["n"] += 1
        if calls["n"] > 3:
            raise RuntimeError("disk went away")
        return real_flush(self, *a, **k)

    monkeypatch.setattr(Session, "flush", exploding_flush)
    with pytest.raises(RuntimeError, match="disk went away"):
        merge_traces(src, dst)

    monkeypatch.undo()
    assert _count(dst) == 2, "the pre-existing evidence must be untouched"


def test_cli_refuses_to_merge_a_store_into_itself(capsys) -> None:
    assert main(["--from", "sqlite:///x.db", "--into", "sqlite:///x.db"]) == 2
    assert "into itself" in capsys.readouterr().out


def test_merge_stays_off_the_frozen_surface() -> None:
    import traceguard

    for name in ("merge", "merge_traces", "MergeResult"):
        assert name not in traceguard.__all__, name

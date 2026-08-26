"""Tests for the probe timeline report."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from traceguard.routing_integrity.timeline import (
    collect_runs,
    regime_changes,
    render,
)
from traceguard.sdk.tracer import Tracer
from traceguard.sdk.wrappers._base import routing_detail
from traceguard.store.models import make_engine

UTC = timezone.utc
T0 = datetime(2026, 8, 26, 15, 55, tzinfo=UTC)


class _Resp:
    def __init__(self, model: str | None) -> None:
        if model is not None:
            self.model = model


@pytest.fixture()
def engine():
    return make_engine("sqlite:///:memory:")


def _run(engine, stamp: datetime, served: list[str | None], *, project="routing-conformance"):
    """Write one probe run: every trace shares the stamp, as the harness does."""
    tracer = Tracer(engine)
    for model in served:
        with tracer.span(project, "orcarouter", "llm_complete", feature_as_of=stamp) as span:
            span.record_input({"p": "hi"})
            span.record_model_prompt(model_id="orcarouter/auto")
            parsed: dict[str, Any] = {"content_text": "ok"}
            routing = routing_detail("orcarouter/auto", _Resp(model))
            if routing:
                parsed["routing"] = routing
            span.record_perf(tokens_in=100, tokens_out=50)
            span.record_output(parsed=parsed, parse_status="success")


def test_traces_group_into_runs_by_stamp(engine) -> None:
    _run(engine, T0, ["qwen3.7-plus"] * 3)
    _run(engine, T0 + timedelta(minutes=20), ["deepseek-v4-pro"] * 3)

    runs = collect_runs(engine)
    assert len(runs) == 2
    assert runs[0].stamp == T0            # oldest first
    assert runs[0].calls == 3
    assert runs[0].dominant == "qwen3.7-plus"
    assert runs[1].dominant == "deepseek-v4-pro"


def test_the_headline_is_a_shift_between_runs(engine) -> None:
    """Not 'a prompt drifted' but 'the model most calls land on changed'."""
    _run(engine, T0, ["qwen3.7-plus"] * 9 + ["glm-5.2"] * 2 + ["deepseek-v4-pro"])
    _run(engine, T0 + timedelta(minutes=20), ["deepseek-v4-pro"] * 10 + ["glm-5.2"] * 2)

    runs = collect_runs(engine)
    shifts = regime_changes(runs)
    assert len(shifts) == 1
    assert shifts[0][0].dominant == "qwen3.7-plus"
    assert shifts[0][1].dominant == "deepseek-v4-pro"

    out = render(runs)
    assert "shift(s) in the dominant model" in out
    assert "The request did not change between these runs" in out


def test_a_stable_alias_is_reported_without_overclaiming(engine) -> None:
    _run(engine, T0, ["qwen3.7-plus"] * 3)
    _run(engine, T0 + timedelta(days=1), ["qwen3.7-plus"] * 3)

    out = render(collect_runs(engine))
    assert regime_changes(collect_runs(engine)) == []
    # A stable sample must not be reported as a stable alias.
    assert "evidence about that week, not about the alias" in out


def test_failures_and_silent_calls_are_counted_apart(engine) -> None:
    tracer = Tracer(engine)
    with pytest.raises(RuntimeError):
        with tracer.span("routing-conformance", "orcarouter", "llm_complete",
                         feature_as_of=T0) as span:
            span.record_input({"p": "hi"})
            span.record_model_prompt(model_id="orcarouter/auto")
            raise RuntimeError("401")
    _run(engine, T0, [None])  # succeeded, named no model

    run = collect_runs(engine)[0]
    assert run.failures == 1
    assert run.silent == 1
    assert run.calls == 2
    assert not run.models
    out = render(collect_runs(engine))
    assert "1 failed" in out and "1 named no model" in out


def test_tokens_are_summed_and_labelled_as_volume(engine) -> None:
    _run(engine, T0, ["qwen3.7-plus"] * 2)
    out = render(collect_runs(engine))
    assert "200 in" in out and "100 out" in out
    # The report must not let a token count be mistaken for a bill.
    assert "not cost" in out


def test_other_projects_are_excluded_by_default(engine) -> None:
    _run(engine, T0, ["qwen3.7-plus"], project="real-work")
    assert collect_runs(engine) == []
    assert len(collect_runs(engine, project="")) == 1


def test_empty_store_says_so(engine) -> None:
    assert "no probe runs" in render(collect_runs(engine))


def test_a_single_run_makes_no_claim_about_time(engine) -> None:
    _run(engine, T0, ["qwen3.7-plus"] * 3)
    out = render(collect_runs(engine))
    assert "shift" not in out
    assert "evidence about that week" not in out


def test_timeline_stays_off_the_frozen_surface() -> None:
    import traceguard

    for name in ("timeline", "collect_runs", "regime_changes"):
        assert name not in traceguard.__all__, name

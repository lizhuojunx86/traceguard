"""Tests for routing capture and the invariant-2 integrity classifier."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from traceguard.registry.models import register_model
from traceguard.routing_integrity import Verdict, classify, classify_trace, scan
from traceguard.routing_integrity.check import summarise
from traceguard.sdk.tracer import Tracer
from traceguard.sdk.wrappers._base import routing_detail
from traceguard.store.models import make_engine

UTC = timezone.utc
AS_OF = datetime(2025, 6, 30, tzinfo=UTC)


class _Response:
    """Minimal stand-in for an OpenAI/Anthropic response object."""

    def __init__(self, model: Any = None) -> None:
        if model is not None:
            self.model = model


@pytest.fixture()
def engine():
    eng = make_engine("sqlite:///:memory:")
    register_model(
        "deepseek/deepseek-v4",
        model_family="deepseek",
        capability_class="general-llm",
        released_at=datetime(2024, 1, 1, tzinfo=UTC),
        available_to_us_at=datetime(2024, 2, 1, tzinfo=UTC),
        engine=eng,
    )
    return eng


# ── routing_detail ────────────────────────────────────────────────────────


def test_no_requested_model_records_nothing() -> None:
    assert routing_detail(None, _Response("x")) is None


def test_direct_call_agrees_and_is_not_flagged() -> None:
    detail = routing_detail("gpt-5.2", _Response("gpt-5.2"))
    assert detail == {
        "requested_model": "gpt-5.2",
        "served_model": "gpt-5.2",
        "requested_is_alias": False,
        "diverged": False,
    }


def test_router_divergence_is_captured() -> None:
    detail = routing_detail("orcarouter/auto", _Response("deepseek/deepseek-v4"))
    assert detail is not None
    assert detail["requested_is_alias"] is True
    assert detail["served_model"] == "deepseek/deepseek-v4"
    assert detail["diverged"] is True


def test_silent_gateway_gives_none_not_false() -> None:
    """'We do not know' must not be recorded as 'they agree'."""
    detail = routing_detail("orcarouter/auto", _Response())
    assert detail is not None
    assert detail["served_model"] is None
    assert detail["diverged"] is None


# ── classify ──────────────────────────────────────────────────────────────


def test_alias_with_no_served_model_is_unverifiable(engine) -> None:
    verdict, detail = classify(
        routing_detail("orcarouter/auto", _Response()), engine=engine
    )
    assert verdict is Verdict.UNVERIFIABLE
    assert "did not report a served model" in detail


def test_alias_served_by_registered_model_is_diverged(engine) -> None:
    verdict, detail = classify(
        routing_detail("orcarouter/auto", _Response("deepseek/deepseek-v4")),
        engine=engine,
    )
    assert verdict is Verdict.DIVERGED
    # The message must name the model that has to be re-checked.
    assert "deepseek/deepseek-v4" in detail


def test_served_model_absent_from_registry_is_unregistered(engine) -> None:
    verdict, _ = classify(
        routing_detail("orcarouter/auto", _Response("mystery/model-9")), engine=engine
    )
    assert verdict is Verdict.UNREGISTERED


def test_agreeing_registered_model_is_verified(engine) -> None:
    verdict, _ = classify(
        routing_detail("deepseek/deepseek-v4", _Response("deepseek/deepseek-v4")),
        engine=engine,
    )
    assert verdict is Verdict.VERIFIED


def test_pre_capture_trace_falls_back_to_model_id(engine) -> None:
    """Traces written before routing capture must still classify."""
    verdict, detail = classify(None, engine=engine, model_id="deepseek/deepseek-v4")
    assert verdict is Verdict.VERIFIED
    assert "no routing record" in detail

    verdict, _ = classify(None, engine=engine, model_id="orcarouter/auto")
    assert verdict is Verdict.UNREGISTERED


# ── scan over a real store ────────────────────────────────────────────────


def _write(engine, *, model: str, served: str | None, as_of: datetime | None) -> None:
    tracer = Tracer(engine)
    with tracer.span("proj", "comp", "llm_complete", feature_as_of=as_of) as span:
        span.record_input({"x": 1})
        span.record_model_prompt(model_id=model)
        parsed: dict[str, Any] = {"content_text": "ok"}
        routing = routing_detail(model, _Response(served))
        if routing is not None:
            parsed["routing"] = routing
        span.record_output(parsed=parsed, parse_status="success")


def test_scan_separates_the_dangerous_from_the_fine(engine) -> None:
    _write(engine, model="deepseek/deepseek-v4", served="deepseek/deepseek-v4", as_of=AS_OF)
    _write(engine, model="orcarouter/auto", served="deepseek/deepseek-v4", as_of=AS_OF)
    _write(engine, model="orcarouter/auto", served=None, as_of=AS_OF)

    findings = list(scan(engine))
    counts = summarise(findings)
    assert counts[Verdict.VERIFIED] == 1
    assert counts[Verdict.DIVERGED] == 1
    assert counts[Verdict.UNVERIFIABLE] == 1
    assert [f.actionable for f in findings].count(True) == 2


def test_undated_traces_are_skipped_by_default(engine) -> None:
    """A trace with no feature_as_of makes no point-in-time claim."""
    _write(engine, model="orcarouter/auto", served=None, as_of=None)
    assert list(scan(engine)) == []
    assert len(list(scan(engine, only_dated=False))) == 1


def test_scan_can_filter_by_project(engine) -> None:
    _write(engine, model="orcarouter/auto", served=None, as_of=AS_OF)
    assert list(scan(engine, project="nope")) == []
    assert len(list(scan(engine, project="proj"))) == 1


def test_findings_carry_what_you_need_to_fix_them(engine) -> None:
    _write(engine, model="orcarouter/auto", served="deepseek/deepseek-v4", as_of=AS_OF)
    finding = next(iter(scan(engine)))
    assert finding.trace_id is not None
    assert finding.requested_model == "orcarouter/auto"
    assert finding.served_model == "deepseek/deepseek-v4"
    assert finding.feature_as_of == AS_OF


def test_classify_trace_handles_a_missing_output_parsed(engine) -> None:
    tracer = Tracer(engine)
    with tracer.span("proj", "comp", "llm_complete", feature_as_of=AS_OF) as span:
        span.record_input({"x": 1})
        span.record_model_prompt(model_id="deepseek/deepseek-v4")
        span.record_output(parsed=None, parse_status="success")
    finding = next(iter(scan(engine)))
    assert finding.verdict is Verdict.VERIFIED


def test_extension_stays_off_the_frozen_surface() -> None:
    """Contract-external means: not re-exported, not in ``__all__``.

    Not ``hasattr(traceguard, "routing_integrity")`` — importing a submodule
    binds it on the parent package, which is equally true of ``routing_audit``
    and ``loop``. The surface that is actually frozen is ``__all__`` and the
    names lifted into the top-level namespace.
    """
    import traceguard

    for name in ("routing_integrity", "Verdict", "classify", "classify_trace", "scan"):
        assert name not in traceguard.__all__, name
    assert not hasattr(traceguard, "classify")
    assert not hasattr(traceguard, "Verdict")


def test_classify_trace_is_exported_for_single_row_use(engine) -> None:
    _write(engine, model="orcarouter/auto", served=None, as_of=AS_OF)
    from sqlalchemy.orm import Session

    from traceguard.store.models import Trace

    with Session(engine) as sess:
        trace = sess.scalars(__import__("sqlalchemy").select(Trace)).one()
        assert classify_trace(trace, engine=engine).verdict is Verdict.UNVERIFIABLE

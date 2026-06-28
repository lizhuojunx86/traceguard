"""Tests for the guardian->traceguard bridge.

All fakes here are plain objects that merely *duck-type* guardian's StepOutput /
GuardianDecision — the bridge never imports guardian, so these run without it
installed. (A static check below pins that no-import guarantee.)
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.bridges.guardian import write_trace_from_guardian
from traceguard.sdk.tracer import Tracer
from traceguard.store.models import Trace

AS_OF = datetime(2025, 1, 1, tzinfo=timezone.utc)


class _FakeStepOutput:
    """Duck-types guardian.core.step.StepOutput."""

    def __init__(self, step_name="step_01", output_data=None, metadata=None):
        self.step_name = step_name
        self.output_data = output_data if output_data is not None else {"entities": ["Apple"]}
        self.metadata = metadata or {}

    def output_as_dict(self):
        return self.output_data if isinstance(self.output_data, dict) else None

    def output_as_string(self):
        import json

        return json.dumps(self.output_data) if isinstance(self.output_data, dict) else str(self.output_data)


class _FakeDecision:
    """Duck-types guardian.core.guardian_node.GuardianDecision."""

    def __init__(self, action="pass", score=0.9, issues=None):
        self.action = action
        self.score = score
        self.issues = issues or []
        self.semantic_score = 4
        self.semantic_status = "evaluated"
        self.flag_type = "standard"
        self.retry_hint = None


@pytest.fixture
def tg(engine):
    return Tracer(engine=engine)


def _row(engine):
    with Session(engine) as s:
        return s.scalars(select(Trace)).one()


def test_writes_one_trace_with_decision_and_returns_id(tg, engine):
    out = _FakeStepOutput(step_name="collect", output_data={"k": "v"})
    dec = _FakeDecision(action="pass", score=0.95, issues=["minor"])

    trace_id = write_trace_from_guardian(out, dec, project="huadian", tracer=tg)

    assert isinstance(trace_id, int)
    row = _row(engine)
    assert row.trace_id == trace_id
    assert row.project == "huadian"
    assert row.component == "collect"  # defaults to step_name
    assert row.operation == "guardian_check"
    assert row.input_hash is not None
    assert row.parse_status == "success"
    assert row.output_parsed["action"] == "pass"
    assert row.output_parsed["score"] == 0.95
    assert row.output_parsed["issues"] == ["minor"]
    assert row.output_parsed["semantic_score"] == 4


def test_component_override(tg, engine):
    write_trace_from_guardian(
        _FakeStepOutput(), _FakeDecision(), project="p", component="custom", tracer=tg
    )
    assert _row(engine).component == "custom"


def test_feature_as_of_from_arg_makes_trace_pit_stamped(tg, engine):
    write_trace_from_guardian(
        _FakeStepOutput(), _FakeDecision(), project="p", tracer=tg, feature_as_of=AS_OF
    )
    assert _row(engine).feature_as_of == AS_OF


def test_feature_as_of_falls_back_to_metadata(tg, engine):
    out = _FakeStepOutput(metadata={"feature_as_of": AS_OF})
    write_trace_from_guardian(out, _FakeDecision(), project="p", tracer=tg)
    assert _row(engine).feature_as_of == AS_OF


def test_input_hash_is_deterministic(tg, engine):
    out = _FakeStepOutput(output_data={"a": 1, "b": 2})
    write_trace_from_guardian(out, _FakeDecision(), project="p", tracer=tg)
    h1 = _row(engine).input_hash
    # same payload, second tracer/engine -> same hash
    from traceguard.store.models import make_engine

    eng2 = make_engine("sqlite:///:memory:", create_all=True)
    write_trace_from_guardian(
        _FakeStepOutput(output_data={"a": 1, "b": 2}), _FakeDecision(), project="p",
        tracer=Tracer(engine=eng2),
    )
    with Session(eng2) as s:
        h2 = s.scalars(select(Trace)).one().input_hash
    assert h1 == h2


def test_fail_open_on_malformed_output_returns_none(tg, engine):
    class _Boom:
        step_name = "s"
        metadata: dict = {}
        output_as_dict = None  # not callable -> skipped
        output_as_string = None

        @property
        def output_data(self):
            raise RuntimeError("boom")

    # Must not raise, must return None, must write nothing.
    result = write_trace_from_guardian(_Boom(), _FakeDecision(), project="p", tracer=tg)
    assert result is None
    with Session(engine) as s:
        assert s.scalars(select(Trace)).all() == []


def test_partial_decision_does_not_crash(tg, engine):
    class _BareDecision:
        action = "abort"  # only the action; everything else missing

    write_trace_from_guardian(_FakeStepOutput(), _BareDecision(), project="p", tracer=tg)
    row = _row(engine)
    assert row.output_parsed["action"] == "abort"
    assert row.output_parsed["score"] is None
    assert row.output_parsed["issues"] == []


def test_bridge_never_imports_guardian():
    # The two-package firewall: the bridge must duck-type, never import guardian.
    src = Path(__file__).resolve().parent.parent / "src" / "traceguard" / "bridges" / "guardian.py"
    text = src.read_text()
    assert "import guardian" not in text
    assert "from guardian" not in text

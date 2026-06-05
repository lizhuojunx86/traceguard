"""Tests for the generic reverse_calc structural check (E1).

The check is domain-agnostic: it operates only on configured field names,
boundary values, and a ground-truth ``sigma_floor``. These tests use the real
case-2 cross-batch yield signature as the positive fixture and a natural-
variance series as the negative control, plus boundary/edge cases.
"""
import json

from guardian.core.config import (
    ReverseCalcConfig,
    SpecEdge,
    StructuralCheckConfig,
)
from guardian.core.step import StepOutput
from guardian.validators.structural import (
    FLAG_STANDARD,
    FLAG_SUSPICION,
    validate_structural,
)


class FakeReader:
    """Minimal TraceReader stand-in returning canned prior traces.

    Each prior trace mimics what TraceWriter persists: an ``output_preview``
    holding the step output as a JSON string.
    """

    def __init__(self, prior_values, field_name="yield_pct", extra=None):
        self._traces = []
        for v in prior_values:
            payload = {field_name: v}
            if extra:
                payload.update(extra)
            self._traces.append({"output_preview": json.dumps(payload)})

    def query_traces(self, pipeline_name=None, step_name=None, days=7, limit=100):
        return self._traces[:limit]


def _rc(**overrides):
    base = dict(
        target_field="yield_pct",
        window_batches=6,
        sigma_floor=0.5,
        sigma_ratio=20.0,
        edge_band=1.0,
        group_field="product",
        spec_edges={"WK": SpecEdge(type="interval_low", value=40.0)},
    )
    base.update(overrides)
    return ReverseCalcConfig(**base)


def _out(yield_pct, product="WK"):
    return StepOutput(
        step_name="cross_batch_yield",
        output_data={"product": product, "yield_pct": yield_pct},
    )


# --- positive: the real case-2 6-batch signature ---------------------------


def test_fires_on_flat_near_edge_series():
    # Real GO-D-alpha cross-batch yields: sigma ~= 0.022pp, hugging 40.0 floor.
    prior = [40.27, 40.26, 40.28, 40.32, 40.30]  # 5 prior + 1 current = 6
    config = StructuralCheckConfig(reverse_calc=_rc())
    result = validate_structural(
        _out(40.30),
        config,
        reader=FakeReader(prior),
        pipeline_name="tcm-extraction-qa",
        step_name="cross_batch_yield",
    )
    assert result.passed is False
    assert result.flag_type == FLAG_SUSPICION
    assert any("reverse-calc suspicion" in i for i in result.issues)


# --- negative control: natural variance should NOT fire --------------------


def test_does_not_fire_on_natural_variance():
    prior = [39.1, 41.8, 42.5, 38.7, 41.0]
    config = StructuralCheckConfig(reverse_calc=_rc())
    result = validate_structural(
        _out(40.3),
        config,
        reader=FakeReader(prior),
        pipeline_name="tcm-extraction-qa",
        step_name="cross_batch_yield",
    )
    assert result.passed is True
    assert result.flag_type == FLAG_STANDARD


def test_flat_but_far_from_edge_does_not_fire():
    # Flat series, but mean is nowhere near the 40.0 edge -> not hugging.
    prior = [55.0, 55.01, 54.99, 55.0, 55.02]
    config = StructuralCheckConfig(reverse_calc=_rc())
    result = validate_structural(
        _out(55.0),
        config,
        reader=FakeReader(prior),
        pipeline_name="p",
        step_name="s",
    )
    assert result.passed is True


# --- no-op / safety boundaries ---------------------------------------------


def test_insufficient_history_is_noop():
    prior = [40.27, 40.26]  # only 3 total < window_batches=6
    config = StructuralCheckConfig(reverse_calc=_rc())
    result = validate_structural(
        _out(40.30),
        config,
        reader=FakeReader(prior),
        pipeline_name="p",
        step_name="s",
    )
    assert result.passed is True


def test_no_reverse_calc_config_is_noop():
    config = StructuralCheckConfig()  # reverse_calc absent
    result = validate_structural(_out(40.30), config, reader=FakeReader([40.3] * 5))
    assert result.passed is True
    assert result.flag_type == FLAG_STANDARD


def test_missing_target_field_is_noop():
    config = StructuralCheckConfig(reverse_calc=_rc())
    out = StepOutput(step_name="s", output_data={"product": "WK"})  # no yield_pct
    result = validate_structural(
        out, config, reader=FakeReader([40.3] * 5), pipeline_name="p", step_name="s"
    )
    assert result.passed is True


def test_unknown_group_value_is_noop():
    config = StructuralCheckConfig(reverse_calc=_rc())
    # product "ZZZ" has no configured spec edge -> cannot conclude
    result = validate_structural(
        _out(40.30, product="ZZZ"),
        config,
        reader=FakeReader([40.27, 40.26, 40.28, 40.32, 40.30]),
        pipeline_name="p",
        step_name="s",
    )
    assert result.passed is True


def test_no_reader_without_enough_inline_data_is_noop():
    # Without a reader, only the current sample is available -> insufficient.
    config = StructuralCheckConfig(reverse_calc=_rc())
    result = validate_structural(_out(40.30), config)
    assert result.passed is True


def test_truncated_preview_skipped_gracefully():
    # A non-JSON / truncated preview must not crash; it is simply skipped,
    # which then yields insufficient history (the E4 motivation).
    bad_reader = FakeReader([40.27, 40.26, 40.28, 40.32, 40.30])
    bad_reader._traces[1]["output_preview"] = '{"yield_pct": 40.2'  # truncated
    config = StructuralCheckConfig(reverse_calc=_rc())
    result = validate_structural(
        _out(40.30),
        config,
        reader=bad_reader,
        pipeline_name="p",
        step_name="s",
    )
    # 6 - 1 unparseable = 5 valid < window 6 -> no-op (passed), no exception
    assert result.passed is True


def test_single_edge_without_group_field():
    config = StructuralCheckConfig(
        reverse_calc=_rc(
            group_field=None,
            spec_edges={"only": SpecEdge(type="benchmark", value=40.0)},
        )
    )
    result = validate_structural(
        _out(40.30),
        config,
        reader=FakeReader([40.27, 40.26, 40.28, 40.32, 40.30]),
        pipeline_name="p",
        step_name="s",
    )
    assert result.passed is False
    assert result.flag_type == FLAG_SUSPICION

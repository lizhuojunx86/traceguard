"""Tests for look-ahead invariant validators (SPEC §4.5, §5)."""
from __future__ import annotations

import warnings
from datetime import datetime, timezone

import pytest

from traceguard.registry import register_model
from traceguard.validators import (
    InvariantViolation,
    validate_feature_as_of,
    validate_model_timing,
    validate_reference_timing,
)


UTC = timezone.utc


# ---------- Invariant 1 ----------


def _trace_like(feature_as_of):
    class T:
        pass

    t = T()
    t.feature_as_of = feature_as_of
    return t


def test_feature_as_of_passes_when_output_le_inputs():
    inputs = [
        _trace_like(datetime(2025, 5, 1, tzinfo=UTC)),
        _trace_like(datetime(2025, 5, 2, tzinfo=UTC)),
    ]
    validate_feature_as_of(inputs, datetime(2025, 5, 1, tzinfo=UTC))  # equal is ok


def test_feature_as_of_raises_when_output_after_earliest_input():
    inputs = [
        _trace_like(datetime(2025, 5, 1, tzinfo=UTC)),
        _trace_like(datetime(2025, 5, 10, tzinfo=UTC)),
    ]
    with pytest.raises(InvariantViolation) as ei:
        validate_feature_as_of(inputs, datetime(2025, 5, 2, tzinfo=UTC))
    assert ei.value.invariant == 1


def test_feature_as_of_raises_when_no_input_timestamps():
    class Bare:
        pass

    with pytest.raises(InvariantViolation, match="no input trace exposed"):
        validate_feature_as_of([Bare()], datetime(2025, 5, 1, tzinfo=UTC))


def test_feature_as_of_accepts_dicts():
    inputs = [{"feature_as_of": datetime(2025, 5, 1, tzinfo=UTC)}]
    validate_feature_as_of(inputs, datetime(2025, 5, 1, tzinfo=UTC))


# ---------- Invariant 2 ----------


def test_model_timing_strict_passes_when_available_in_past(engine):
    register_model(
        "m-1",
        model_family="anthropic",
        capability_class="general-llm",
        released_at=datetime(2024, 1, 1, tzinfo=UTC),
        available_to_us_at=datetime(2024, 1, 5, tzinfo=UTC),
        engine=engine,
    )
    validate_model_timing("m-1", datetime(2025, 6, 1, tzinfo=UTC), strict=True, engine=engine)


def test_model_timing_strict_raises_when_available_in_future(engine):
    register_model(
        "m-future",
        model_family="anthropic",
        capability_class="general-llm",
        released_at=datetime(2026, 1, 1, tzinfo=UTC),
        available_to_us_at=datetime(2026, 1, 5, tzinfo=UTC),
        engine=engine,
    )
    with pytest.raises(InvariantViolation) as ei:
        validate_model_timing(
            "m-future", datetime(2025, 6, 1, tzinfo=UTC), strict=True, engine=engine
        )
    assert ei.value.invariant == 2


def test_model_timing_loose_warns_instead_of_raising(engine):
    register_model(
        "m-future",
        model_family="anthropic",
        capability_class="general-llm",
        released_at=datetime(2026, 1, 1, tzinfo=UTC),
        available_to_us_at=datetime(2026, 1, 5, tzinfo=UTC),
        engine=engine,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        validate_model_timing(
            "m-future", datetime(2025, 6, 1, tzinfo=UTC), strict=False, engine=engine
        )
    assert any("anachronism" in str(w.message) for w in caught)


def test_model_timing_unregistered_raises(engine):
    with pytest.raises(InvariantViolation, match="not registered"):
        validate_model_timing(
            "nonexistent", datetime(2025, 6, 1, tzinfo=UTC), strict=True, engine=engine
        )


# ---------- Invariant 3 ----------


def test_reference_timing_passes_when_valid_from_before_feature():
    validate_reference_timing(
        valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        feature_as_of=datetime(2025, 6, 1, tzinfo=UTC),
        kind="prompt_template",
    )


def test_reference_timing_raises_when_valid_from_after_feature():
    with pytest.raises(InvariantViolation) as ei:
        validate_reference_timing(
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            feature_as_of=datetime(2025, 6, 1, tzinfo=UTC),
            kind="entity_alias",
        )
    assert ei.value.invariant == 3
    assert "entity_alias" in str(ei.value)

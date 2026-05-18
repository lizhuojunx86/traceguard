"""Look-ahead bias invariant validators (SPEC §4.5, §5).

All functions are pure (no side effects beyond raising) so business code can
call them directly inside pytest assertions. They raise ``InvariantViolation``
on violation; loose mode for invariant 2 emits a ``UserWarning`` instead.

Phase 0 covers invariants 1 and 2 fully and provides a general validator for
invariant 3 (time-versioned reference data). Invariant 4 (locked replay set
immutability) ships when replay tables exist in Phase 2.
"""
from __future__ import annotations

import warnings
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard.store.models import ModelRegistryEntry, make_engine


class InvariantViolation(AssertionError):
    """Raised when a look-ahead bias invariant is violated.

    Attributes:
        invariant: Which invariant (1, 2, 3, or 4) was violated.
    """

    def __init__(self, invariant: int, message: str) -> None:
        super().__init__(f"[invariant {invariant}] {message}")
        self.invariant = invariant


def _input_timestamp(item: Any) -> datetime | None:
    """Pick the most appropriate timestamp from an input trace-like object."""
    for attr in ("feature_as_of", "invoked_at", "recorded_at", "acceptance_ts"):
        value = getattr(item, attr, None)
        if isinstance(value, datetime):
            return value
        if isinstance(item, dict) and isinstance(item.get(attr), datetime):
            return item[attr]
    return None


def validate_feature_as_of(
    input_traces: Iterable[Any],
    output_feature_as_of: datetime,
) -> None:
    """Invariant 1: output feature_as_of MUST be <= min(input timestamps).

    Each item in ``input_traces`` may be a ``Trace`` ORM instance, a dict, or
    any object that exposes one of ``feature_as_of`` / ``invoked_at`` /
    ``recorded_at`` / ``acceptance_ts``. Items without any such attribute are
    skipped — but if no item contributes a timestamp, the check raises.
    """
    timestamps: list[datetime] = []
    for item in input_traces:
        ts = _input_timestamp(item)
        if ts is not None:
            timestamps.append(ts)
    if not timestamps:
        raise InvariantViolation(
            1,
            "no input trace exposed a timestamp (feature_as_of / invoked_at / "
            "recorded_at / acceptance_ts) — cannot establish input lower bound",
        )
    earliest_input = min(timestamps)
    if output_feature_as_of > earliest_input:
        raise InvariantViolation(
            1,
            f"output feature_as_of={output_feature_as_of.isoformat()} is after "
            f"earliest input timestamp {earliest_input.isoformat()}",
        )


def validate_model_timing(
    model_id: str,
    feature_as_of: datetime,
    *,
    strict: bool,
    engine: Engine | None = None,
) -> None:
    """Invariant 2: model.available_to_us_at MUST be <= feature_as_of.

    ``strict`` is keyword-only required, matching ``select_model`` discipline.
    In strict mode a violation raises. In loose mode anachronism is permitted
    but a ``UserWarning`` is emitted so callers see it in logs.
    """
    eng = engine if engine is not None else make_engine()
    with Session(eng) as sess:
        entry = sess.get(ModelRegistryEntry, model_id)
    if entry is None:
        raise InvariantViolation(
            2, f"model_id={model_id!r} is not registered in model_registry"
        )
    if entry.available_to_us_at <= feature_as_of:
        return
    msg = (
        f"model {model_id!r} available_to_us_at={entry.available_to_us_at.isoformat()} "
        f"is after feature_as_of={feature_as_of.isoformat()}"
    )
    if strict:
        raise InvariantViolation(2, msg)
    warnings.warn(
        f"loose-mode anachronism: {msg}; caller must apply a discount factor",
        UserWarning,
        stacklevel=2,
    )


def validate_reference_timing(
    valid_from: datetime,
    feature_as_of: datetime,
    *,
    kind: str,
) -> None:
    """Invariant 3: any time-versioned reference data MUST satisfy
    ``valid_from <= feature_as_of``.

    ``kind`` is a free-form tag (e.g. ``"prompt_template"``, ``"entity_alias"``)
    used only in the error message for debuggability.
    """
    if valid_from <= feature_as_of:
        return
    raise InvariantViolation(
        3,
        f"reference data kind={kind!r} valid_from={valid_from.isoformat()} "
        f"is after feature_as_of={feature_as_of.isoformat()}",
    )

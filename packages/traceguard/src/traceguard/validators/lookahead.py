"""Look-ahead bias invariant validators (SPEC §4.5, §5).

These raise ``InvariantViolation`` on violation (loose mode for invariant 2
emits a ``UserWarning`` instead) and are designed to be called directly inside
pytest/CI assertions (SPEC §7.4).

Most are side-effect-free given their arguments. Two — ``validate_model_timing``
(invariant 2) and ``assert_replay_set_locked`` (invariant 4) — necessarily read
registry/replay state from the store, so they accept an optional ``engine`` and
are not literally pure. The SPEC §4.5 "pure function" wording is an
idealization; the binding guarantee is "no side effects beyond raising and
reading the store", which all four honor.

All four invariants are now implemented: 1 (feature_as_of monotonicity), 2
(model timing), 3 (time-versioned reference data), and 4 (locked replay-set
immutability), whose physical write-rejection lives in the store layer.
"""
from __future__ import annotations

import warnings
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from traceguard.store.models import ModelRegistryEntry, ReplaySet, make_engine


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


def assert_replay_set_locked(
    replay_set_id: str,
    *,
    engine: Engine | None = None,
) -> None:
    """Invariant 4: the named replay set MUST exist and be locked.

    Consumers call this in CI (SPEC §7.4) to guarantee a regression / A/B set
    they depend on is immutable, so results from different periods stay
    comparable. The physical write-rejection lives in the store layer
    (``ReplaySetLockedError``); this is the read-side assertion.

    Reads ``replay_sets`` from the store; ``engine`` defaults to ``make_engine``.
    A missing ``replay_sets`` table (un-migrated DB) surfaces as a clear
    ``InvariantViolation`` rather than a raw ``OperationalError``.
    """
    eng = engine if engine is not None else make_engine()
    try:
        with Session(eng) as sess:
            entry = sess.get(ReplaySet, replay_set_id)
    except OperationalError as exc:
        raise InvariantViolation(
            4,
            f"could not read replay_set {replay_set_id!r}: the replay_sets table "
            "is missing — create the schema (e.g. make_engine()) before asserting "
            f"invariant 4 ({exc})",
        ) from exc
    if entry is None:
        raise InvariantViolation(
            4, f"replay_set {replay_set_id!r} is not registered"
        )
    if not entry.is_locked:
        raise InvariantViolation(
            4,
            f"replay_set {replay_set_id!r} exists but is not locked; lock it so "
            "A/B and regression results stay comparable",
        )

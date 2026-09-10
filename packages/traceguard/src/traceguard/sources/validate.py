"""Invariant 3 applied to retrieved data (SPEC v1.2 §5).

``published_at`` — what the source claims about when its content became
valid — is the ``valid_from`` of invariant 3. This module is the judgement,
and it produces one of four verdicts rather than a bool, following the
precedent of :class:`traceguard.routing_integrity.check.Verdict`: a check that
ran but had nothing to compare against must not report the same thing as a
check that passed.

The four states, and why none of them collapses into another:

- ``VERIFIED`` — ``published_at <= feature_as_of``. The content the host used
  demonstrably existed at the simulated moment.
- ``ANACHRONISTIC`` — ``published_at > feature_as_of``. The content did not
  exist yet. This is the defect invariant 3 exists to catch.
- ``UNVERIFIABLE`` — the source states no ``published_at``. Common: vendor
  JSON endpoints with no ``Last-Modified``, MCP tool results. Being unable to
  prove existence is NOT proof of absence, so this is its own verdict.
- ``UNCHECKED`` — the call site passed no ``feature_as_of``. Nothing was
  compared at all. Distinct from ``UNVERIFIABLE`` on purpose: one is "the
  source would not say", the other is "we never asked". Drift statistics
  count neither as an observation.

Strict versus loose is the same discipline ``select_model`` and
``validate_model_timing`` already impose: the mode is explicit at the call
site, and the two modes say different things about a source that cannot prove
itself (SPEC v1.2 §5, decision D1).

This module does not touch :mod:`traceguard.validators.lookahead`'s four
function signatures; it reuses :func:`validate_reference_timing` for the
comparison it already owns, so there is exactly one place where "valid_from
after feature_as_of" is decided.
"""
from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum

from traceguard.sources.record import SourceSnapshot
from traceguard.validators.lookahead import InvariantViolation, validate_reference_timing

_log = logging.getLogger("traceguard.sources")

#: ``kind`` passed to :func:`validate_reference_timing`, so a violation message
#: names the reference-data class the way every other invariant-3 call site does.
REFERENCE_KIND = "retrieved_source"


class SourceVerdict(str, Enum):
    """What invariant 3 on this snapshot is worth.

    Ordered worst to best so ``max``/sorting behave sensibly in reports, the
    same convention as ``routing_integrity.Verdict``.
    """

    #: The source's own published_at is AFTER feature_as_of — the content did
    #: not exist at the simulated moment. The defect invariant 3 catches.
    ANACHRONISTIC = "anachronistic"
    #: The source states no published_at, so its existence at feature_as_of can
    #: be neither confirmed nor ruled out. Not a pass.
    UNVERIFIABLE = "unverifiable"
    #: No feature_as_of at the call site: no comparison was made at all.
    UNCHECKED = "unchecked"
    #: published_at <= feature_as_of. The content demonstrably already existed.
    VERIFIED = "verified"

    @property
    def actionable(self) -> bool:
        """True when this snapshot must not be trusted for a point-in-time claim.

        ``UNCHECKED`` is not in that set: nothing was claimed, so there is
        nothing to distrust. Counting it would let un-instrumented call sites
        inflate the report, which is the fastest way to teach someone to ignore
        it (SPEC B3.4 / the routing_integrity ``FAILED_CALL`` precedent).
        """
        return self in (SourceVerdict.ANACHRONISTIC, SourceVerdict.UNVERIFIABLE)


def validate_source_snapshot(
    snapshot: SourceSnapshot,
    feature_as_of: datetime | None,
    *,
    strict: bool,
) -> SourceVerdict:
    """Judge one snapshot against ``feature_as_of``; return its verdict.

    In strict mode both failure states raise :class:`InvariantViolation`
    (invariant 3): an anachronistic source, and a source that cannot establish
    it existed. In loose mode neither raises — the verdict is returned and the
    row is recorded carrying it.

    ``strict`` is keyword-only and has no default here for the same reason
    ``select_model``'s does not: the mode is a decision, and every call site
    should have to state it.
    """
    if feature_as_of is None:
        return SourceVerdict.UNCHECKED

    if feature_as_of.tzinfo is None:
        # A naive feature_as_of cannot be compared against published_at, which
        # __post_init__ guarantees is aware — the bare `<=` inside
        # validate_reference_timing raises TypeError. That TypeError used to
        # escape record_source onto the HOST's stack, so a snapshot recorded
        # with strict=False broke the instrumented call: a §4.1 violation, and
        # the more galling for being caused by span state the tracer itself
        # accepted without complaint.
        #
        # Handled the way resolve_feature_as_of already handles the identical
        # input for the same reason: fail open with a warning, not an
        # exception. UNCHECKED is the honest verdict — nothing WAS compared —
        # and strict still refuses, so strict never silently passes a source it
        # could not check.
        if strict:
            raise ValueError(
                f"feature_as_of={feature_as_of!r} is a naive datetime, so it cannot be "
                f"compared against the tz-aware published_at of {snapshot.source_uri!r}. "
                "Pass a tz-aware datetime, e.g. datetime.now(timezone.utc), or "
                "traceguard.resolve_feature_as_of(value) which downgrades a naive one "
                "to None."
            )
        _log.warning(
            "feature_as_of is a naive datetime (no tzinfo); recording the snapshot for "
            "%s as 'unchecked' because it cannot be compared against published_at — "
            "pass a tz-aware datetime, e.g. datetime.now(timezone.utc)",
            snapshot.source_uri,
        )
        return SourceVerdict.UNCHECKED

    if snapshot.published_at is None:
        if strict:
            raise InvariantViolation(
                3,
                f"source {snapshot.source_uri!r} states no published_at: cannot "
                f"establish that the source existed at feature_as_of="
                f"{feature_as_of.isoformat()}. Pass strict=False to record this as "
                "'unverifiable' instead of refusing it.",
            )
        return SourceVerdict.UNVERIFIABLE

    try:
        # One decision point for "valid_from after feature_as_of", shared with
        # every other invariant-3 call site (SPEC §4.5).
        validate_reference_timing(
            snapshot.published_at, feature_as_of, kind=REFERENCE_KIND
        )
    except InvariantViolation:
        if strict:
            raise
        return SourceVerdict.ANACHRONISTIC
    return SourceVerdict.VERIFIED

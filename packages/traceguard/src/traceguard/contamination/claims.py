"""Claim-level temporal verification.

The ``ClaimVerifier`` protocol (the interface) shipped in 0.3.0; 0.4.0 adds a
reference implementation, ``TimelineClaimVerifier``, over a pluggable
``EvidenceSource``. The temporal *logic* is in-package and dependency-free; the
heavy, opinionated parts (extracting claims from a model answer, retrieving the
earliest supporting source) stay behind the ``EvidenceSource`` seam so the SDK
takes on no retrieval/LLM dependency.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ClaimVerdict:
    """Outcome of verifying one model-generated claim against the timeline."""

    claim: str
    supported_as_of: datetime | None  # earliest source timestamp supporting it
    is_contaminated: bool  # model asserted it before any source existed


@runtime_checkable
class ClaimVerifier(Protocol):
    """Protocol for claim-level temporal verification.

    An implementation checks whether a model-generated claim could have been
    *known* by the simulated cutoff: it finds the earliest evidence supporting
    the claim and flags contamination when the model stated it before any source
    existed (``supported_as_of`` is None or later than ``as_of``).

    Only the interface ships in the groundwork — real implementations need
    retrieval / LLM dependencies and belong behind the ``contamination`` extra.
    Keeping it a Protocol lets consumers plug their own verifier in without the
    SDK taking on those deps.
    """

    def verify(self, claim: str, *, as_of: datetime) -> ClaimVerdict:
        """Return a verdict for ``claim`` as of the simulated time ``as_of``."""
        ...


def _require_aware(dt: datetime, name: str) -> None:
    if dt.tzinfo is None:
        raise ValueError(
            f"{name} must be tz-aware (e.g. datetime.now(timezone.utc)), got naive"
        )


@runtime_checkable
class EvidenceSource(Protocol):
    """Finds the earliest time a claim was supportable by some source.

    The pluggable seam for :class:`TimelineClaimVerifier`. A real implementation
    might query a time-stamped document index, a vintage news corpus, or an
    LLM-backed retriever — none of which the SDK depends on.
    """

    def earliest_support(self, claim: str) -> datetime | None:
        """Earliest source timestamp supporting ``claim``, or ``None`` if unsupported."""
        ...


class InMemoryEvidenceSource:
    """:class:`EvidenceSource` backed by a static ``{claim: earliest_support}`` map.

    For tests, demos, and small fixed corpora. All timestamps must be tz-aware
    (mirroring the DB layer's discipline); a naive datetime raises ``ValueError``.
    """

    def __init__(self, support: Mapping[str, datetime]) -> None:
        for claim, dt in support.items():
            _require_aware(dt, f"support[{claim!r}]")
        self._support = dict(support)

    def earliest_support(self, claim: str) -> datetime | None:
        return self._support.get(claim)


class TimelineClaimVerifier:
    """Reference :class:`ClaimVerifier` over a pluggable :class:`EvidenceSource`.

    Flags a claim as contaminated when its earliest supporting source postdates
    the simulated cutoff (or no source supports it) — the model could not have
    legitimately known it at ``as_of``. This is the claim-level companion to
    :class:`traceguard.loop.EvidenceGate`, applying the same temporal rule to
    model-generated claims rather than loop memory writes.

    Args:
        source: the evidence index to query for earliest support.
    """

    def __init__(self, source: EvidenceSource) -> None:
        self._source = source

    def verify(self, claim: str, *, as_of: datetime) -> ClaimVerdict:
        """Return a verdict for ``claim`` as of the simulated time ``as_of``.

        ``is_contaminated`` is True when no supporting source exists, or the
        earliest one is dated after ``as_of``.

        Raises:
            ValueError: if ``as_of`` (or a source timestamp) is tz-naive.
        """
        _require_aware(as_of, "as_of")
        supported = self._source.earliest_support(claim)
        if supported is not None:
            _require_aware(supported, "earliest_support result")
        is_contaminated = supported is None or supported > as_of
        return ClaimVerdict(
            claim=claim, supported_as_of=supported, is_contaminated=is_contaminated
        )

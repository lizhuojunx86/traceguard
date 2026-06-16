"""Tests for the claim-verification reference implementation (0.4.0, additive)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from traceguard.contamination import (
    ClaimVerifier,
    EvidenceSource,
    InMemoryEvidenceSource,
    TimelineClaimVerifier,
)


def _dt(year: int, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


_SUPPORT = {
    "fed cut rates in march": _dt(2024, 3, 20),
    "company beat earnings": _dt(2024, 1, 10),
}


def test_in_memory_source_satisfies_protocol():
    assert isinstance(InMemoryEvidenceSource(_SUPPORT), EvidenceSource)


def test_verifier_satisfies_claim_verifier_protocol():
    verifier = TimelineClaimVerifier(InMemoryEvidenceSource(_SUPPORT))
    assert isinstance(verifier, ClaimVerifier)


def test_claim_supported_before_cutoff_is_clean():
    verifier = TimelineClaimVerifier(InMemoryEvidenceSource(_SUPPORT))
    verdict = verifier.verify("company beat earnings", as_of=_dt(2024, 6, 1))
    assert verdict.is_contaminated is False
    assert verdict.supported_as_of == _dt(2024, 1, 10)
    assert verdict.claim == "company beat earnings"


def test_claim_supported_after_cutoff_is_contaminated():
    verifier = TimelineClaimVerifier(InMemoryEvidenceSource(_SUPPORT))
    # The model asserted a March event as of February — it could not have known.
    verdict = verifier.verify("fed cut rates in march", as_of=_dt(2024, 2, 1))
    assert verdict.is_contaminated is True
    assert verdict.supported_as_of == _dt(2024, 3, 20)


def test_unsupported_claim_is_contaminated_with_none_support():
    verifier = TimelineClaimVerifier(InMemoryEvidenceSource(_SUPPORT))
    verdict = verifier.verify("no source ever said this", as_of=_dt(2024, 6, 1))
    assert verdict.is_contaminated is True
    assert verdict.supported_as_of is None


def test_support_exactly_at_cutoff_is_clean():
    verifier = TimelineClaimVerifier(InMemoryEvidenceSource(_SUPPORT))
    # supported_as_of == as_of is admissible (rule is strictly "after").
    verdict = verifier.verify("fed cut rates in march", as_of=_dt(2024, 3, 20))
    assert verdict.is_contaminated is False


def test_naive_as_of_raises():
    verifier = TimelineClaimVerifier(InMemoryEvidenceSource(_SUPPORT))
    with pytest.raises(ValueError, match="tz-aware"):
        verifier.verify("company beat earnings", as_of=datetime(2024, 6, 1))


def test_in_memory_source_rejects_naive_timestamp():
    with pytest.raises(ValueError, match="tz-aware"):
        InMemoryEvidenceSource({"x": datetime(2024, 1, 1)})

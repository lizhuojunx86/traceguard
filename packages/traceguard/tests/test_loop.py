"""Tests for traceguard.loop evidence-gating."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from traceguard.loop import Evidence, EvidenceGate, EvidenceRejected, evidence_gated

UTC = timezone.utc
CUTOFF = datetime(2023, 1, 1, tzinfo=UTC)


def test_admits_pre_cutoff_sourced_evidence():
    gate = EvidenceGate(cutoff=CUTOFF)
    assert gate.admit(claim="Acme revenue = $1B", source_as_of=datetime(2022, 6, 1, tzinfo=UTC)) is True
    assert len(gate.admitted) == 1
    assert gate.admitted[0].claim == "Acme revenue = $1B"


def test_rejects_unsourced_self_generated_claim():
    gate = EvidenceGate(cutoff=CUTOFF)
    assert gate.admit(claim="invented", source_as_of=None) is False
    assert gate.admitted == []


def test_rejects_post_cutoff_evidence():
    gate = EvidenceGate(cutoff=CUTOFF)
    assert gate.admit(claim="future", source_as_of=datetime(2024, 1, 1, tzinfo=UTC)) is False


def test_boundary_at_cutoff_is_admitted():
    gate = EvidenceGate(cutoff=CUTOFF)
    assert gate.admit(claim="edge", source_as_of=CUTOFF) is True


def test_strict_mode_raises_on_inadmissible():
    gate = EvidenceGate(cutoff=CUTOFF, strict=True)
    with pytest.raises(EvidenceRejected):
        gate.admit(claim="invented", source_as_of=None)


def test_evidence_dataclass():
    ev = Evidence(claim="x", source_as_of=None)
    assert ev.claim == "x"
    assert ev.source_as_of is None


def test_decorator_blocks_self_contamination():
    gate = EvidenceGate(cutoff=CUTOFF)
    memory: list[str] = []

    @evidence_gated(
        gate,
        claim_from=lambda claim, **kw: claim,
        source_as_of_from=lambda claim, *, source_as_of: source_as_of,
    )
    def remember(claim: str, *, source_as_of):
        memory.append(claim)
        return claim

    # Sourced before the cutoff -> written to memory.
    assert remember("real fact", source_as_of=datetime(2022, 1, 1, tzinfo=UTC)) == "real fact"
    # Self-invented (no source) -> blocked, memory untouched.
    assert remember("invented fact", source_as_of=None) is None
    assert memory == ["real fact"]


def test_evidence_gated_strict_raises_and_skips_fn():
    gate = EvidenceGate(cutoff=CUTOFF, strict=True)
    ran: list[str] = []

    @evidence_gated(
        gate,
        claim_from=lambda claim, **kw: claim,
        source_as_of_from=lambda claim, *, source_as_of: source_as_of,
    )
    def remember(claim: str, *, source_as_of):
        ran.append(claim)
        return claim

    with pytest.raises(EvidenceRejected):
        remember("invented", source_as_of=None)
    assert ran == []  # wrapped fn never ran


def test_naive_datetimes_rejected_with_clear_error():
    naive = datetime(2022, 6, 1)  # no tzinfo
    with pytest.raises(ValueError):
        EvidenceGate(cutoff=naive)
    gate = EvidenceGate(cutoff=CUTOFF)
    with pytest.raises(ValueError):
        gate.admit(claim="x", source_as_of=naive)

"""Claim-level temporal verification — interface only (groundwork)."""
from __future__ import annotations

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

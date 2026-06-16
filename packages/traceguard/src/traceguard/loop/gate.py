"""Evidence-gating for self-improving loops.

A self-improving / agentic loop that writes its own outputs back into memory can
contaminate itself: a claim it invented at step N becomes "evidence" at step
N+1. In a point-in-time setting that is look-ahead leakage — the memory acquires
"facts" not traceable to any source that existed before the simulated cutoff.

``EvidenceGate`` admits a memory write only if its supporting evidence has a
source timestamp at or before the cutoff. Unsourced (self-generated) claims and
claims sourced after the cutoff are rejected. This is the loop-level companion to
invariant 1 (feature_as_of monotonicity) in validators.lookahead.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import Any, Callable


class EvidenceRejected(ValueError):
    """Raised by a strict ``EvidenceGate`` when inadmissible evidence is offered."""


def _require_aware(dt: datetime, name: str) -> None:
    if dt.tzinfo is None:
        raise ValueError(
            f"{name} must be tz-aware (e.g. datetime.now(timezone.utc)), got naive"
        )


@dataclass(frozen=True)
class Evidence:
    """A candidate fact for the loop's memory.

    ``source_as_of`` is when the supporting source existed; ``None`` means the
    claim is unsourced (e.g. the model generated it itself).
    """

    claim: str
    source_as_of: datetime | None


class EvidenceGate:
    """Admit memory writes only when their evidence predates the cutoff.

    Args:
        cutoff: the simulated point in time; evidence must be sourced at or
            before this instant (tz-aware datetime).
        strict: if True, ``admit`` raises ``EvidenceRejected`` instead of
            returning False on inadmissible evidence.
    """

    def __init__(self, *, cutoff: datetime, strict: bool = False) -> None:
        _require_aware(cutoff, "cutoff")
        self.cutoff = cutoff
        self.strict = strict
        self._admitted: list[Evidence] = []

    def is_admissible(self, evidence: Evidence) -> bool:
        """True iff the evidence is sourced at or before the cutoff.

        Raises ValueError if ``source_as_of`` is a naive datetime (mirroring the
        tz-awareness the DB layer enforces elsewhere).
        """
        if evidence.source_as_of is None:
            return False
        _require_aware(evidence.source_as_of, "source_as_of")
        return evidence.source_as_of <= self.cutoff

    def admit(self, *, claim: str, source_as_of: datetime | None) -> bool:
        """Offer one claim to the gate; record and return True if admissible.

        Returns False for unsourced or post-cutoff evidence (or raises
        ``EvidenceRejected`` when ``strict``).
        """
        evidence = Evidence(claim=claim, source_as_of=source_as_of)
        if self.is_admissible(evidence):
            self._admitted.append(evidence)
            return True
        if self.strict:
            raise EvidenceRejected(
                f"evidence for claim {claim!r} is inadmissible: source_as_of="
                f"{source_as_of} is unsourced or after cutoff={self.cutoff}"
            )
        return False

    @property
    def admitted(self) -> list[Evidence]:
        """The evidence admitted so far (a copy)."""
        return list(self._admitted)


def evidence_gated(
    gate: EvidenceGate,
    *,
    claim_from: Callable[..., str],
    source_as_of_from: Callable[..., datetime | None],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a memory-write function so it only runs for admissible evidence.

    ``claim_from`` / ``source_as_of_from`` receive the wrapped call's
    ``(*args, **kwargs)`` and derive the claim text and its source timestamp
    (mirrors the ``correlation_from`` / ``feature_as_of_from`` pattern in the
    tracer). If the gate rejects the evidence the wrapped function is NOT called
    and the decorator returns None — unless the gate is strict, which raises.
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            claim = claim_from(*args, **kwargs)
            source_as_of = source_as_of_from(*args, **kwargs)
            if not gate.admit(claim=claim, source_as_of=source_as_of):
                return None
            return fn(*args, **kwargs)

        return wrapper

    return deco

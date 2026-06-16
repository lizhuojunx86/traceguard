"""Evidence-gating helpers for self-improving loops.

Only evidence traceable to a source available before a simulated cutoff is
admitted as fact, so a loop cannot contaminate itself by citing its own output.
See docs/loop-integration.md.

    from traceguard.loop import EvidenceGate, evidence_gated
"""
from traceguard.loop.gate import (
    Evidence,
    EvidenceGate,
    EvidenceRejected,
    evidence_gated,
)

__all__ = ["EvidenceGate", "Evidence", "EvidenceRejected", "evidence_gated"]

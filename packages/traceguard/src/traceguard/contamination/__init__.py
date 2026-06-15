"""Training-contamination groundwork (look-ahead *kind 1*).

Estimators and interfaces for detecting that a model was pre-trained on the
period it is being tested on. Detection only — unlike harness leakage (kind 2),
contamination cannot be structurally *refused*, only *estimated*. See
docs/POSITIONING.md for the two-kinds-of-look-ahead framing.

Deliberately minimal in 0.3.0: correct interfaces plus baselines, not full
coverage of the literature. The shipped estimators are pure standard-library;
the ``contamination`` extra reserves the dependency-isolation point for heavier
future implementations (retrieval / LLM-backed claim verification).

    from traceguard.contamination import min_k_prob, attach_contamination_score
"""
from traceguard.contamination.claims import ClaimVerdict, ClaimVerifier
from traceguard.contamination.decay import RegimeDecay, performance_decay_across_regimes
from traceguard.contamination.mia import min_k_prob
from traceguard.contamination.scoring import (
    CONTAMINATION_KEY,
    ContaminationScore,
    attach_contamination_score,
)

__all__ = [
    "min_k_prob",
    "performance_decay_across_regimes",
    "RegimeDecay",
    "ClaimVerifier",
    "ClaimVerdict",
    "ContaminationScore",
    "attach_contamination_score",
    "CONTAMINATION_KEY",
]

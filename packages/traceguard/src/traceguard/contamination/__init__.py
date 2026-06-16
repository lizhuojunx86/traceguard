"""Training-contamination groundwork (look-ahead *kind 1*).

Estimators and interfaces for detecting that a model was pre-trained on the
period it is being tested on. Detection only — unlike harness leakage (kind 2),
contamination cannot be structurally *refused*, only *estimated*. See
docs/POSITIONING.md for the two-kinds-of-look-ahead framing.

The shipped estimators are pure standard-library; the ``contamination`` extra
reserves the dependency-isolation point for heavier future implementations
(retrieval / LLM-backed claim verification). The reference logprob backend for
MIN-K% (``HFLogprobBackend``) is heavier still and lives in
:mod:`traceguard.contamination.logprobs_hf` behind the ``contamination-hf``
extra; it is not re-exported here so the dependency boundary stays explicit.

    from traceguard.contamination import min_k_prob, attach_contamination_score
"""
from traceguard.contamination.claims import (
    ClaimVerdict,
    ClaimVerifier,
    EvidenceSource,
    InMemoryEvidenceSource,
    TimelineClaimVerifier,
)
from traceguard.contamination.decay import (
    RegimeDecay,
    RegimeDecayTest,
    RegimeDecayTrend,
    performance_decay_across_regimes,
    regime_decay_test,
    regime_decay_trend,
)
from traceguard.contamination.logprobs import LogprobBackend, min_k_prob_for_text
from traceguard.contamination.mia import min_k_prob
from traceguard.contamination.scoring import (
    CONTAMINATION_KEY,
    ContaminationScore,
    attach_contamination_score,
)

__all__ = [
    "min_k_prob",
    "min_k_prob_for_text",
    "LogprobBackend",
    "performance_decay_across_regimes",
    "RegimeDecay",
    "regime_decay_test",
    "RegimeDecayTest",
    "regime_decay_trend",
    "RegimeDecayTrend",
    "ClaimVerifier",
    "ClaimVerdict",
    "EvidenceSource",
    "InMemoryEvidenceSource",
    "TimelineClaimVerifier",
    "ContaminationScore",
    "attach_contamination_score",
    "CONTAMINATION_KEY",
]

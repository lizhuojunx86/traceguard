"""Performance decay across time regimes — a contamination signal."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True)
class RegimeDecay:
    """Result of comparing model performance between two time regimes."""

    regime_means: dict[str, float]
    decay: float
    flagged: bool


def performance_decay_across_regimes(
    scores_by_regime: Mapping[str, Sequence[float]],
    *,
    baseline_regime: str,
    comparison_regime: str,
    threshold: float = 0.1,
) -> RegimeDecay:
    """Compare model performance between two regimes (e.g. pre/post training cutoff).

    A model contaminated on pre-cutoff data tends to score higher on the
    baseline (pre-cutoff) regime than on the comparison (post-cutoff) one. The
    gap is a contamination signal that survives even when MIN-K% is inconclusive.

    ``decay = mean(baseline) - mean(comparison)``; ``flagged`` is True when
    ``decay > threshold``. A *higher* metric is assumed to be *better* (accuracy,
    information coefficient, hit-rate); invert your metric first if lower is
    better.

    Args:
        scores_by_regime: regime label -> sequence of per-example metric values.
        baseline_regime: the regime expected to be inflated by contamination
            (typically pre-cutoff).
        comparison_regime: the honest out-of-sample regime (typically post-cutoff).
        threshold: minimum decay to flag.

    Raises:
        ValueError: if either named regime is missing or has no scores.
    """
    means = {r: mean(s) for r, s in scores_by_regime.items() if s}
    for r in (baseline_regime, comparison_regime):
        if r not in means:
            raise ValueError(f"regime {r!r} missing or has no scores")
    decay = means[baseline_regime] - means[comparison_regime]
    # Report only the two compared regimes, even if the caller passed extras.
    compared = {
        baseline_regime: means[baseline_regime],
        comparison_regime: means[comparison_regime],
    }
    return RegimeDecay(regime_means=compared, decay=decay, flagged=decay > threshold)

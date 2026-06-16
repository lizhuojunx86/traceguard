"""Performance decay across time regimes — a contamination signal."""
from __future__ import annotations

import math
import random
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


# --- statistical regime-decay tests (0.4.0, additive) ---------------------
#
# performance_decay_across_regimes above is a point estimate + fixed threshold.
# The two functions below add significance: regime_decay_test (a two-regime
# permutation test with effect size and a bootstrap CI) and regime_decay_trend
# (a Spearman monotonic-trend test across >= 2 ordered regimes). Both are pure
# standard library and fully seeded, so results are deterministic.

_TWO_REGIME_ALTERNATIVES = ("greater", "less", "two-sided")
_TREND_ALTERNATIVES = ("decreasing", "increasing", "two-sided")


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted list; ``q`` in [0, 1]."""
    if not sorted_vals:
        raise ValueError("cannot take a percentile of an empty sample")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _cliffs_delta(a: Sequence[float], b: Sequence[float]) -> float:
    """Cliff's delta in [-1, 1]: ``P(a > b) - P(a < b)`` over all pairs.

    O(len(a) * len(b)) — fine for screening-scale samples; for very large groups
    prefer a subsample.
    """
    gt = lt = 0
    for x in a:
        for y in b:
            if x > y:
                gt += 1
            elif x < y:
                lt += 1
    return (gt - lt) / (len(a) * len(b))


def _avg_ranks(values: Sequence[float]) -> list[float]:
    """Average (fractional, 1-based) ranks; tied values share the mean of their ranks."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # mean 1-based rank for the tie block [i, j]
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson correlation; returns 0.0 when either series is constant."""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0.0 or syy == 0.0:
        return 0.0
    return sxy / math.sqrt(sxx * syy)


@dataclass(frozen=True)
class RegimeDecayTest:
    """Significance-aware comparison of performance between two time regimes.

    Richer sibling of :class:`RegimeDecay`: the same ``decay`` point estimate
    plus a permutation-test ``p_value``, a Cliff's-delta ``effect_size`` in
    [-1, 1], and a percentile-bootstrap CI ``[ci_low, ci_high]`` on the decay.
    """

    regime_means: dict[str, float]
    decay: float
    p_value: float
    effect_size: float
    ci_low: float
    ci_high: float
    n: dict[str, int]
    alternative: str
    flagged: bool


def regime_decay_test(
    scores_by_regime: Mapping[str, Sequence[float]],
    *,
    baseline_regime: str,
    comparison_regime: str,
    alternative: str = "greater",
    n_resamples: int = 10_000,
    confidence: float = 0.95,
    alpha: float = 0.05,
    seed: int = 0,
) -> RegimeDecayTest:
    """Test whether performance decays from ``baseline_regime`` to ``comparison_regime``.

    Like :func:`performance_decay_across_regimes`, a *higher* metric is assumed
    *better* (accuracy, IC, hit-rate) — invert your metric first if lower is
    better. ``decay = mean(baseline) - mean(comparison)``; a contaminated model
    tends to score higher on the (pre-cutoff) baseline, so the default
    ``alternative="greater"`` tests for ``decay > 0``.

    The ``p_value`` is a permutation test (reshuffling regime labels under the
    null that membership is exchangeable), ``effect_size`` is Cliff's delta, and
    ``[ci_low, ci_high]`` is a percentile bootstrap interval on the decay. All
    resampling is seeded, so results are deterministic for a given ``seed``.

    Args:
        scores_by_regime: regime label -> per-example metric values.
        baseline_regime: regime expected to be inflated by contamination
            (typically pre-cutoff).
        comparison_regime: the honest out-of-sample regime (typically post-cutoff).
        alternative: ``"greater"`` (default), ``"less"``, or ``"two-sided"``.
        n_resamples: permutation and bootstrap iterations (each).
        confidence: bootstrap CI level in (0, 1), e.g. 0.95.
        alpha: significance threshold for ``flagged``.
        seed: RNG seed for reproducibility.

    Raises:
        ValueError: if a named regime is missing/empty, ``alternative`` is
            unknown, or a numeric parameter is out of range.
    """
    if alternative not in _TWO_REGIME_ALTERNATIVES:
        raise ValueError(
            f"alternative must be one of {_TWO_REGIME_ALTERNATIVES}, got {alternative!r}"
        )
    if n_resamples < 1:
        raise ValueError("n_resamples must be >= 1")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be in (0, 1)")

    b = [float(x) for x in scores_by_regime.get(baseline_regime, [])]
    c = [float(x) for x in scores_by_regime.get(comparison_regime, [])]
    for name, vals in ((baseline_regime, b), (comparison_regime, c)):
        if not vals:
            raise ValueError(f"regime {name!r} missing or has no scores")

    mean_b, mean_c = mean(b), mean(c)
    observed = mean_b - mean_c
    n_b, n_c = len(b), len(c)
    pooled = b + c
    total = sum(pooled)

    rng = random.Random(seed)
    ge = le = abs_ge = 0
    abs_obs = abs(observed)
    for _ in range(n_resamples):
        perm_b_sum = sum(rng.sample(pooled, n_b))
        perm = perm_b_sum / n_b - (total - perm_b_sum) / n_c
        if perm >= observed:
            ge += 1
        if perm <= observed:
            le += 1
        if abs(perm) >= abs_obs:
            abs_ge += 1
    if alternative == "greater":
        p_value = (ge + 1) / (n_resamples + 1)
    elif alternative == "less":
        p_value = (le + 1) / (n_resamples + 1)
    else:
        p_value = (abs_ge + 1) / (n_resamples + 1)

    boot: list[float] = []
    for _ in range(n_resamples):
        bb = rng.choices(b, k=n_b)
        cc = rng.choices(c, k=n_c)
        boot.append(sum(bb) / n_b - sum(cc) / n_c)
    boot.sort()
    tail = (1.0 - confidence) / 2.0
    ci_low = _percentile(boot, tail)
    ci_high = _percentile(boot, 1.0 - tail)

    effect = _cliffs_delta(b, c)
    if alternative == "greater":
        flagged = observed > 0 and p_value < alpha
    elif alternative == "less":
        flagged = observed < 0 and p_value < alpha
    else:
        flagged = p_value < alpha

    return RegimeDecayTest(
        regime_means={baseline_regime: mean_b, comparison_regime: mean_c},
        decay=observed,
        p_value=p_value,
        effect_size=effect,
        ci_low=ci_low,
        ci_high=ci_high,
        n={baseline_regime: n_b, comparison_regime: n_c},
        alternative=alternative,
        flagged=flagged,
    )


@dataclass(frozen=True)
class RegimeDecayTrend:
    """Monotonic-trend test of performance across regimes ordered in time.

    Pools every example as ``(regime_position, score)`` and reports the Spearman
    rank correlation between regime position and score. A significant *negative*
    ``rho`` means scores fall monotonically as you move along ``order`` — from
    the pre-cutoff regime toward the post-cutoff, out-of-sample region — and is
    the decay this test flags (``flagged``) as a contamination signal: a model
    inflated on pre-cutoff data collapses once it has to genuinely predict out of
    sample. A flat trend (``rho`` near 0, roughly constant performance across
    regimes) is the non-suspicious / inconclusive case. This is the trend-curve
    companion to :func:`regime_decay_test` and shares its "higher metric is
    better" convention.
    """

    order: list[str]
    regime_means: dict[str, float]
    spearman_rho: float
    p_value: float
    n: dict[str, int]
    alternative: str
    flagged: bool


def regime_decay_trend(
    scores_by_regime: Mapping[str, Sequence[float]],
    *,
    order: Sequence[str],
    alternative: str = "decreasing",
    n_resamples: int = 10_000,
    alpha: float = 0.05,
    seed: int = 0,
) -> RegimeDecayTrend:
    """Spearman monotonic-trend test of performance across ordered regimes.

    ``order`` lists the regimes from earliest to latest (e.g. by distance from
    the model cutoff). Each example contributes ``(position_in_order, score)``;
    the Spearman ``rho`` between position and score summarizes the decay curve,
    and the ``p_value`` is a permutation test (reshuffling scores against the
    fixed positions). A *higher* metric is assumed *better*.

    Args:
        scores_by_regime: regime label -> per-example metric values.
        order: >= 2 regime labels in temporal order.
        alternative: ``"decreasing"`` (default; tests ``rho < 0``, the decay
            direction), ``"increasing"``, or ``"two-sided"``.
        n_resamples: permutation iterations.
        alpha: significance threshold for ``flagged``.
        seed: RNG seed for reproducibility.

    Raises:
        ValueError: if ``order`` has < 2 regimes, names a regime missing/empty in
            ``scores_by_regime``, or ``alternative`` is unknown.
    """
    if alternative not in _TREND_ALTERNATIVES:
        raise ValueError(
            f"alternative must be one of {_TREND_ALTERNATIVES}, got {alternative!r}"
        )
    if len(order) < 2:
        raise ValueError("order must list at least two regimes")
    if n_resamples < 1:
        raise ValueError("n_resamples must be >= 1")

    positions: list[float] = []
    scores: list[float] = []
    means: dict[str, float] = {}
    counts: dict[str, int] = {}
    for pos, regime in enumerate(order):
        vals = [float(x) for x in scores_by_regime.get(regime, [])]
        if not vals:
            raise ValueError(f"regime {regime!r} missing or has no scores")
        means[regime] = mean(vals)
        counts[regime] = len(vals)
        positions.extend([float(pos)] * len(vals))
        scores.extend(vals)

    pos_ranks = _avg_ranks(positions)
    score_ranks = _avg_ranks(scores)
    rho = _pearson(pos_ranks, score_ranks)

    rng = random.Random(seed)
    shuffled = list(score_ranks)
    le = ge = abs_ge = 0
    abs_rho = abs(rho)
    for _ in range(n_resamples):
        rng.shuffle(shuffled)
        perm_rho = _pearson(pos_ranks, shuffled)
        if perm_rho <= rho:
            le += 1
        if perm_rho >= rho:
            ge += 1
        if abs(perm_rho) >= abs_rho:
            abs_ge += 1
    if alternative == "decreasing":
        p_value = (le + 1) / (n_resamples + 1)
        flagged = rho < 0 and p_value < alpha
    elif alternative == "increasing":
        p_value = (ge + 1) / (n_resamples + 1)
        flagged = rho > 0 and p_value < alpha
    else:
        p_value = (abs_ge + 1) / (n_resamples + 1)
        flagged = p_value < alpha

    return RegimeDecayTrend(
        order=list(order),
        regime_means=means,
        spearman_rho=rho,
        p_value=p_value,
        n=counts,
        alternative=alternative,
        flagged=flagged,
    )

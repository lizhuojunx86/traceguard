"""Tests for the statistical regime-decay tests (0.4.0, additive)."""
from __future__ import annotations

import pytest

from traceguard.contamination import (
    RegimeDecayTest,
    RegimeDecayTrend,
    performance_decay_across_regimes,
    regime_decay_test,
    regime_decay_trend,
)

# Strong separation: every baseline score exceeds every comparison score.
_SEPARATED = {
    "pre": [0.80, 0.82, 0.79, 0.85, 0.81, 0.83],
    "post": [0.50, 0.48, 0.52, 0.49, 0.51, 0.47],
}
# No separation: identical means, overlapping values.
_FLAT = {
    "pre": [0.50, 0.60, 0.40, 0.55, 0.45],
    "post": [0.55, 0.45, 0.60, 0.40, 0.50],
}


def test_regime_decay_test_flags_clear_separation():
    r = regime_decay_test(
        _SEPARATED, baseline_regime="pre", comparison_regime="post", n_resamples=2000
    )
    assert isinstance(r, RegimeDecayTest)
    assert r.decay > 0.25
    assert r.p_value < 0.05
    assert r.flagged is True
    assert r.effect_size == pytest.approx(1.0)  # all baseline > all comparison
    assert r.ci_low > 0.0  # bootstrap CI excludes zero
    assert r.n == {"pre": 6, "post": 6}


def test_regime_decay_test_not_flagged_when_flat():
    r = regime_decay_test(
        _FLAT, baseline_regime="pre", comparison_regime="post", n_resamples=2000
    )
    assert r.decay == pytest.approx(0.0, abs=1e-9)
    assert r.p_value > 0.05
    assert r.flagged is False


def test_regime_decay_test_is_deterministic_for_seed():
    kw = dict(baseline_regime="pre", comparison_regime="post", n_resamples=1500, seed=7)
    a = regime_decay_test(_SEPARATED, **kw)
    b = regime_decay_test(_SEPARATED, **kw)
    assert a.p_value == b.p_value
    assert (a.ci_low, a.ci_high) == (b.ci_low, b.ci_high)


def test_regime_decay_test_two_sided_detects_either_direction():
    r = regime_decay_test(
        _SEPARATED,
        baseline_regime="pre",
        comparison_regime="post",
        alternative="two-sided",
        n_resamples=2000,
    )
    assert r.alternative == "two-sided"
    assert r.p_value < 0.05
    assert r.flagged is True


def test_regime_decay_test_rejects_unknown_alternative():
    with pytest.raises(ValueError, match="alternative"):
        regime_decay_test(
            _SEPARATED, baseline_regime="pre", comparison_regime="post", alternative="up"
        )


def test_regime_decay_test_rejects_missing_regime():
    with pytest.raises(ValueError, match="missing or has no scores"):
        regime_decay_test(_SEPARATED, baseline_regime="pre", comparison_regime="absent")


def test_regime_decay_test_rejects_bad_confidence():
    with pytest.raises(ValueError, match="confidence"):
        regime_decay_test(
            _SEPARATED, baseline_regime="pre", comparison_regime="post", confidence=1.5
        )


def test_performance_decay_across_regimes_unchanged():
    # The 0.3.0 point-estimate API still behaves exactly as before.
    r = performance_decay_across_regimes(
        _SEPARATED, baseline_regime="pre", comparison_regime="post"
    )
    assert r.flagged is True
    assert r.decay > 0.25


# --- regime_decay_trend ----------------------------------------------------

_MONOTONIC = {
    "r0": [0.90, 0.88, 0.92, 0.91],
    "r1": [0.70, 0.72, 0.68, 0.71],
    "r2": [0.50, 0.48, 0.52, 0.49],
}


def test_regime_decay_trend_flags_monotonic_decline():
    r = regime_decay_trend(_MONOTONIC, order=["r0", "r1", "r2"], n_resamples=2000)
    assert isinstance(r, RegimeDecayTrend)
    assert r.spearman_rho < -0.8
    assert r.p_value < 0.05
    assert r.flagged is True
    assert r.order == ["r0", "r1", "r2"]
    assert r.n == {"r0": 4, "r1": 4, "r2": 4}


def test_regime_decay_trend_not_flagged_when_flat():
    flat = {"a": [0.5, 0.5, 0.5], "b": [0.5, 0.5, 0.5]}
    r = regime_decay_trend(flat, order=["a", "b"], n_resamples=2000)
    assert r.spearman_rho == pytest.approx(0.0)
    assert r.flagged is False


def test_regime_decay_trend_increasing_alternative():
    r = regime_decay_trend(
        _MONOTONIC, order=["r2", "r1", "r0"], alternative="increasing", n_resamples=2000
    )
    assert r.spearman_rho > 0.8
    assert r.flagged is True


def test_regime_decay_trend_requires_two_regimes():
    with pytest.raises(ValueError, match="at least two"):
        regime_decay_trend(_MONOTONIC, order=["r0"])


def test_regime_decay_trend_rejects_missing_regime():
    with pytest.raises(ValueError, match="missing or has no scores"):
        regime_decay_trend(_MONOTONIC, order=["r0", "absent"])


def test_regime_decay_trend_is_deterministic_for_seed():
    a = regime_decay_trend(_MONOTONIC, order=["r0", "r1", "r2"], n_resamples=1500, seed=3)
    b = regime_decay_trend(_MONOTONIC, order=["r0", "r1", "r2"], n_resamples=1500, seed=3)
    assert a.spearman_rho == b.spearman_rho
    assert a.p_value == b.p_value

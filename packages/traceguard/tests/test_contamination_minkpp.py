"""Tests for Min-K%++ (0.6.0, additive)."""
from __future__ import annotations

from collections.abc import Sequence

import pytest

from traceguard.contamination import (
    CalibratedLogprobBackend,
    TokenLogprobStats,
    min_k_plus_plus,
    min_k_plus_plus_for_text,
    min_k_prob,
)


def _stats(triples: list[tuple[float, float, float]]) -> list[TokenLogprobStats]:
    return [TokenLogprobStats(*t) for t in triples]


# A fixed fixture whose per-token z = (logprob - mu)/sigma are [1.0, -0.5, 1.0, -2.0].
_FIXTURE = _stats(
    [
        (-1.0, -2.0, 1.0),  # z = 1.0
        (-3.0, -2.0, 2.0),  # z = -0.5
        (-0.5, -1.0, 0.5),  # z = 1.0
        (-4.0, -1.0, 1.5),  # z = -2.0
    ]
)


def test_matches_hand_computed_z_scores():
    # lowest 50% (2 tokens) of z = [-2.0, -0.5] -> mean -1.25
    assert min_k_plus_plus(_FIXTURE, k=0.5) == pytest.approx(-1.25)
    # k=1.0 averages all z: (1.0 - 0.5 + 1.0 - 2.0) / 4 = -0.125
    assert min_k_plus_plus(_FIXTURE, k=1.0) == pytest.approx(-0.125)


def test_k_fraction_floors_to_at_least_one_token():
    # int(4 * 0.2) == 0, floored to 1 -> just the single lowest z = -2.0
    assert min_k_plus_plus(_FIXTURE, k=0.2) == pytest.approx(-2.0)


def test_aggregation_ranks_by_normalized_z_not_raw_logprob():
    # The token with the lowest *raw* logprob (-5.0) has a HIGH z (3.0, far above
    # its position's mean); the lowest z belongs to the other token. Min-K%++ must
    # rank by the normalized score, so the lowest-50% picks z=-0.5, NOT -5.0's z.
    stats = _stats(
        [
            (-5.0, -8.0, 1.0),  # lowest raw logprob, but z = 3.0
            (-1.0, -0.5, 1.0),  # higher raw logprob, but z = -0.5
        ]
    )
    assert min_k_plus_plus(stats, k=0.5) == pytest.approx(-0.5)
    # MIN-K% (raw) would instead select the -5.0 token at the same k.
    raw = min_k_prob([-5.0, -1.0], k=0.5)
    assert raw == pytest.approx(-5.0)


def test_skips_zero_sigma_tokens():
    degenerate = TokenLogprobStats(logprob=-1.0, mu=-1.0, sigma=0.0)
    assert min_k_plus_plus(_FIXTURE + [degenerate], k=1.0) == pytest.approx(
        min_k_plus_plus(_FIXTURE, k=1.0)
    )


def test_negative_sigma_treated_as_degenerate():
    # A backend should never emit sigma < 0, but if one does it must be skipped,
    # not produce a sign-flipped z.
    bad = TokenLogprobStats(logprob=-1.0, mu=-2.0, sigma=-1.0)
    assert min_k_plus_plus(_FIXTURE + [bad], k=1.0) == pytest.approx(
        min_k_plus_plus(_FIXTURE, k=1.0)
    )


def test_all_degenerate_raises():
    with pytest.raises(ValueError, match="sigma"):
        min_k_plus_plus(_stats([(-1.0, -1.0, 0.0), (-2.0, -2.0, 0.0)]), k=0.5)


def test_empty_raises():
    with pytest.raises(ValueError, match="non-empty"):
        min_k_plus_plus([], k=0.5)


@pytest.mark.parametrize("k", [0.0, -0.1, 1.1, 2.0])
def test_invalid_k_raises(k):
    with pytest.raises(ValueError, match="k must be"):
        min_k_plus_plus(_FIXTURE, k=k)


def test_memorized_scores_higher_than_natural():
    # Memorized: each token sits well above its position's mean -> high z.
    memorized = _stats([(-0.1, -3.0, 1.0)] * 5)  # z = 2.9 each
    natural = _stats([(-3.0, -2.5, 1.0)] * 5)  # z = -0.5 each
    assert min_k_plus_plus(memorized, k=0.4) > min_k_plus_plus(natural, k=0.4)


# --- backend composition --------------------------------------------------


class _FakeCalibratedBackend:
    """A CalibratedLogprobBackend returning canned stats, ignoring the text."""

    def __init__(self, stats: Sequence[TokenLogprobStats]) -> None:
        self._stats = list(stats)
        self.calls: list[str] = []

    def token_logprob_stats(self, text: str) -> Sequence[TokenLogprobStats]:
        self.calls.append(text)
        return self._stats


def test_fake_backend_satisfies_protocol():
    assert isinstance(_FakeCalibratedBackend([]), CalibratedLogprobBackend)


def test_for_text_composes_backend_and_scorer():
    backend = _FakeCalibratedBackend(_FIXTURE)
    got = min_k_plus_plus_for_text("anything", backend=backend, k=0.5)
    assert got == pytest.approx(min_k_plus_plus(_FIXTURE, k=0.5))
    assert backend.calls == ["anything"]


def test_for_text_propagates_empty_error():
    with pytest.raises(ValueError, match="non-empty"):
        min_k_plus_plus_for_text("t", backend=_FakeCalibratedBackend([]))


def test_k_floor_denominator_uses_surviving_token_count():
    # The k fraction must floor over the tokens that SURVIVE the sigma<=0 skip,
    # not the original count. 2 valid (z = 1.0, -2.0) + 4 degenerate, k=0.5:
    #   correct:  n = max(1, int(2 * 0.5)) = 1 -> lowest surviving z = -2.0
    #   buggy:    n = max(1, int(6 * 0.5)) = 3 -> mean of both survivors = -0.5
    valid = _stats([(-1.0, -2.0, 1.0), (-4.0, -1.0, 1.5)])
    degenerate = _stats([(-1.0, -1.0, 0.0)] * 4)
    score = min_k_plus_plus(valid + degenerate, k=0.5)
    assert score == pytest.approx(-2.0)
    assert score != pytest.approx(-0.5)

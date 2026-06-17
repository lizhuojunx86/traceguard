"""Membership-inference baselines: MIN-K% PROB and Min-K%++.

MIN-K% PROB (Shi et al., 2024, arXiv 2310.16789) scores the raw per-token
log-probabilities. Min-K%++ (Zhang et al., 2024, arXiv 2404.02936, ICLR'25)
improves on it by *normalizing* each token's log-prob against the mean/std of
the next-token distribution over the whole vocabulary at that position — see
:func:`min_k_plus_plus`.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


def min_k_prob(token_logprobs: Sequence[float], *, k: float = 0.2) -> float:
    """MIN-K% PROB score: mean log-prob of the least-likely ``k`` fraction of tokens.

    Intuition (Shi et al., 2024): memorized text has few very-low-probability
    tokens, so the average over the *lowest* ``k`` fraction of per-token
    log-probs is higher (closer to 0) for sequences the model saw in
    pretraining. A higher return value => more likely the text was memorized.

    Args:
        token_logprobs: per-token natural-log probabilities (each <= 0) from the
            model for the text under test.
        k: fraction in (0, 1]; defaults to 0.2 (the canonical "MIN-20% PROB").

    Returns:
        The mean log-prob of the lowest ``k`` fraction of tokens.

    Raises:
        ValueError: if ``token_logprobs`` is empty or ``k`` is outside (0, 1].

    This is a screening baseline, not proof. Corroborate with held-out tests and
    :func:`traceguard.contamination.performance_decay_across_regimes`.
    """
    if not token_logprobs:
        raise ValueError("token_logprobs must be non-empty")
    if not 0.0 < k <= 1.0:
        raise ValueError(f"k must be in (0, 1], got {k}")
    n = max(1, int(len(token_logprobs) * k))
    lowest = sorted(token_logprobs)[:n]
    return sum(lowest) / len(lowest)


@dataclass(frozen=True)
class TokenLogprobStats:
    """Per-token quantities Min-K%++ needs, beyond the bare log-prob.

    For one predicted token ``x_t`` given its prefix ``x_<t``:

    - ``logprob``: ``log p(x_t | x_<t)`` — the actual token's log-prob (``<= 0``).
    - ``mu``: the mean log-prob of the *whole vocabulary* next-token distribution
      at this position, ``E_{z~p(.|x_<t)}[log p(z | x_<t)]``.
    - ``sigma``: the standard deviation of that log-prob over the vocabulary
      (``>= 0``).

    A :class:`~traceguard.contamination.CalibratedLogprobBackend` produces a
    sequence of these from a model's full per-position logits.
    """

    logprob: float
    mu: float
    sigma: float


def min_k_plus_plus(token_stats: Sequence[TokenLogprobStats], *, k: float = 0.2) -> float:
    """Min-K%++ score (Zhang et al., 2024): mean of the lowest ``k`` fraction of
    the *normalized* per-token scores.

    Where :func:`min_k_prob` averages the raw lowest-``k``-fraction log-probs,
    Min-K%++ first standardizes each token against its position's vocabulary
    distribution::

        z_t   = (logprob_t - mu_t) / sigma_t          # Eq. 4 (per token)
        score = mean of the lowest-k% of {z_t}         # Eq. 5 (aggregate)

    Intuition: a memorized token tends to be a *local mode* of the conditional
    distribution, so its log-prob sits high above that position's mean — a large
    ``z_t``. Normalizing strips out the position's intrinsic flatness/peakedness
    that confounds the raw MIN-K%. A higher return value => more likely the text
    was in pretraining. Like :func:`min_k_prob`, this is screening, not proof.

    Args:
        token_stats: per-token :class:`TokenLogprobStats` for the text under
            test, in order. Tokens whose ``sigma <= 0`` (a degenerate next-token
            distribution, where ``z`` is undefined) are skipped and the ``k``
            fraction is taken over the rest; a real LM's vocabulary distribution
            always has ``sigma > 0``, so this only guards pathological inputs.
        k: fraction in (0, 1]; defaults to 0.2 (mirroring MIN-20% PROB). The
            paper sweeps ``k`` and reports the best, finding results fairly
            ``k``-robust.

    Returns:
        The Min-K%++ score (higher => more likely memorized).

    Raises:
        ValueError: if ``token_stats`` is empty, ``k`` is outside (0, 1], or
            every token had ``sigma <= 0`` (the score is then undefined).
    """
    if not token_stats:
        raise ValueError("token_stats must be non-empty")
    if not 0.0 < k <= 1.0:
        raise ValueError(f"k must be in (0, 1], got {k}")
    z_scores = [(s.logprob - s.mu) / s.sigma for s in token_stats if s.sigma > 0.0]
    if not z_scores:
        raise ValueError(
            "every token had sigma <= 0 (a degenerate next-token distribution); "
            "Min-K%++ is undefined"
        )
    n = max(1, int(len(z_scores) * k))
    lowest = sorted(z_scores)[:n]
    return sum(lowest) / len(lowest)

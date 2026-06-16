"""Membership-inference baseline: MIN-K% PROB (Shi et al., 2024)."""
from __future__ import annotations

from collections.abc import Sequence


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

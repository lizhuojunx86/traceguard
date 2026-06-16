"""Pluggable logprob backends for MIN-K% PROB membership inference.

:func:`min_k_prob` scores *already-computed* per-token log-probabilities. The
open question is where those come from: the Anthropic API (and most hosted chat
APIs) do **not** expose per-token logprobs, so a pure-API user cannot produce
the input at all. For those users, prefer
:func:`~traceguard.contamination.regime_decay_test` /
:class:`~traceguard.contamination.TimelineClaimVerifier`, which need no logprobs.

To run MIN-K% you supply logprobs from a model that exposes them. This module
defines the seam — the :class:`LogprobBackend` protocol — and
:func:`min_k_prob_for_text`, which composes a backend with the scorer. A
reference open-weight backend lives in
:mod:`traceguard.contamination.logprobs_hf` (extra
``traceguard[contamination-hf]``); any vLLM / OpenAI-compatible endpoint that
returns token logprobs can implement the protocol just as well.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from traceguard.contamination.mia import min_k_prob


@runtime_checkable
class LogprobBackend(Protocol):
    """Source of per-token natural-log probabilities for a text under a model.

    An implementation returns the log-probability the model assigns to each
    *actual* token of ``text`` under teacher forcing (each value ``<= 0``).

    The numbers are only comparable *within one backend/model*: a MIN-K% score
    from model A and one from model B are not on the same scale. Bring your own
    backend — :class:`~traceguard.contamination.logprobs_hf.HFLogprobBackend`,
    or any endpoint (vLLM, an OpenAI-compatible server with ``logprobs=True``)
    that can report per-token logprobs.
    """

    def token_logprobs(self, text: str) -> Sequence[float]:
        """Return per-token natural-log probabilities of ``text`` (each ``<= 0``)."""
        ...


def min_k_prob_for_text(text: str, *, backend: LogprobBackend, k: float = 0.2) -> float:
    """MIN-K% PROB for raw ``text``, computing its logprobs via ``backend``.

    Convenience composition of ``backend.token_logprobs(text)`` and
    :func:`min_k_prob`. See :func:`min_k_prob` for the score's meaning, the
    ``k`` parameter, and the screening-not-proof caveat.

    Args:
        text: the text under test (e.g. a model-generated answer or a passage
            you suspect was memorized in pretraining).
        backend: any :class:`LogprobBackend`.
        k: fraction in ``(0, 1]``; defaults to ``0.2`` (canonical MIN-20% PROB).

    Returns:
        The MIN-K% PROB score (higher => more likely memorized).

    Raises:
        ValueError: propagated from :func:`min_k_prob` if the backend returns no
            tokens, or if ``k`` is outside ``(0, 1]``. Any backend-specific
            error (e.g. a missing model) also propagates unchanged.
    """
    return min_k_prob(backend.token_logprobs(text), k=k)

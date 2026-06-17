"""HuggingFace reference :class:`LogprobBackend` (extra ``traceguard[contamination-hf]``).

Computes per-token log-probabilities for a text under a local open-weight causal
LM via teacher forcing, so :func:`~traceguard.contamination.min_k_prob_for_text`
can run MIN-K% PROB on a model whose weights you control.

The heavy dependencies (``torch``, ``transformers``) are imported **lazily**, on
first use, so importing :mod:`traceguard.contamination` — or even this module —
never pulls them in. Install them with::

    pip install "traceguard[contamination-hf]"

Caveats:

- The score is model-specific; only compare MIN-K% values computed by the same
  model (see :class:`~traceguard.contamination.LogprobBackend`).
- This audits *open-weight* models you can run. It cannot probe a closed API
  model whose weights you do not have — that is the whole reason the Anthropic
  path uses regime decay / claim verification instead.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from traceguard.contamination.mia import TokenLogprobStats


class HFLogprobBackend:
    """:class:`LogprobBackend` backed by a HuggingFace causal language model.

    Args:
        model_name_or_path: any HF causal-LM id or local path
            (e.g. ``"sshleifer/tiny-gpt2"`` for a smoke test).
        device: torch device string (``"cpu"``, ``"cuda"``, ``"mps"``); ``None``
            leaves the model on its loaded device.
        dtype: a ``torch`` dtype for the weights; ``None`` uses the model default.
        add_special_tokens: whether the tokenizer prepends/affixes special tokens
            (default ``True``). The first token's log-prob is undefined under
            teacher forcing and is dropped regardless of this setting.

    The model and tokenizer load on first call to :meth:`token_logprobs` and are
    cached on the instance for reuse.
    """

    def __init__(
        self,
        model_name_or_path: str,
        *,
        device: str | None = None,
        dtype: Any = None,
        add_special_tokens: bool = True,
    ) -> None:
        self.model_name_or_path = model_name_or_path
        self._device = device
        self._dtype = dtype
        self._add_special_tokens = add_special_tokens
        self._torch: Any = None
        self._tokenizer: Any = None
        self._model: Any = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ImportError(
                "HFLogprobBackend requires the 'contamination-hf' extra: "
                'pip install "traceguard[contamination-hf]"'
            ) from exc
        tokenizer = AutoTokenizer.from_pretrained(self.model_name_or_path)
        model = AutoModelForCausalLM.from_pretrained(
            self.model_name_or_path, torch_dtype=self._dtype
        )
        if self._device is not None:
            model = model.to(self._device)
        model.eval()
        self._torch = torch
        self._tokenizer = tokenizer
        self._model = model

    def token_logprobs(self, text: str) -> Sequence[float]:
        """Teacher-forced per-token log-probabilities of ``text`` (each ``<= 0``).

        The first token has no preceding context, so its log-prob is undefined
        and excluded; the returned list has ``len(tokens) - 1`` entries. Returns
        an empty list if the text tokenizes to fewer than two tokens.
        """
        self._ensure_loaded()
        torch = self._torch
        enc = self._tokenizer(
            text, return_tensors="pt", add_special_tokens=self._add_special_tokens
        )
        input_ids = enc["input_ids"]
        if self._device is not None:
            input_ids = input_ids.to(self._device)
        if input_ids.shape[1] < 2:
            return []
        with torch.no_grad():
            logits = self._model(input_ids).logits
        # logits[:, t, :] is the model's distribution over token t+1 given tokens
        # 0..t. Align predictions with the actual next tokens: drop the last
        # position's logits (predicts a token past the sequence) and the first
        # input id (has no prediction), then gather the log-prob of each target.
        log_probs = torch.log_softmax(logits[:, :-1, :], dim=-1)
        targets = input_ids[:, 1:]
        gathered = log_probs.gather(-1, targets.unsqueeze(-1)).squeeze(-1)
        return [float(x) for x in gathered[0].tolist()]

    def token_logprob_stats(self, text: str) -> Sequence[TokenLogprobStats]:
        """Teacher-forced per-token stats for Min-K%++: each token's log-prob plus
        the mean/std of log-prob over the whole vocabulary at that position.

        Uses the same alignment as :meth:`token_logprobs` (the first token has no
        context and is dropped); returns ``len(tokens) - 1`` entries, or an empty
        list if the text tokenizes to fewer than two tokens. For each predicted
        position, over the full next-token distribution ``p``::

            mu    = sum_z p(z) * log p(z)
            sigma = sqrt( sum_z p(z) * log p(z)^2  -  mu^2 )   # clamped to >= 0

        matching the reference Min-K%++ implementation (Zhang et al., 2024).
        """
        self._ensure_loaded()
        torch = self._torch
        enc = self._tokenizer(
            text, return_tensors="pt", add_special_tokens=self._add_special_tokens
        )
        input_ids = enc["input_ids"]
        if self._device is not None:
            input_ids = input_ids.to(self._device)
        if input_ids.shape[1] < 2:
            return []
        with torch.no_grad():
            logits = self._model(input_ids).logits
        # Same alignment as token_logprobs: drop the last position's logits and
        # the first input id. log_probs[:, t, :] is the full next-token
        # distribution given tokens 0..t.
        log_probs = torch.log_softmax(logits[:, :-1, :], dim=-1)
        probs = log_probs.exp()
        mu = (probs * log_probs).sum(dim=-1)
        # Var = E[L^2] - (E[L])^2 over the vocabulary; clamp away negative values
        # from float round-off before the sqrt.
        var = (probs * log_probs.square()).sum(dim=-1) - mu.square()
        sigma = var.clamp_min(0.0).sqrt()
        targets = input_ids[:, 1:]
        token_lp = log_probs.gather(-1, targets.unsqueeze(-1)).squeeze(-1)
        return [
            TokenLogprobStats(logprob=float(lp), mu=float(m), sigma=float(s))
            for lp, m, s in zip(
                token_lp[0].tolist(), mu[0].tolist(), sigma[0].tolist()
            )
        ]

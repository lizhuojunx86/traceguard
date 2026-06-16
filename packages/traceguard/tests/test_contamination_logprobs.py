"""Tests for the pluggable logprob backend (0.4.0, additive)."""
from __future__ import annotations

import os
from collections.abc import Sequence

import pytest

from traceguard.contamination import LogprobBackend, min_k_prob, min_k_prob_for_text


class FakeBackend:
    """A LogprobBackend that returns canned logprobs, ignoring the text."""

    def __init__(self, logprobs: Sequence[float]) -> None:
        self._logprobs = list(logprobs)
        self.calls: list[str] = []

    def token_logprobs(self, text: str) -> Sequence[float]:
        self.calls.append(text)
        return self._logprobs


def test_contamination_package_and_new_submodules_import():
    # Guard: traceguard.contamination.__init__ imports these submodules, so a
    # commit that forgets to track logprobs.py / logprobs_hf.py would make the
    # whole (already-published) contamination surface un-importable. Fail loudly.
    import importlib

    importlib.import_module("traceguard.contamination")
    importlib.import_module("traceguard.contamination.logprobs")
    importlib.import_module("traceguard.contamination.logprobs_hf")


def test_fake_backend_satisfies_protocol():
    assert isinstance(FakeBackend([-0.1]), LogprobBackend)


def test_min_k_prob_for_text_composes_backend_and_scorer():
    lp = [-0.01, -2.0, -0.5, -3.0, -0.2]
    backend = FakeBackend(lp)
    got = min_k_prob_for_text("anything", backend=backend, k=0.4)
    assert got == min_k_prob(lp, k=0.4)
    assert backend.calls == ["anything"]


def test_min_k_prob_for_text_passes_k_through():
    lp = [-0.1, -1.0, -2.0, -3.0]
    backend = FakeBackend(lp)
    # k=1.0 averages all tokens; should equal the plain mean.
    assert min_k_prob_for_text("t", backend=backend, k=1.0) == pytest.approx(
        sum(lp) / len(lp)
    )


def test_min_k_prob_for_text_propagates_empty_backend_error():
    with pytest.raises(ValueError, match="non-empty"):
        min_k_prob_for_text("t", backend=FakeBackend([]))


# --- HFLogprobBackend (reference, behind the contamination-hf extra) -------

try:  # the dev environment intentionally does not install the heavy extra
    import torch  # noqa: F401

    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False

_RUN_HF = os.environ.get("TRACEGUARD_RUN_HF_TESTS") == "1"


def test_hf_backend_importable_without_extra():
    # Importing the class must not require torch/transformers (lazy load).
    from traceguard.contamination.logprobs_hf import HFLogprobBackend

    assert HFLogprobBackend("sshleifer/tiny-gpt2").model_name_or_path


@pytest.mark.skipif(_HAS_TORCH, reason="torch installed; the missing-extra path is not exercised")
def test_hf_backend_without_extra_raises_pointing_to_extra():
    from traceguard.contamination.logprobs_hf import HFLogprobBackend

    with pytest.raises(ImportError, match="contamination-hf"):
        HFLogprobBackend("sshleifer/tiny-gpt2").token_logprobs("hello world")


@pytest.mark.skipif(
    not _RUN_HF, reason="set TRACEGUARD_RUN_HF_TESTS=1 (and install the extra) to run"
)
def test_hf_backend_real_tiny_model():
    pytest.importorskip("transformers")
    from traceguard.contamination.logprobs_hf import HFLogprobBackend

    backend = HFLogprobBackend("sshleifer/tiny-gpt2")
    logprobs = backend.token_logprobs("The quick brown fox jumps")
    assert len(logprobs) >= 1
    assert all(x <= 0.0 for x in logprobs)
    # The convenience path should run end-to-end on a real backend too.
    assert min_k_prob_for_text("The quick brown fox jumps", backend=backend) <= 0.0

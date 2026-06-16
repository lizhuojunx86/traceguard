"""Example (illustrative): detecting training contamination — look-ahead *kind 1*.

This is the OTHER look-ahead bias (see docs/POSITIONING.md). Here the leakage is
not in the harness — it is baked into the model weights. The model was
pre-trained on data covering events whose outcomes are already resolved, so when
you "backtest" it on those events it *recalls* the answer instead of reasoning
to it. No registry or invariant can refuse this structurally; it has to be
*estimated* statistically.

The estimators live in `traceguard.contamination` (shipped with the core
package, pure standard library):

  - `min_k_prob` / `min_k_prob_for_text` — MIN-K% PROB membership inference. Needs
    per-token logprobs, which the Anthropic API does not expose; supply them via
    a `LogprobBackend` (the reference `HFLogprobBackend` needs the heavier
    `traceguard[contamination-hf]` extra). This example uses a tiny fake backend
    so it runs offline.
  - `regime_decay_test` / `regime_decay_trend` — quantify and significance-test
    performance decay across time regimes (Look-Ahead-Bench style).
  - `TimelineClaimVerifier` — claim-level temporal verification over a pluggable
    evidence source.

Every number below is synthetic and labelled illustrative; it demonstrates the
API surface, not a validated contamination measurement.

Run (from the repo root)::

    uv run python examples/training_contamination.py
"""
from __future__ import annotations

import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

_PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "traceguard" / "src"
if _PKG_SRC.is_dir() and str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))


class _DemoBackend:
    """A toy LogprobBackend that returns canned logprobs so the demo runs offline.

    A real backend would compute these from a model; see
    traceguard.contamination.logprobs_hf.HFLogprobBackend.
    """

    def __init__(self, logprobs: Sequence[float]) -> None:
        self._logprobs = list(logprobs)

    def token_logprobs(self, text: str) -> Sequence[float]:  # noqa: ARG002 - canned demo
        return self._logprobs


def main() -> int:
    print(__doc__)
    print("-" * 72)

    try:
        from traceguard.contamination import (
            InMemoryEvidenceSource,
            TimelineClaimVerifier,
            min_k_prob_for_text,
            regime_decay_test,
            regime_decay_trend,
        )
    except ImportError:
        print("traceguard.contamination is not importable here — nothing to compute.")
        return 0

    def dt(year: int, month: int = 1, day: int = 1) -> datetime:
        return datetime(year, month, day, tzinfo=timezone.utc)

    # 1) MIN-K% PROB via a (fake) logprob backend ---------------------------
    print("[1] MIN-K% PROB (membership inference) — via a fake logprob backend\n")
    memorized = _DemoBackend([-0.01, -0.02, -0.01, -0.03, -0.01])  # implausibly confident
    natural = _DemoBackend([-2.1, -3.4, -1.8, -4.0, -2.7])
    s_mem = min_k_prob_for_text("<memorized passage>", backend=memorized, k=0.4)
    s_nat = min_k_prob_for_text("<fresh passage>", backend=natural, k=0.4)
    print(f"    memorized={s_mem:.3f}  natural={s_nat:.3f}  (higher => more memorized)")
    print("    A pure-Anthropic user cannot produce token logprobs — use [2]/[3] instead.\n")

    # 2) Performance decay across regimes -----------------------------------
    print("[2] Performance decay across time regimes (significance-tested)\n")
    scores = {
        "pre_cutoff": [0.81, 0.83, 0.79, 0.85, 0.80, 0.82],   # suspiciously strong
        "near_cutoff": [0.66, 0.64, 0.69, 0.65, 0.67, 0.63],
        "post_cutoff": [0.51, 0.49, 0.53, 0.48, 0.52, 0.50],  # falls apart out of sample
    }
    test = regime_decay_test(
        scores, baseline_regime="pre_cutoff", comparison_regime="post_cutoff"
    )
    print(
        f"    pre vs post: decay={test.decay:.3f} "
        f"(95% CI [{test.ci_low:.3f}, {test.ci_high:.3f}]), "
        f"p={test.p_value:.4f}, Cliff's d={test.effect_size:.2f}, flagged={test.flagged}"
    )
    trend = regime_decay_trend(
        scores, order=["pre_cutoff", "near_cutoff", "post_cutoff"]
    )
    print(
        f"    monotonic trend across 3 regimes: Spearman rho={trend.spearman_rho:.3f}, "
        f"p={trend.p_value:.4f}, flagged={trend.flagged}\n"
    )

    # 3) Claim-level temporal verification ----------------------------------
    print("[3] Claim-level temporal verification (TimelineClaimVerifier)\n")
    source = InMemoryEvidenceSource(
        {
            "Q4 revenue beat consensus": dt(2024, 1, 25),  # reported, then knowable
            "the merger was announced": dt(2024, 5, 2),
        }
    )
    verifier = TimelineClaimVerifier(source)
    as_of = dt(2024, 2, 1)
    for claim in ("Q4 revenue beat consensus", "the merger was announced", "unsourced rumor"):
        v = verifier.verify(claim, as_of=as_of)
        when = v.supported_as_of.date().isoformat() if v.supported_as_of else "never"
        flag = "CONTAMINATED" if v.is_contaminated else "ok"
        print(f"    as_of={as_of.date()}  support={when:>10}  {flag:>12}  | {claim}")

    print("\n(All figures synthetic & illustrative — they demonstrate the API, not a result.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

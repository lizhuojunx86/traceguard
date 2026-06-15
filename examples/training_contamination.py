"""Example (STUB): detecting training contamination — look-ahead *kind 1*.

This is the OTHER look-ahead bias (see docs/POSITIONING.md). Here the leakage is
not in the harness — it is baked into the model weights. The model was
pre-trained on data covering events whose outcomes are already resolved, so when
you "backtest" it on those events it *recalls* the answer instead of reasoning
to it. No registry or invariant can refuse this structurally; it has to be
*estimated* statistically.

The groundwork for that estimation ships behind an optional extra:

    pip install "traceguard[contamination]"

and lives in `traceguard.contamination` (min_k_prob, performance decay across
regimes, claim-level verification). This file is an illustrative STUB: it
sketches the scenario and points at the real interfaces, which are intentionally
minimal in 0.3.0 (correct interfaces + baselines, not full coverage).

Run (from the repo root)::

    uv run python examples/training_contamination.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "traceguard" / "src"
if _PKG_SRC.is_dir() and str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))


def main() -> int:
    print(__doc__)
    print("-" * 72)
    print(
        "Scenario: an LLM scores earnings events from 2019-2021. Those events,\n"
        "and the market's reaction, are in the model's pretraining data. The\n"
        "model looks uncannily accurate on them — and falls apart out of sample.\n"
    )
    print(
        "Three estimators in traceguard.contamination probe for this:\n"
        "  1. min_k_prob(token_logprobs, k=0.2)          # membership inference\n"
        "  2. performance_decay_across_regimes(scores)   # pre/post-cutoff gap\n"
        "  3. ClaimVerifier protocol                     # claim-level temporal check\n"
    )

    try:
        import traceguard.contamination as contamination  # noqa: F401
    except ImportError:
        print(
            "traceguard.contamination is not importable in this environment.\n"
            "Install the extra to enable the estimators:\n"
            '    pip install "traceguard[contamination]"\n'
        )
        print("(stub) nothing to compute — see docs/POSITIONING.md for the framing.")
        return 0

    # Illustrative only — synthetic, NOT a validated contamination measurement.
    suspicious = [-0.01, -0.02, -0.01, -0.03, -0.01]  # implausibly confident
    natural = [-2.1, -3.4, -1.8, -4.0, -2.7]
    score_suspicious = contamination.min_k_prob(suspicious, k=0.4)
    score_natural = contamination.min_k_prob(natural, k=0.4)
    print(
        f"(illustrative) min_k_prob suspicious={score_suspicious:.3f} "
        f"natural={score_natural:.3f} — higher => more likely memorized."
    )
    print(
        "These numbers are synthetic and prove nothing on their own; treat\n"
        "min_k_prob as a screen, corroborated by regime decay + held-out tests."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

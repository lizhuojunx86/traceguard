"""Case study (illustrative): catching training contamination in an LLM backtest.

Narrative
---------
You scored an LLM-derived alpha signal on 2021-2023 earnings events and it
backtested beautifully (information coefficient ~0.4). Live, from 2024 on, it
fell apart. The suspicion: the model was *pre-trained* on those already-resolved
events, so on pre-cutoff inputs it recalled outcomes instead of forecasting them
-- look-ahead bias baked into the weights (kind 1; see docs/POSITIONING.md). No
registry or invariant can refuse this structurally; it has to be *estimated*.

This script runs traceguard's three independent contamination signals on the
same scenario and shows how **Min-K%++ sharpens the membership-inference signal
over plain MIN-K%**:

  [1] MIN-K%  vs  Min-K%++   token-level membership inference on model text
  [2] regime decay           does accuracy collapse out-of-sample? (significance)
  [3] claim verification      did the model 'know' things before any source did?

then prints a combined verdict.

Running it
----------
By default everything runs OFFLINE on small canned inputs -- no model download,
no network::

    cd packages/traceguard
    uv run python ../../examples/contamination_case_study.py

Pass ``--hf`` to compute signal [1] for real with a small open-weight model
(``distilgpt2``, ~350 MB, downloaded on first use), which needs the extra::

    pip install "traceguard[contamination-hf]"
    uv run python ../../examples/contamination_case_study.py --hf

Honesty
-------
Every business/finance figure below is synthetic and labelled *illustrative* --
it shows the API and the *shape* of each signal, not a measured result. The
offline signal [1] uses hand-built logprob statistics chosen to demonstrate the
*mechanism* (how normalization exposes a signal that raw MIN-K% misses). The
``--hf`` path computes **real** log-probs, so its numbers are real -- but
distilgpt2 membership is not something we can prove (screening, not proof).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "traceguard" / "src"
if _PKG_SRC.is_dir() and str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))

_RULE = "-" * 74


def _dt(year: int, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _membership_offline(k: float) -> float:
    """[1] offline: hand-built stats where raw MIN-K% can't separate but Min-K%++ can.

    Returns the Min-K%++ separation (pre-cutoff minus post-cutoff) for the verdict.
    """
    from traceguard.contamination import (
        TokenLogprobStats,
        min_k_plus_plus,
        min_k_prob,
    )

    # Both passages have ~identical raw token logprobs (~-1.5), so MIN-K% sees
    # them as equally (un)likely. They differ in the *shape* of each position's
    # next-token distribution: the pre-cutoff passage sits far above a flat, wide
    # distribution (high z) -- the fingerprint of recall -- while the post-cutoff
    # one is only confident where the distribution is already peaked (low z).
    pre = [
        TokenLogprobStats(logprob=lp, mu=-6.0, sigma=2.5)
        for lp in (-1.5, -1.4, -1.6, -1.5, -1.5, -1.5)
    ]  # z ~ +1.8
    post = [
        TokenLogprobStats(logprob=lp, mu=-1.2, sigma=0.6)
        for lp in (-1.5, -1.5, -1.4, -1.6, -1.5, -1.5)
    ]  # z ~ -0.5

    pre_raw = min_k_prob([s.logprob for s in pre], k=k)
    post_raw = min_k_prob([s.logprob for s in post], k=k)
    pre_pp = min_k_plus_plus(pre, k=k)
    post_pp = min_k_plus_plus(post, k=k)

    pct = int(k * 100)
    print(f"[1] Membership inference (offline, illustrative) -- MIN-{pct}%\n")
    print(f"    {'passage':<22}{'MIN-K% (raw)':>16}{'Min-K%++ (norm.)':>20}")
    print(f"    {'pre-cutoff event':<22}{pre_raw:>16.3f}{pre_pp:>20.3f}")
    print(f"    {'post-cutoff event':<22}{post_raw:>16.3f}{post_pp:>20.3f}")
    print(
        f"    {'separation |delta|':<22}{abs(pre_raw - post_raw):>16.3f}"
        f"{abs(pre_pp - post_pp):>20.3f}"
    )
    print(
        "    -> raw MIN-K% barely separates them; Min-K%++ exposes the recall"
        " fingerprint.\n"
    )
    return pre_pp - post_pp


def _membership_hf(k: float, model: str) -> float:
    """[1] --hf: real log-probs from a small open-weight model."""
    from traceguard.contamination import (
        min_k_plus_plus_for_text,
        min_k_prob_for_text,
    )
    from traceguard.contamination.logprobs_hf import HFLogprobBackend

    # A stand-in for membership: text the model has strong priors for (it appears
    # widely in web pretraining) vs novel text built from invented proper nouns.
    familiar = "Apple is an American technology company headquartered in California."
    novel = "Zorvex Dynamics is a plasma-foundry headquartered in Qel'thar, sector V7."

    backend = HFLogprobBackend(model)
    pct = int(k * 100)
    print(f"[1] Membership inference (--hf, real log-probs from {model}) -- MIN-{pct}%\n")
    print(f"    {'passage':<10}{'MIN-K% (raw)':>16}{'Min-K%++ (norm.)':>20}")
    rows = []
    for label, text in (("familiar", familiar), ("novel", novel)):
        raw = min_k_prob_for_text(text, backend=backend, k=k)
        pp = min_k_plus_plus_for_text(text, backend=backend, k=k)
        rows.append((raw, pp))
        print(f"    {label:<10}{raw:>16.3f}{pp:>20.3f}")
    sep_raw = abs(rows[0][0] - rows[1][0])
    sep_pp = abs(rows[0][1] - rows[1][1])
    print(f"    {'separation':<10}{sep_raw:>16.3f}{sep_pp:>20.3f}")
    print(
        "    (real numbers. Both rank familiar > novel; raw and normalized scores"
        " are on\n     different scales, so a single contrast can't say which"
        " detector is better --\n     Min-K%++'s edge is ranking AUROC on labelled"
        " sets, not one separation.)\n"
    )
    return rows[0][1] - rows[1][1]


def _regime_decay(k: float) -> bool:
    from traceguard.contamination import regime_decay_test, regime_decay_trend

    print("[2] Performance decay across time regimes (significance-tested, illustrative)\n")
    scores = {
        "pre_cutoff": [0.41, 0.39, 0.43, 0.40, 0.42, 0.38],  # suspiciously strong IC
        "near_cutoff": [0.22, 0.19, 0.24, 0.21, 0.20, 0.23],
        "post_cutoff": [0.04, 0.02, 0.06, 0.03, 0.05, 0.01],  # collapses live
    }
    test = regime_decay_test(
        scores, baseline_regime="pre_cutoff", comparison_regime="post_cutoff"
    )
    trend = regime_decay_trend(scores, order=["pre_cutoff", "near_cutoff", "post_cutoff"])
    print(
        f"    pre vs post IC decay = {test.decay:.3f} "
        f"(95% CI [{test.ci_low:.3f}, {test.ci_high:.3f}]), "
        f"p={test.p_value:.4f}, Cliff's d={test.effect_size:.2f}, flagged={test.flagged}"
    )
    print(
        f"    monotonic trend across 3 regimes: rho={trend.spearman_rho:.3f}, "
        f"p={trend.p_value:.4f}, flagged={trend.flagged}\n"
    )
    return test.flagged or trend.flagged


def _claim_verification() -> bool:
    from traceguard.contamination import InMemoryEvidenceSource, TimelineClaimVerifier

    print("[3] Claim-level temporal verification (illustrative)\n")
    # The model's 'forecast', made as of 2024-02-01, asserts an outcome that no
    # source supported until weeks later -- it could only 'know' it by recall.
    source = InMemoryEvidenceSource(
        {
            "Q4 revenue beat consensus": _dt(2024, 1, 25),
            "the acquisition closed": _dt(2024, 3, 12),
        }
    )
    verifier = TimelineClaimVerifier(source)
    as_of = _dt(2024, 2, 1)
    any_contaminated = False
    for claim in (
        "Q4 revenue beat consensus",
        "the acquisition closed",
        "an unsourced rumor",
    ):
        v = verifier.verify(claim, as_of=as_of)
        when = v.supported_as_of.date().isoformat() if v.supported_as_of else "never"
        flag = "CONTAMINATED" if v.is_contaminated else "ok"
        any_contaminated = any_contaminated or v.is_contaminated
        print(
            f"    as_of={as_of.date()}  earliest_support={when:>10}  "
            f"{flag:>12}  | {claim}"
        )
    print()
    return any_contaminated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="traceguard training-contamination case study (illustrative)."
    )
    parser.add_argument(
        "--hf", action="store_true", help="compute signal [1] with a real open-weight model"
    )
    parser.add_argument(
        "--hf-model", default="distilgpt2", help="HF causal-LM id for --hf (default: distilgpt2)"
    )
    parser.add_argument(
        "--k", type=float, default=0.2, help="MIN-K%% fraction in (0,1] (default: 0.2)"
    )
    args = parser.parse_args(argv)

    print(__doc__)
    print(_RULE)
    try:
        sep = _membership_hf(args.k, args.hf_model) if args.hf else _membership_offline(args.k)
        regime_flag = _regime_decay(args.k)
        claim_flag = _claim_verification()
    except ImportError as exc:
        print(f"\nMissing optional dependency for --hf: {exc}")
        return 0

    print(_RULE)
    print("Combined verdict (illustrative):")
    membership_hit = sep > 0
    print(
        f"  [1] membership : Min-K%++ scores the pre/familiar text {sep:+.3f} above the"
        f" post/novel baseline ({'consistent with recall' if membership_hit else 'inconclusive'})"
    )
    print(f"  [2] regime     : significant out-of-sample decay = {regime_flag}")
    print(f"  [3] claims     : a claim predates any supporting source = {claim_flag}")
    signals = int(membership_hit) + int(regime_flag) + int(claim_flag)
    print(
        f"\n  -> {signals}/3 independent signals point to contamination. Three weak signals"
        "\n     agreeing is far stronger than any one alone -- but this is screening, not proof."
    )
    print("\n(All figures synthetic & illustrative unless run with --hf.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

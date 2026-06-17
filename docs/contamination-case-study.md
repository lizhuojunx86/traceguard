# Catching training contamination: a Min-K%++ case study

> **Illustrative.** Every business/finance number here is synthetic and chosen to
> show the *shape* of each signal, not a measured result. The runnable companion
> is [`examples/contamination_case_study.py`](../examples/contamination_case_study.py).
> Chinese version: [contamination-case-study.zh.md](contamination-case-study.zh.md).

## The scenario

You scored an LLM-derived alpha signal on 2021–2023 earnings events. It
backtested beautifully — information coefficient ≈ 0.4. Live, from 2024 on, it
collapsed. The likely cause is not a bug in your harness: it is that the model
was **pre-trained on the very events you backtested on**, so on pre-cutoff
inputs it *recalled* outcomes instead of forecasting them.

This is the *other* look-ahead bias — **kind 1, training contamination** — the
one that lives inside the model weights, invisible to your code (see
[POSITIONING.md](POSITIONING.md)). Unlike harness leakage (kind 2: a 2025 model
touching a 2021 backtest), no registry or invariant can *refuse* it. It can only
be **estimated**, statistically, after the fact. traceguard ships three
independent estimators; this case study runs all three and combines them.

## Signal 1 — MIN-K% vs Min-K%++ (the 0.6.0 upgrade)

[MIN-K% PROB](https://arxiv.org/abs/2310.16789) (Shi et al., 2024) is a
membership-inference baseline: memorized text has few very-low-probability
tokens, so the average log-prob over its *lowest-k%* tokens sits unusually high.
But a raw log-prob conflates two things — how likely the token is, and how
peaked or flat that position's distribution is to begin with.

**Min-K%++** ([Zhang et al., 2024, ICLR'25](https://arxiv.org/abs/2404.02936))
fixes this by *normalizing* each token against its own position's distribution
over the whole vocabulary:

```
z_t   = ( log p(x_t | x_<t) − μ_t ) / σ_t          # per token (Eq. 4)
μ_t   = E_{z∼p(·|x_<t)}[ log p(z | x_<t) ]          # mean log-prob over the vocab
σ_t   = std of log p(z | x_<t) over the vocab
score = mean of the lowest-k% of { z_t }            # aggregate (Eq. 5)
```

A memorized token tends to be a **local mode** of the conditional distribution —
its log-prob sits far *above* that position's mean (large `z`). Normalizing
strips out the position's intrinsic flatness/peakedness that confounds raw
MIN-K%. Higher score ⇒ more likely the text was in pretraining.

### What the offline demo shows

The two passages below are built to have **identical raw token logprobs**
(≈ −1.5 each), so MIN-K% sees them as equally likely. They differ only in the
*shape* of each position's distribution: the pre-cutoff passage sits high above a
flat, wide one (the recall fingerprint), the post-cutoff one is merely confident
where the distribution is already peaked.

| passage | MIN-K% (raw) | Min-K%++ (normalized) |
|---|---:|---:|
| pre-cutoff event | −1.600 | **+1.760** |
| post-cutoff event | −1.600 | **−0.667** |
| **separation \|Δ\|** | **0.000** | **2.427** |

Raw MIN-K% **cannot tell them apart** (Δ = 0.000). Min-K%++ separates them
cleanly (Δ = 2.427) by reading the normalized signal. That is the upgrade.

> On a real model (`--hf`, `distilgpt2`) both methods rank the familiar passage
> above the novel one. But raw and normalized scores live on **different scales**,
> so a single familiar-vs-novel contrast can't say which detector is better — and
> here raw MIN-K% actually shows the *larger* numeric gap (≈7.1 vs ≈2.6), because
> the novel proper nouns are intrinsically rare and raw log-probs aren't
> normalized. Min-K%++'s documented edge is **ranking** quality (+6–10% AUROC over
> MIN-K% on WikiMIA), which only a labelled dataset reveals — not a single gap.
> The offline table above isolates the *mechanism* by which normalization helps:
> a case raw MIN-K% genuinely cannot resolve.

```python
from traceguard.contamination import min_k_plus_plus_for_text
from traceguard.contamination.logprobs_hf import HFLogprobBackend  # traceguard[contamination-hf]

backend = HFLogprobBackend("distilgpt2")
score = min_k_plus_plus_for_text("…model-generated analysis…", backend=backend, k=0.2)
```

`min_k_plus_plus` needs the full per-position vocabulary distribution (μ, σ), so
a backend must expose logits — not just the chosen token's log-prob. The
Anthropic API (and most hosted chat APIs) expose neither, which is exactly why
signals 2 and 3 exist: they need no logprobs at all.

## Signal 2 — performance decay across time regimes

A contaminated model scores suspiciously well *before* its cutoff and falls apart
*after*. `regime_decay_test` quantifies the gap with a permutation test, an
effect size (Cliff's δ), and a bootstrap CI; `regime_decay_trend` checks for a
monotonic decline across ≥ 2 ordered regimes (Spearman ρ).

```
pre vs post IC decay = 0.370 (95% CI [0.352, 0.390]), p=0.0013, Cliff's d=1.00, flagged=True
monotonic trend across 3 regimes: rho=-0.944, p=0.0001, flagged=True
```

This signal needs no logprobs and works against a fully closed API model — you
only need its scores bucketed by time regime.

## Signal 3 — claim-level temporal verification

Did the model assert something it could only have known by recall? Given an
evidence source dated by *earliest support*, `TimelineClaimVerifier` flags any
claim whose earliest source postdates the simulated cutoff (`as_of`).

```
as_of=2024-02-01  earliest_support=2024-01-25            ok  | Q4 revenue beat consensus
as_of=2024-02-01  earliest_support=2024-03-12  CONTAMINATED  | the acquisition closed
as_of=2024-02-01  earliest_support=     never  CONTAMINATED  | an unsourced rumor
```

The model "forecast" (as of 2024-02-01) an acquisition that no source supported
until 2024-03-12 — it could only know it by having seen the future.

## The combined verdict

```
[1] membership : Min-K%++ scores the pre/familiar text +2.427 above the post baseline
[2] regime     : significant out-of-sample decay = True
[3] claims     : a claim predates any supporting source = True

-> 3/3 independent signals point to contamination.
```

Each signal is weak alone and rests on different evidence (token statistics,
score timelines, claim provenance). Three independent weak signals agreeing is
far stronger than any one — but this remains **screening, not proof**.
Corroborate with held-out, genuinely post-cutoff data.

## Run it

```bash
cd packages/traceguard
uv run python ../../examples/contamination_case_study.py          # offline, no deps
uv run python ../../examples/contamination_case_study.py --hf     # real distilgpt2 (~350MB)
```

`--hf` needs the extra: `pip install "traceguard[contamination-hf]"`.

## Research anchors

- **Min-K%++** — *Improved Baseline for Detecting Pre-Training Data from Large
  Language Models*, Zhang et al., ICLR'25,
  [arXiv 2404.02936](https://arxiv.org/abs/2404.02936). Basis for
  `min_k_plus_plus`.
- **MIN-K% PROB** — *Detecting Pretraining Data from Large Language Models*, Shi
  et al., [arXiv 2310.16789](https://arxiv.org/abs/2310.16789). Basis for
  `min_k_prob`.
- **Look-Ahead-Bench** — Benhenda,
  [arXiv 2601.13770](https://arxiv.org/abs/2601.13770). Regime-decay framing
  behind signal 2.

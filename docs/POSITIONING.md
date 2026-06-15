# TraceGuard Positioning

> Status: living document (v0.3). The binding interface contract is
> [SPEC.md](SPEC.md); this file fixes *what TraceGuard is for* and *what it is
> deliberately not*. When marketing language here and the contract in SPEC.md
> appear to conflict, SPEC.md wins.

## One-liner

**TraceGuard is the time-integrity layer for LLM pipelines: it makes it
structurally impossible for a run over historical data to use a model, prompt,
or feature that did not exist yet.**

Most observability tools answer *"what happened, and how much did it cost?"*
TraceGuard answers a different, lower-level question: *"could this have happened
at the point in time you are simulating?"*

## The two kinds of look-ahead

"Look-ahead bias" in an LLM pipeline is two distinct failure modes. They have
different root causes, live in different places, and need different tools.
Fixing one while shipping the other is the common, expensive mistake.

### (1) Training contamination — *the model remembers the future*

The model was pre-trained on data from the period you are predicting, so on
pre-cutoff inputs it *recalls* an answer instead of reasoning to one. A forecast
backtest then looks brilliant for reasons that will never generalize forward.

- **Where it lives:** inside the model weights — invisible to your code.
- **How you detect it:** statistical, not structural — membership-inference
  attacks (e.g. MIN-K% PROB), performance decay across time regimes,
  claim-level temporal verification of generated text.
- **TraceGuard's stance today:** *groundwork.* The optional
  `traceguard[contamination]` extra ships clean interfaces plus minimal
  baselines (`min_k_prob`, `performance_decay_across_regimes`, a claim-level
  verification protocol). It is intentionally narrow — correct interfaces and a
  starting implementation, not full coverage of the literature.

### (2) Harness / pipeline leakage — *the code uses things that did not exist*

Nobody intentionally fed the model the future. The leakage rides in through
ordinary pipeline code: a "2023 backtest" that calls a model released in 2025, a
prompt you quietly rewrote last week, a vendor "actual" that was silently
revised months after the fact (worked through in a local case study,
`docs/case-studies/fmp-revision.md`, kept out of the published repo per
`.gitignore`).

- **Where it lives:** in your orchestration / harness code — fully under your
  control, and therefore fixable *structurally*.
- **How TraceGuard handles it:** this is the mature surface.
  - A **model registry** with `released_at` *and* `available_to_us_at`;
    `select_model(..., strict=...)` has no default mode, so every call site
    states its intent and anachronistic picks fail loudly.
  - A **git-tracked prompt registry** whose content hash is pinned into every
    trace.
  - One canonical **`normalize_input` / `input_hash`** so identical inputs hash
    identically across runs and machines.
  - **Look-ahead invariants** (1–3 today, 4 in Phase 2) exposed as pure
    functions you call in CI; violations raise `InvariantViolation`.

| | (1) Training contamination | (2) Harness / pipeline leakage |
|---|---|---|
| Root cause | Model pre-trained on the future | Code uses not-yet-existing model/prompt/feature |
| Lives in | Model weights | Pipeline / harness code |
| Detection | Statistical (MIA, regime decay) | Structural (registries, invariants) |
| Can be *refused*? | No — only estimated | Yes — fail the run / red CI test |
| TraceGuard maturity | Groundwork (opt-in extra) | **Primary, production focus** |

## Anti-positioning — what TraceGuard is not

TraceGuard is a thin, dependency-light layer that deliberately does **not**
overlap with the crowded observability market. It sits *underneath* it.

| Category | Examples | What they do | Relationship to TraceGuard |
|---|---|---|---|
| Trace dashboards | Langfuse, Phoenix (Arize), LangSmith | Visualize traces, latency, cost, prompt playgrounds | **Interoperate** — export TraceGuard traces up via `traceguard[otel]` |
| Proxy / gateways | Helicone | Sit in the request path, log + cache + rate-limit | Orthogonal — TraceGuard records intent + timeline, not the wire |
| Eval harnesses | Braintrust, promptfoo | Score outputs against datasets/judges | Complementary — TraceGuard guarantees *which* model/prompt scored *when* |

Concretely, TraceGuard is **not**: a dashboard, a UI, a hosted service, a proxy,
a generic eval framework, or a vendor SDK replacement. It is the time-integrity
substrate those tools can sit on top of.

### Interoperate, not compete

- **SQLite is the default** local store; nothing about the SDK assumes a
  backend.
- **OpenTelemetry / OpenInference** export (`traceguard[otel]`) emits the same
  time-correct traces as OTLP spans, carrying the attributes that matter for
  time-integrity (`input_hash`, `model_id` + `available_to_us_at`,
  `prompt_template_hash`, `feature_as_of`). They flow into Langfuse, Phoenix, or
  any OTLP collector unchanged.
- You keep your dashboard for observability; you add TraceGuard to guarantee the
  timeline underneath it.

## Wedge audience

The first users are people for whom an off-by-one-timestamp result is a
*correctness* bug, not a cosmetic one:

1. **Quant / AI-for-finance researchers** — backtesting LLM-derived signals,
   where one anachronistic model or one revised vendor "actual" silently
   inflates returns and Sharpe.
2. **LLM-eval / contamination researchers** — measuring temporal generalization
   and pretraining contamination, who need provenance on which model and prompt
   produced which score, as of when.
3. **Pipeline-replay teams** — replaying extraction/scoring pipelines over
   document archives, who must answer "could this output have been produced at
   that point in time?"

## Research anchors

The harness-leakage invariants are the engineering counterpart to a growing
body of work on temporal validity and contamination. The contamination
groundwork draws on these in particular:

<!-- ⚠️ arXiv IDs below are placeholders pending verification — confirm before citing. -->

- *A Test of Lookahead Bias in LLM Forecasts* — arXiv 2512.23847 *(ID to verify)*.
  Evidence that LLM forecasts leak future knowledge; motivates measuring, not
  assuming, the look-ahead tax.
- *Look-Ahead-Bench* — arXiv 2601.13770 *(ID to verify)*. Benchmark framing for
  look-ahead bias evaluation.
- *All Leaks Count, Some Count More / TimeSPEC / Shapley-DCLR* — arXiv 2602.17234
  *(ID to verify)*. Attribution of contamination contribution across sources.
- **MIN-K% PROB** — Shi et al., 2024. Membership-inference baseline for detecting
  pretraining-data membership; the basis for `contamination.min_k_prob`.

These inform the contamination side (kind 1). The harness-leakage side (kind 2)
is governed entirely by the invariants in [SPEC.md](SPEC.md) §5.

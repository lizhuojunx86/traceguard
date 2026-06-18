# Changelog

All notable changes to the `traceguard` SDK are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Versioning policy for the interface contract is defined in
[`docs/SPEC.md`](../../docs/SPEC.md) §6.

## [0.7.0] - 2026-06-18

Adds an **OpenAI client wrapper**, bringing auto-instrumentation parity with
`wrap_anthropic`. **No breaking changes** — purely additive, so every
0.2.0–0.6.1 public signature is unchanged (SemVer minor): no existing
function or extra is touched, the heavy `openai` dependency stays behind a new
opt-in extra, and SPEC §§3–5 are untouched.

### Added

- **`wrap_openai`** (`traceguard.sdk.wrappers.openai`): wraps an
  `openai.OpenAI` client so `chat.completions.create(...)` — and
  `responses.create(...)` when the installed SDK exposes the Responses API —
  each produce one `traces` row (input hash, model, output text/id,
  prompt+completion tokens, latency). Mirrors `wrap_anthropic`: the response
  object is returned untouched, every other attribute passes through, and an
  un-wrapped client is unaffected. The heavy dependency is isolated behind the
  new `traceguard[openai]` = `["openai>=1.0"]` extra; core dependencies
  unchanged.
- `examples/openai_call.py`: synthetic, no-key demo (fake or real client)
  making one `chat.completions` and one `responses` call and reading back both
  traces.

## [0.6.1] - 2026-06-17

Docs-and-metadata patch — **no code or public-API change** (the integration
guide below uses `Tracer.enable_otel`, shipped in 0.5.0; SPEC §§3–5 untouched).
It refreshes the PyPI page so the expanded metadata becomes visible and surfaces
the OpenTelemetry integration guide to package visitors.

### Changed

- **PyPI metadata**: expanded `keywords` (point-in-time, temporal-integrity,
  data-contamination, llm-evaluation) and `classifiers` (Financial and Insurance
  Industry audience; Scientific/Engineering :: Artificial Intelligence). License
  stays the SPDX `License-Expression: Apache-2.0` form (+ bundled `LICENSE`).
- **Package README** now links the OpenTelemetry → Langfuse/Phoenix integration
  guide.

### Docs

- Published the FMP `epsActual` data-revision case study (harness/pipeline
  leakage, look-ahead kind 2) plus a faithful Chinese translation under
  `docs/case-studies/`; added `docs/integrations/otel-langfuse-phoenix.md` and a
  runnable, self-checking `examples/otel_console_export.py`.

## [0.6.0] - 2026-06-17

Adds **Min-K%++**, a stronger membership-inference variant for
training-contamination detection, and brings the SDK suite (plus a real
open-weight contamination lane) into CI. **No breaking changes** — every
addition preserves the 0.2.0–0.5.0 public signatures (SemVer minor):
`min_k_prob` / `min_k_prob_for_text` / `LogprobBackend` are untouched, heavy
deps stay behind the existing `traceguard[contamination-hf]` extra, and SPEC
§§3–5 are unchanged (§6.x opt-in).

### Added

- **Min-K%++** (`traceguard.contamination`): `min_k_plus_plus(token_stats, *, k)`
  averages the lowest-k% of *normalized* per-token scores
  `z = (logprob − μ) / σ`, where μ/σ are the mean/std of log-prob over the whole
  vocabulary at each position (Zhang et al., 2024, arXiv 2404.02936) — a stronger
  pre-training-data detector than raw MIN-K%. `TokenLogprobStats` carries each
  token's `(logprob, μ, σ)`; degenerate `σ ≤ 0` positions are skipped.
- **`CalibratedLogprobBackend`** protocol + `min_k_plus_plus_for_text(text, *,
  backend, k)`: the calibrated counterpart to `LogprobBackend` /
  `min_k_prob_for_text`. It needs the full per-position vocabulary distribution
  (not just the chosen token's logprob), so a backend must expose logits.
  `HFLogprobBackend` gains `token_logprob_stats`, deriving μ/σ from logits with
  the same teacher-forcing alignment as `token_logprobs`.
- **End-to-end contamination case study**:
  `examples/contamination_case_study.py` (offline by default; `--hf` runs
  Min-K%++ on a real `distilgpt2`) combines MIN-K% vs Min-K%++, regime decay, and
  claim verification into one verdict, with a bilingual writeup
  (`docs/contamination-case-study.md` / `.zh.md`).

### CI

- The `traceguard` SDK suite now runs in CI (`traceguard-sdk` job) — it lives in
  `packages/traceguard` with its own uv project and had never been run before.
- New `traceguard-contamination-hf` job installs the `contamination-hf` extra
  (CPU torch) and runs the `TRACEGUARD_RUN_HF_TESTS=1` lane, so MIN-K% / Min-K%++
  on a real `tiny-gpt2` is exercised instead of perpetually skipped.

## [0.5.0] - 2026-06-17

Adds **opt-in real-time OpenTelemetry dual-write**: a tracer can emit one OTLP
span the moment a trace closes, *in addition to* (never replacing) the SQLite
write, which stays the source of truth (SPEC §6.1). **No breaking changes** —
every addition preserves the 0.2.0/0.3.0/0.4.0 public signatures (SemVer minor);
default behaviour is byte-for-byte unchanged until you opt in; the heavy
dependency stays behind the existing `traceguard[otel]` extra. No new MUST
fields, no new schema, no new extra (SPEC §§3–5 untouched).

### Added

- **Real-time OTel dual-write** (`traceguard[otel]`): `Tracer.enable_otel(*,
  tracer_provider=None, model_name_map=None, scope_name="traceguard")` and
  `Tracer.disable_otel()`. Once enabled, every `span` / `trace` also emits one
  OTLP span at close time. Mirrors the existing `configure(engine)` setter, so
  it configures the module-level singleton (and already-bound `@tracer.trace`
  decorators) in place.
- **`OtelDualWriteSink`** (`traceguard.exporters.otel`): the live sink behind
  `enable_otel`. Reuses the batch exporter internals so a live span is
  **byte-identical** to what `export_trace` would later produce for the same row
  — same attributes (incl. the Plan-A `model_name` mapping and
  `traceguard.model_id`), same `invoked_at - latency_ms` → `invoked_at` timing,
  same OK/ERROR status. Dedup downstream on `traceguard.trace_id`.

### Notes

- **Default OFF, fully isolated**: not calling `enable_otel` changes nothing.
  When enabled, any exporter failure is swallowed (logged at WARNING on
  `traceguard.otel`) and never breaks tracing, the SQLite write, or the business
  call — including not masking a business exception on the error path.
- **Optional dependency**: `traceguard.sdk.tracer` never imports
  `opentelemetry`; `enable_otel` imports the sink lazily and raises the canonical
  `traceguard[otel]` `ImportError` if the extra is missing.
- **Batch path unchanged**: `export_trace` / `export_traces` keep their
  signatures and behaviour; dual-write is a third caller of the shared internals.
  Production tip: use `BatchSpanProcessor` so the OTLP send does not run
  synchronously on the traced call's exit.

## [0.4.0] - 2026-06-16

Turns the 0.3.0 contamination *groundwork* into working estimators. **No
breaking changes** — every addition preserves the 0.2.0/0.3.0 public signatures
(SemVer minor); new behaviour arrives as new functions/params, and heavy deps
stay behind extras (SPEC §6.1).

### Added

- **Pluggable logprob backend for MIN-K% PROB** (`traceguard.contamination`):
  the `LogprobBackend` protocol and `min_k_prob_for_text(text, *, backend, k)`
  let you run MIN-K% on raw text from any model that exposes per-token
  log-probabilities. `min_k_prob(token_logprobs, *, k)` is unchanged. A
  reference `HFLogprobBackend` (open-weight causal LM via teacher forcing) ships
  in `traceguard.contamination.logprobs_hf` behind the new
  `traceguard[contamination-hf]` extra (`torch`, `transformers`). Anthropic-API
  users cannot obtain token logprobs and should use `regime_decay_test` /
  `TimelineClaimVerifier` instead.
- **Statistical regime-decay tests** (`traceguard.contamination`):
  `regime_decay_test` (permutation-test p-value, Cliff's-delta effect size, and
  a bootstrap CI on the decay between two regimes) and `regime_decay_trend`
  (Spearman monotonic-trend test across ≥2 regimes ordered by distance from the
  model cutoff), with `RegimeDecayTest` / `RegimeDecayTrend` results. Both are
  pure standard-library and seeded for determinism.
  `performance_decay_across_regimes` is unchanged.
- **Claim-level temporal verification reference** (`traceguard.contamination`):
  `TimelineClaimVerifier` implements the `ClaimVerifier` protocol over a
  pluggable `EvidenceSource` (with an `InMemoryEvidenceSource` for tests/demos),
  flagging a claim as contaminated when its earliest supporting source postdates
  the simulated cutoff (or no source exists) — the claim-level companion to
  `loop.EvidenceGate`. Retrieval/LLM claim extraction stays a user-supplied
  seam.
- `examples/training_contamination.py` upgraded from a sketch to a runnable
  illustration exercising `min_k_prob_for_text`, `regime_decay_test`, and
  `TimelineClaimVerifier` (synthetic, clearly labelled illustrative data).
- **OTel exporter: vendor model name.** `export_trace(..., model_name=...)` and
  `export_traces(..., model_name_map=...)` set `gen_ai.request.model` to the
  vendor model name Phoenix/Langfuse expect; the internal id is preserved under
  the new `traceguard.model_id` span attribute. With no mapping the field falls
  back to `model_id` (unchanged default). No trace/registry schema change.

### Changed

- `export_traces` prefetches model availability (`available_to_us_at`) in a
  single registry query for the whole batch instead of one query per trace
  (removes an N+1).

### Fixed

- The `traceguard[otel]` extra now installs `opentelemetry-exporter-otlp-proto-http`,
  so the OTLP snippet in the `traceguard.exporters.otel` docstring imports; the
  primary docstring example now uses a console exporter and runs offline.

## [0.3.0] - 2026-06-15

Positioning, evidence, and interoperability round. **No breaking changes** —
everything is additive, so the 0.2.0 public API is unchanged (SemVer minor).

### Added

- **OpenTelemetry / OpenInference export** behind the new `traceguard[otel]`
  extra (`traceguard.exporters.otel`): `export_trace` / `export_traces` /
  `trace_to_attributes` map a trace to an OTLP span carrying the time-integrity
  attributes (`input_hash`, `gen_ai.request.model` +
  `traceguard.model.available_to_us_at`, prompt hash, `feature_as_of`,
  `openinference.span.kind`). The SQLite/SQLAlchemy store stays the source of
  truth; OTel is an additional export (SPEC §6.1).
- **Training-contamination groundwork** in `traceguard.contamination`
  (look-ahead kind 1): `min_k_prob` (MIN-K% PROB membership-inference baseline),
  `performance_decay_across_regimes`, the `ClaimVerifier` protocol +
  `ClaimVerdict`, and `attach_contamination_score`, which records scores via a
  trace's `output_parsed` JSON (no schema change, SPEC §6.1). The
  `traceguard[contamination]` extra reserves the dependency-isolation point for
  heavier future implementations (currently empty — baselines are
  standard-library).
- **Loop evidence-gating** in `traceguard.loop`: `EvidenceGate` and the
  `evidence_gated` decorator admit a memory write only if its evidence is
  sourced at/before a cutoff — the loop-level companion to invariant 1.
- Documentation: `docs/POSITIONING.md` (two kinds of look-ahead,
  anti-positioning, research anchors) and `docs/loop-integration.md`.
- Examples: `model_anachronism.py` and `prompt_drift.py` (runnable), plus
  `training_contamination.py` and `loop_self_contamination.py` (run a real slice,
  sketch the rest), and `examples/README.md`.

### Changed

- READMEs and `docs/SPEC.md` / `TRACEGUARD_SPEC.md` sharpened the positioning
  (time-integrity layer; two kinds of look-ahead; named anti-positioning vs
  Langfuse/Phoenix/LangSmith/Helicone; research anchors). Contract clauses
  (SPEC §§3–5) are unchanged; SPEC §6.1/§6.6 register the opt-in extensions.

### Notes

- Research-anchor arXiv IDs are flagged as placeholders pending verification.
- A flagship case study (`docs/case-studies/fmp-revision.md`) is kept local and
  out of the published repo (its directory is `.gitignore`d as a real-data
  guardrail); its numbers are placeholders pending log reconciliation.

## [0.2.0] - 2026-06-11

First public PyPI release.

### Added

- Trace + model-registry ORM (SQLAlchemy 2.0, SQLite default).
- Git-tracked YAML prompt registry; `load_prompt` pins the content hash.
- `@tracer.trace` decorator + `tracer.span` context manager.
- Canonical `normalize_input` / `input_hash`.
- `select_model` (mandatory explicit `strict`) and `register_model`.
- Look-ahead invariant validators 1–3 (`validate_feature_as_of`,
  `validate_model_timing`, `validate_reference_timing`); invariant 4 is Phase 2.
- `wrap_anthropic` client wrapper behind the `traceguard[anthropic]` extra.

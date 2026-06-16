# Changelog

All notable changes to the `traceguard` SDK are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Versioning policy for the interface contract is defined in
[`docs/SPEC.md`](../../docs/SPEC.md) §6.

## [0.4.0] - Unreleased

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

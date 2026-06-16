# Changelog

All notable changes to the `traceguard` SDK are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Versioning policy for the interface contract is defined in
[`docs/SPEC.md`](../../docs/SPEC.md) §6.

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

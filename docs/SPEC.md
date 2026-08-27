# TraceGuard Integration Specification (English)

> **Status**: translation of [`TRACEGUARD_SPEC.md`](../TRACEGUARD_SPEC.md)
> v1.1 (2026-08-27) — the contract is in force (v1.0 was frozen with
> `traceguard` v1.0.0 on 2026-07-12) and evolves under SemVer per §6; v1.1
> is a **minor** revision (revision history: Appendix D of the Chinese
> original). The Chinese original is authoritative; if the two disagree, the
> original wins.
>
> **Type**: interface contract. Any project integrating TraceGuard MUST
> conform to the data model, SDK signatures, and invariants defined here.
> **MUST / SHOULD / MAY** follow RFC 2119. Changing a MUST item is a
> constitutional amendment and requires a SemVer major bump.

## 0. Positioning (non-normative)

TraceGuard addresses **harness / pipeline look-ahead leakage**: code that uses a
model, prompt, or feature that did not exist at the simulated time. That class is
exactly what this contract makes structurally refusable (§5). A second class —
**training contamination**, where the model itself was pre-trained on the future
it predicts — is statistical rather than structural, and is handled by *opt-in
extensions outside this contract* (see §6.1 and
[POSITIONING.md](POSITIONING.md)). This section is non-normative; §§3–7 are the
binding contract.

## 1. Scope

This spec defines:

1. The required field subset and invariant properties of the core tables (§3)
2. The signatures and semantics of public SDK methods (§4)
3. The four look-ahead-bias invariants (§5)
4. The minimal obligations of an integrating project (§7)

It deliberately does **not** define: storage backend choice, instrumentation
forms beyond the decorator/context manager, drift-check names or thresholds,
alerting, dashboards, cost reconciliation, PII scrubbing, or CLI command
names. Those belong to implementations and integrating projects.

## 2. Terms

| Term | Definition |
|------|------------|
| **Trace** | The complete record of one observable computation (input + model/prompt + output + perf + errors). The atomic unit. |
| **Correlation ID** | Business-object identifier linking traces. A string; semantics are defined by the integrating project. |
| **Prompt Template** | A versioned, content-hashed prompt string template. |
| **Model Registry Entry** | Metadata for one `model_id`, including `released_at` and `available_to_us_at`. |
| **Replay Set** | A locked sample of inputs used for regression and A/B testing. |
| **Project** | The integrator's string identifier (lower snake case, e.g. `huadian`, `semdiff`). Not pre-enumerated. |

## 3. Data model contract

Listed fields are MUST fields. Implementations MAY add nullable columns;
they MUST NOT rename, delete, or retype the fields below.

### 3.1 `traces`

| Field | Type | Required | Semantics |
|-------|------|----------|-----------|
| `trace_id` | int | ✔ | Primary key |
| `project` | text | ✔ | Integrating project identifier |
| `component` | text | ✔ | Business component name (free within project) |
| `operation` | text | ✔ | `llm_complete` \| `embedding` \| `ml_inference` \| `parse` \| other |
| `correlation_id` | text | nullable | Business-object link |
| `parent_trace_id` | int | nullable | Nesting support |
| `agent_id` | text | nullable | Stable identifier of the executing principal that made this call (agent instance / service / person). The identity dimension for correlating multiple executors after the fact |
| `session_id` | text | nullable | Grouping key for one run / session / episode; traces sharing a session belong to the same execution context |
| `input_hash` | text | ✔ | SHA-256 of canonicalized input; MUST be computed by the SDK normalizer (§4.4) |
| `input_summary` | text | nullable | Human-readable, SHOULD be ≤ 500 chars |
| `model_id` | text | nullable | If set, MUST be registered in `model_registry` |
| `prompt_template_id` | text | nullable | If set, MUST be registered in `prompt_registry` |
| `prompt_template_hash` | text | nullable | MUST match the registered record |
| `output_parsed` | json | nullable | Structured output |
| `parse_status` | text | ✔ | `success` \| `partial` \| `failed` |
| `latency_ms` / `tokens_in` / `tokens_out` | int | nullable | |
| `cost_usd` | decimal | nullable | List price at write time; reconciliation out of scope |
| `feature_as_of` | timestamp | nullable | **Business-level as-of time**, used by invariant checks |
| `invoked_at` | timestamp | ✔ | Physical write time (defaults to NOW()) |
| `error_class` / `error_message` | text | nullable | |

`feature_as_of` and `invoked_at` are two independent times; under backfill
they differ significantly (see §5).

`agent_id` / `session_id` (v1.1) take no part in the `input_hash` computation
(the §4.4 algorithm is unchanged) and no part in invariants 1–4. Shared-resource
/ credential fingerprints SHOULD be recorded under `output_parsed["correlation"]`
(convention: `docs/spec-changes/2026-08-27-audit-v2-correlation-schema.md`);
plaintext credentials MUST NOT be recorded — only one-way hashed fingerprints.

### 3.2 `model_registry`

| Field | Type | Required | Semantics |
|-------|------|----------|-----------|
| `model_id` | text | ✔ (PK) | Stable unique identifier |
| `model_family` | text | ✔ | `anthropic` \| `openai` \| `voyage` \| `internal-ml` \| other |
| `capability_class` | text | ✔ | `general-llm` \| `embedding` \| `classifier` \| `regressor` \| `vision` (extensible) |
| `released_at` | timestamp | ✔ | Public release time (world fact) |
| `available_to_us_at` | timestamp | ✔ | First time callable inside this system |
| `deprecated_at` | timestamp | nullable | |

Contract semantics:

- Strict look-ahead protection MUST compare against `available_to_us_at` (§5.2).
- `released_at <= available_to_us_at` MUST hold.
- A model upgrade is a **new** `model_id`; in-place mutation of existing
  entries is forbidden.

### 3.3 `prompt_registry`

| Field | Type | Required |
|-------|------|----------|
| `prompt_template_id` | text | ✔ — naming convention `<project>/<component>/v<N>` |
| `prompt_template_hash` | text | ✔ — SHA-256 of `template_body` |
| `template_body` | text | ✔ |
| `template_format` | text | ✔ — `jinja2` \| `fstring` \| `raw` |
| `expected_output_schema` | json | nullable |
| `introduced_at` | timestamp | ✔ |
| `superseded_at` / `superseded_by` | — | nullable |

The hash of a given `prompt_template_id` MUST be immutable — editing in
place requires a new id. Registrations are never deleted, only superseded.
The Phase 0 backend is git-tracked YAML files: prompt history = git log.

### 3.4 `replay_sets` / `replay_set_items`

Once `is_locked = TRUE`, any mutation of the set's items MUST be rejected by
the implementation. This is the physical guarantee behind invariant 4 (§5.4).

## 4. SDK interface contract

Signatures below are MUST-stable. New parameters with defaults may be added;
renaming or changing the semantics of existing parameters is a major bump.

### 4.1 Instrumentation

```python
@tracer.trace(project, component, operation, *,
              correlation_from=None, feature_as_of_from=None,
              agent_id=None, session_id=None)          # v1.1
def fn(...): ...

with tracer.span(project, component, operation, *,
                 correlation_id=None, feature_as_of=None,
                 agent_id=None, session_id=None) as span:   # v1.1
    span.record_input(data)
    span.record_model_prompt(model_id=..., prompt_template_id=..., prompt_template_hash=...)
    span.record_output(parsed=..., parse_status=...)
    span.record_perf(latency_ms=..., tokens_in=..., tokens_out=..., cost_usd=...)
```

Client wrappers (`wrap_anthropic`, …) are optional implementations and not
constrained by this spec.

**Identity dimensions (v1.1).** `agent_id` / `session_id` are keyword-only
optional parameters (§6: a new parameter with a default is a minor) written
verbatim to the §3.1 columns of the same name; `wrap_anthropic` accepts the
same two parameters. Implementations SHOULD offer an environment-variable
fallback, `TRACEGUARD_AGENT_ID` / `TRACEGUARD_SESSION_ID` (an explicit
argument wins over the environment; with neither, the column is NULL),
because agent runtimes often cannot pass them per call. Neither field takes
part in `input_hash` (§4.4) or invariants 1–4.

**Failure semantics (MUST).** Instrumentation MUST NOT break the instrumented
call. Trace persistence is **fail-open** by default — a failure is swallowed and
logged, never propagated, and on the error path it never replaces the original
business exception. The implementation MUST also offer an opt-in **fail-closed**
mode (e.g. a `strict_persistence` flag / `TRACEGUARD_STRICT_PERSISTENCE` env
var) for backtests where a silently-missing trace could hide an anachronism.

### 4.2 Model registry queries

```python
select_model(capability_class, *, available_at, strict: Literal[True]) -> str
# no eligible model -> raises NoEligibleModelError

select_model(capability_class, *, available_at, strict: Literal[False]) -> tuple[str, bool]
# returns (model_id, is_anachronistic)
```

`strict` MUST be keyword-only **with no default**. Forcing every call site to
state its mode creates the deliberate friction that prevents unconscious
look-ahead bias.

### 4.3 Prompt loading

```python
load_prompt(template_id: str) -> PromptTemplate
# PromptTemplate: .prompt_template_id, .prompt_template_hash, .render(**kwargs)
```

### 4.4 Input normalization (single source of truth)

```python
normalize_input(data: Any) -> bytes
input_hash(data: Any) -> str   # sha256(normalize_input(data)).hexdigest()
```

The algorithm MUST be reproducible across languages and versions. Integrators
MUST NOT implement their own hash. Rules: dicts serialize with sorted keys
(`ensure_ascii=False`, compact separators); strings are stripped with
newlines normalized to `\n`; floats use fixed-precision serialization;
None/NaN/Inf have defined forms. Changing the algorithm is a constitutional
amendment (it breaks comparability of all historical traces).

### 4.5 Invariant validators

```python
validate_feature_as_of(input_traces: list, output_feature_as_of) -> None
validate_model_timing(model_id, feature_as_of, *, strict: bool, engine=None) -> None
validate_reference_timing(valid_from, feature_as_of, *, kind: str) -> None
assert_replay_set_locked(replay_set_id, *, engine=None) -> None
```

`validate_feature_as_of` and `validate_reference_timing` are pure functions.
`validate_model_timing` (invariant 2) and `assert_replay_set_locked`
(invariant 4) necessarily read the model_registry / replay_sets store, so they
take an optional `engine` and are not literally pure; the binding guarantee is
"no side effects beyond raising and reading the store". All four raise on
violation and are called directly inside pytest/CI.

## 5. The four look-ahead-bias invariants

All integrating projects MUST uphold these in both production and backtest
code.

**Invariant 1 — `feature_as_of` monotonicity.** Any output feature's
`feature_as_of` MUST be ≤ the minimum of all upstream inputs' own timestamps
(`recorded_at` / `acceptance_ts` / the data's intrinsic time — *not* the
trace's `invoked_at`, which is merely backfill time).

**Invariant 2 — model timing.** The `model_id` used to compute a feature
MUST satisfy `available_to_us_at <= feature_as_of` in strict mode; in loose
mode the result carries `is_anachronistic=True` and the caller MUST apply an
explicit discount on the strategy side.

**Invariant 3 — time-versioned reference data (general principle).** Any
time-sensitive reference data — prompt templates
(`prompt_registry.introduced_at`), entity-alias tables, any lookup dictionary
with a `valid_from` — MUST satisfy `valid_from <= feature_as_of`. Each
project MUST enumerate its applicable reference-data kinds in its own
integration document. (Invariant 2 is conceptually a special case with a
strict/loose split; other reference data is strict-only.)

**Invariant 4 — locked replay sets are immutable.** After
`is_locked = TRUE`, the implementation MUST reject all writes to the set's
items, guaranteeing comparability of A/B results across time.

## 6. Stability and evolution

- **Patch**: bugfixes, no interface change.
- **Minor**: new methods, new nullable fields, new invariants (opt-in first,
  default-on a release later).
- **Major**: changing existing MUST fields, signature semantics, the
  normalize algorithm, or invariant definitions.

Field renames/deletes/retypes require dual-write and a migration window of
at least one release.

### 6.1 Opt-in extensions (non-normative)

The following ship as optional extras and are **purely additive** — they add no
MUST fields, change no existing signatures, and do not touch the normalize
algorithm, so each is a SemVer **minor**:

- `traceguard[otel]` — export traces as OpenTelemetry / OpenInference (OTLP)
  spans, *in addition to* (never replacing) the SQLite/SQLAlchemy store.
- `traceguard[contamination]` — training-contamination estimators
  (membership-inference, regime decay, claim-level checks). Detection only;
  scores attach to a trace via `output_parsed`, **not** via new MUST columns.
- `traceguard.loop` — evidence-gating helpers for self-improving loops, so only
  evidence traceable before a cutoff is admitted as fact.
- `traceguard.audit` — opt-in audit evidence layer (**stable since SPEC
  v1.1**, zero new dependencies): an ORM-layer append-only guard (anti-mistake), a row hash
  chain (tamper-EVIDENT, not tamper-proof — without an externally stored
  anchor, a full-chain rewrite or tail truncation is undetectable), and an
  exportable chain-head anchor. The hash envelope **excludes `cost_usd`**
  (its legal in-place write path, §3.1); cost corrections are evidenced via
  chained cost events. Importing has no side effects — explicit
  `enable()/attach()` required; the chain's own failures are fail-open by
  default (§4.1) with an opt-in strict mode. Since SPEC v1.1: its public API
  surface evolves under the §6 rules (a new defaulted parameter is a minor;
  removing a parameter or changing its semantics is a major); the verify
  finding kinds and their severities are frozen (a new kind is a minor;
  changing or removing an existing kind is a major); the three boundary
  statements in `docs/audit.md` are normative and may only be made more
  conservative. The hash algorithm is versioned: algo v1 is frozen by golden
  tests and stays verifiable forever; an algorithm change is algo v2 and MUST
  NOT invalidate existing chains. Honest layering and limits:
  `docs/audit.md`.

- `traceguard.routing_integrity` — audits whether invariant 2 meant anything
  under a gateway. The SDK wrappers attach `requested_model` / `served_model`
  to `output_parsed["routing"]` (**no** new MUST column — the same route the
  contamination scores take). §3.1's `model_id` is unchanged and still holds
  the *requested* model. Behind an adaptive routing alias (`orcarouter/auto`
  and friends) the requested name and the model that answered can differ per
  request, and an alias has no `available_to_us_at` — so the check passes
  **silently** against a name that was never a model. The extension grades each
  trace (verified / diverged / unregistered / unverifiable) and ships a CLI. It
  does **not** weaken invariant 2. See `docs/integrations/gateways.md`.

These are integrator-optional: a project may depend on the core contract above
without installing any of them.

## 7. Minimal obligations of an integrating project

A project integrating TraceGuard MUST:

1. Maintain `<project>_TRACEGUARD_INTEGRATION.md` at its repo root, listing
   its `component` enumeration, `model_id`s, `prompt_template_id`s, and
   drift checks (if enabled).
2. Route **all** LLM/ML inference calls through SDK instrumentation.
3. Register every `model_id` and `prompt_template_id` before use.
4. Call the §4.5 invariant validators in CI.
5. Declare the TraceGuard spec version it depends on in its CHANGELOG.

SHOULD: keep at least one locked replay set per (project, component); add a
"did you change a prompt template?" reminder to the PR template.

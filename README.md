# TraceGuard

[![PyPI](https://img.shields.io/pypi/v/traceguard)](https://pypi.org/project/traceguard/)
[![Python](https://img.shields.io/pypi/pyversions/traceguard)](https://pypi.org/project/traceguard/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

**Point-in-time correct LLM instrumentation — tracing, version pinning, and
look-ahead-bias protection for research pipelines.**

When you run LLMs over historical data — backtesting a trading signal,
replaying a research pipeline, re-scoring an archive — a normal observability
stack will happily let your "2023 backtest" call a model released in 2025,
rendered through a prompt you rewrote last week. The numbers come out great
and mean nothing.

TraceGuard is a small Python SDK that makes that class of mistake
structurally hard:

- **Model registry with two timestamps** — `released_at` (when the model
  existed in the world) and `available_to_us_at` (when *your* system could
  first call it). `select_model(..., strict=True)` refuses anachronistic
  choices; `strict` has no default, so every call site states its intent.
- **Git-tracked prompt registry** — prompts are versioned YAML files;
  history is `git log`, and the template hash is pinned into every trace.
- **Reproducible input hashing** — one canonical `normalize_input` /
  `input_hash` implementation (sorted keys, fixed float precision, normalized
  whitespace) so identical inputs hash identically across runs and machines.
- **Four look-ahead invariants as pure functions** — call them in pytest/CI;
  violations raise, nothing is silently logged-and-forgotten.
- **Lightweight tracing** — a `@trace` decorator, a `span()` context manager,
  and a `wrap_anthropic` client wrapper that record every LLM/embedding/ML
  call (input hash, model, prompt version, output, latency, tokens, cost)
  into SQLite/SQLAlchemy.

## Who this is for

Anyone doing *serious research over historical data with LLMs in the loop*:
quant researchers backtesting LLM-derived signals, teams replaying extraction
pipelines over document archives, anyone who must answer "could this result
have been produced at that point in time?" General-purpose LLM observability
(dashboards, latency percentiles, prompt playgrounds) is a crowded space —
TraceGuard deliberately focuses on the reproducibility and time-correctness
guarantees those tools don't give you.

## Install

```bash
pip install traceguard
```

Requires Python 3.11+. Core dependencies: SQLAlchemy 2, Pydantic 2, PyYAML.
The Anthropic wrapper is an extra: `pip install "traceguard[anthropic]"`.

To track the development version instead of PyPI releases:

```toml
# pyproject.toml
[project]
dependencies = [
    "traceguard @ git+https://github.com/lizhuojunx86/traceguard.git@main#subdirectory=packages/traceguard",
]
```

## Five-minute tour

Everything below is synthetic and runnable —
see [examples/quickstart](examples/quickstart/) for the full script.

```python
from datetime import datetime, timezone
from traceguard.registry.models import register_model, select_model
from traceguard.store.models import make_engine

engine = make_engine("sqlite:///:memory:")
UTC = timezone.utc

register_model("demo-llm-2024", model_family="internal-ml",
               capability_class="general-llm",
               released_at=datetime(2024, 1, 10, tzinfo=UTC),
               available_to_us_at=datetime(2024, 2, 1, tzinfo=UTC),
               engine=engine)
register_model("demo-llm-2026", model_family="internal-ml",
               capability_class="general-llm",
               released_at=datetime(2026, 1, 5, tzinfo=UTC),
               available_to_us_at=datetime(2026, 1, 15, tzinfo=UTC),
               engine=engine)

# Backtesting as of mid-2025: the 2026 model must be invisible.
backtest_date = datetime(2025, 6, 30, tzinfo=UTC)
model_id = select_model("general-llm", available_at=backtest_date,
                        strict=True, engine=engine)
# -> "demo-llm-2024"; at a 2023 date it raises NoEligibleModelError
```

Trace a call with version pinning:

```python
from traceguard.registry.prompts import load_prompt
from traceguard.sdk.tracer import Tracer

prompt = load_prompt("demo/extractor/v1", prompts_root="prompts")
tracer = Tracer(engine)

with tracer.span("myproject", "extractor", "llm_complete",
                 correlation_id="doc-001", feature_as_of=backtest_date) as span:
    span.record_input({"text": prompt.render(text="...")})
    span.record_model_prompt(model_id=model_id,
                             prompt_template_id=prompt.prompt_template_id,
                             prompt_template_hash=prompt.prompt_template_hash)
    # ... call the model ...
    span.record_output(parsed={"entities": []}, parse_status="success")
    span.record_perf(latency_ms=42, tokens_in=120, tokens_out=18)
```

Enforce the invariants in CI:

```python
from traceguard.validators.lookahead import (
    validate_feature_as_of, validate_model_timing, InvariantViolation,
)

# Invariant 2: a 2025 feature may not be computed by a 2026 model.
validate_model_timing("demo-llm-2026", backtest_date, strict=True, engine=engine)
# -> raises InvariantViolation: [invariant 2] model 'demo-llm-2026'
#    available_to_us_at=2026-01-15 is after feature_as_of=2025-06-30
```

## The four invariants

| # | Invariant | Validator |
|---|-----------|-----------|
| 1 | A derived feature's `feature_as_of` ≤ the earliest timestamp of all its inputs | `validate_feature_as_of` |
| 2 | The model used must satisfy `available_to_us_at` ≤ `feature_as_of` (strict), or carry an explicit anachronism flag (loose) | `validate_model_timing` |
| 3 | Any time-versioned reference data (prompt templates, alias tables, lookup dictionaries) must satisfy `valid_from` ≤ `feature_as_of` | `validate_reference_timing` |
| 4 | A locked replay set is immutable | planned (Phase 2) |

The full interface contract — table schemas, SDK signatures, semantics, and
SemVer rules — lives in [docs/SPEC.md](docs/SPEC.md) (English) and
[TRACEGUARD_SPEC.md](TRACEGUARD_SPEC.md) (Chinese original, authoritative).

## Repository layout

This repo hosts two Python packages:

| Package | Path | Status |
|---------|------|--------|
| **`traceguard`** — the SDK described above | [packages/traceguard/](packages/traceguard/) | Active development; all new features land here |
| **`pipeline-guardian`** (import name `guardian`) — checkpoint validation for multi-agent pipelines: structural checks, LLM-as-Judge, retry/abort actions, dashboard | repo root (`guardian/`) | Frozen: bugfixes only; its 4-symbol public API stays stable for existing integrators |

Pipeline Guardian's full documentation is in
[docs/pipeline-guardian.md](docs/pipeline-guardian.md). The two packages
share no imports and release independently.

## Development

```bash
# SDK
cd packages/traceguard
uv sync && uv run pytest        # 44 tests

# Pipeline Guardian (legacy)
uv sync && uv run pytest        # 246 tests, from repo root
```

Roadmap: [TRACEGUARD_ROADMAP.md](TRACEGUARD_ROADMAP.md) — Phase 0 (current)
ships the tracer, registries, normalizer, and invariants 1–3; Phase 1+ adds
drift checks, replay sets, and more client wrappers.

## License

Licensed under the [Apache License 2.0](LICENSE).

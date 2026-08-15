# traceguard

**Point-in-time correct LLM instrumentation — the time-integrity layer for
LLM pipelines.**

When you run LLMs over historical data — backtesting a signal, replaying a
pipeline, re-scoring an archive — TraceGuard makes it structurally hard to
accidentally use a model or prompt that did not yet exist at the point in
time you are simulating.

It is not a dashboard or a gateway; it is the lower layer that guarantees the
timeline underneath one. It interoperates with observability stacks
(Langfuse / Phoenix via the optional `traceguard[otel]` exporter) rather than
competing with them — see the
[OpenTelemetry → Langfuse/Phoenix guide](https://github.com/lizhuojunx86/traceguard/blob/main/docs/integrations/otel-langfuse-phoenix.md)
and [docs/POSITIONING.md](https://github.com/lizhuojunx86/traceguard/blob/main/docs/POSITIONING.md).

- `traceguard.registry.models` — model registry with `released_at` /
  `available_to_us_at`; `select_model(..., strict=...)` with mandatory
  explicit mode (no default), so anachronistic choices fail loudly.
- `traceguard.registry.prompts` — git-tracked YAML prompt templates;
  `load_prompt` pins the content hash into every trace.
- `traceguard.sdk.tracer` — `@tracer.trace` decorator and `tracer.span()`
  context manager recording input hash, model/prompt versions, output, and
  perf into SQLAlchemy (SQLite by default).
- `traceguard.sdk.normalizer` — the single canonical `normalize_input` /
  `input_hash` (sorted keys, fixed float precision, normalized whitespace).
- `traceguard.sdk.wrappers.anthropic` — `wrap_anthropic` auto-instruments an
  Anthropic SDK client (extra: `traceguard[anthropic]`).
- `traceguard.sdk.wrappers.openai` — `wrap_openai` auto-instruments an OpenAI
  SDK client's `chat.completions` and `responses` calls (extra:
  `traceguard[openai]`).
- `traceguard.registry.replay` — curated, lockable replay sets
  (`create_replay_set` / `add_replay_item` / `lock_replay_set` /
  `build_locked_replay_set`); once locked, the store physically rejects any
  mutation (invariant 4).
- `traceguard.validators.lookahead` — invariant validators
  (`validate_feature_as_of`, `validate_model_timing`,
  `validate_reference_timing`, `assert_replay_set_locked`) that raise
  `InvariantViolation`; call them in pytest/CI to enforce all four look-ahead
  invariants.

All of the above are also re-exported from the top level (`from traceguard
import select_model, tracer, assert_replay_set_locked, ...`); the deep paths
stay valid as aliases. The package ships `py.typed`, so the annotations
(including the `Literal`-typed `select_model(..., strict=...)`) reach
type-checkers. Persistence is **fail-open**: a tracing/DB failure never breaks
or masks the instrumented call (set `TRACEGUARD_STRICT_PERSISTENCE=1` to fail
closed).

## Install

```bash
pip install traceguard
```

Requires Python 3.11+. Optional extras:
`pip install "traceguard[anthropic]"` / `pip install "traceguard[openai]"`
(Anthropic / OpenAI client wrappers) and
`pip install "traceguard[otel]"` (OpenTelemetry / OpenInference export to
Langfuse, Phoenix, or any OTLP backend).

## Example

```python
from datetime import datetime, timezone
from traceguard.registry.models import register_model, select_model
from traceguard.store.models import make_engine

engine = make_engine("sqlite:///traceguard.db")

register_model("demo-llm-2024", model_family="internal-ml",
               capability_class="general-llm",
               released_at=datetime(2024, 1, 10, tzinfo=timezone.utc),
               available_to_us_at=datetime(2024, 2, 1, tzinfo=timezone.utc),
               engine=engine)

# Backtesting as of mid-2025: models that arrived later are invisible.
model_id = select_model("general-llm",
                        available_at=datetime(2025, 6, 30, tzinfo=timezone.utc),
                        strict=True, engine=engine)
```

A complete runnable tour (synthetic data, no API keys) lives in
[examples/quickstart](https://github.com/lizhuojunx86/traceguard/tree/main/examples/quickstart).

## Cache-efficiency audit

`traceguard.routing_audit.cache_audit` turns "your prompt cache hit rate is
low" into a number you can act on. It is a **read-only report** — it opens the
store with SQLite `mode=ro`, writes nothing, and emits only aggregates, token
counts and money. No prompt or answer text ever leaves the DB.

It does not ingest. Fill the store first, then audit it:

```bash
# 1. once (and thereafter incrementally) — pull ~/.claude/projects into a store
python -m traceguard.routing_audit.ingest_claude_code --db sqlite:///traces_routing_audit.db

# 2. as often as you like — read it back
python -m traceguard.routing_audit.cache_audit --db sqlite:///traces_routing_audit.db
```

Flags: `--format table|md|csv` (default `table`), `--since` / `--until`
(inclusive ISO date or datetime; a bare date opens at 00:00 and closes at
23:59).

Four sections. First, per-model: the token-weighted hit rate
`cache_read / (input + cache_read + cache_write_5m + cache_write_1h)`, what the
input side actually cost at list price, and what the same prompts would have
cost with caching switched off entirely.

```
model                       messages  prompt tok      hit rate  input cost  no-cache    saved       saved %
claude-opus-4-8             23,759    5,535,807,891   96.1%     $4,616.39   $27,691.44  $23,075.05  83.3%
claude-fable-5              15,291    3,583,420,473   96.2%     $5,847.08   $35,834.20  $29,987.13  83.7%
claude-opus-5               6,316     1,750,760,343   97.7%     $1,205.08   $8,753.80   $7,548.72   86.2%
claude-sonnet-4-5-20250929  2         42,410          0.0%      n/a         n/a         n/a         n/a
TOTAL                       58,753    12,264,612,945  96.2%     $12,110.31  $75,044.44  $62,934.13  83.9%
```

A model with no entry in the list-price table is listed with its tokens and
`n/a` money — prices come from `routing_audit.pricing` (including Sonnet 5's
two price eras, resolved by `invoked_at`) and are never guessed.

Second, where the idle time goes: gaps between consecutive requests inside each
`session_id`, bucketed `<5m / 5m–1h / 1–4h / >4h`, plus an **upper bound** on
what cache expiry costs — the `cache_creation` of every first-message-after-a-
>1h-gap at its own TTL write multiplier. Upper bound because that figure also
contains whatever the turn genuinely added; `usage` does not separate the two.

Third, the keep-alive question, answered rather than assumed: one ping every 55
minutes across every >1h gap, each billed as a 0.1× read of the prompt as it
stood before the gap, against that rewrite bound. On this repo's own corpus it
comes out as a refusal, which is the point of computing it:

```
gaps bridged                        422
pings needed                        6,765
ping cost                           $2,009.54
rewrite cost avoided (upper bound)  $1,912.84
verdict                             NOT WORTH IT: pings $2,009.54 >= avoidable rewrites $1,912.84
```

Fourth, direct API traffic — traces whose `output_parsed.source` is not
`claude_code_session` (the SDK wrappers, harnesses). Calls, hit rate, average
prompt length, and whether that average even reaches the model's minimum
cacheable prefix (Opus 5 / Fable 5 512, Opus 4.8 / Sonnet 5 1,024, Opus 4.7
2,048, Haiku 4.5 4,096 tokens — deliberately not monotonic, so a model absent
from the table reports `unknown` rather than a guess). Below that floor a 0%
hit rate is structural and no amount of `cache_control` will move it.

The report closes with one copyable line:

```
Claude Code caching already saves us 84% ($12,110.31 vs $75,044.44 list). Checked with: python -m traceguard.routing_audit.cache_audit
```

## Contract

The binding interface contract — table schemas, SDK signatures, the four
look-ahead invariants, SemVer rules — is in
[docs/SPEC.md](https://github.com/lizhuojunx86/traceguard/blob/main/docs/SPEC.md).

Implemented: tracer, model/prompt registries, normalizer, all four look-ahead
invariants (1–4, including locked replay sets), Anthropic + OpenAI wrappers,
and a frozen public API — since 1.0.0 a formal SemVer commitment (SPEC §6).
Not yet (post-1.0): drift checks + alerts, the full
replay executor / A-B compare tooling, a CLI, Postgres/TimescaleDB, Voyage
wrapper — see
[TRACEGUARD_ROADMAP.md](https://github.com/lizhuojunx86/traceguard/blob/main/TRACEGUARD_ROADMAP.md).

## Development

```bash
cd packages/traceguard
uv sync
uv run pytest        # 181 tests (4 skip without the contamination-hf extra)
```

## License

Apache-2.0.

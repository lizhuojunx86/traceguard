# TraceGuard

[![PyPI](https://img.shields.io/pypi/v/traceguard)](https://pypi.org/project/traceguard/)
[![Python](https://img.shields.io/pypi/pyversions/traceguard)](https://pypi.org/project/traceguard/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![CI](https://github.com/lizhuojunx86/traceguard/actions/workflows/ci.yml/badge.svg)](https://github.com/lizhuojunx86/traceguard/actions/workflows/ci.yml)

**The token-accounting conformance suite has moved** to [lizhuojunx86/token-accounting-conformance](https://github.com/lizhuojunx86/token-accounting-conformance), with its history. It was a different subject with a different audience, and burying it in a repository about look-ahead bias helped nobody looking for either. The old paths still resolve here, as stubs pointing there.

**Point-in-time correct LLM instrumentation — the time-integrity layer for
LLM pipelines.**

TraceGuard makes it *structurally impossible* for a run over historical data to
use a model, prompt, or feature that did not exist yet. Tracing, version
pinning, and look-ahead-bias invariants for research pipelines that have to be
reproducible *in time*, not just in code.

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
- **Four look-ahead invariants as callable validators** — call them in
  pytest/CI; violations raise, nothing is silently logged-and-forgotten.
  Invariants 1 and 3 are pure functions; 2 and 4 necessarily read the
  registry/store and take an optional `engine`.
- **Lightweight tracing** — a `@tracer.trace` decorator, a `tracer.span()`
  context manager, and `wrap_anthropic` / `wrap_openai` client wrappers that
  record every LLM/embedding/ML call (input hash, model, prompt version, output, latency,
  tokens, cost) into SQLite/SQLAlchemy.

## Two kinds of look-ahead

"Look-ahead bias" in an LLM pipeline is really two distinct failure modes, and
they need different tools. Conflating them is how teams fix one and ship the
other.

| | **(1) Training contamination** | **(2) Harness / pipeline leakage** |
|---|---|---|
| What | The model was pre-trained on the future it is predicting — it *recalls* rather than reasons | Your code uses a model, prompt, or feature that did not exist at the simulated time |
| Lives in | The model weights | Your pipeline / orchestration code |
| Symptom | Suspiciously good on pre-cutoff data, decays after | A backtest that looks great and means nothing |
| Tooling | Membership-inference (MIN-K%), performance decay across regimes, claim-level temporal checks | Model/prompt registries, canonical input hashing, look-ahead invariants |
| TraceGuard today | **Groundwork** — opt-in `traceguard[contamination]` (interfaces + baselines) | **Primary focus** — structurally refused at the registry/validator layer |

TraceGuard's mature surface targets **(2)**: leakage that rides in through
harness code — a "2023 backtest" calling a 2025 model, a prompt you rewrote
last week, a vendor "actual" that was silently revised. Detection for **(1)**
is younger and lives behind an optional extra; see
[docs/POSITIONING.md](docs/POSITIONING.md).

## Who this is for

The wedge audience is people for whom a wrong-by-one-timestamp result is a
*correctness* failure, not a cosmetic one:

- **Quant / AI-for-finance researchers** backtesting LLM-derived signals, where
  a single anachronistic model or revised "actual" inflates a Sharpe ratio.
- **LLM-eval researchers** measuring contamination and temporal generalization,
  who need provenance on which model/prompt produced which score, as of when.
- **Teams replaying extraction pipelines** over document archives who must
  answer "could this result have been produced at that point in time?"

## Where TraceGuard sits

TraceGuard is **not** a dashboard (Langfuse, Phoenix, LangSmith), **not** a
proxy/gateway (Helicone), and **not** a general-purpose eval harness
(Braintrust). Those answer "what happened and how much did it cost?". TraceGuard
answers a different, lower-level question: **"could this have happened at the
time you're simulating?"**

It is the *time-integrity layer* that sits underneath those tools — and it aims
to **interoperate, not compete**. SQLite is the default local store; an
OpenTelemetry / OpenInference exporter (`traceguard[otel]`) lets the same
time-correct traces flow up into Langfuse, Phoenix, or any OTLP backend
unchanged. Use your dashboard for observability; use TraceGuard to guarantee the
timeline underneath it. Step-by-step:
[docs/integrations/otel-langfuse-phoenix.md](docs/integrations/otel-langfuse-phoenix.md).

The same goes for OpenAI-compatible gateways. TraceGuard instruments whatever
client you hand it, so a gateway is just a `base_url`; presets for OrcaRouter and
OpenRouter ship in `traceguard.gateways`, in alphabetical order and with no
provider recommended over another. Read
[docs/integrations/gateways.md](docs/integrations/gateways.md) before pointing
one at historical data: a routing alias like `orcarouter/auto` or
`openrouter/auto` records the alias rather than the model that actually served
the call, which defeats look-ahead invariant 2 *silently*. Pin a concrete model
id for anything you intend to reproduce.

One boundary worth stating here rather than 200 lines down. TraceGuard makes a
record correct in time; it does not make that record something a third party can
check without trusting you. That is
[tg-attest](https://github.com/lizhuojunx86/tg-attest) — RFC 3161 timestamps over
Merkle epoch roots, aimed at EU AI Act Article 12 rather than at backtests.
TraceGuard answers "could this have happened then?"; tg-attest answers "can you
prove this record has not been edited since?". Separate repo, separate package,
no code dependency in either direction. Detail:
[Evidence layer](#evidence-layer-traceguardaudit).

## Field evidence: the measurement this project started from

TraceGuard exists because of one number. Polling a commercial fundamentals feed
against a live trading strategy's own logs over four months of 2026, **41.4% of
`epsActual` values differed between the value the vendor served first and the
value it serves now, and 15.3% differed enough to flip a long-entry decision**.
Those are the two numbers quoted in
[tg-attest](https://github.com/lizhuojunx86/tg-attest) and in the case study, so
the record set behind them is published and the arithmetic runs in one command
with no dependencies and no vendor account:

```console
$ python analysis/eps_revision.py
```

It prints N, the capture window, both rates with 95% Wilson intervals, the
magnitude distribution, and a nine-point sweep showing what the flip rate would
have been at other decision thresholds. It also re-verifies every row against
its digest pair and exits non-zero if anything disagrees.

Three things worth knowing before quoting the number:

- **The capture cannot be re-run.** A first-seen vendor value is gone once it is
  overwritten; there is no vintage endpoint. What ships is the record captured
  at the time plus the code that turns it into the statistic, not a script that
  pretends it can go back and collect it again.
- **The raw values are not published**, because the vendor's terms forbid
  redistributing data "contained in or derived from" the service. Each record
  carries a keyed digest of each value, whether they differ, the direction and a
  coarse magnitude bucket. That is enough to recompute 41.4% and not enough to
  reconstruct anything the vendor sells.
- **A second, better-designed capture gives 18.6% and 4.6%**, over a broader
  universe and a much shorter post-print horizon. It is published in the same
  directory. Neither number is hidden behind the other.

Method, decision-flip definition, and a twelve-item limitations section:
[docs/eps-revision-methodology.md](docs/eps-revision-methodology.md) ·
narrative: [docs/case-studies/fmp-revision.md](docs/case-studies/fmp-revision.md)
([中文](docs/case-studies/fmp-revision.zh.md))

## One-minute check: which of your agents pick their own model?

A Claude Code subagent whose definition omits `model:` runs whatever the main
thread is running. Usually that is what you want. It stops being what you want
when a frontier model is doing work nobody would have chosen a frontier model
for — at that point no routing decision is being made, and the cost lands
anyway.

```bash
python -m traceguard.routing_audit.agent_lint
# or, with nothing installed at all:
python agent_lint.py ~/.claude/agents ./.claude/agents
```

Reads the YAML frontmatter of `.claude/agents/**/*.md` and nothing else — two
keys, `name` and `model`. No transcripts, no network, no dependencies, ~35 ms
on a 15-agent tree. Exits 1 if anything is unpinned, so it works as a CI gate.

`model:` absent and `model: inherit` are reported separately. Both run the
parent's model; only one of them is a decision somebody made.

That answers *whether* you have the exposure. What those agents actually ran,
and what it cost, needs a trace store —

## Field evidence: `routing_audit`

The opt-in `traceguard.routing_audit` extension applies the same discipline to
your own agent history: it ingests Claude Code session transcripts
(`~/.claude/projects/**/*.jsonl`) into an **append-only, `message.id`-keyed**
trace store, so usage history stops changing underneath you. Claude Code
rewrites session files in place on resume/compact; anything that recomputes
totals from live files inherits that drift.

From that log it scores each `(unit, component)` routing decision against a
policy file you write: the tier the policy expected, the model that actually
ran, and a verdict. The verdict has three shapes rather than two — `compliant`,
`deviation`, and `unresolved`, the last split into `no_rule` (nothing in the
policy matched) and `unknown_model` (the model sits outside every tier). A
default that quietly resolves those cases fabricates verdicts in both
directions: on this corpus it scored one component compliant and another
deviant, each against a rule nobody had written.

Current corpus, 789 decisions: 627 compliant, 160 deviation, 2
`unresolved:no_rule`, 0 `unresolved:unknown_model`. The two unresolved classes
are counted apart because they decay differently. `unknown_model` is an
operational gap that clears itself once the tier table catches up; `no_rule`
stays until somebody writes a rule or decides the case belongs outside policy.

The summary carries two coverage counts next to them: decisions no rule
reached, and rules no decision reached. Those catch different failures,
uncovered behavior on one side, dead or shadowed policy on the other. Brian
Jin, whose comment prompted the three-state verdict, [put it this
way](https://dev.to/kikashy/comment/3d3m7): "That makes unresolved much more
than an error bucket. It becomes an observable property of the policy surface
itself."

Having a stable reference log turned out to be enough to audit *other* tools.
Run against [splitrail](https://github.com/Piebald-AI/splitrail), a Rust usage
tracker for agentic CLIs, it has produced three upstream fixes:

| finding | upstream |
|---|---|
| Totals drift because resume/compact rewrites live JSONL | [#200](https://github.com/Piebald-AI/splitrail/issues/200) → SQLite history store in 3.6.0 |
| Subagent transcripts (`<session>/subagents/**`) sit below a depth-2 discovery cap — on this corpus, 54% of live messages and ~1/3 of the spend never entered any total | [#207](https://github.com/Piebald-AI/splitrail/issues/207) → maintainer-authored [#209](https://github.com/Piebald-AI/splitrail/pull/209) in 3.6.1 |
| Partial streaming snapshots summed per fingerprint, inflating input +62% / cache_read +69% on subagent transcripts (this corpus); inflation ratios predicted from the corpus before the fix branch existed, then matched on every field | [#220](https://github.com/Piebald-AI/splitrail/issues/220) → [#222](https://github.com/Piebald-AI/splitrail/pull/222), corpus-verified and merged the same day |

On the subset splitrail scans, 3.6.0 and the audit log agree **token-exact —
18,548,947 output tokens on both sides** across ~13.5k messages. Two
independent implementations, different languages, different dedup strategies,
same number to the digit; that exactness is what turned the remaining gap into
a nameable defect instead of a rounding argument. The end-to-end regression
fixture from that work was [merged upstream](https://github.com/Piebald-AI/splitrail/pull/208).

The same reference log now also emits the cross-tool
[usage-drift-log](https://github.com/m1kapp/runmaxing/blob/main/docs/usage-drift-log.md)
record each scheduled run (`--usage-report-history`) — a six-field per-run
spec published by clauderank after the drift pattern reproduced at
leaderboard scale ([viberank#83](https://github.com/sculptdotfun/viberank/issues/83));
`routing_audit` is its second independent implementation.

viberank became the third, and adopting it there forced six revisions —
per-month scoping, absence is not deletion, and the multi-agent source gap
among them. Which tool implements what, each revision and the measurement
that forced it, and what is still open:
[`docs/specs/usage-drift-log.md`](docs/specs/usage-drift-log.md).

Method and reproduction protocol: [`splitrail-validation/`](splitrail-validation/) ·
write-up: [An append-only audit log caught two accounting bugs in a 216-star usage tracker](https://dev.to/lizhuojunx86/an-append-only-audit-log-caught-two-accounting-bugs-in-a-216-star-usage-tracker-38co)

The same protocol pointed at
[claude-code-templates](https://github.com/davila7/claude-code-templates)
(30k★) found the opposite sign — a 2.36× over-count, fix submitted as
[PR #754](https://github.com/davila7/claude-code-templates/pull/754):
[`cct-dedup-check/`](https://github.com/lizhuojunx86/token-accounting-conformance/tree/main/cct-dedup-check/) ·
write-up: [The vendor documents this bug. A 30k-star repo shipped it anyway.](https://dev.to/lizhuojunx86/the-vendor-documents-this-bug-a-30k-star-repo-shipped-it-anyway-27pb)

What the series adds up to is a catalog: eleven invariants anything counting
tokens from `~/.claude/projects` has to hold, each one measured in a shipped
tracker before it was written down — [`CONFORMANCE.md`](CONFORMANCE.md).
Maintainers can run the checks in CI with a drop-in workflow:
[`ci/`](https://github.com/lizhuojunx86/token-accounting-conformance/tree/main/ci/).

## Install

```bash
pip install traceguard
```

Requires Python 3.11+. Core dependencies: SQLAlchemy 2, Pydantic 2, PyYAML.
The Anthropic and OpenAI wrappers are extras:
`pip install "traceguard[anthropic]"` / `pip install "traceguard[openai]"`.

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
| 4 | A locked replay set is immutable | `assert_replay_set_locked` |

The full interface contract — table schemas, SDK signatures, semantics, and
SemVer rules — lives in [docs/SPEC.md](docs/SPEC.md) (English) and
[TRACEGUARD_SPEC.md](TRACEGUARD_SPEC.md) (Chinese original, authoritative).

## Evidence layer: `traceguard.audit`

Time-correct traces are only worth as much as the guarantee that they were not
edited afterwards. The opt-in `traceguard.audit` submodule (1.1.0, experimental,
off the frozen surface) adds that guarantee to the `traces` table:

- **Append-only guard at the ORM layer** — blocks accidental UPDATE/DELETE
  against `traces`; `cost_usd`-only updates pass, since repricing is the one
  legal mutation the spec allows.
- **Row hash chain** — every inserted trace is chained as
  `sha256(prev_hash || canonical(entry))`, with the algorithm frozen by golden
  tests. `verify_chain()` recomputes the chain in two passes and reports
  BREAK / WARN / GAP findings.
- **Exportable anchor** — `export_anchor()` emits the chain head for storage
  outside the database, which is what turns tamper-*evident* into something an
  auditor can actually check.
- **CLI** — `python -m traceguard.audit enable|disable|verify|anchor`.

Boundaries are stated rather than glossed: this is tamper-**evident**, not
tamper-proof. Core SQL, raw drivers, and bulk APIs bypass the ORM guard, and
without an external anchor a full-chain rewrite is undetectable. Details:
[docs/audit.md](docs/audit.md).

**Where this stops.** `export_anchor()` emits the chain head; what you do with it
afterwards is out of scope here, and that last sentence is the reason it matters.
[tg-attest](https://github.com/lizhuojunx86/tg-attest) is the sibling project that
closes the gap — RFC 3161 timestamps over Merkle epoch roots, selective disclosure
of a single record without exposing the rest of the batch, and a disclosure bundle
an auditor checks with `openssl ts` and a CA certificate they fetch themselves.
Same point-in-time problem, aimed at EU AI Act Article 12 rather than at backtests.
Separate repo, separate package, no code dependency in either direction.

## Research anchors

TraceGuard's harness-leakage invariants are the engineering counterpart to a
growing body of work on temporal validity and contamination in LLMs. The
contamination groundwork (extra `traceguard[contamination]`) draws on:

- *A Test of Lookahead Bias in LLM Forecasts* — Gao, Jiang & Yan, [arXiv 2512.23847](https://arxiv.org/abs/2512.23847)
- *Look-Ahead-Bench: a Standardized Benchmark of Look-ahead Bias in Point-in-Time LLMs for Finance* — Benhenda, [arXiv 2601.13770](https://arxiv.org/abs/2601.13770)
- *All Leaks Count, Some Count More: Interpretable Temporal Contamination Detection in LLM Backtesting* (TimeSPEC / Shapley-DCLR) — Zhang, Chen & Stadie, [arXiv 2602.17234](https://arxiv.org/abs/2602.17234)
- **MIN-K% PROB** — *Detecting Pretraining Data from Large Language Models*, Shi et al., [arXiv 2310.16789](https://arxiv.org/abs/2310.16789)

See [docs/POSITIONING.md](docs/POSITIONING.md) for how these map onto the two
kinds of look-ahead.

## Repository layout

This repo hosts two Python packages:

| Package | Path | Status |
|---------|------|--------|
| **`traceguard`** — the SDK described above | [packages/traceguard/](packages/traceguard/) | Active development; all new features land here (public API frozen under SemVer since 1.0.0) |
| **`pipeline-guardian`** (import name `guardian`) — checkpoint validation for multi-agent pipelines: structural checks, LLM-as-Judge, retry/abort actions, dashboard | repo root (`guardian/`) | Frozen: bugfixes only; its 4-symbol public API stays stable for existing integrators |

Pipeline Guardian's full documentation is in
[docs/pipeline-guardian.md](docs/pipeline-guardian.md). The two packages
share no imports and release independently.

[`analysis/`](analysis/) belongs to neither. It holds the published evidence for
the `epsActual` revision claim — the disclosure dataset, the script that
recomputes the two headline rates from it, and the builder that produced it from
the private captures.

## Development

```bash
# SDK
cd packages/traceguard
uv sync && uv run pytest        # 359 tests (3 skip without the contamination-hf extra)

# Pipeline Guardian (legacy)
uv sync && uv run pytest        # 259 tests, from repo root
```

Roadmap: [TRACEGUARD_ROADMAP.md](TRACEGUARD_ROADMAP.md). Phase 0 was accepted in
June 2026, and **1.0.0 froze the contract**: every SPEC MUST implemented and
enforced (invariants 1–4), a curated 29-symbol public surface held by a required
`contract-guard` CI job, and fail-open instrumentation that never breaks or masks
the host call. The public API is stable under SemVer from 1.0.0 onward.

Everything since has been additive and opt-in, off the frozen surface:
OpenTelemetry export and live dual-write (`traceguard[otel]`),
training-contamination detection incl. MIN-K%++ (`traceguard.contamination`),
loop evidence-gating (`traceguard.loop`), `wrap_openai` / `wrap_anthropic`,
the 1.1.0 audit evidence layer (`traceguard.audit`), and the 1.2.0 read-only
prompt-cache efficiency audit (`traceguard.routing_audit.cache_audit`). Full
history: [CHANGELOG](packages/traceguard/CHANGELOG.md).

**1.3.0** rebuilt that cache audit's keep-alive answer after three separate
recomputations overturned it: the give-up cap is now solved by a sweep instead
of hand-picked, expiry cost is a bracket instead of one bound, mid-gap **model
switches** are measured instead of assumed, and the reportable conclusion is a
*band* (`RECOMMENDED CAP: 9h..12h`) rather than a single argmax. `--benchmark`
pins the window every quoted number comes from.

**1.4.0** lets that audit leave the laptop. `--emit-share` writes an aggregate
JSON summary and `--show-share` prints the exact bytes first, so the sender
reads all of it before deciding; a `corpus.fingerprint` identifies which traces
a window actually loaded, because a closed window bounds timestamps and not the
corpus; entries are immutable and refuse to be overwritten; and
`generated_at` / `settling_days` record when the pull happened, not only what
it pulled. `benchmark/` collects the results. See the
[package README](packages/traceguard/README.md#cache-efficiency-audit).

## License

Licensed under the [Apache License 2.0](LICENSE).

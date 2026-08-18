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

Everything described here ships in **1.3.0**. If you are on 1.2.0 the report is
a shorter thing — four sections, `--db / --format / --since / --until`, and a
section 2 that is a bare `gap | count | share` histogram with no money in it —
so `pip install -U traceguard` before expecting per-bucket cost, the rewrite
bracket, the three-state verdict, the measured model switch, section 3b's cap
sweep, `--benchmark` or `--peak-band-tolerance`. What changed and why is in the
[CHANGELOG](CHANGELOG.md).

It does not ingest. Fill the store first, then audit it:

```bash
# 1. once (and thereafter incrementally) — pull ~/.claude/projects into a store.
#    Without --write this is a dry run that parses and prints, touching nothing.
python -m traceguard.routing_audit.ingest --write --db sqlite:///traces_routing_audit.db

# 2. as often as you like — read it back
python -m traceguard.routing_audit.cache_audit --db sqlite:///traces_routing_audit.db
```

Flags: `--format table|md|csv` (default `table`), `--since` / `--until`
(inclusive ISO date or datetime; a bare date opens at 00:00 and closes at
23:59), `--peak-band-tolerance K`, and `--benchmark`.

**Every number below comes from `--benchmark`**, a frozen window
(`2026-05-30 .. 2026-08-16`) over this repo's own store:

```bash
python -m traceguard.routing_audit.cache_audit --db sqlite:///traces_routing_audit.db --benchmark
```

The window exists because the store is appended to continuously and two runs
minutes apart disagree — the expired-gap count moved 429 → 432 across one
afternoon of editing. A closed window fixes that for every future run, which
copying the DB aside does not; `--benchmark` refuses to combine with
`--since` / `--until` so a "benchmark" run cannot quietly be a different one.

Five sections. First, per-model: the token-weighted hit rate
`cache_read / (input + cache_read + cache_write_5m + cache_write_1h)`, what the
input side actually cost at list price, and what the same prompts would have
cost with caching switched off entirely.

```
model                       messages  prompt tok      hit rate  input cost  no-cache    saved       saved %
claude-opus-4-8             23,759    5,535,807,891   96.1%     $4,616.39   $27,691.44  $23,075.05  83.3%
claude-fable-5              15,958    3,744,394,764   96.3%     $6,057.40   $37,443.95  $31,386.54  83.8%
claude-opus-5               7,309     1,929,288,473   97.7%     $1,331.44   $9,646.44   $8,315.00   86.2%
claude-sonnet-5             11,157    1,327,315,994   95.1%     $423.37     $2,654.63   $2,231.26   84.1%
claude-haiku-4-5-20251001   1,573     57,312,161      93.9%     $9.76       $57.31      $47.56      83.0%
claude-opus-4-7             64        11,045,966      96.6%     $9.04       $55.23      $46.19      83.6%
claude-sonnet-4-5-20250929  2         42,410          0.0%      n/a         n/a         n/a         n/a
(none)                      654       0               n/a       n/a         n/a         n/a         n/a
TOTAL                       60,476    12,605,207,659  96.3%     $12,447.41  $77,549.01  $65,101.60  83.9%
```

A model with no entry in the list-price table is listed with its tokens and
`n/a` money — prices come from `routing_audit.pricing` (including Sonnet 5's
two price eras, resolved by `invoked_at`) and are never guessed.

Second, where the idle time goes: gaps between consecutive requests inside each
`session_id`, bucketed `<5m / 5m–1h / 1–4h / >4h`, each with the money that
bucket carries — a **bracket** on what its cache expiries cost, against what
bridging only that bucket would have cost in pings. Buckets inside the 1h TTL
expire nothing and read `no expiry`, which is not the `n/a` that means no list
price.

```
gap    count   share  model switch                  rewrite >=  rewrite <=  ping cost  verdict
<5m    58,619  97.2%  no expiry                     no expiry   no expiry   no expiry  no expiry
5m-1h  1,252   2.1%   no expiry                     no expiry   no expiry   no expiry  no expiry
1-4h   189     0.3%   3 of 166 (1.8%), 23 unknown   $896.02     $900.03     $82.65     WORTH IT
>4h    243     0.4%   15 of 212 (7.1%), 31 unknown  $1,036.71   $1,041.43   $1,962.07  NOT WORTH IT
```

`model switch` is measured, not assumed: the share of that bucket's expired
gaps that came back on a **different** `model_id` than they left on. The
`unknown` count (a NULL `model_id` on either side) is reported beside the rate
and is deliberately **not** in its denominator. Caches are model-scoped, so a
cross-model gap is one
no keep-alive could have helped — and the rate climbs with idle time, 1.8% in
`1-4h` against 7.1% in `>4h`. Section 3b deducts those savings; the rewrite
columns here do not, because they measure what expiry cost rather than what
pinging could recover.

The bracket is two assumptions, not a measurement, and the report says so.
The **upper** bound charges the whole `cache_creation` of every
first-message-after-a->1h-gap to the expiry, which overshoots because that
figure also contains whatever the turn genuinely added. The **lower** bound
first credits each of those messages with the median `cache_creation` of the
same session's ordinary turns and charges only the remainder, floored at zero
and priced at that message's own 5m/1h mix. `usage` supports neither split. On
this corpus the two land 0.4% apart — but only because a post-gap write
averages ~350,000 tokens against a ~1,500-token session baseline, so the narrow
interval is a fact about this traffic and not evidence that either bound is
tight.

Third, the keep-alive question, answered rather than assumed: one ping every 55
minutes across every >1h gap, each billed as a 0.1× read of the prompt as it
stood before the gap. The verdict is three-state — a ping bill that lands
*between* the two rewrite bounds gets `UNDECIDED`, because in that band the
sign of the answer comes from the modelling choice rather than from anything
measured. Then the same question for a policy you could actually run: ping
until some threshold of idle, then give up, paying for the pings burned on
gaps that outlive the cap and banking savings only on the ones it bridges.

```
gaps bridged                                    432
pings needed                                    6,884
ping cost                                       $2,044.72
rewrite cost avoided (upper bound)              $1,941.46
rewrite cost avoided (lower bound)              $1,932.73
verdict                                         NOT WORTH IT: pings $2,044.72 >= avoidable rewrites $1,941.46
cross-model gaps (measured)                     18 switched / 360 same / 54 undecidable — 4.8% of decidable
keep-alive cap                                  10h (solved)
gaps bridged / abandoned (capped 10h)           308 / 124
pings needed (capped 10h)                       2,205
ping cost (capped 10h)                          $595.04
pings wasted on cross-model gaps (capped 10h)   143 / $56.44
rewrite cost avoided (capped 10h, upper bound)  $1,406.34
rewrite cost avoided (capped 10h, lower bound)  $1,399.90
verdict (capped 10h)                            WORTH IT: pings $595.04 < avoidable rewrites $1,399.90 (lower bound)
```

"Avoided" is net of cross-model gaps. That used to be an *assumption* in this
section's footnotes — "caches are model-scoped, so a mid-session switch makes
the preceding pings worthless" — and both `model_id`s were in the store the
whole time, so it is now measured instead. The direction is what matters: a
session is likelier to come back on a different model the longer it has been
away, so a policy that pays to stay alive longer collects a larger share of
exactly the gaps this removes. Leaving it out did not add noise to the cap, it
pushed the cap systematically **long**.

The unbounded policy still refuses, and that first `verdict` line is kept
verbatim as the control. Everything here leans pro-ping by construction — ping
cost is charged as a pure cache read of a frozen prompt, and a real session's
prompt grows — so a refusal is solid and an endorsement is only as wide as its
margin, which is now printed rather than implied.

That 10h is **solved, not chosen**. An earlier version hardcoded 4h to line up
with the `1-4h` / `>4h` bucket boundary, which is a tidy number rather than an
answer. Section 3b costs every cap from 1h to 12h in 15-minute steps, plus an
uncapped policy competing on equal terms, and takes the argmax of net benefit:

```
cap     bridged / abandoned  pings  ping cost  cross-model waste  rewrite >=  rewrite <=  net >=    net <=    verdict
1h      0 / 432              391    $106.21    18 / $6.75         $0.00       $0.00       -$106.21  -$106.21  NOT WORTH IT
1h15m   47 / 385             391    $106.21    18 / $6.75         $207.44     $208.34     $101.23   $102.13   WORTH IT
...
4h      189 / 243            1,183  $323.09    63 / $24.82        $888.21     $892.19     $565.13   $569.10   WORTH IT
...
10h     308 / 124            2,205  $595.04    143 / $56.44       $1,399.90   $1,406.34   $804.86   $811.30   WORTH IT
...
12h     326 / 106            2,543  $689.29    182 / $72.23       $1,445.06   $1,451.76   $755.77   $762.47   WORTH IT
no cap  432 / 0              6,884  $2,044.72  992 / $353.31      $1,847.96   $1,856.33   -$196.76  -$188.39  NOT WORTH IT
```

The report's quotable conclusion is the **band**, not the argmax:

```
RECOMMENDED CAP: 9h..12h (cadence 55m). Within this band the choice costs under
10% of the optimum, which is less than the size of corrections still outstanding.
Quote this range; the argmax (10h) is in the table above for reference, not for
citation.
```

That demotion is not modesty, it is arithmetic. 10h leads the runner-up (9h45m)
by **$7.63**, while measuring the model switch — one correction, applied once —
moved this same cap by **$18.76**. A correction larger than the gap between
first and second place is enough to reorder them, and there are more still
unquantified: the undecidable gaps below, the frozen prompt volume, the unswept
cadence. So the sweep reports **two** ranges around the argmax, because they
answer different questions and one footnote must not claim both:

- **sign-stable range** — where the net stays positive. Here `1h15m..12h`,
  10h45m wide, 44 grid points. This says *capping is the right shape of
  policy* anywhere in there. It does **not** say the caps are
  interchangeable: net inside it runs `$102.13..$811.30`, an 8x spread.
- **argmax neighbourhood** — where the net stays within `k` of the maximum
  (`k` defaults to 0.10, `--peak-band-tolerance`). Here `9h..12h`, 3h wide,
  13 grid points. *This* is the range where picking a different cap costs you
  almost nothing. One grid point wide would mean the optimum is a spike.

Two more things the sweep says about its own answer. The argmax **sits on a
ping-count step**: 10h is the last cap before `pings_to_bridge` increments, and
stepping to 10h15m adds $33.49 of pings for $4.07 more avoided rewrite. So part
of why 10h wins is a 55-minute cadence dividing into it, and the honest
statement of the result is "cap 10h **at cadence 55m**" — this sweep holds the
cadence fixed and does not search it. And the neighbourhood is flagged
**censored** at the 12h ceiling, which here means the run reaches the edge of
the grid, *not* that the right-hand side went unexamined: no cap above the
argmax recovers to it, the cumulative drift out to 12h is -$48.83, and the one
observation beyond the grid (`no cap`) is a further -$950.86. A peak past 12h
is not excluded — it is unsupported.

Finally, the cross-model deduction removes only the gaps **proven** to have
switched, which makes it a floor and makes the headline the optimistic end of a
range. The sweep therefore runs the other end too — every undecidable gap
treated as cross-model — and prints both:

```
UNDECIDABLE GAPS, RUN BOTH WAYS. The deduction above only removes gaps PROVEN
cross-model, so the 10h / $811.30 headline is the OPTIMISTIC end. Treating every
undecidable gap as cross-model instead, the argmax stays at 10h and its net falls
to $663.69. The truth is somewhere between the two runs and this report cannot
say where — it is a range, not an answer with an error bar.
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
Claude Code caching already saves us 84% ($12,447.41 vs $77,549.01 list). Checked with: python -m traceguard.routing_audit.cache_audit
```

### Sharing a result

`--emit-share PATH` writes an aggregate JSON summary (schema v1) so one store
can be compared against another; `--show-share` prints the exact bytes that
file would contain and stops, so you can read all of it before deciding to send
anything. The tool makes no network calls and has no upload path.

```bash
python -m traceguard.routing_audit.cache_audit \
  --db sqlite:///traces_routing_audit.db --benchmark --show-share
```

The file carries per-model hit rates and token volumes, gap-bucket counts and
per-bucket cost, the cross-model switch rate (overall, per bucket, and by
gap-length decile), the recommended cap band, both ends of the net-benefit
range, and the undecidable-gap count. It carries no prompt text, no paths, no
session ids, no per-trace timestamps and no free-form strings. The rule is
stated as an invariant rather than a list: every string in the payload is a
schema constant, a model id on the published-price whitelist, one of the two
window bounds, the installed version, a decimal money literal, or the corpus
fingerprint. `model_id` is
whitelisted rather than passed through, since an arbitrary one can name an
internal gateway or an employer; the rest folds into a single `(unrecognized)`
row that keeps the counts and drops the name.

That invariant is tested by poisoning a store with sentinel strings in prompts,
file paths, session ids, model names and keys nobody planned for, then
asserting none survive the export —
`test_emit_share_leaks_no_sentinel_from_prompts_paths_sessions_or_model_names`
in [`tests/test_cache_share.py`](tests/test_cache_share.py). Two further tests
exist only to prove that one can still fail: one drops the model whitelist and
requires the leak to reappear, one adds an unfiltered field and requires the
scan to catch it.

An export needs a **closed window** and refuses without one. Every rate and
dollar figure scales with how long you looked, so a corpus whose members each
measured "all time" is not comparable with itself.

A closed window is necessary and not sufficient, which is why the file also
carries `corpus.fingerprint`. The window closes over timestamps; the store
keeps growing inside it, because `ingest` walks `~/.claude/projects` and a
transcript that only turns up later carries messages timestamped weeks ago.
This repo's own `--benchmark` window went from 432 expired gaps over 168
sessions to 439 over 174 in under a day without moving by a second, and the
argmax net went with it. The fingerprint is a sha256 over one tuple per trace
the window loaded, sorted and length-delimited, so two files agreeing on the
window and disagreeing on the digest were computed over different traffic.
Compare it before comparing anything else.

`tool_version` comes from installed package metadata, never a literal, so a
submission cannot claim a version it was not produced by.

Submission criteria and the corpus itself are in
[`benchmark/`](benchmark/README.md), which currently holds one file and says so.

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
uv run pytest        # 562 tests (3 skip without the contamination-hf extra)
```

## License

Apache-2.0.

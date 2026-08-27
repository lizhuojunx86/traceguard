# Changelog

All notable changes to the `traceguard` SDK are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Versioning policy for the interface contract is defined in
[`docs/SPEC.md`](../../docs/SPEC.md) §6.

## [1.5.0] - 2026-08-28

SemVer **minor**, on two counts: SPEC v1.0 → v1.1 (two new
nullable `traces` columns, new defaulted SDK parameters — §6.2 / §6.3), and
the audit evidence layer graduating out of experimental. No existing
signature moves, the normalize algorithm and the audit algo v1 envelope are
untouched.

**SPEC v1.1 — `agent_id` / `session_id`, and `traceguard.audit` becomes contract.**
Motivation and compatibility analysis:
`docs/spec-changes/2026-08-27-audit-v2-correlation-schema.md` (the METR /
Redwood investigation of the OpenAI–Hugging Face incident: roughly 7% of the
evaluated transcripts with spoofed tool calls, agents researching how to
spoof, edit or delete their own transcripts, ~1200 "isolated" agents
coordinating over a shared cache).

### Added

- `traces.agent_id` / `traces.session_id` (SPEC §3.1 v1.1): nullable, indexed
  identity dimensions for correlating several executors after the fact.
  `tracer.span` / `tracer.trace` / `wrap_anthropic` / `wrap_openai` take them as
  keyword-only arguments; `TRACEGUARD_AGENT_ID` / `TRACEGUARD_SESSION_ID` are
  the fallback, resolved per span. They take no part in `input_hash` or the
  invariants, and they are **outside the audit algo v1 hash envelope** —
  append-only under the guard, not attested by the chain (algo v2 pending).
- Databases created before this release get the two columns added on open
  (`ensure_trace_columns`, run by `make_engine`): `ALTER TABLE traces ADD
  COLUMN`, nullable, plus the index — additive only. Verified on a 58,769-row
  store (286 ms). A DB the process cannot alter raises with the manual
  statement in the message, since the ORM could not read it either.
- `traceguard.audit` is stable since SPEC v1.1: its `__all__`, finding kinds
  and severities (`FINDING_SEVERITY`), public function parameters, and the
  three boundary statements in `docs/audit.md` are frozen by
  `tests/test_audit_api_surface.py` in the contract-guard CI job.
- `traceguard.audit.anchors` — `FileAnchorSink` / `GitNoteAnchorSink` /
  `WebhookAnchorSink`, `anchor_to()` (tries every sink, then raises if any
  failed), `AnchorScheduler` (periodic anchoring; the interval is the exposure
  window). CLI: `anchor --sink SPEC [--every SECONDS]`, `verify --anchor-file`.
- `traceguard.audit.reconcile` — capture-fidelity layer L1: self-reported
  token volume per model and UTC bucket vs the provider's usage report
  (`fetch_anthropic_usage` against the Usage Admin API, or a saved JSON).
  Disagreement is the new finding kind `capture_mismatch` (WARN), with the
  direction spelled out. CLI: `reconcile --source anthropic-usage|json:PATH
  --window START,END`. Totals only — it cannot vouch for a single call.
- `routing_audit.ingest_claude_code` fills the new columns: `session_id` is
  the Claude Code `sessionId`, `agent_id` the subagent's `agentId` (NULL for
  the main transcript). Rows ingested earlier keep both inside
  `output_parsed`; the columns are not backfilled — they are outside the
  audit envelope, but not outside the append-only guard.
- The OTel exporter emits them as `traceguard.agent_id` /
  `traceguard.session_id` and, for backends that group natively, as the
  semantic-convention `gen_ai.agent.id` / `session.id`. Omitted when NULL.

### Changed

- `docs/audit.md`: threat-category table for control bypass / evidence
  tampering; "(experimental)" dropped; anchor sinks and reconcile documented
  with what they do and do not prove.

---

Earlier in this release — contract-external, SemVer **minor**: no MUST field
is added or changed, no existing signature moves, the normalize algorithm is
untouched.

**One release, one problem: invariant 2 could pass without checking a model.**
`validate_model_timing` checks the trace's `model_id`, and SPEC §3.1 fixes that
as the model the caller *requested*. Direct against a provider, requested and
served are the same string. Behind an OpenAI-compatible gateway asked for a
routing alias — `orcarouter/auto`, `openrouter/auto` — they are not: something
else answers, chosen per request by the router's policy and upstream health. An
alias has no `available_to_us_at`, so the invariant compared a name that was
never a model, did not raise, and the backtest came out green.

### Added

- `traceguard.gateways` — presets for OpenAI-compatible gateways (OrcaRouter,
  OpenRouter), alphabetical and with none recommended over another. Adding one
  is a dict entry, not code. `is_alias_model()` flags routing aliases.
- The SDK wrappers record who actually answered, under
  `output_parsed["routing"]`: `requested_model`, `served_model`,
  `requested_is_alias`, `diverged`. `diverged` is `null`, never `false`, when
  the gateway reported no model — "we don't know" and "they agree" are
  different states.
- `traceguard.routing_integrity` — grades each trace `verified` / `diverged` /
  `unregistered` / `unverifiable`, with a CLI that exits 1 on anything
  actionable: `python -m traceguard.routing_integrity --db …`. Traces with no
  `feature_as_of` are skipped by default; they make no point-in-time claim.
- `examples/gateway_call.py` — runnable offline (prints the client config it
  would build when no key is set).

### Not in this release

The scan reports that a diverged trace needs re-checking; it does not yet
re-run invariant 2 against the served model and fail on the result. Until it
does, `diverged` means "go look", not "this is fine".

## [1.4.0] - 2026-08-19

Contract-external, all inside `traceguard.routing_audit`. Additive: sections
1-4 and 3b of the cache audit render byte-for-byte as before, verified against
`main` on the reference store for all three of `--format table|md|csv`.

**One release, one problem: making a cache audit leave the laptop.** 1.3.0
finished the report and left it stranded — the numbers were only ever true of
one machine, and nothing in the output let a second person check whether their
numbers and yours described the same thing. Every item below exists so that two
sets of figures can be put beside each other and the comparison mean something.
They are listed in the order that question gets answered: get the numbers out,
say which traffic they came from, stop them changing afterwards, say when they
were taken, and give them somewhere to go.

**`--emit-share` / `--show-share`: a cache-audit summary you can hand to
someone else.** Comparing cache behaviour across organisations needs a corpus,
and a corpus needs people willing to send a file. That is a trust problem
rather than a serialisation one, so the export is shaped around the reader
rather than the writer: `--show-share` prints the exact bytes `--emit-share`
would write, in full, so there is nothing to find out afterwards. The tool
makes no network calls and has no upload path.

- **Aggregates only.** Per-model hit rates and token volumes, gap-bucket counts
  and per-bucket cost, the cap band, both ends of the net-benefit range, the
  cross-model switch rate, and the undecidable count. No prompt text, no
  paths, no session ids, no per-trace timestamps, no free-form strings.
- **The invariant, not a list of things remembered.** Every string in the
  payload is a schema constant, a whitelisted model id, one of the two window
  bounds, the installed version, or a decimal money literal. `model_id` is
  whitelisted against the published price sheet rather than passed through,
  because an arbitrary model id can name an internal gateway or an employer;
  anything else folds into one `(unrecognized)` row that keeps the counts and
  drops the name. A store poisoned with sentinel strings in prompts, paths,
  session ids, model names and unplanned-for keys is asserted to leak none of
  them, and two further tests exist only to prove that assertion can still fail.
- **An open window is refused, not warned about.** Every rate and dollar figure
  scales with how long you looked, so `--since`/`--until` (or `--benchmark`)
  are mandatory for an export. A corpus whose members each measured "all time"
  is not comparable with itself, and nothing downstream can repair that.
- **The band is the citable field.** `recommended_cap_band` names the answer
  and `argmax_reference_only` names the point estimate, so quoting the argmax
  reads wrong at the call site. Net is emitted at both ends of the
  undecidable-gap assumption (`measured` and `pessimistic`), never as one
  number.
- **`tool_version` is read from installed package metadata**, never from a
  literal in this repo. A submission claiming a version it was not produced by
  would corrupt the corpus in a way nobody could detect later, and this repo
  has already had to fix one hand-copied number that went stale.
- **New: cross-model switch rate by gap-length decile**, each group carrying
  its own bounds in minutes. On the reference store the rate runs from 0% at
  60-72 minutes of idle to 23.1% past 24 hours, which is the empirical reason
  an optimal keep-alive cap is finite rather than infinite. A single overall
  rate averages that away.

- **`corpus.fingerprint`, because a closed window is necessary and not
  sufficient.** The window closes over timestamps; the store keeps growing
  inside it, since `ingest` walks `~/.claude/projects` and a transcript that
  only appears later carries messages timestamped weeks ago. The reference
  `--benchmark` window went from 432 expired gaps over 168 sessions to 439 over
  174 in under a day without moving by a second, and the argmax net moved from
  $811.30 to $806.82 with it. The fingerprint is a sha256 over one tuple per
  trace the window loaded (session, timestamp, model, prompt and output volume,
  source), sorted so row order does not matter and length-delimited so two
  corpora cannot serialise to the same bytes. Session ids are hashed into it and
  none comes back out: one digest covers the whole set and no per-record digest
  is emitted anywhere. The first version of `benchmark/README.md` told people to
  pick a window and keep it, full stop; that advice was a necessary condition
  sold as a sufficient one and has been corrected in place.

- **`--emit-share` refuses to overwrite an existing file** and exits 2. A corpus
  entry is an immutable record of one corpus, not a document that gets a new
  version: its numbers can already have been cited from a path that cannot be
  updated, and overwriting would rewrite them while the path kept pointing at
  what looks like the same thing. Entries are named
  `NNN-<source>-<first 8 of corpus.fingerprint>.json`, so a re-run over grown
  traffic is a new entry sitting beside the old one rather than replacing it.

- **`generated_at` / `settling_days`: when you pulled it, not just what you
  pulled.** `corpus.fingerprint` shows that two submissions are different
  corpora and cannot show why. `settling_days` — export time minus
  `window.until` — can: a file exported the day a window shut reports a tail
  still filling in, one exported a month later reports a tail that has probably
  stopped. Proposed by **Boris Dzhingarov**, from the Google Search Console
  version of the same bug, where the last days of any date range are
  provisional and quietly revise themselves after export so a weekly report
  never reconciles with the previous one. Neither field feeds the fingerprint:
  mixing a clock into it would make every re-run of unchanged traffic look like
  fresh traffic. `settling_days` may be negative and is not validated away —
  `--until <date>` closes at 23:59:59.999999, so a 09:00 export legitimately
  sits ten hours before its own bound, and a rule that refused it would pass at
  23:59 and fail at 09:00, which is the opposite of reproducible.

**`benchmark/`** — schema specification, submission criteria, and the first
entry. It holds one file and says plainly that under 20 submissions any
cross-organisation number in it is an anecdote.

## [1.3.0] - 2026-08-17

Contract-external, all inside `traceguard.routing_audit`. The frozen public
surface, the SPEC MUSTs and every existing signature are untouched.

**Read this as one conclusion overturned three times, not as a feature list.**
1.2.0 shipped a keep-alive counterfactual that answered "should you ping to
hold the cache open?" with a single verdict over a single number. A reader
recomputed it and the answer changed; recomputing it twice more changed it
twice more. Each correction was larger than the thing it corrected, which is
why the report no longer prints a single number at all.

**1 — The aggregate verdict was averaging two populations with opposite signs.**
1.2.0 said NOT WORTH IT and stopped. Splitting the money by gap bucket shows
that verdict is the sum of a win and a loss, not a finding: 1–4h gaps pay for
themselves ($82.65 of pings against $896.02–$900.03 of avoidable rewrites)
while >4h gaps drown them ($1,962.07 against $1,036.71–$1,041.43). A verdict
averaged over buckets that behave differently is not a decision — which was the
failure the 1.2.0 write-up was itself about, one level down. Section 3 now also
prices a *capped* policy, one you could actually run: ping until some threshold
of idle, then give up, paying for the pings burned on gaps that outlive the cap
and banking only the ones it bridges.

**2 — The cap that made the split look good was hand-picked.** That threshold
was 4h because 4h is the `1-4h` / `>4h` bucket boundary; the code comment said
so. New section 3b costs every cap from 1h to 12h in 15-minute steps, plus an
uncapped policy competing on equal terms, and takes the argmax: **10h, not 4h**
($811.30 net against $569.10 at 4h). In the same pass, `_rewrite_cost` — an
upper bound with nothing underneath it — got a floor, so every verdict now
compares against an interval instead of one side of one. Verdicts are
three-state accordingly: below the floor WORTH IT, above the ceiling NOT WORTH
IT, and **UNDECIDED** in between, a band the two-state version silently scored
as a win. `session_gaps` and `audit` take a `cap` argument; the default solves
it.

That floor moves this corpus by only 0.4% ($1,941.46 → $1,932.73), and that is
not reassurance: a post-gap write averages ~350,000 tokens against a ~1,500-
token session baseline, so the interval is narrow because the two populations
differ by two orders of magnitude, not because either bound is tight. The
report says so where it prints the number.

**3 — The argmax that replaced 4h was a point estimate, and a single
measurement moved it further than its own lead.** Section 3 had been listing
"caches are model-scoped, so a mid-session model switch makes the preceding
pings worthless" as a stated *approximation* — while both `model_id`s sat in
the store the whole time. Measured: **18 of 378 decidable expired gaps (4.8%)
came back on a different model**, and the rate climbs with idle time, 1.8% in
`1-4h` against 7.1% in `>4h`. Those gaps bank nothing while still costing what
they cost, so their savings are deducted before the argmax is taken. The
direction is the whole point — a longer cap collects a larger share of exactly
the gaps this removes, so omitting it had not added noise, it had pushed the
cap systematically long.

The deduction moved 10h by **$18.76**, while 10h leads the runner-up by
**$7.63**. A correction bigger than the gap between first and second place is
enough to reorder them, and more remain unquantified — the 54 gaps with a NULL
`model_id`, the prompt volume frozen at the pre-gap message, the ping cadence
held at 55m and never swept.

**So the output is a band and an interval, not a number.** Section 3b closes
with the one line meant to be quoted:

```
RECOMMENDED CAP: 9h..12h (cadence 55m). Within this band the choice costs under
10% of the optimum, which is less than the size of corrections still outstanding.
```

The argmax stays in the table, marked "not for citation". Two ranges are
reported because they answer different questions and one footnote had been
claiming both: the **sign-stable range** (net > 0) is `1h15m..12h`, 44 grid
points, and says only that capping is the right shape of policy — net inside it
spans `$102.13..$811.30`, 8x, so it emphatically does not say the caps are
interchangeable. The **argmax neighbourhood** (net within `k` of the maximum,
`k` default 0.10, `--peak-band-tolerance`) is `9h..12h`, 13 points, and is the
one that does. Where a range ends at a grid edge it is flagged censored, and
the flag carries the marginal evidence rather than a shrug: nothing above the
argmax recovers to it, drift out to 12h is -$48.83, and the single observation
beyond the grid is a further -$950.86 — a peak past 12h is unsupported, not
excluded.

Because the cross-model deduction only removes gaps *proven* to have switched,
the headline is the optimistic end of a range, so the sweep runs the other end
too: treating every undecidable gap as cross-model, the argmax stays at 10h and
its net falls to **$663.69**. The truth is between the two runs and the report
declines to say where.

### Also

- **`--benchmark`** pins a frozen reporting window (`2026-05-30..2026-08-16`)
  and refuses to combine with `--since`/`--until`. The store is appended to
  continuously and the expired-gap count drifted 429 → 432 across one afternoon
  of editing; copying the DB aside fixes one comparison, a closed window fixes
  every future run. Every number quoted above and in the README comes from it.
- Section 2 gained per-bucket money, the rewrite bracket and a measured model
  switch column reading `3 of 166 (1.8%), 23 unknown` — the unknowns are
  deliberately outside the denominator, which the earlier shorthand hid.
- Sections 1 and 4 are byte-identical to 1.2.0 throughout all of the above.
- Money is still never guessed: unpriced models, unpriced speed tiers and
  gaps with no comparable `model_id` are counted and left out of the totals.

### Fixed

- **`python -m traceguard.routing_audit.ingest_claude_code` silently did
  nothing.** The module holds the parser while the CLI lives in `ingest`, and
  it had no `__main__` guard, so the documented command imported it and exited
  0 without a word — indistinguishable from a backfill that worked. The
  package README documented exactly that command, and without `--write` besides.
  The module now delegates to the real CLI, the README shows
  `python -m traceguard.routing_audit.ingest --write`, and a test runs `--help`
  through every documented entry point.

## [1.2.0] - 2026-08-16

Minor: a read-only prompt-cache efficiency audit under `traceguard.routing_audit`,
and a metering correction in `wrap_anthropic`. The contract is untouched — the
frozen 29-symbol surface, all SPEC MUSTs, the normalize algorithm and every
existing signature are unchanged, and no dependency was added. `routing_audit`
is contract-external and `tokens_in` is a nullable non-MUST field whose value
convention the SPEC does not pin, so neither change reaches the major bar
(SPEC §6).

**Read the `wrap_anthropic` entry first if you already have a store of traces.**
The same API call now records a larger `tokens_in` than it did in ≤1.1.1, and
old traces are not migrated: on cache-heavy traffic the upgrade boundary is a
break in the series, not a step in it.

### Fixed

- **`wrap_anthropic` severely under-recorded `tokens_in` on cached traffic.**
  This is a **recorded-metrics semantics change**: the same API call now writes
  a larger `tokens_in` than it did in ≤1.1.1. Traces written by earlier
  versions are not migrated and are not comparable to new ones for any
  cache-heavy workload — re-derive from `output_parsed.usage` where it exists,
  or treat the boundary as a break in the series.

  The Messages API reports three **mutually exclusive** input counts:
  `input_tokens` covers only the uncached prefix, while `cache_read_input_tokens`
  and `cache_creation_input_tokens` cover the rest. The wrapper recorded
  `input_tokens` alone, so any prompt served from cache was counted as a
  fraction of its real size. Agent-shaped traffic is the worst case: in this
  repo's own routing-audit corpus, `opus-4-8` shows 5.1M bare input tokens
  against 5,318M cache-read — an under-count of roughly three orders of
  magnitude, which propagates into any per-token rate, cost estimate or
  routing decision computed from the affected traces.

  `tokens_in` is now the sum of the three, i.e. **full prompt volume**. That is
  the convention `traceguard.routing_audit.ingest_claude_code` and
  `routing_audit.rerun` have always used, and the wrapper was the one writer
  that disagreed. `tokens_out` is unchanged.

  The streaming branch is deliberately untouched: usage is not available until
  the caller drains the stream, so it still records `parse_status="partial"`
  with no tokens rather than a false zero.

  `wrap_openai` was checked and is **correct as-is** — OpenAI's convention is
  the opposite. `usage.prompt_tokens` (Responses: `input_tokens`) already
  includes the cached prefix, which `prompt_tokens_details.cached_tokens`
  reports as a subset; summing there would double-count. No metering change on
  that side.

### Added

- **`traceguard.routing_audit.cache_audit` — a read-only prompt-cache
  efficiency audit.** `python -m traceguard.routing_audit.cache_audit
  --db sqlite:///traces_routing_audit.db`, with `--format table|md|csv` and an
  optional `--since` / `--until` window. It opens the store with SQLite
  `mode=ro` and never writes; like `routing_audit.rerun` it emits only
  aggregates, token counts and money — no prompt or answer text.

  It answers "your prompt cache hit rate is low" in four sections: per-model
  token-weighted hit rate with input-side list cost against a no-cache
  counterfactual; the distribution of gaps between consecutive requests inside
  a `session_id` (`<5m / 5m–1h / 1–4h / >4h`) with an **upper bound** on
  cache-expiry rewrite cost; a keep-alive-ping counterfactual (one ping per 55
  minutes across every >1h gap, billed at the 0.1× read multiplier) with an
  explicit worth-it / not-worth-it verdict; and the non-`claude_code_session`
  traffic, checked against each model's minimum cacheable prefix so a
  structurally-uncacheable 0% is not mistaken for a misconfiguration.

  It does **not** ingest — point it at a store `ingest_claude_code` already
  filled. No new price table either: every figure comes from
  `routing_audit.pricing` (`price_for`, so Sonnet 5's two price eras resolve by
  `invoked_at`, and `cache_creation_split`, so both the flat store shape and
  the nested transcript shape reconcile). A model with no list price, or a
  speed tier with no published price, keeps its token counts and reports `n/a`
  money rather than a guess.

  The one new constant is `MIN_CACHEABLE_TOKENS` (Opus 5 / Fable 5 512, Opus
  4.8 / Sonnet 5 1,024, Opus 4.7 2,048, Haiku 4.5 4,096), read from the
  Anthropic prompt-caching reference on 2026-08-16. It is **not monotonic
  across generations** — 4.7 needs twice its successor's prefix — so a model
  absent from it reports `unknown` instead of being interpolated from a
  neighbour.

- Both wrappers now record the per-kind usage split under
  `output_parsed["usage"]`, so cost can be recomputed from the store instead of
  only at write time.

  For `wrap_anthropic` the keys are flat and match the block written by
  `routing_audit.ingest_claude_code` exactly — `input_tokens`,
  `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`,
  `cache_creation_5m`, `cache_creation_1h`, `service_tier`, `speed` — which
  makes a wrapper-produced trace directly consumable by
  `routing_audit.pricing.compute_cost_usd` (its `cache_creation_split` reads
  this flat shape), including the 2× one-hour cache-write multiplier. The API
  reports the TTL split nested under `usage.cache_creation`; the wrapper
  flattens it on write, as ingest does.

  For `wrap_openai` the keys are OpenAI's own (`prompt_tokens` /
  `completion_tokens` / `cached_tokens`, and `input_tokens` /
  `output_tokens` / `cached_tokens` on the Responses API) — deliberately not
  remapped onto the Anthropic names, since the pricing table covers only
  `claude-*` models and there is no reader for a cross-provider key convention.

  No public API changed and no cost is computed inside either wrapper: pricing
  stays a `routing_audit` concern (contract-external), per the layering rule.

## [1.1.1] - 2026-08-05

Patch: a concurrency fix in the audit evidence layer. No contract change — the
frozen 29-symbol surface, all SPEC MUSTs, the normalize algorithm and the chain
hash algorithm (v1) are untouched, and no dependency was added.

### Fixed

- `traceguard.audit.enable()` could raise `OperationalError: table
  audit_chain_entries already exists` when several processes reached the
  **first** `enable()` on the same database at the same time.

  `ensure_audit_tables` handled the TOCTOU race with a single retry, on the
  assumption that whoever loses the race re-runs `create_all` and finds
  everything present. That assumption misses the case that actually bites:
  `create_all` walks several tables and the walk is not atomic, so two racers
  can lose to each other on *different* tables in turn — A creates `t1` while
  B fails on `t1`; B retries and reaches `t2` just as A gets there. The second
  collision escapes an except-block that only wraps one retry.

  Now retries in a bounded loop and treats "every audit table is present" as
  success regardless of which racer created which table, which is the
  postcondition the function actually promises.

  The window is narrow — the existing two-thread regression test passes 30/30
  locally and still surfaced the failure once on a slower CI runner. Covered by
  a new higher-concurrency test (8 racers × 12 rounds) that reds reliably
  against the single-retry implementation; the original two-thread test is
  unchanged.

## [1.1.0] - 2026-07-13

First post-1.0 minor: the **audit evidence layer** (`traceguard.audit`,
experimental, SPEC §6.6 off-surface opt-in). Purely additive — the frozen
29-symbol surface, all SPEC MUSTs, the normalize algorithm, and every existing
signature are untouched; zero new dependencies (stdlib `hashlib`/`json`).

### Added

- `traceguard.audit` submodule (import from the submodule path; NOT on the
  frozen surface):
  - **ORM-layer append-only guard** — blocks accidental ORM UPDATE/DELETE
    (unit-of-work flush, `session.delete`) and `session.execute(update(Trace)
    ...)`/`delete(Trace)` against `traces`; `cost_usd`-only updates pass (the
    legal reprice path, SPEC §3.1). Anti-mistake tier only: Core SQL, raw
    drivers, legacy bulk APIs (`bulk_update_mappings` etc.) and dialect
    upserts bypass it — documented in docs/audit.md.
  - **Row hash chain** (`audit_chain_entries`) — every ORM-inserted trace is
    chained (`sha256(prev_hash || canonical(entry metadata + content))`,
    `cost_usd` excluded from the envelope, algo v1 frozen by golden tests).
    `verify_chain()` recomputes the whole chain in two passes and reports
    BREAK/WARN/GAP findings; `export_anchor()` emits the chain head for
    external storage. Tamper-EVIDENT, not tamper-proof — without an external
    anchor, full-chain rewrite and tail truncation are undetectable
    (docs/audit.md states the exact boundaries).
  - **Cost event ledger** (`audit_cost_events` + chained entries) — records
    legal `cost_usd` writes; `verify_chain` cross-checks the live column
    against the newest chained evidence (`cost_mismatch` WARN).
  - Deletion tombstones (`record_deletion`), backfill of pre-existing rows at
    `enable()` (attests state-at-enable-time), `python -m traceguard.audit`
    CLI (`enable|disable|verify|anchor`).
  - Fail-open by default per SPEC §4.1 (SAVEPOINT-isolated, bounded retries on
    head races); opt-in strict mode via `enable(strict=True)` /
    `TRACEGUARD_AUDIT_STRICT=1`, mirroring `strict_persistence`.
  - Importing the module has zero side effects; activation is explicit
    (`enable()` / `attach()`) and scoped to attached engines.
- `routing_audit.reprice`: optional keyword-only `on_cost_write` callback on
  `reprice_null_costs()` / `rollback_reprice()` and a `--audit` CLI flag that
  wires it to `record_cost_event` (default `None` = behavior unchanged).
- SPEC §6.6 (zh) / §6.1 (en): `traceguard.audit` added to the opt-in
  extension list. New doc: `docs/audit.md` (honest three-tier threat table).

## [1.0.0] - 2026-07-12

The freeze-flip. **Zero functional changes** — no new symbols, no signature
changes, no behavioural changes; every 0.9.0 test passes unmodified. This
release turns the contract that 0.8.0–0.9.0 already enforced (SPEC §3–5 MUSTs
including invariant 4, fail-open instrumentation, the 29-symbol curated surface
guarded by the required `contract-guard` CI job) into a formal SemVer
commitment: from this release, breaking any public signature or MUST item
requires a major version bump (SPEC §6).

Soak evidence behind the flip: two external consumers (`huadian` via the
guardian bridge, `quant_alpha_v2` via manual spans) writing real traces since
0.9.0, and a two-week quiet period in which the frozen surface needed zero
fixes.

### Changed

- Development Status classifier: `3 - Alpha` → `5 - Production/Stable`.
- SPEC status flipped from Draft to **v1.0** (`TRACEGUARD_SPEC.md` /
  `docs/SPEC.md`); the SPEC version now tracks the package major. The Chinese
  original remains authoritative.
- Doc status lines updated: ROADMAP marks the 1.0 definition as achieved;
  `INTEGRATING.md` no longer says the SDK is unreleased.

Note: this is the first wheel that ships `traceguard.routing_audit`. It is
**contract-external and experimental** (off the frozen surface, like
`exporters.otel` / `contamination` / `loop`) — importing it implies no SemVer
promise.

## [0.9.0] - 2026-06-29

Point-in-time instrumentation reaches the wrappers, plus a guardian bridge — the
release that turned on traceguard's first **external** adoption (Phase 0
acceptance #7): `huadian` (via the bridge) and `quant_alpha_v2` (via manual
spans) now write real traces, the latter catching **206 look-ahead violations**
in a 2016–2026 backtest scan. **No breaking changes** — every 0.2.0–0.8.1 public
signature is unchanged; all additions are new keyword params, one new symbol, and
a new off-surface submodule (SemVer minor).

### Added

- **`feature_as_of` on `wrap_openai` / `wrap_anthropic`** — a `datetime`, a
  zero-arg callable resolved per call (to replay many points in time without
  touching the `create()` call site), or `None` (default, unchanged behaviour).
  Stamping it makes wrapper traces checkable by the look-ahead invariants
  (SPEC §3) — turning "tracing" into "look-ahead protection". Fail-open: a
  callable that raises, or a naive (tz-less) datetime, downgrades to
  `feature_as_of=None` with a warning rather than breaking the host call or
  silently dropping the trace.
- **`resolve_feature_as_of`** (new public symbol) — the wrappers' point-in-time
  resolution, exposed so consumers instrumenting by hand (their own
  `Tracer.span`, e.g. a no-SDK / bare-httpx client the wrappers cannot attach to)
  get identical fail-open semantics instead of re-implementing them.
- **`traceguard.bridges.guardian.write_trace_from_guardian`** — an opt-in,
  off-the-frozen-surface bridge that writes one trace from a `pipeline-guardian`
  `StepOutput` + `GuardianDecision`. Duck-types guardian (never imports it), is
  fully fail-open, and carries `feature_as_of`, so a project already running
  guardian adopts traceguard with ~5 lines at its existing decision seam —
  without changing its pinned guardian dependency.
- **`register_model(if_exists="error" | "ignore")`** — `"ignore"` makes a fixed
  set of registrations idempotent across re-runs; default `"error"` preserves the
  insert-only contract (SPEC §3.2).
- `examples/manual_span.py` — the bare-client manual-instrumentation recipe and
  the sanctioned sync-context-manager / async-body pattern.

### Changed

- CI gained a **required `contract-guard`** job (frozen public surface +
  normalizer golden hashes + the four look-ahead invariants) and `main` is now
  branch-protected, so the 1.0 freeze is enforced by mechanism, not convention.

The public import surface is now **29 symbols** (added `resolve_feature_as_of`).
1.0 remains a freeze-only flip — now gated on soak, with genuine external
adoption already in hand.

## [0.8.1] - 2026-06-28

Patch release: one adoption-blocking bugfix in the SDK wrappers. **No API
changes** — the public surface (`__all__`, 28 symbols) and all signatures are
unchanged; this is a strict behavioural fix (SemVer patch).

### Fixed

- **Wrapped clients are now transparent to `copy.deepcopy` / `copy.copy`.** A
  client returned by `wrap_openai` / `wrap_anthropic` previously raised
  `RecursionError` when copied (and `TypeError` on the engine-backed tracer once
  past it). The delegating `__getattr__` forwarded the `__setstate__` /
  `__reduce_ex__` dunders the copy/pickle protocol probes on a half-constructed
  (`cls.__new__`) instance to a not-yet-set delegate attribute, recursing
  forever. Frameworks such as LangChain / LlamaIndex deep-copy LLM clients, so a
  wrapped client crashed where the raw client would not. Delegation is now
  factored into a private `_DelegatingWrapper` mixin that (1) raises
  `AttributeError` for any private/dunder lookup, so the copy/pickle protocol
  falls back cleanly, and (2) implements `__deepcopy__` sharing the process-level
  `Tracer` by reference (a sink, never copied) while deep-copying the wrapped
  client. The wrapper no longer *adds* a copy-time failure the underlying client
  didn't already have.

## [0.8.0] - 2026-06-28

The **contract-close** release on the road to 1.0: every SPEC §3–5 MUST is now
implemented and enforced, the public import surface is curated and ready to
freeze, and instrumentation can no longer break the host call. **No breaking
changes** — every 0.2.0–0.7.0 public signature is unchanged (SemVer minor): all
additions are new tables/symbols/keyword params, the default happy path is
preserved, and the two behavioural fixes (fail-open persistence, no streaming
false-success) strictly improve correctness. 1.0 itself will be a freeze-only
flip (no new features) once this has soaked.

### Added

- **Replay sets + invariant 4** (SPEC §3.4/§4.5/§5.4): `replay_sets` /
  `replay_set_items` ORM tables with **physical lock rejection** — once a set is
  locked, ORM flush-layer events reject any item add/modify/delete, any mutation
  or unlock of the set, and deletion, raising `ReplaySetLockedError`. The
  read-side validator `assert_replay_set_locked(replay_set_id, *, engine=None)`
  completes the four look-ahead invariants so consumers can satisfy SPEC §7.4
  ("call invariants 1–4 in CI"); an un-migrated DB surfaces a clear
  invariant-4 error instead of a raw `OperationalError`. The sanctioned
  write-path ships in `traceguard.registry.replay`: `create_replay_set`,
  `add_replay_item`, `lock_replay_set`, and the `build_locked_replay_set`
  convenience.
- **Curated top-level public API**: `traceguard/__init__` now re-exports the
  stable contract surface behind a real `__all__` (Tracer/Span/tracer,
  normalize_input/input_hash, wrap_anthropic/wrap_openai, the model/prompt
  registries, the replay write-path, all four validators, and the ORM). Deep
  submodule paths remain importable as aliases, so pinned consumers do not
  break. Opt-in non-contract extras (otel/contamination/loop) stay off the
  frozen surface.
- **`py.typed`** (PEP 561): the fully-annotated package now advertises its
  types, so downstream type-checkers see them — including the `Literal`-typed
  `select_model(..., strict=...)` safety story. Verified it ships in the wheel.
- **Opt-in fail-closed persistence**: `Tracer(strict_persistence=...)` /
  `TRACEGUARD_STRICT_PERSISTENCE=1` make a persistence failure propagate, for
  backtests where a silently-missing trace could hide an anachronism.
- **Tests**: a frozen golden-hash table for `normalize_input` (the highest-
  blast-radius function) and an API-surface snapshot test, so canonicalization
  drift and accidental surface changes fail CI rather than slipping through.

### Fixed

- **Instrumentation is now fail-open** (SPEC §4.1 failure-mode MUST): the SQLite
  source-of-truth write was unguarded while only the opt-in OTel path was
  isolated — backwards. A locked/full/missing-table DB propagated to the caller,
  and on the error path the flush ran before `raise`, so a persistence error
  *replaced* the original business exception. Persistence is now swallowed +
  logged by default and never masks the business call.
- **No more streaming false-success traces**: a `stream=True` call returns an
  iterator the wrappers don't drain, yet they recorded `parse_status='success'`
  with empty text, null tokens, and ~0 latency — corrupting the dataset
  TraceGuard exists to make trustworthy. All three entry points (Anthropic
  messages, OpenAI chat.completions, OpenAI responses) now record an honest
  `parse_status='partial'`. (Full stream accumulation is deferred post-1.0.)

### Changed

- **SPEC v0.2 → v0.3**: adds the §4.1 fail-open MUST, corrects the §4.5
  "pure function" wording (invariants 2 and 4 read the store and take
  `engine=`), and records that invariant 4 / `replay_sets` are now implemented.
  `validate_model_timing` / `assert_replay_set_locked` are documented as
  store-reading. `TRACEGUARD_ROADMAP.md` carries a 2026-06-28 status update that
  supersedes "1.0 = Phase 2 complete" with the real 1.0 definition (contract
  honored + frozen surface + fail-open) and fixes a false "drift_alerts table is
  SPEC-defined" claim.

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

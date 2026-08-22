# DeepSeek Harness session accounting — conformance invariants

v0.3.0 · 2026-08-19 · companion to [`CONFORMANCE.md`](CONFORMANCE.md) (Claude Code) ·
harness in [`usage-tracker-audit/dsh-probe/`](usage-tracker-audit/dsh-probe/) ·
D-3 and D-4 filed upstream as [deepseek-harness#1886](https://github.com/deepseek-ai/deepseek-harness/discussions/1886)

If your plugin reads a DeepSeek Harness session log and reports tokens, cost,
or usage totals, these are the invariants it has to hold.

The Claude Code catalog exists because shipped trackers violated its entries.
This one starts earlier: DSH is nine days old at v0.1.0-rc.6, and the
ecosystem around it is two days old. So the entries below are split by how
they were established. Four were measured on a real corpus, two of those are
gaps in the official projection itself, a fifth needs no corpus at all and is
a reconciliation you run against your own output, and four Claude Code
invariants are recorded here as **structurally satisfied**, because a catalog
that only ever adds rules is not a catalog, it is a list of fears.

Corpus figures are measured, not estimated. Source citations are file and
line in `deepseek-ai/deepseek-harness` at `47f9438` (v0.1.0-rc.6). What the
corpus does *not* cover is stated in **Limits** at the bottom; read that
before quoting any percentage here.

**The corpus.** Four sessions, 8,650 events, 78 usage samples, two providers
on two independent routes: a local Ollama endpoint serving `qwen2.5:14b`
(no cache traffic) and MiniMax-M3 over `minimax-cn` (249,728 cache-read
tokens, 81% of its corrected total). Each route was folded separately as well
as together; where a figure differs between them, both are given.

---

## What carries over from Claude Code, and what does not

| Claude Code invariant | On DSH |
|---|---|
| I-1 · count each message once | **restated as D-1** — same class, new mechanism |
| I-2 · collapse under per-bucket max | **restated as D-4** — the "duplicates" are sometimes separate attempts |
| I-4 · never sum streaming partial snapshots | **structurally satisfied** (S-1) |
| I-5 · walk the tree recursively | **inverted into D-2** — children are siblings, and the hazard runs the other way |
| I-7 · sources rewrite themselves | **structurally satisfied** (S-2) |
| I-11 · an unpriced model is an error | **applies unchanged** (S-4), and DSH ships no pricing layer at all |
| — | **D-3** is new: a metering event the official projection does not fold |
| — | **D-5** is new: a residual you can check against your own output, no corpus |

---

## D-1 · Fold usage per `(turn, step)` — every sample is written twice

One model call reports its usage twice. Once as a stream chunk
(`assistant/chunk` whose `chunk.type === 'usage'`), once on the assembled
message (`assistant/message.usage`). Both carry the same `TokenUsage`. A
walker that adds every usage it sees doubles the whole corpus.

`TokenUsage` counts are disjoint (`packages/llm/llm/src/types.ts:135`:
"Counts are DISJOINT: inputTokens is uncached input only"), so the four
buckets sum without further correction, and `reasoningTokens` is a subset of
output that must not be added again.

**Measured:** naive summing came out at **2.000000×** the deduplicated total.
On the full corpus, on the qwen route alone, and on the MiniMax route alone,
independently, to six digits with no remainder. 13 of the 14 usage-bearing
`(turn, step)` groups in the inspected log carry exactly two byte-identical
samples; the fourteenth is D-4.

The cache buckets behave the same way. 249,728 cache-read tokens on the
MiniMax route double exactly like input and output do, which is the point
worth having: cache is usually the largest bucket and the cheapest per token,
so a doubling there moves a cost report more than a doubling of output does.

**The official implementation already does this**, which is the evidence that
the hazard is real rather than theoretical:
`packages/llm/token-meter/src/usage-projection.ts` keeps a single `last` slot
and, on a repeated `(turn, step)`, subtracts the previous buckets before
adding the new ones (`addReplacing`, lines 126-136). Its README says so in
words: "a final assistant-message usage for the same (turn, step) replaces
that sample instead of double-counting it."

**Check:** fold naive and fold per `(turn, step)` over the same log. The
ratio is the answer. Anything near 2 means the dedup is missing.

## D-2 · Discriminate an inherited prefix on `seedLength`, never on `origin`

A forked child's log physically contains a copy of the parent's completed
prefix. The child's header carries `seedLength`; every event with
`seq < seedLength` is the parent's work, sitting in the child's file. Summing
sessions across a root double-counts that prefix.

The trap is which field you filter on. A **subagent** child is stamped
`origin: 'subagent'` (`packages/subagent/subagent/src/child-agent.ts:115`)
and its delegation depth increments. An **ordinary session fork** is not.
`ctx.sessions.fork({sessionId, atSeq, increaseTitle})`
(`packages/client/runtime/src/client/contract/sessions.ts:97`) is a
user-facing action on any session, documented as "fork a session from a
completed-turn prefix of the source". It produces a child with
`parentSession` and `seedLength` set, `delegationDepth: 0`, and no `origin`
key at all.

The codebase draws this line itself, correctly, for its own purpose:
`client/sessions/subagent-lineage.ts` opens with "Ordinary forks terminate
propagation so each visible session owns only its uninterrupted subagent
subtree", and `indexSubagentDescendants` starts with
`if (descendant.origin !== 'subagent') continue`. That filter is right for
counting subagent descendants. Reused for token accounting it silently admits
every ordinary fork.

**Measured, twice, and the two numbers are the finding:**

| fork | inherited | own work | folded whole | overstatement |
|---|---|---|---|---|
| `session-e61d64ec` | 1,008 of parent's 1,012 events | 2,204 | 11,418 | **5.18×** |
| `session-e7c289bf` | 3,171 of parent's 3,171 events | 11,442 | 263,790 | **23.05×** |

The error is not a factor, it is a ratio of inherited work to own work, and
nothing bounds it. Fork a long session, ask one question, and the child
reports the whole parent. Both headers carry `delegationDepth: 0` and no
`origin`:

```json
{"type":"session","version":0,"id":"session-e7c289bf-…","createdAt":1786787…,
 "parentSession":"session-6a717f8a-…","seedLength":3171,
 "delegationDepth":0,"agentPreset":"standard"}
```

Official telemetry handles it by the right key: the OTel coordinator emits
`session.parent_id` and `session.seed_length` and expects receivers to
"stitch on (parent_id, seed_length)"
(`packages/session/session-telemetry/src/coordinator.ts:315-317`). Nothing
does that for you on the file side.

**Check:** fold a child twice, once over all events and once over
`seq >= header.seedLength`. If the two agree on a session whose header has a
`seedLength`, the filter is not running.

## D-3 · Count what compaction costs — the official projection does not

Compaction summarizes older history by making a model call. That call's cost
lands on `compaction/summary.usage`
(`packages/compaction/compaction/src/types.ts`: "Provider-reported token
usage for the summarization request, when emitted"; written at
`packages/compaction/compaction-basic/src/region.ts:447`).

The official `tokenUsage` projection cannot see it. Its `usageOf()` matches
`assistant/chunk` and `assistant/message` and nothing else
(`usage-projection.ts:116-123`), and the summarize call is not a loop step,
so it produces neither. Every plugin built on that projection inherits the
gap, including the ones doing the correct thing and reading
`sessionProjections.tokenUsage` rather than folding the log themselves.

**Measured:** three compaction events, 48,895 tokens, none of them counted.
The largest was one MiniMax-M3 summarize call reporting 44,444 tokens
(536 input, 2,436 output, 41,472 cache-read) to shadow a range whose
`shadowedTokenCount` was 19,962. Whether spending 44,444 tokens to remove
19,962 is a good trade depends on your cache pricing. Nothing in the official
projection tells you it happened.

`usage?` is optional on the event, so a provider reporting no usage on the
summarize call produces no gap. That is a condition on the finding, not an
escape from it: both providers here populated it.

**Check:** fold with and without `compaction/summary.usage` on a log
containing at least one `compaction/summary`. `command/done.sourceEventSeq`
names the summary event of a `/compact` transaction, so the check does not
have to guess from adjacency.

**Reported upstream:** [deepseek-harness#1886](https://github.com/deepseek-ai/deepseek-harness/discussions/1886).

## D-4 · A retried step carries more than two samples — never keep-first

A step is not one request. When a stream dies, the harness retries under the
**same `(turn, step)`**, and each attempt can leave its own usage chunk. The
observed sequence:

```
seq 3203  assistant/chunk  usage   {"inputTokens":0,"outputTokens":0}
seq 3204  assistant/chunk  finish  {kind:'error', failure:{code:'TRANSPORT'}}
seq 3205  llm/retry               retryId=afabacf1 provider=minimax-cn
seq 3206  llm/retry-started       retry=1
…                                  the whole response streams again
seq 3279  assistant/chunk  usage   {"inputTokens":32,"outputTokens":1042,"cacheReadTokens":10368}
seq 3281  assistant/message usage  {"inputTokens":32,"outputTokens":1042,"cacheReadTokens":10368}
```

Three usage samples under one `(turn, step)`, and nothing bounds the count:
one more retry is one more sample.

**Keep-first reports 0 tokens for that step.** Not an approximation — the
entire step's cost, including 10,368 cache reads, disappears. This is the DSH
form of Claude Code's I-2, where keep-first lost 46.2% of output tokens on an
agent-heavy tree.

The doubling in D-1 survived this group only by arithmetic: the failed
attempt reported zeros, so summing three samples still gave twice the truth.
That is luck, not structure, and it is why the ratio is reported alongside
the group-size distribution rather than on its own.

**A second consequence, mechanism confirmed and magnitude unobserved.** The
official fold *replaces* on a repeated `(turn, step)` rather than adding, and
its `apply()` never sees `llm/retry`. So a failed attempt that reported real
usage before the transport died would have its cost silently dropped. Every
failed attempt in this corpus reported zeros, so nothing was lost here and I
am claiming no number. The discriminator exists if you want to handle it:
`llm/retry` / `llm/retry-started` carry a `retryId` and sit between the
attempts.

**Check:** group usage samples by `(turn, step)` and print the size
distribution. Any group above 2 is a retried step. Assert that your fold
reports that step's real cost and not zero.

**Reported upstream:** [deepseek-harness#1886](https://github.com/deepseek-ai/deepseek-harness/discussions/1886) — the replace-not-add half.
The keep-first half is a consumer-side rule and stays here.

## D-5 · Reconcile your fold against the official projection, and the residual must be exactly the compaction usage

D-1 through D-4 are claims about the log. This one is a claim about your own
output, and it is the only entry here you can run without a corpus, without a
vendor account, and without knowing what the true totals are.

State it as `sum(events) == projection` and it fails for the wrong reason.
D-1 writes every sample twice and D-4 adds a third under a retried step, so a
raw sum of usage sightings lands near 2× the projection on any session at all,
compaction or no compaction. The left side has to be a corrected fold: collapse
per `(turn, step)`, treat a failed terminal chunk as an attempt boundary, skip
the inherited prefix on `seedLength`.

The residual between that fold and the official projection is then not zero,
and it is not one number either. Three things separate them, each computable
from the log on its own:

> ```
> corrected − official  ==  Σ compaction/summary.usage        (D-3)
>                        +  Σ usage of superseded attempts    (D-4)
>                        −  Σ official fold over the inherited seed   (D-2)
> ```
> Bucket for bucket, with nothing left over.

Two sides off the same file, no ground truth in between. The decomposition is
the part worth having: a total that is merely wrong tells you to go looking,
while a residual that lands exactly on one term tells you which invariant you
missed, and a residual matching none of them says the catalog is incomplete.

**An earlier version of this entry stated the narrow form** — residual equals
the compaction term alone. That holds only on a log with no forks and no
token-bearing failed attempt, and it is not true of the corpus this catalog
was measured on: the inherited-seed term there is 261,562 tokens, so the narrow
identity misses by more than it captures. Recorded rather than quietly edited,
because the narrow form was published before it was checked.

**Check.** `usage-tracker-audit/dsh-conformance/` runs it against a committed
synthetic fixture. `check.py --self-test` asserts every fold and the identity
above in under a second, stdlib only, no corpus and no vendor account.
`check.py --cmd "<your fold>"` runs the same assertion against an
implementation and names the invariant behind a mismatch.

Verified both ways: on the fixture the identity closes at +2,208 against
+2,208, and on the measured corpus at −212,667 against −212,667, where the
superseded term is 0 because both failed attempts there reported zeros.

**Direction, stated plainly.** Against the official projection today this
assertion fails by construction, because failing is what D-3 *is*. Its use is
as a consumer-side gate now, and as the acceptance test for the upstream fix
later: when [#1886](https://github.com/deepseek-ai/deepseek-harness/discussions/1886)
lands, the residual against the official projection goes to zero, and the same
line that was the bug report becomes the regression.

Asked for by Ethan Walker, who wanted the cheap internal-consistency case
rather than another measurement. The corpus-free substrate is
`le-soleil-se-couche`'s synthetic fixture in that thread, which carries a
compacted session, a retried step and a fork seed in one file, so the residual
can be exercised against all three without anyone's private logs.

---

## Structurally satisfied — recorded so the catalog can be trusted

## S-1 · Streaming partial snapshots (Claude Code I-4) cannot occur

DSH adapters emit one terminal usage value per attempt, not a growing
snapshot: "Adapters emit usage before the terminal finish and nothing
afterward" (`packages/llm/llm/src/types.ts:291`). The Claude Code failure
that inflated input 1.63× and cache_read 1.61× on subagent transcripts has no
analogue. Groups above two members come from retries (D-4), not snapshots,
and the distinction matters: snapshot members are partial views of one cost,
retry members are separate costs.

## S-2 · Logs do not rewrite themselves (Claude Code I-7)

Append-only is a written contract, not an observation: "Flushed events are
never rewritten" (`session-persistence-jsonl/README.md`, Durability and crash
semantics), with a dense contiguous `seq` (`events[i].seq === i`, enforced on
append at `session-persistence/src/coordinator.ts:699`). Compaction shadows
events in the surface projection and leaves the bytes: "The shadowed events
remain in the raw log, so replay is deterministic" (`compaction/README.md`).
The drift class that cost viberank 11% of a month-to-date total between two
submissions cannot arise from the storage layer.

One narrow exception, which does not move token totals: crash recovery
truncates a structurally incomplete tail frame and re-encodes the complete
records, appending synthetic tool/step/turn closers. Closers carry no usage.
`listSnapshots()` (device, inode, size, nanosecond timestamps) is the
supported way to notice.

## S-3 · Chunk packing is not an accounting hazard

`packChunks` (default on) packs runs of ≥3 consecutive same-block delta
chunks into one storage row, which looks like a trap for a line reader. It is
not: the encoder whitelists delta kinds only, and
`packages/core/session/src/chunk-rows.ts` states the boundary —
"block-start/end, usage, finish, and any future chunk variant stay one event
per line." Usage is never packed. Skipping `text-chunks` /
`reasoning-chunks` / `tool-call-chunks` rows is exact.

## S-4 · No pricing layer exists (Claude Code I-11, unchanged)

DSH ships no prices. `grep costUsd|pricePerToken|per_million` across the
repository returns nothing, and `llm-pi-ai`'s README says it outright:
"Pricing and input modalities have no harness consumer." Every plugin carries
its own table, which makes I-11 (an unpriced model is an error, not a zero)
apply here exactly as written, with one addition: two plugins pricing the
same model differently is not a bug in either, and a cross-plugin comparison
that assumes one price is measuring nothing.

**Price consequence, added 2026-08-19. Not a corpus measurement.** DeepSeek's
own list moved on 2026-08-17, and the move is what turns a bucket error from
untidy into expensive. Off-peak `deepseek-v4-pro` input is $0.022/M on a cache
hit against $0.66/M on a miss; `deepseek-v4-flash` is $0.007 against $0.22.
That is 30× and 31×, and the ratio survives peak rates on both models. The RMB
figures circulating in `deepseek-ai/deepseek-harness#2554` (¥0.15/M hit,
¥4.5/M miss) are the same 30× on the v4-pro line.

Two entries inherit this. D-1 doubles every bucket including cache-read, and
cache-read is the largest bucket on the MiniMax route here (249,728 tokens,
81% of its corrected total); doubling the cheap bucket and doubling the dear
one stop being the same error once the two differ by 30×. Separately, a token
moved across the hit/miss line by a mapping bug is wrong by 30×, not by the
few percent its share of the count suggests — #2554 reports roughly 638k
tokens sitting on the wrong side of that line against DeepSeek's own console.

Prices are dated facts, not invariants. This paragraph is here to size D-1's
consequence, and it needs re-reading against the vendor's page before anyone
quotes it.
---

## Running the check

```bash
python3 dsh_usage_probe.py --self-test          # folds vs a hand-computed fixture
python3 dsh_usage_probe.py --root <sessions>    # four folds over a real corpus
```

Stdlib only. `--self-test` builds a parent/child pair exercising D-1, D-2 and
D-3 with constructed ground truth and asserts all four folds against it; it
runs in under a second, touches no real data, and needs no vendor account.
Run it before pointing the probe anywhere, and do not quote a number the
self-test did not precede.

The corpus was produced with `compression: 'none'` and `packChunks: false`
so the log is line-readable by any tool; `PROTOCOL.md` in the same directory
has the patch file and the four-step recipe. One trap that recipe records:
a custom provider needs a credential even when the endpoint does not, because
the provider id doubles as the credential name, and an empty key fails the
request with `MISSING_CREDENTIAL` rather than sending an unauthenticated one.

## Limits

Read these before quoting anything above.

- **Four sessions, 78 usage samples, two routes.** D-1's ratio is exact and
  reproduced independently on both routes. D-2 has two observations, D-3
  three, D-4 one.
- **`cacheWriteTokens` is still untested.** Neither route populated it —
  MiniMax omits the key entirely. Cache *reads* are covered (249,728 tokens);
  cache *writes* are not.
- **D-3's share of the total is corpus-specific** (15.1% overall, 14.4% on
  the MiniMax route). Three compactions over two short sessions. The citable
  facts are 48,895 tokens and 3 events.
- **D-4's second consequence has no number.** The undercount mechanism for
  retried steps is read from source; every failed attempt observed here
  reported zeros.
- **D-5 is a predicate, not a measurement.** The identity closes on the
  conformance fixture and on this corpus. It has not been run against a third
  party's implementation, so how often it catches something the three named
  terms do not cover is unknown.
- **D-4's magnitude is fixture-only.** The superseded-attempt term is 2,220 on
  the synthetic fixture and 0 on every real session measured here, because the
  corpus contains exactly two failed attempts, both reporting zeros, and no
  `aborted` finish at all. The mechanism is read from source; the number is
  constructed.
- **The price figures under S-4 are dated, not measured.** Read from the
  vendor's page on 2026-08-19; everything else here was folded from the corpus.
  Neither route in the corpus is a DeepSeek route, so the 30× gap sizes the
  consequence of D-1 and of mis-bucketing, it does not measure either.
- **Two providers, both `openai-completions`-family routes.** Whether an
  Anthropic-protocol or Responses-protocol route behaves the same is not
  established.

## Who runs these

D-3 is being fixed in a second implementation — tokscale's DSH parser, in
[#1162](https://github.com/junhoyeo/tokscale/pull/1162). The independent
reproductions of D-3 and D-4 on the harness side, and what is still
outstanding upstream, are listed in
[`usage-tracker-audit/ADOPTERS.md`](usage-tracker-audit/ADOPTERS.md).

## Contributing a counterexample

An invariant is a claim, and claims are for breaking. If your corpus
contradicts one, file it with the numbers and the fold you used: a step whose
two usage samples disagree, a `seedLength` that does not bound the inherited
prefix, a provider that populates `compaction/summary.usage` into the
projection, or a failed attempt that reported real tokens before it died.
This catalog changes when a measurement says it should, the same way the
Claude Code one did.

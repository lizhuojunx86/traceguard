# Claude Code transcript accounting — conformance invariants

v0.1 · 2026-08-10 · maintained with the harnesses in [`usage-tracker-audit/`](usage-tracker-audit/)

If your tool reads `~/.claude/projects` and reports tokens, cost, or usage
totals, these are the invariants it has to hold. Every one of them is here
because a shipped tracker violated it and the violation was measured: ten
fixes across four repos (splitrail ×3, tokscale ×2, Clawdmeter ×1, viberank
×4) came out of this list before it was a list, and one PR is still open.
None of these are hypothetical.

Each entry: the invariant, the failure it prevents, where that failure
shipped, and the check. Corpus figures below are measured, not estimated;
sources are the linked issues, which carry the full tables.

---

## I-1 · Count each message once, not once per record

Claude Code writes one assistant message as several JSONL records — one per
content block (thinking / text / tool_use) — and every record repeats the
same `message.id`. Summing `usage` per record multiplies each message by its
block count (mode: 3).

**Shipped violations:** [claude-code-templates #754](https://github.com/davila7/claude-code-templates/pull/754)
(2.36×, open) · [Clawdmeter #21](https://github.com/weltern/Clawdmeter/issues/21)
(2.34–2.37× at three call sites, fixed v3.0.1).
The vendor documents the hazard: the Agent SDK cost-tracking guide says
"Always deduplicate by ID" in a warning box.

**Check (needs no corpus):** the number of usage events must equal the number
of distinct `message.id`s. If it equals the number of records, this invariant
is violated.

## I-2 · Collapse under per-bucket max — duplicates are not always identical

On the main conversation path, repeated `usage` objects are byte-identical
(100.0% of 10,645 groups on one corpus). On sidechain (subagent) records they
are a **running total**: early records partial, the last final (20.82%
byte-identical). The discriminator is the `isSidechain` field, not the writer
version. Keep-first loses 46.2% of output tokens on an agent-heavy tree; max
per bucket is correct in both regimes and immune to all-zero replay copies.

**Shipped violation:** surfaced in the Clawdmeter v3.0.1 fix exchange
([#21](https://github.com/weltern/Clawdmeter/issues/21)); the corrected
aggregator folds every record into its message under per-bucket max.

**Check:** within `(file, message.id)`, `output_tokens` must be
non-decreasing and the last record must carry the max. 12,709 of 12,709
differing groups held this on the corpus that established it.

## I-3 · Group per (file, message.id); dedupe cross-file at the aggregator

Resuming a session replays its records verbatim into a new transcript file.
Two consequences. Grouping by `message.id` alone across files splices
unrelated files into fake non-monotonic sequences (560 phantom groups on one
corpus — a check that lies). And per-file dedup alone leaves cross-file
duplication in the totals (1.095× on one corpus, 1.0353× on another): the
collapse belongs in the account-wide aggregator, not the per-file parser.

**Check:** per-file grouping first; then assert account-wide distinct ids,
not per-file sums.

## I-4 · Never sum streaming partial snapshots

Under one `(requestId, message.id)`, a streaming message leaves ≤2 distinct
usage fingerprints: one partial snapshot plus one final record, differing
only in `output_tokens`. Summing distinct fingerprints adds the identical
input/cache fields once more per snapshot: measured +47–75% on cache fields,
output nearly right (+0.36–0.4%) — which is what makes it hard to notice.
Keep the whole finished record (the one with max `output_tokens`); a
per-field max across fingerprints would assemble a record nobody observed.

**Shipped violation:** [splitrail #220 → #222](https://github.com/Piebald-AI/splitrail/issues/220)
(input 1.63×, cache_read 1.61× on subagent transcripts; fixed).

**Check:** a fixture with two fingerprints under one key — one partial, one
final — reproduces the whole behaviour.

## I-5 · Walk the tree recursively — subagents live below the session

Subagent transcripts live under `<project>/<session>/subagents/**`. A flat
glob missed 54% of messages on a subagent-heavy corpus
([splitrail #207 → #209](https://github.com/Piebald-AI/splitrail/issues/207),
fixed), and saw 75 of 1,398 / 153 of 985 files when the same mistake reached
corpus counters ([viberank #112](https://github.com/sculptdotfun/viberank/issues/112)).
This failure lies in the dangerous direction: it makes drift discriminators
confidently wrong rather than noisy.

**Check:** a regression fixture with a nested `subagents/` directory, not a
comment. Assert the recursive walk finds strictly more than a flat read.

## I-6 · Never stack an estimate on top of an API-reported number

`tool_result` content is sent back to the model in the next turn, so the next
turn's API-reported `input_tokens` / cache fields already account for it. A
`ceil(chars/4)` estimate added per tool_result double-counts: measured 84%
and 87.6% of reported input being estimate (6.26×–8.09× inflation of the
field) on two corpora. Minimal repro: one 21-char tool_result, API says 7,
tool reports 13.

**Shipped violation:** [tokscale #1011](https://github.com/junhoyeo/tokscale/issues/1011)
(parser fixed in v4.11.0; cached history still open — see I-7's cousin:
corrections do not reach caches by themselves).

**Check:** A/B with every tool_result content emptied; reported input must
not move.

## I-7 · Sources rewrite themselves — never let a re-read lower history silently

Claude Code rewrites transcripts in place on resume/compact. A tool that
recomputes from the live tree inherits the rewrite: a cumulative
month-to-date total fell 11% between two submissions 16 hours apart
([viberank #83](https://github.com/sculptdotfun/viberank/issues/83));
3,098 traces arrived carrying pre-cutoff timestamps after the cutoff on
another corpus. Record at first sight, append-only; warn on drift instead of
absorbing it. The cross-tool record for this is
[usage-drift-log](https://github.com/m1kapp/claude-rank/blob/main/docs/usage-drift-log.md)
(six fields, three independent implementations).

**Check:** ingest twice around a rewrite; totals must not silently drop
(>2% MTD drop must surface as a warning, not a new truth).

## I-8 · Scope corpus counters to the period they describe

A corpus disagrees with itself across months (91.5% short for one month,
10.2% for the next, higher for the running one — same instant, same tree). A
single global `{files, bytes}` pair cannot carry per-month verdicts, and it
recovers: at a median 23 new transcript files per active day against 91 files
for a whole past month, deleting that month is masked within 2–4 days of
ordinary work — for exactly the heaviest users. Month is the finest
well-defined scope: 0.4% of files span a month boundary (6.2% of records);
per-day scoping double-counts 8.2%.

**Shipped violation:** the unscoped design died in review; the scoped one
shipped in [viberank #121](https://github.com/sculptdotfun/viberank/pull/121).
Its first implementation then silently dropped 17 of 922 files — caught
because the per-month shape made a 2% undercount legible. The spec's
structure verified its own implementation; that is the argument for the
structure.

**Check:** per-month `{files, bytes}`, recursive count (I-5), compared only
against the same client's earlier record.

## I-9 · Absence is not an observation

A month absent from a scan may have been cleared by the user, pruned by
retention (`cleanupPeriodDays` — `~/.claude/projects` is a rolling window,
not an archive), or never used with this tool at all. Only a positive
`{files: 0}` observation is a deletion. Reading absence as deletion flagged
19 months across 5 production users — and 20 of 20 were months the user
simply ran a different tool ([viberank #124](https://github.com/sculptdotfun/viberank/pull/124),
fixed server-side).

**Check:** the scan must state its own window (`covers.since`); months
outside it are unknown, never deleted.

## I-10 · A verdict must not outrun its evidence

Totals often merge several sources (ccusage `daily` is all-agent since v20);
a corpus scan of `~/.claude/projects` is evidence about Claude Code only.
Letting a Claude-file verdict lower the same day's Codex tokens is the same
population mismatch one level down: $270.61 of non-Claude spend gated by a
Claude file count on one machine ([viberank #125](https://github.com/sculptdotfun/viberank/issues/125),
open). Slice verdicts per (machine, agent) — and note the strict gate is not
a fix: on a multi-tool machine it reaches zero days and switches the feature
off.

**Check:** classify per source; a verdict reaches only rows produced by the
population it measured.

## I-11 · An unpriced model is an error, not a zero

Models enter corpora before price sheets learn their names. A pricing
function that refuses to guess is correct; a total that silently treats the
refusal as $0.00 is not. Measured: $1,213.91 of list-price spend missing
as-a-zero for two weeks while the warning fired into an unread channel;
`claude-opus-5` priced to $0 in a second tracker's registry the same month.

**Check:** unpriced traces must be loud in the machine-readable output (a
nonzero exit, a populated warnings list — not stdout), and countable.

---

## Running the checks

The runnable harnesses live under
[`usage-tracker-audit/`](usage-tracker-audit/): synthetic corpus with a
by-construction manifest, red/green exit code, about a minute, no real
`~/.claude` data touched. Where a GUI was in the way, the harness stubs it
and imports the vendor's own module — the numbers come out of their code, not
a model of it. Where an invariant is about *your* corpus rather than a
target's code (I-2, I-3, I-8, I-9), the entry is a read-only measurement
script instead of a red/green check.

| entry | tracker / corpus | invariants | run |
|---|---|---|---|
| `cct-dedup-check/` | claude-code-templates | I-1 | `./run_check.sh [--commit <sha>]` — red on upstream main |
| `clawdmeter-dedup/` | Clawdmeter | I-1 | `./run_check.sh [--commit <sha>]` — red on v3.0.0, green on v3.0.1 |
| `clawdmeter-dedup/duplicate_usage_shape.py` | your corpus | I-2, I-3 | read-only, stdlib only; prints the splice-artifact count next to the real one |
| `tokscale-input-estimate-check/` | tokscale | I-6 | `./run_ab.sh <binary> <workdir>` (A/B needs real transcripts); `retroactivity_check.sh` for the cache half |
| `tokscale-drift-check/` | tokscale | I-7 | `./run_check.sh [--bin <path>]` |
| `viberank-83/corpus_scope.py` | your corpus | I-5, I-8 | measures flat-vs-recursive counts and period-boundary double-counting |
| `viberank-83/corpus_absence_scope.py` | your corpus | I-9 | measures how much of your submission history has aged off disk |
| splitrail I-4 | splitrail | I-4 | fixture + repro shipped upstream in [#220](https://github.com/Piebald-AI/splitrail/issues/220) → #222; the issue body carries a self-contained repro script |

`traceguard`'s own `routing_audit` store is the substrate these were measured
against: append-only, `message.id`-keyed, Apache-2.0. `pip install traceguard`.

## Status

| tracker | stars (Aug 2026) | violations found | fixed in |
|---|---|---|---|
| splitrail | 216 | I-4, I-5 (+ an earlier dedup fix) | #208, #209, #222 |
| tokscale | 4.6k | I-6, I-7 | v4.9.0, v4.11.0 |
| Clawdmeter | — | I-1 (I-2, I-3 established in the fix exchange) | v3.0.1 |
| viberank | — | I-7, I-8, I-9, I-10 | #111, #121, #124, #143 (CLI v1.9.0) |
| claude-code-templates | 30.1k | I-1 | PR #754 open |

Where these invariants are now enforced by someone else's code, who
reproduced a measurement independently, and what has been offered and not
taken up: [`usage-tracker-audit/ADOPTERS.md`](usage-tracker-audit/ADOPTERS.md).

## Contributing a counterexample

An invariant here is a claim, and claims are for breaking. If your corpus
violates one of these in a way the linked measurements don't cover — a
main-path running total, a k>2 fingerprint group, an all-zero replay copy
that reproduces — file an issue with the numbers and the grouping you used
(I-3 first: the sort can manufacture findings). The catalog changes when a
measurement says it should, the same way it was built.

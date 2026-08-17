# tokscale-input-estimate-check

A/B measurement for one question: how much of the `input` number
[tokscale](https://github.com/junhoyeo/tokscale) reports for Claude Code is the
API's own figure, and how much is a character-count estimate added on top?

Answer: on this corpus, **87.6% is the estimate** — an 8.09× inflation of the
field. Filed upstream as
[#1011](https://github.com/junhoyeo/tokscale/issues/1011).

Unlike [`../tokscale-drift-check/`](../tokscale-drift-check/), which runs on a
synthetic corpus with a known-exact manifest, this one needs real transcripts:
the estimate only fires on `tool_result` blocks that carry no explicit token
metadata, and how much they inflate `input` depends entirely on how tool-heavy
the real sessions are. No synthetic corpus can stand in for that.

## The mechanism

Every `tool_result` block without explicit token metadata gets
`estimate_tokens_from_chars` — `chars.div_ceil(4)` — and that estimate is added
to `input`. Claude Code never writes token metadata on tool_result blocks, so
the fallback fires on all of them.

The tool result is then sent back to the model as part of the next turn's
prompt, and that turn's API-reported `input_tokens` / `cache_creation_input_tokens`
/ `cache_read_input_tokens` already account for it. The same content is counted
twice: once by the API, once by the estimate.

tokscale's own suppression rule names this hazard. The bare-transcript branch
refuses the char estimate because it "would double-count usage already tracked
by the originating client's own parser." Same double count, different route —
here the originating parser is the API's own usage field.

## What the check does

1. `prepare_ab_corpus.py` builds two copies of the real transcript tree under
   isolated fake `$HOME`s: **A** untouched, **B** with every `tool_result`
   block's content emptied. Emptying the content zeroes the estimate and
   changes nothing else: same records, same message ids, same API-reported
   usage. Whatever `input` drops by between A and B is the estimate.
2. It also computes an independent prediction — `ceil(chars/4)` summed over
   unique `(session, tool_use_id)` pairs — so the A/B delta can be checked
   against the mechanism rather than taken as a black-box difference.
3. `run_ab.sh` runs a tokscale binary against each fake `$HOME` and diffs the
   totals.

```bash
./prepare_ab_corpus.py --src ~/.claude/projects --work ./ab
./run_ab.sh /path/to/tokscale ./ab
```

Build the binary from source to test an unreleased branch or `main`:
`cargo build --release` in a tokscale checkout, then pass
`target/release/tokscale`.

## Measurements

| | 2026-08-03 | 2026-08-05 |
|---|---|---|
| tokscale | `9ceae64` | `fc7b26f` (4.9.0, incl. #1038) |
| transcripts | 1,618 | 1,504 |
| `input`, intact | 24,793,081 | 22,778,313 |
| `input`, emptied | 3,960,326 | 2,817,117 |
| estimate share | 84% | **87.6%** |
| inflation | 6.26× | **8.09×** |
| cost impact | $115.53 (1.6%) | $100.67 (1.42%) |
| prediction vs measured | 0.71% apart | **0.010% apart** |

Two independent corpora, two source revisions, same defect. `output`,
`cacheRead`, `cacheWrite` and `messageCount` are byte-identical across each A/B
pair, which is what says the intervention isolated the estimate rather than
perturbing something else.

The tighter prediction agreement in the second run is a measurement improvement,
not a code change: the first prediction summed characters without deduping, the
second dedupes by `(session, tool_use_id)` the way the parser does.

**The cost impact is small and stays small.** Input tokens are cheap next to
cache and output on these corpora, so the dollar figure barely moves. The token
figure is the misleading one, and it is the one a "tokens used" display shows.

## Source references

Line numbers as of `fc7b26f` (they moved since the original report):

| what | where |
|---|---|
| `estimate_tokens_from_chars` — `chars.div_ceil(4)` | `crates/tokscale-core/src/sessions/claudecode.rs:1233` |
| the fallback call site | `:1151`, in `extract_tool_result_input_tokens` |
| the gate | `:563`, `allow_char_estimate: !is_bare_transcript` |

[#1038](https://github.com/junhoyeo/tokscale/pull/1038) ("skip synthetic
tool-result attribution") does not touch this. It stops a `<synthetic>`
placeholder model from inheriting attribution, which is a different path; a
normal tool result still gets its char estimate added to the API-reported
number.

## Retroactivity check (added 2026-08-07, after #1037)

`retroactivity_check.sh <pre-fix-bin> <post-fix-bin> <workdir>` answers a
different question from `run_ab.sh`: not "is the parser right now" but "does
the correction reach numbers that were already cached".

`parser_version(Claude)` was deliberately not bumped in #1037, because bumping
it discards the `RetainObserved` turns #994 exists to keep. So a cache entry
written by a pre-fix build is never re-parsed. Three legs share one HOME:

1. pre-fix binary builds the cache
2. post-fix binary reads the same cache — if the totals are identical, the fix
   is not retroactive
3. transcripts change on disk, post-fix binary re-reads them — measures how
   much clears, and checks that nothing except `input` moves

Measured on 1,513 real transcripts, v4.10.0 against v4.11.0:

| state | input | vs cold-cache truth |
|---|---|---|
| pre-fix, cold cache | 23,132,812 | 8.18x |
| post-fix, inherited cache | 23,132,812 | 8.18x — unchanged |
| post-fix, 400 of 1,513 touched | 12,597,521 | 4.46x |
| post-fix, cold cache (truth) | 2,827,429 | 1.00x |

`output`, `cacheRead`, `cacheWrite` and `messageCount` are identical across
every leg, so partial healing retires nothing.

## Release gate (added 2026-08-17)

`migration_gate.sh <pre-fix-bin> <fixed-bin> <corpus-dir>` is the pass/fail form
of the same three legs. It was written for a targeted cache migration; the
migration turned out not to be needed, because the retention-provenance rebuild
added in [#1085](https://github.com/junhoyeo/tokscale/pull/1085) re-parses every
markerless Claude entry, and every entry that can still hold a char estimate is
markerless — #1037 merged four days before #1085, so no estimate-carrying entry
was ever written by a build that knew about the marker. #1085 has not shipped:
v4.13.0 was cut 2026-08-10, one day before it merged.

So the gate now measures a release rather than a migration branch. Against
`main` at `f169a1f0`, pre-fix binary `v4.10.0`, 1,166 transcripts:

| leg | `input` | `output` | `cacheRead` | `cacheWrite` | `messageCount` |
|---|---|---|---|---|---|
| v4.10.0, cold | 18,989,450 | 31,849,533 | 6,724,587,039 | 212,463,901 | 29,590 |
| `main`, inherited cache | 1,879,713 | 31,849,533 | 6,724,587,039 | 212,463,901 | 29,590 |
| `main`, cold | 1,879,713 | 31,849,533 | 6,724,587,039 | 212,463,901 | 29,590 |

PASS: 17,109,737 tokens cleared, 10.10x down to 1.00x, and only `input` moved.
The rebuild is one-shot — 3.35s for the rebuild pass, 0.20s for every scan after
it, so the marker write-back holds.

**What the measurement does not cover.** A retained row is cloned out of the old
entry (`crates/tokscale-core/src/lib.rs:960`), not re-derived, so an
estimate-carrying row that could be retained would survive the rebuild. None can:
tool-result usage keys embed the session id behind `:tool_result:`, which makes
`dedup_key_is_globally_stable` false for all of them
(`sessions/claudecode.rs:1001`), and the retain filter drops them. This corpus
could not have tested it anyway — leg 1 and leg 3 have the same `messageCount`,
so a cold parse recovered every cached row and the tree held no retained-only
rows at all.

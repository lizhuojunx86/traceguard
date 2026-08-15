# dsh-probe — token accounting for DeepSeek Harness session logs

Folds a DSH session corpus four ways and prints the deltas. The gaps between
the folds are the findings; the invariants they establish live in
[`CONFORMANCE-DSH.md`](../../CONFORMANCE-DSH.md).

```
N  naive      every usage sighting summed
O  official   a line-for-line transcription of tokenUsageProjectionDefinition
S  seed-aware O, minus events inherited through a fork seed (seq < seedLength)
C  correct    S, plus compaction/summary.usage
```

| delta | mechanism | measured |
|---|---|---|
| `N' − O` | usage rides both `assistant/chunk` and `assistant/message` | **2.000000×**, on two providers independently |
| `O − S` | a forked child's log physically contains its parent's prefix | **5.18×** and **23.05×** on two forks |
| `C − S` | `compaction/summary.usage` is outside the official projection | **48,895 tokens** across 3 events |

(`N'` is naive minus compaction. Keeping compaction in would credit the third
mechanism's tokens to the first.)

Corpus: 4 sessions, 8,650 events, 78 usage samples, MiniMax-M3 over
`minimax-cn` and `qwen2.5:14b` over a local OpenAI-completions endpoint.
Measured against `deepseek-ai/deepseek-harness` at `47f9438` (v0.1.0-rc.6).

## Run it

```bash
python3 dsh_usage_probe.py --self-test          # ~1s, no real data touched
python3 dsh_usage_probe.py --root <sessions>    # your corpus
python3 dsh_usage_probe.py --root <sessions> --json report.json
```

Python 3.9+, standard library only. `--self-test` builds a parent/child pair
exercising all three mechanisms with hand-computed ground truth and asserts
every fold against it. Run it first. A fold that disagrees with the fixture
is a bug in the probe, and no number it produces should leave your machine.

`.jsonl.zstd` corpora need one of: the `zstandard` module, a `zstd` binary,
or Node ≥22.15 (`zstd_cat.mjs` handles the concatenated-frame layout, which a
single-frame decode silently truncates to the header). Writing the corpus
uncompressed avoids the question entirely.

## Produce a corpus

[`PROTOCOL.md`](PROTOCOL.md) has the four-step recipe.
[`probe.patch.yml`](probe.patch.yml) is the cordis overlay it uses:

```bash
dsh --profile web --patch ./probe.patch.yml --dump-config | grep -A6 session-persistence-jsonl
dsh --profile web --patch ./probe.patch.yml --port 3099
```

`compression: 'none'` makes the log line-readable; `packChunks: false` puts
one event per line so `seq` contiguity can be asserted directly. Neither
changes accounting behaviour — chunk packing never touches usage events
(`packages/core/session/src/chunk-rows.ts`).

Three traps that cost time, recorded so they don't cost yours:

- `--profile <name>` is required, and parent flags may not precede the `web`
  subcommand. `dsh --patch x.yml web` is rejected; `dsh web --patch x.yml` is not.
- A custom provider needs a credential even when the endpoint doesn't. The
  provider id doubles as the credential name, and an empty key fails with
  `MISSING_CREDENTIAL` rather than sending an unauthenticated request.
- Compaction is easiest to trigger with `/compact` (no arguments) after 4–6
  turns. Lowering `thresholdRatio` invites a compaction loop that pollutes
  the corpus.

## What is not covered

- `cacheWriteTokens` — neither provider populated it. Cache *reads* are
  covered (249,728 tokens); cache *writes* are not.
- Non-`openai-completions` protocol families.
- The retry undercount described as D-4 in the catalog: the mechanism is read
  from source, and every failed attempt in this corpus reported zeros, so
  there is no measured magnitude.

## Files

| file | |
|---|---|
| `dsh_usage_probe.py` | the probe; four folds, self-test, JSON output |
| `zstd_cat.mjs` | multi-frame zstd decoder, fallback for compressed corpora |
| `probe.patch.yml` | cordis overlay: plain JSONL, one event per line |
| `PROTOCOL.md` | corpus recipe and how to read the output |
| `DISCUSSION-POST.md` | the upstream report for the two projection-side gaps |
| `ARTICLE-DRAFT.md` | write-up |

Session logs are **not** committed: a real DSH transcript contains whatever
you talked about. `.gitignore` excludes them. Publish measurements, not
corpora.

## Counterexamples

An invariant is a claim, and claims are for breaking. A step whose two usage
samples disagree, a `seedLength` that doesn't bound the inherited prefix, a
provider that populates `compaction/summary.usage` into the projection, or a
failed attempt that reported real tokens before it died — file it with the
numbers and the fold you used.

Apache-2.0, with the rest of [traceguard](../../README.md).

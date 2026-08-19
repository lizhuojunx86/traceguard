# DSH token-accounting conformance

If your project reads a DeepSeek Harness session log and reports tokens or
cost, this tells you whether the number is right, and when it is wrong, which
invariant you missed.

No corpus. No DeepSeek account. No secrets. Stdlib Python, runs in about a
second.

```bash
git clone https://github.com/lizhuojunx86/traceguard
cd traceguard/usage-tracker-audit/dsh-conformance
python3 check.py --self-test
python3 check.py --cmd "node dist/dsh-total.js"
```

The invariants themselves are in
[`CONFORMANCE-DSH.md`](../../CONFORMANCE-DSH.md); this directory is the
runnable half. `workflow.yml` is a drop-in GitHub Action with one line to edit.

## The contract

Your command is invoked with the fixture's sessions root appended as its last
argument, and the same path in `DSH_CONFORMANCE_ROOT`. It prints one JSON
object to stdout:

```json
{"uncachedInputTokens": 1050, "outputTokens": 105,
 "cacheReadTokens": 10500, "cacheWriteTokens": 13}
```

Absent keys read as zero, and the last JSON object on stdout wins, so logging
before the result is fine. Track only two buckets? Pass
`--buckets uncachedInputTokens,outputTokens` and only those are compared.

Exit status is 0 when every checked bucket matches.

## What the fixture contains, and why

Two sessions, a parent and a fork of it, hand-built so every hazard shows up
exactly once and every number is distinct. `build_fixture.py` regenerates it
and re-derives the expectations by arithmetic before writing anything, so the
committed fixture and the committed totals cannot drift apart silently.

| in the fixture | invariant |
|---|---|
| one assistant message written twice, byte-identical usage | D-1 |
| a child whose leading events are a physical copy of the parent's prefix, bounded by `seedLength` | D-2 |
| a `compaction/summary` carrying provider-reported usage | D-3 |
| a retried step whose **failed attempt reports real tokens** | D-4 |
| non-zero `cacheWriteTokens` | — |

The fourth row is the one worth pointing at. Every failed attempt in the real
corpus behind this catalog reported zeros, so a fold that drops superseded
attempts entirely still lands on the right total there. The fixture is where
that luck runs out: drop the dead attempt and you are 2,220 tokens short, and
the checker says so by name.

The last row is a gap the corpus could not close either. Neither provider route
populated `cacheWriteTokens`, so the bucket had never been exercised.

## The four folds

An implementation usually lands on one of these, and landing on one exactly is
itself the diagnosis.

| fold | fixture total | what it is |
|---|---:|---|
| `naive` | 27,244 | every usage sighting summed |
| `official` | 9,460 | the harness's own `tokenUsage` projection, gaps included |
| `seed_aware` | 5,008 | official, minus the inherited fork prefix |
| `corrected` | 11,668 | seed-aware, plus the attempt boundary, plus compaction |

`corrected` is the answer. The three terms that separate it from `official`
are reported separately, which is what D-5 asserts:

```
corrected − official  ==  compaction + superseded − inherited
   +2,208             ==     4,440   +   2,220   −   4,452
```

Bucket for bucket, with nothing left over. That identity needs no ground truth,
which is why it belongs in CI on day one rather than behind a corpus somebody
has to collect first.

## Reading a failure

```
  reported   uncachedInput=850  output=85  cacheRead=8,500  cacheWrite=25
  expected   uncachedInput=1,050  output=105  cacheRead=10,500  cacheWrite=13

  FAIL

  Your total is exactly the `official` fold.
  You are reporting exactly what the official tokenUsage projection reports,
  including its own gaps: compaction is uncounted, a superseded attempt is
  replaced rather than kept, and a forked child re-counts the prefix it
  inherited.
  Invariant: D-2, D-3, D-4
```

Reading the projection rather than folding the log yourself is the recommended
thing to do, and it is why this failure is the most likely one. The gaps are in
the projection, so every consumer doing the correct thing inherits them. They
are being fixed upstream in
[deepseek-ai/deepseek-harness#1886](https://github.com/deepseek-ai/deepseek-harness/discussions/1886);
until that lands, a consumer that wants the real number has to correct for
them.

When the total matches none of the four folds, the checker decomposes the
residual against the three gap terms instead, and says plainly when none of
them explains it. That last case is the interesting one, and it is worth
filing: the catalog takes counterexamples.

## Files

| | |
|---|---|
| `check.py` | the runner and the diagnosis |
| `reference.py` | the folds, ~200 lines, the only thing worth porting |
| `build_fixture.py` | regenerates `fixture/` and re-derives `expected.json` |
| `fixture/` | two committed session logs and their expected totals |
| `workflow.yml` | drop-in GitHub Action |

`reference.py` is deliberately readable rather than fast. If you would rather
implement the folds in your own language than shell out to Python, port that
file and keep the fixture.

## Scope

The folds are transcribed from `deepseek-ai/deepseek-harness` at `47f9438`
(v0.1.0-rc.6). The fixture is synthetic, so it says nothing about how often
these hazards occur in real logs; for that, see the measured figures and the
**Limits** section in `CONFORMANCE-DSH.md`. What it does say is that a fold
which mishandles any one of them will be caught, which is the part a CI job
needs.

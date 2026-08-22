# tokscale DSH compaction A/B — invariant D-3

Two binaries, one corpus, isolated `HOME` per leg. Answers one question: does
[tokscale#1162](https://github.com/junhoyeo/tokscale/pull/1162) move `input`,
`output` and `cacheRead` by exactly the `compaction/summary` events' own usage,
and nothing else?

Invariant: [D-3](../../CONFORMANCE-DSH.md) — count what compaction costs.
Reported at [tokscale#1152](https://github.com/junhoyeo/tokscale/issues/1152).

## Why two binaries rather than one binary and an argument

The figures in #1152 came from `../dsh-probe/tokscale_dsh_fold.py`, a
line-for-line transcription of `crates/tokscale-core/src/sessions/dsh.rs`,
because the DSH client had not shipped and there was no released binary to
run. This harness closes that loop: it builds the parser itself, before and
after the fix, and runs both over the same bytes.

## Legs

| script | what it does |
|---|---|
| `build_pair.sh` | builds `86126c2^` (pre) and `522027d` (PR head) from a clone at `/tmp/ts1162`, drops both binaries in `/tmp/ab1162` |
| `run_ab.sh` | runs each binary cold — fresh `HOME`, `DSH_HOME` pinned at a frozen corpus copy — into `pre.json` / `post.json` |
| `diff_legs.py` | prints every numeric leaf that moved between the two, and counts the ones that did not |
| `cache_leg.sh` | writes a v1 cache with the pre binary, then runs the post binary twice over that same `HOME` |
| `count_compaction.py` | reads the corpus directly and prints the delta the A/B is supposed to produce, computed without either binary |

`results/` holds the JSON from the run recorded in #1162.

## What the legs have to show

1. **Cold pre** reproduces the transcription. If the compiled parser and the
   Python fold disagree, the transcription was wrong and every number in #1152
   is suspect.
2. **Cold post − cold pre** equals `count_compaction.py`'s figure in every
   bucket. A delta that is merely *close* means something other than the
   compaction events moved.
3. **`messageCount` rises by exactly the number of usage-bearing
   `compaction/summary` events.** Note the contrast with the Claude-lane
   property in [#1011](https://github.com/junhoyeo/tokscale/issues/1011),
   where `messageCount` must *not* move: there a change meant a retained turn
   had been retired, here a change is the whole point. Same field, opposite
   expectation, because the two corrections do different things.
4. **Warm post lands on cold post.** The PR bumps the DSH parser version 1→2;
   if the bump does not reach an existing cache, an upgraded user keeps the
   undercount — which is exactly what happened on the Claude lane in v4.11.0.
5. **A third run is identical to the second.** A migration that keeps moving
   is not a migration.

## Running it

```bash
./build_pair.sh                 # ~20 min from cold; two LTO release builds
cp -R ~/.dsh/sessions /tmp/dsh-corpus-frozen/sessions
./run_ab.sh
python3 diff_legs.py
./cache_leg.sh
python3 count_compaction.py --root /tmp/dsh-corpus-frozen
```

Needs a real DSH corpus. Nothing is written back to it — every leg reads a
frozen copy and an isolated `HOME`, so your own tokscale cache and submissions
are untouched. For a check that needs no corpus at all, use
`../dsh-conformance/`, which ships its own fixture.

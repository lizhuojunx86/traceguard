# dsh-1886-crosscheck

Cross-checks the four independent implementations of
[deepseek-harness#1886](https://github.com/deepseek-ai/deepseek-harness/discussions/1886)
against each other and against upstream, on the compaction dimension (D-3).

Nothing here is a transcription. `apply()` and `view()` are pulled from each
tree's own `packages/llm/token-meter/src/usage-projection.ts` at a pinned SHA,
transpiled by esbuild, and otherwise untouched. `zod` is the real dependency at
the version the repo pins (`^4.4.3`); `surface-projection` is stubbed to throw,
so a run that completes is itself the proof that the `tokenUsage` fold never
reaches it.

```bash
./run_check.sh
```

Clones three repos at three SHAs, builds four folds, prints them against the
committed fixture in `../dsh-conformance/` and against two probes built here.
About a minute on a cold cache. Submits nothing, touches no real session data.

## Trees

| tree | SHA | scope |
|---|---|---|
| `deepseek-ai/deepseek-harness` | `b150a551b8` (`dsh-v0.1.1-rc.2`) | unpatched |
| `yha9806/…` `codex/fix-token-usage-retry-compaction` | `63688b0` | compaction + attempt boundary |
| `a137460387/…` `upstream-pr/token-meter-compaction-usage` | `64ee978` | compaction only |
| — | ablation of `63688b0` | compaction only, by removing the boundary branch |

The ablation is the load-bearing step. It strips the `error`/`aborted` finish
branch out of `63688b0` by verbatim string match — the script aborts if the
branch is not found byte-for-byte, so a refactor upstream fails the run rather
than silently comparing something else.

## What it establishes

`b150a551b8` lands on the fixture's `official` fold bucket for bucket
(850 / 85 / 8,500 / 25). That fold was hand-computed and reproduced by
`reference.py`; this is the first time it has been checked against the vendor's
compiled code rather than a reading of it.

`64ee978` minus upstream is 400 / 40 / 4,000 / 0, which is `gap_compaction`
exactly. The forked child in the fixture carries no `compaction/summary` and is
a strict no-op across the patch (450 / 45 / 4,500 / 13 either side).

The ablation of `63688b0` is identical to `64ee978` on every bucket. The two
patches agree exactly on compaction and differ by exactly the retry term.

`stateVersion` reads 2 on both.

## Probes

Two cases the fixture cannot exercise, built here as the smallest logs that
isolate them.

`probe/nousage` — a `compaction/summary` carrying no `usage`. Both patches must
be strict no-ops, equal to unpatched. This is the branch both authors could only
cover with unit tests, because every summary in every real corpus measured so
far reported usage.

`probe/midstep` — a `compaction/summary` landing between a usage chunk and its
own `assistant/message`, under the same `(turn, step)`. The message must still
*replace* the chunk: 500 / 50 / 5,000 / 5. A summary that consumed the `last`
marker would give 600 / 60 / 6,000 / 10. This is the strongest form of the
"a summary never claims a turn start" property, and the failure it rules out is
silent — it inflates rather than errors.

## Limits

The compaction dimension only. D-2 (fork seed) and D-4 (superseded attempt) are
untouched here and both stay open upstream; `../dsh-conformance/check.py` is
where those are asserted.

One synthetic fixture and two probes. No real corpus, so nothing here says
anything about how often the patched path runs, only about what it computes when
it does. `cacheWriteTokens` is exercised on the assistant path but is 0 on every
`compaction/summary` in the fixture, so a summary reporting cache writes remains
untested by construction as well as by corpus.

The API-drift finding is read from a diff, not from a build: `63688b0` targets
the pre-`b150a551b8` `ProjectionDefinition` shape (`schema` / `view` /
`stateVersion` at top level) and current master uses `stateSchema` plus
`wire: { viewSchema, view }` with a `declare module` augmentation. This harness
transpiles each file against its own tree's expectations and reads the view
through whichever shape is present, so it folds both. That it folds both is not
evidence that `63688b0` would typecheck against current master. It would not.

Invariants: [`CONFORMANCE-DSH.md`](../../CONFORMANCE-DSH.md).

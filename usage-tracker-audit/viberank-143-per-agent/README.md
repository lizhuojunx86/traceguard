# viberank #143 — a drift verdict applied to the tool it is evidence about

Invariant [I-10](../../CONFORMANCE.md): *a verdict must not outrun its
evidence.* viberank's corpus scan reads `~/.claude/projects`, so it is evidence
about Claude and nothing else. Until
[#143](https://github.com/sculptdotfun/viberank/pull/143) a mixed day was stored
as one lump per machine, and honouring a Claude deletion swapped that lump
whole.

Reported as [#125](https://github.com/sculptdotfun/viberank/issues/125), fixed
by nikshepsvn in `15da384` on 2026-08-22: contributions are keyed per
`(machine, agent)`, a split is kept only when it reconciles with the day it
divides, and a Claude verdict lowers the Claude slice alone.

This harness checks both halves against a real corpus, using viberank's own
code rather than a restatement of it.

```sh
./run_check.sh                    # generates the report itself
./run_check.sh path/to/cc.json    # or reuse one you already have
```

Clones viberank into `.work/`, checks out `15da384` and its parent, and runs
their `mergeMachineContribution` from both. Submits nothing anywhere.

## What it asserts

**1 · The split reconciles.** Every day's per-agent slices must add up to the
row they divide, in all five token fields and in cost. This is the gate that
stops a malformed or hostile split from inflating one agent while the headline
total stays believable.

**2 · A Claude verdict costs no other tool its high-water mark.** For every
mixed day, build the pair the drift path sees — `prior` as observed, `incoming`
with Claude pruned to 40% — merge with `acceptLower = true`, and read off how
much non-Claude money survived. The incoming report's *other* agents are scaled
separately, because that turns out to be the variable that decides whether the
bug fires at all.

## Measured

197 days, 45 of them mixed, 56 non-Claude agent-day slices. `viberank-cli@1.10.0`
from npm passes `--by-agent` (`package/cli.js:57`), so this is the artifact
users get.

Reconciliation: 197 of 197 days carried a split, 0 token mismatches, worst cost
residual `0.0000000000`. nikshepsvn measured 103 of 103 on his own report; this
is a second corpus.

Merge A/B, non-Claude cost surviving the verdict:

| incoming's non-Claude slice | pre (`7a6b8cf`) | post (`15da384`) |
|---|---|---|
| re-reported unchanged | $376.13 | $376.13 |
| 70% of observed | $263.29 | $376.13 |
| absent | $0.00 | $376.13 |

## What the first row means, and what I got wrong

I filed #125 as though a Claude verdict lowered Codex by itself. It does not.
When the other tool re-reports the same numbers, the whole-slice swap and the
per-agent merge agree, because the incoming record still carries that tool in
full.

The fix bites when the other tool's own slice also arrives lower — its history
rotated, its own cleanup ran, or the report was generated somewhere that no
longer has it. The verdict does not push the other tool down; it removes the
high-water mark that would have caught the other tool falling for its own
reasons. Same money at risk, different trigger, and the corrected version is
narrower and testable, which the original was not.

## What this does not cover

- **One tree.** 45 mixed days against 34.33% of board-wide cost sitting on
  mixed days ([#143](https://github.com/sculptdotfun/viberank/pull/143)). My
  sizing in #125 — 3.35% — was low by an order of magnitude for exactly this
  reason: a single tree cannot see that the heavy days are the multi-agent ones.
- **The scaling is constructed.** `CLAUDE_KEEP` and `nc_keep` model a pruned
  re-report; they are not two real submissions taken weeks apart.
- **Server-side only.** It exercises `mergeMachineContribution`, not the HTTP
  path, the stored `machine_contributions` rows, or the legacy slices that
  carry a union `agents` list with no way to split their amounts.
- **`DEFAULT_MACHINE_ID` is untouched.** No-id submissions replace the day
  whole regardless, which was true before #143 and after it
  ([#81](https://github.com/sculptdotfun/viberank/issues/81)).

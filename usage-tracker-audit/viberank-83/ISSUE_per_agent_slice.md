# Title

A drift verdict about Claude files still lowers the same day's Codex tokens

# Body

Follow-up to #121, split out rather than bolted on. #124 fixed the scope of the
*verdict*. What is left is the scope of the *record* it acts on.

## What is left

`acceptLower` now reaches only days the Claude corpus is evidence about
(`corpusCoversDay`, `src/lib/drift.ts`). But `mergeMachineContribution` keys
contributions by machine, and swaps the day's slice whole:

```ts
if (!acceptLower && prior && prior.totalCost > incoming.totalCost) { ... }
```

The whole-record swap is right, for the reason its own comment gives: a
per-field max would synthesise a slice nobody observed. The problem is that the
record and the verdict describe different populations. A mixed day (Claude plus
Codex) passes `corpusCoversDay` on its Claude half, and then the Codex half
rides down with it on evidence that was only ever about `~/.claude/projects`.

## Size, on my tree

181 days in my payload:

| | days |
|---|---|
| claude only | **0** |
| claude plus another tool | 47 |
| no claude at all | 134 |

On the 47 mixed days: `$8,065.95` total, of which `$270.61` (3.35%) is Codex and
OpenClaw money gated by a Claude file count.

Sizing it honestly, because I would rather you not act on this today:

- It is small next to what #124 fixed — 3.35% of 47 days, against 134 days that
  were wrong entirely.
- #124 narrowed the trigger as a side effect. Absence no longer classifies, so a
  month has to be positively observed smaller before any of this runs. My tree
  cannot produce that today.
- It only affects id'd machines. The `DEFAULT_MACHINE_ID` branch replaces the
  day whole regardless of `acceptLower`, so no-id submitters were never
  protected here in the first place (#81).

## The gate cannot fix it

Both settings are wrong the same way. Strict (`every()` rather than `some()`)
reaches zero days on my machine, so it switches the feature off rather than
narrowing it. Loose, which is what shipped and is correct, lets the non-Claude
portion ride. Neither can be right while the verdict and the record are scoped
to different things.

## Fix

Key contributions per `(machine, agent)` instead of per `(machine)`. The
high-water comparison then happens inside the population the corpus is evidence
about, and the swap stays whole within a slice, so nothing gets synthesised.

**Client.** `cli.js` runs `npx ccusage@latest daily --json` in two places.
`daily --by-agent --json` returns the split.

**Server.** `selectAuthoritativeRows` prefers the `agent: "all"` row when one is
present for a date, and `normalizeCcData` otherwise sums per date and unions
`agents`. So the per-agent shape already parses and is deliberately collapsed;
keeping it means preferring the split rows and carrying agent down into
`MachineContribution`. `agents` is already on that type — today it describes the
slice rather than keying it.

## The part I do not have an answer for

Migration. Stored `machine_contributions` are keyed by machine, and a legacy
slice carries a union `agents` list with no way to split its amounts
retroactively. The first split submission for such a day has to either drop the
stored high-water mark for it, or keep the legacy slice beside the new per-agent
ones and double-count. Comparing machine-wide totals once before adopting the
split avoids both, but it is a one-time rule and I would rather you picked it
than have me assume.

Happy to measure whatever helps. The per-agent day split above came off
`daily --by-agent --json` on the same machine that produced the numbers in #121.

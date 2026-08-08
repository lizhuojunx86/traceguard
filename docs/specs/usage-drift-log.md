# usage-drift-log — implementation record and revisions

**This is not the spec.** The spec is
[`usage-drift-log.md`](https://github.com/m1kapp/runmaxing/blob/main/docs/usage-drift-log.md)
by [@yoo-minho](https://github.com/yoo-minho), published under MIT after the
drift pattern reproduced at leaderboard scale in
[sculptdotfun/viberank#83](https://github.com/sculptdotfun/viberank/issues/83).
The six field names and their semantics are his and are used here unchanged.

This page records three things his document does not: which tools implement
the record, which revisions came out of the first downstream adoption, and
what is still open. It exists because the revisions were settled across four
GitHub threads and would otherwise only be findable by reading all of them.

Maintained by Li Zhuojun. Corrections welcome as issues on this repo; changes
to the spec itself belong on runmaxing.

Last updated 2026-08-08. The spec repo was `m1kapp/claude-rank` until it was
renamed to `m1kapp/runmaxing`; older links redirect, but cite the new name.

---

## The problem the record addresses

Claude Code rewrites its own transcripts. Resume and compact edit session
files in place, so a month-to-date total recomputed from those files can go
down — which for a cumulative figure should be impossible. Measured from the
file side: one transcript lost 5 assistant messages between two scans of a
34-day corpus. Measured from the leaderboard side: a July total of $17,117
came back as $15,226 sixteen hours later, with no deletions on the user's
side and the file count higher, not lower.

A number that moves is not the failure. A number that moves silently is. The
record makes it visible by freezing what was observed and writing it once.

## The record

```json
{
  "at": "2026-07-31T02:07:03+09:00",
  "month": "2026-07",
  "cost_usd": 19105.20,
  "messages": 9538,
  "corpus": { "files": 1055, "bytes": 1796481718 }
}
```

One line appended per run, never rewritten. Against the most recent prior
record for the same month, warn when `prior.cost_usd > current.cost_usd * 1.02`,
then read `corpus` to classify:

- files decreased → data was removed
- files same or higher, totals down → the files were rewritten in place

Field semantics, including why `at` keeps its local offset and why `messages`
excludes `isSidechain`, are in the spec. Read that first.

## Implementations

| # | implementation | layer | shape |
|---|---|---|---|
| 1 | clauderank submitter (@yoo-minho) — `plugins/claude-run/skills/usage-report/build.py` | local log | full six fields |
| 2 | `traceguard.routing_audit` — [`usage_report.py`](../../packages/traceguard/src/traceguard/routing_audit/usage_report.py) | local log | full six fields |
| 3 | viberank server + CLI ≥ 1.4.0 (run 1.4.1) | wire | `corpus` only, scoped per month |

Two of these are local logs and one is a wire format, which is the distinction
that took a thread to establish. A local log re-reads every field later, so it
carries all six. A server keeps its own history and computes `cost_usd` and
`messages` from the payload in the same request, so a client copy of those
would be a second source of truth with no right answer when the two disagree.
The wire subset is not a competing spec; it is the same contract minus what
the receiver can derive.

Extra keys under `drift` are ignored rather than rejected, so a submitter
emitting the full record needs no change to talk to viberank.

## Revisions

Each of these came from a measurement that contradicted a design, including
two of mine. They are listed with the number that forced them, because
without it they read as preference.

### R1 — the wire form is scoped per month

**Adopted** ([viberank#112](https://github.com/sculptdotfun/viberank/issues/112) →
[#121](https://github.com/sculptdotfun/viberank/pull/121), merged).

A single unscoped `{files, bytes}` pair describes the whole corpus at one
instant, and one corpus disagrees with itself across months. Against my
append-only log, frozen totals versus a fresh recompute on the same day:

| period | frozen | recomputed | shortfall |
|---|---|---|---|
| 2026-06 | 23,266 msgs | 1,985 | 91.5% |
| 2026-07 | 28,921 msgs | 25,969 | 10.2% |
| 2026-08 (in progress) | 4,017 msgs | 4,635 | −15.4% |

Three months, three readings, one pair of counters. At least one gets
classified wrong.

Worse, a global counter recovers. I create a median of 23 new transcript files
on an active day, and everything June still has on disk is 91 files. Delete
June and the global count is back above its old value within two to four days
of ordinary work — at which point the discriminator reads "files same or
higher, totals down" and holds the high-water mark for a month the user
deliberately cleared. That is the exact failure the field exists to prevent,
and it deepens with heavier usage.

Wire form:

```json
{ "drift": { "corpus": { "2026-07": { "files": 985, "bytes": 1796481718 } } } }
```

### R2 — month is the finest well-defined scope

Long sessions span days, so per-day counters would not describe a corpus. A
file is attributable to a period only if all its records fall inside it:

| scope | files spanning >1 period | records in those files | double counting |
|---|---|---|---|
| month | 5 of 1,323 (0.4%) | 6.2% | +0.4% |
| day | 51 of 1,323 (3.9%) | 36.2% | +8.2% |

Double counting a boundary-spanning file is harmless as long as the rule is
stable, because the comparison is always against the same client's earlier
count and never across clients. That is what makes 0.4% acceptable rather than
merely small, and 8.2% not.

Classify at month granularity, then apply the verdict to the drifted days
inside the month. Residual, accepted knowingly: a month where the user deleted
some days while the runtime rewrote others gets one verdict. It errs toward
honouring the user's own lower number.

Measurement:
[`corpus_scope.py`](../../usage-tracker-audit/viberank-83/corpus_scope.py).

The viberank threads cite it at `blob/826d95f/...`, a commit that lived only on
a side branch until merge `ee776b1` brought it onto `main`. SHA-pinned links
survive a branch deletion only while the commit stays reachable from some ref,
so the merge is what makes those citations permanent rather than merely
long-lived.

### R3 — absence is not deletion

**Adopted** ([viberank#124](https://github.com/sculptdotfun/viberank/pull/124), merged).

The shipped 1.4.0 read a month present in the prior record and missing from
the incoming one as "emptied entirely". That holds only if
`~/.claude/projects` is an archive. It is a rolling window.

My tree spans 2026-06-04 to 2026-08-05 and emits three months. The submission
built from the same machine carries 179 days across twelve months, back to
2025-09. Nine of those twelve have totals on the leaderboard and zero files on
disk. Every one of them would have been read as cleared, permanently, since
the upsert only writes months present in the incoming record.

Production had the same symptom from a different cause. Across the five users
who had submitted since 1.4.0, 19 months were exposed; every absent month
nikshepsvn broke down by tool turned out to be a month where the user ran
Codex or Gemini and never opened Claude Code. `~/.claude/projects` was empty
because nothing had ever been in it.

Three causes — cleared, pruned by a retention setting, never used — and
nothing in the payload separates them. The rule was removed. A month absent
from the corpus now keeps its total.

### R4 — the corpus and the totals may describe different populations

**Open** ([viberank#125](https://github.com/sculptdotfun/viberank/issues/125)).

`ccusage daily --json` has been the all-agent report since v20. A corpus scan
of `~/.claude/projects` is Claude Code and nothing else. On my machine, by
agent:

| agent | cost | share | days |
|---|---|---|---|
| claude | $7,765.73 | 89.50% | 45 |
| codex | $425.43 | 4.90% | 73 |
| openclaw | $421.40 | 4.86% | 121 |
| gemini | $51.39 | 0.59% | 12 |
| hermes | $13.19 | 0.15% | 6 |

134 of 179 days carry no Claude Code data at all. The honest figure for the
source gap is the three months where the tree is intact — 10.42%, 1.90%,
8.93% non-Claude — since older months read as 100% non-Claude partly because
their Claude transcripts are already gone.

#124 fixed the month-level version of this. What remains is at day level: a
mixed day passes the coverage gate on its Claude half, and its Codex half
rides down with it. On my 47 mixed days that is $270.61 of $8,065.95, 3.35%.
Small, and not fixable by tightening the gate — the strict version reaches
zero days on a multi-tool machine. The fix is to slice contributions per
`(machine, agent)` so the comparison happens inside the population the corpus
is evidence about.

If a future revision keys `corpus` by source, the shape that follows from the
above is:

```json
{ "drift": { "corpus": { "claude": { "2026-07": { "files": 1197, "bytes": 390605627 } } } } }
```

Not proposed as settled. Recorded so the next implementer sees the axis.

### R5 — the recursive count is load-bearing

Subagent transcripts live under `<session>/subagents/`, so a flat glob
undercounts, and it undercounts in the direction that makes the discriminator
confidently wrong rather than noisy:

| corpus | flat glob | recursive |
|---|---|---|
| mine | 75 | 1,398 |
| @yoo-minho's | 153 | 985 |

This needs a regression test with a nested fixture, not a comment. viberank's
asserts that the recursive walk finds strictly more than a flat read of the
same tree.

### R6 — reading only the ends of a file needs a fallback

Not a spec change; an implementation hazard worth recording once, since every
implementation will meet it.

Scanning whole transcripts is expensive — viberank measured 12.6 s and 541 MB
of RSS over 922 files, on every submission, for a figure the user never sees.
Reading 64 KB from each end brings that to 698 ms and 94 MB, because records
are chronological. But a first attempt that took only the head silently
dropped 17 of 922 files: those whose first timestamp sits past the head
window returned null and were skipped. An undercount, in exactly the direction
the feature exists to detect, and it passed every unit test because the
fixtures were a few hundred bytes each.

On my tree the head fallback fires 119 times in 1,509 files. 109 have no
timestamp anywhere and are correctly dropped; 10 are genuine late-first-stamp
files, 0.66% — the same order as the 17 of 922.

The tail is asymmetric on purpose. A missing first timestamp drops the file
from the corpus; a missing last timestamp can only collapse a file's span to
the month the head already assigned it. Narrowing, not disappearance. Zero
instances across two trees. If one turns up, the fix is the head's fallback.

### Not a revision: the `isSidechain` ratio is workload-dependent

Already in the spec at `c8c7ae9`, noted here because it is the field most
likely to be miscalibrated. Excluding `isSidechain` moved @yoo-minho's count
by 14.1% (11,872 of 84,314 user records across 1,120 files) and moves mine by
over half, because subagent transcripts carry most of my message volume.
Don't calibrate a threshold against someone else's ratio. The drop semantics
are unaffected either way — they compare a metric against its own earlier
value, never across implementations.

## Open

- **R4**, at viberank#125. The part neither of us has an answer for is
  migration: stored contributions are keyed by machine, and a legacy slice
  carries a union agent list with no way to split its amounts retroactively.
- Whether any third-party tool other than the three above emits the record.
  If you ship one, open an issue here and it goes in the table.
- Nothing outstanding on link durability. Both scripts these threads cite are
  SHA-pinned and both SHAs are now reachable from `main`.

## Attribution

Spec: @yoo-minho, MIT, `m1kapp/runmaxing`. Field names and semantics are his
and were deliberately not renamed — convergence that matters is the drop
condition and the corpus discriminator, and renaming a published document
costs it stability for no semantic gain.

Server-side design and both merged viberank implementations: @nikshepsvn.

Measurements in R1–R6 and the per-month scoping argument: this repo. The
`traceguard` implementation is
[`routing_audit/usage_report.py`](../../packages/traceguard/src/traceguard/routing_audit/usage_report.py),
Apache-2.0, emitted after every scheduled ingest via `--usage-report-history`.

Threads, in order:
[viberank#83](https://github.com/sculptdotfun/viberank/issues/83) ·
[#111](https://github.com/sculptdotfun/viberank/pull/111) ·
[#112](https://github.com/sculptdotfun/viberank/issues/112) ·
[#121](https://github.com/sculptdotfun/viberank/pull/121) ·
[#124](https://github.com/sculptdotfun/viberank/pull/124) ·
[#125](https://github.com/sculptdotfun/viberank/issues/125)

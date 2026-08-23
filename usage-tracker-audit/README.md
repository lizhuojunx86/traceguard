# Token accounting conformance

A conformance suite for tools that count LLM tokens and cost from agent
transcripts. Two invariant catalogs, a set of runnable harnesses that check them
against a tracker's own code, and a record of which upstream projects now hold
which invariant.

[![catalog: I-1..I-11](https://img.shields.io/badge/catalog-I--1..I--11-blue)](../CONFORMANCE.md)
[![catalog: D-1..D-5](https://img.shields.io/badge/catalog-D--1..D--5-blue)](../CONFORMANCE-DSH.md)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](../LICENSE)

## Why this exists

Token trackers disagree with each other, and not on edge cases. I wrote up a
comparison of six of them
([EN](https://dev.to/lizhuojunx86/i-audited-six-token-usage-trackers-they-disagree-with-each-other-by-2x-to-8x-2b1h),
[中文](https://zhuanlan.zhihu.com/p/2073158752958791824)); the individual defects
behind that are what this directory measures, one at a time, against the code
that shipped them:

- one assistant message counted once per content block — **2.36×** in one
  tracker, **2.34–2.37×** at three call sites in another
- a `ceil(chars/4)` estimate added on top of an API-reported input count —
  **6.26×–8.09×** inflation of the field
- a flat glob where the transcripts nest — **54%** of messages never seen
- streaming partial snapshots summed instead of collapsed — input **1.63×**,
  cache-read **1.61×** on subagent transcripts
- a cumulative month-to-date total that fell **11%** between two submissions
  sixteen hours apart, because the source rewrote itself

None of those are hard problems. They persist because the category has no
third-party baseline. A tracker's number is checked against nothing, a
leaderboard's number is checked against nothing, and a discrepancy between two
tools reads as a difference of opinion rather than as one of them being wrong.

This is an attempt at the missing baseline. Most entries exist because a shipped
tool violated them and the violation was measured; the rest are marked for what
they are — `CONFORMANCE-DSH.md` carries four entries recorded as *structurally
satisfied* (no violation, written down so the catalog can be trusted), one that
is a predicate rather than a measurement, and one whose mechanism is read from
source with no magnitude claimed. Each entry says which it is.

## The two catalogs

| | scope | entries |
|---|---|---|
| [`CONFORMANCE.md`](../CONFORMANCE.md) | tools reading `~/.claude/projects` (Claude Code transcripts) | I-1 .. I-11 |
| [`CONFORMANCE-DSH.md`](../CONFORMANCE-DSH.md) | tools reading DeepSeek Harness session logs | D-1 .. D-5, plus S-1 .. S-4 recorded as structurally satisfied |

They are separate because the failure modes are. Claude Code writes one message
as several records and rewrites transcripts in place; DSH is append-only with a
dense `seq` and writes every usage sample exactly twice. The DSH catalog states
which Claude Code invariants carry over, which invert, and which cannot occur —
a catalog that only ever adds rules is a list of fears, not a standard.

## Start here

**You maintain a tracker.** Read the catalog for your source format. If your
project is one of the three with a drop-in workflow — Clawdmeter,
claude-code-templates, tokscale — take it from [`ci/`](ci/) and put it in
`.github/workflows/conformance.yml`. Checkout plus one script; the harness calls
the same entry points a user's data reaches and pins nothing about your
internals. If you read DSH session logs, `dsh-conformance/workflow.yml` needs no
per-project version at all. Otherwise see *Getting a harness* below.

**You read DSH session logs and want a verdict now.**
[`dsh-conformance/`](dsh-conformance/) is vendor-neutral: a committed synthetic
fixture and a checker you point at any fold with one flag.

```bash
python3 dsh-conformance/check.py --self-test          # 0.04s, asserts the fixture
python3 dsh-conformance/check.py --cmd "<your fold>"  # names the invariant behind a mismatch
```

Stdlib only, no corpus, no vendor account, no build unless yours needs one. It
does not just say you are wrong; it decomposes the residual against the known
gap terms and names the invariant you missed.

**You want to know what your own corpus looks like.** Several invariants are
about your data rather than someone's code, and ship as read-only measurement
scripts: [`clawdmeter-dedup/duplicate_usage_shape.py`](clawdmeter-dedup/duplicate_usage_shape.py)
(I-2, I-3), [`viberank-83/corpus_scope.py`](viberank-83/corpus_scope.py) (I-5,
I-8) and [`corpus_absence_scope.py`](viberank-83/corpus_absence_scope.py) (I-9),
[`dsh-probe/`](dsh-probe/) (D-1 .. D-3). None of them submit anything anywhere.

**You want to check somebody's published number.** Start with the catalog entry
closest to the claim, then the harness in the table below. Nothing here needs
the target's cooperation.

## The harnesses

Red/green checks import the vendor's own code and exit 0/1. Measurement scripts
read a corpus and print; there is nothing to pass or fail.

| entry | target | invariants | kind |
|---|---|---|---|
| [`cct-dedup-check/`](cct-dedup-check/) | claude-code-templates | I-1 | red on upstream main |
| [`clawdmeter-dedup/`](clawdmeter-dedup/) | Clawdmeter | I-1 | red on v3.0.0, green on v3.0.1 |
| [`tokscale-input-estimate-check/`](tokscale-input-estimate-check/) | tokscale | I-6 | A/B, needs real transcripts |
| [`tokscale-drift-check/`](tokscale-drift-check/) | tokscale | I-7 | red/green, `--bin` |
| [`viberank-143-per-agent/`](viberank-143-per-agent/) | viberank | I-10 | runs the maintainer's own merge fn at two commits |
| [`tokscale-dsh-compaction-check/`](tokscale-dsh-compaction-check/) | tokscale DSH parser | D-3 | A/B across the fix, cold and warm cache |
| [`dsh-conformance/`](dsh-conformance/) | any DSH fold | D-1 .. D-5 | vendor-neutral, fixture-backed |
| [`dsh-1886-crosscheck/`](dsh-1886-crosscheck/) | DSH upstream, two fork implementations, one ablation | D-3 | cross-checks independent implementations |
| [`dsh-probe/`](dsh-probe/) | your DSH corpus | D-1 .. D-3 | measurement |
| [`viberank-83/`](viberank-83/) | your Claude corpus | I-5, I-8, I-9 | measurement |
| [`clawdmeter-dedup/duplicate_usage_shape.py`](clawdmeter-dedup/duplicate_usage_shape.py) | your Claude corpus | I-2, I-3 | measurement |

I-4 has no harness here; the self-contained repro shipped upstream in
[splitrail #220](https://github.com/Piebald-AI/splitrail/issues/220). I-11 is
asserted as a property of a tool's machine-readable output rather than by a
script. D-4's magnitude is fixture-only — `dsh-probe` does not measure it,
because no corpus here contains a failed attempt that reported real tokens.

## The badge

Copy the workflow, then put its badge in your README:

```markdown
[![token accounting conformance](https://github.com/OWNER/REPO/actions/workflows/conformance.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/conformance.yml)
```

The badge is GitHub's, rendered from your own Actions run against your own
commits, so it cannot be granted or revoked by me. A conformance mark issued and
withdrawn by a third party is worth exactly as much as that party's ongoing
attention, and this one does not work that way.

It is not fully independent of this repository either, and it should be said:
the workflows fetch the harness from here, so a green run needs this repo
reachable. If that matters to you, vendor the harness directory into your own
tree and point the workflow at your copy. It is a few files and no network.

**What a green run asserts:** the invariants that harness exercises, named in
the table above, against the commit that triggered it. Not all eleven, and not
that your numbers are right in general. It is a regression gate for the defect
class that already shipped once in your category, which is the class most likely
to ship again.

The static catalog badges at the top of this file are the other half: they say
which catalog a project targets, and assert nothing about whether it passes.

## Getting a harness

If you maintain a tracker that is not in the table, the two reusable parts are
the corpus generator (`gen_corpus_streaming.py`,
`cct-dedup-check/gen_corpus.py`) and the vendor-import pattern.
`clawdmeter-dedup/check_clawdmeter_v301.py` stubs PySide in one function rather
than reimplementing the parser; `dsh-1886-crosscheck/` transpiles the vendor's
own projection and stubs one import to throw, so a completed run is itself the
proof that the code path never reached it. The numbers come out of the target's
code, not a model of it. That is the only rule.

Open an issue with a pointer to your token path.

## Track record

[`ADOPTERS.md`](ADOPTERS.md) — where an invariant is now enforced by someone
else's code, the regression tests other people wrote, and who reproduced a
measurement independently. It opens by saying that nobody cites either catalog
by name, which is true and is the honest place to start.

As of 2026-08-23 it lists eleven upstream fixes across four projects
(Clawdmeter, splitrail, tokscale, viberank), three regression tests written by
other people, and seven independent reproductions. The count of fixes depends on
how you split one Clawdmeter change that closed two invariants at once —
`CONFORMANCE.md` counts it as one and says ten. Read the table rather than
either total.

Separately, one upstream thread now carries four independent implementations of
the same fix, none of which can land, because the project does not accept
external pull requests.

## Limits

Everything here is measured on a small number of corpora, most of them mine.
Each catalog entry states what its own measurement does not cover, and the
percentages are corpus-specific in a way the absolute counts are not. Where a
figure is read from source rather than folded from data, the entry says so.
Several DSH measurements (D-1, D-2, D-5) live only in this repository and have
no upstream issue carrying the table.

No third party has yet pointed this suite at their own tracker. Every harness
here was written after I had already measured the defect and reported it, so how
often the suite catches something nobody was looking for is unknown. None of the
`ci/` workflows is wired into a target repository today.

## Contributing a counterexample

An invariant is a claim, and claims are for breaking. If your corpus contradicts
one, file it with the numbers and the grouping you used — I-3 first, because the
sort can manufacture findings. Both catalogs have changed when a measurement
said they should, including one entry that was published in a narrower form than
was true and is now recorded with the correction rather than quietly edited.

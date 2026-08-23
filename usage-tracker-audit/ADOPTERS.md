# Who runs these invariants

2026-08-23 · companion to [`CONFORMANCE.md`](../CONFORMANCE.md) (Claude Code, I-1..I-11)
and [`CONFORMANCE-DSH.md`](../CONFORMANCE-DSH.md) (DeepSeek Harness, D-1..D-5)

No one has said "I adopted your catalog." As far as I can tell nobody cites
either file by name, and I'd rather open with that than let a page titled
*adopters* imply otherwise.

What does exist is narrower and more useful: places where one of these
invariants is now enforced by somebody else's code, and people who produced
the same number without sharing any code with me. Those are two different
claims and they're kept in two different tables below. Every row carries a
link, so each one can be checked rather than taken from me.

---

## 1 · Enforced in someone else's code

An invariant is in this table when the upstream project ships a change that
holds it. The fix is theirs; the measurement was mine.

| invariant | project | what landed | when |
|---|---|---|---|
| I-1 count each message once | Clawdmeter | three call sites collapsed by `message.id`, v3.0.1 — [#21](https://github.com/weltern/Clawdmeter/issues/21) | 2026-08-08 |
| I-2 collapse under per-bucket max | Clawdmeter | same exchange: weltern passed back the running-total shape on sidechain records, which is where I-2 and I-3 come from | 2026-08-08 |
| I-4 never sum streaming snapshots | splitrail | [#222](https://github.com/Piebald-AI/splitrail/pull/222), written by NickAme03 against the ratios I'd predicted | 2026-07-31 |
| I-5 walk the tree recursively | splitrail | [#209](https://github.com/Piebald-AI/splitrail/pull/209), "Include Claude Code subagent transcripts", written by mike1858 after the report | 2026-07 |
| I-6 no estimate on an API number | tokscale | [#1037](https://github.com/junhoyeo/tokscale/pull/1037) by Yuxin-Qiao, shipped v4.11.0 | 2026-08-06 |
| I-7 a re-read must not lower history | tokscale | v4.9.0, from [#994](https://github.com/junhoyeo/tokscale/issues/994) | 2026-08 |
| I-7 | viberank | [#111](https://github.com/sculptdotfun/viberank/pull/111) | 2026-08 |
| I-8 scope counters to their period | viberank | [#121](https://github.com/sculptdotfun/viberank/pull/121), per-month `{files, bytes}` | 2026-08 |
| I-9 absence is not an observation | viberank | [#124](https://github.com/sculptdotfun/viberank/pull/124), server-side | 2026-08 |
| I-10 a verdict must not outrun its evidence | viberank | [#143](https://github.com/sculptdotfun/viberank/pull/143) (`15da384`), contributions keyed per `(machine, agent)` via `ccusage --by-agent`; a split is kept only when it reconciles with the day it divides, and a Claude verdict now lowers Claude alone | 2026-08-22 |
| D-3 count the compaction call | tokscale (DSH parser) | [#1162](https://github.com/junhoyeo/tokscale/pull/1162) (`d97a829`), `"assistant/message" \| "compaction/summary"` arm at `sessions/dsh.rs:153`, parser version 1→2. Fixed before the DSH client ever shipped | 2026-08-22 |

## 2 · Regression tests other people wrote

This is the form that survives me. A measurement I post decays; a test in
their tree fails on its own.

- **tokscale [#1139](https://github.com/junhoyeo/tokscale/pull/1139)** —
  junhoyeo seeds a cache entry the current parser cannot produce, asserts the
  seeded cache really is inflated, then scans. Disabling the rebuild fails it
  at `left: 110, right: 100`. It pins the cache half of I-6, which is the half
  that was still open. Merged 2026-08-17.
- **tokscale [#1162](https://github.com/junhoyeo/tokscale/pull/1162)** — four
  DSH tests for D-3, plus a non-vacuity check: with the `compaction/summary`
  arm reverted, three of the four fail. Merged 2026-08-22 (`d97a829`). Two of
  the four cover cases my corpus cannot reach — a summary carrying no usage,
  and a summary inside a forked seed prefix.
- **viberank [#121](https://github.com/sculptdotfun/viberank/pull/121)** — the
  per-month scope caught its own first implementation dropping 17 of 922
  files. The structure made a 2% undercount legible; that is the argument for
  the structure.

## 3 · Independent reproductions

Nobody here works from my code. Where a number matches, two implementations
sharing nothing landed on it — which is the only reason any of this is
checkable.

| who | what | where |
|---|---|---|
| yha9806 | implemented D-3 + D-4 on a fork (`63688b0`): folds `compaction/summary.usage`, treats error/aborted finish chunks as attempt boundaries, bumps `stateVersion` 1→2. I replayed it against my corpus: 308,234 whole-log, 55,886 child-own, bucket for bucket | [deepseek-harness#1886](https://github.com/deepseek-ai/deepseek-harness/discussions/1886) |
| hydraxman | produced the same patch independently, ran the official suite (23 tests, Node 22.19.0), and added the version-1 checkpoint argument I had not stated | same thread |
| le-soleil-se-couche | a fully synthetic fixture covering D-1..D-4, fork seed and cache-write traffic — no private logs needed. Also produced the counterexample that **corrected me**: generalising the attempt boundary to any `failure` field would double-count, because AgentLoop still allowlists `error \| aborted` | same thread |
| aron-intframe | reported a viberank double-count in the same shape, self-measured: a public $150.5K / 108.9B against a real $91.3K / 70.8B. Maintainer verified and repaired it | [viberank#127](https://github.com/sculptdotfun/viberank/issues/127) |
| NickAme03 | filed the splitrail I-4 case ([#220](https://github.com/Piebald-AI/splitrail/issues/220)) whose ratios I'd predicted and then verified, wrote the fix himself, and has since carried the same class elsewhere — "Streaming rows are not duplicates: keeping the first one undercounts" | [ccseva#38](https://github.com/Iamshankhadeep/ccseva/issues/38) |
| a137460387 | a fourth implementation of D-3, as one commit on top of master, with unit coverage and a matching Web fixture. Confirms the gap is still live on `b150a551b8` (`dsh-v0.1.1-rc.2`) and deliberately leaves D-4 alone, for the same reason yha9806 did: the attempt-boundary rule is the structural decision still open | [deepseek-harness#1886](https://github.com/deepseek-ai/deepseek-harness/discussions/1886) |
| pinion05 | measured DSH model attribution on a live `~/.dsh` tree — 1,231 of 1,762 rows served by a model other than the configured one. Self-closed, but it is the second person reading `sessions/dsh.rs` for accounting | [tokscale#1163](https://github.com/junhoyeo/tokscale/pull/1163) |

## 4 · Offered and not taken up

Kept here so the page isn't only wins.

- **`ci/tokscale.yml`** — the drift harness as a drop-in workflow, offered at
  [tokscale#1011](https://github.com/junhoyeo/tokscale/issues/1011) and
  [Clawdmeter#21](https://github.com/weltern/Clawdmeter/issues/21). Not wired
  into any repo yet.
- **I-1 in claude-code-templates** —
  [#754](https://github.com/davila7/claude-code-templates/pull/754), open,
  checks green, no maintainer response. The last unshipped fix.
- **D-1..D-5 upstream** — deepseek-harness master still has no
  `compaction/summary` branch, does not read `llm/retry`, and reports
  `stateVersion: 1`. Checked 2026-08-23 against `b150a551b8`, tagged
  `dsh-v0.1.1-rc.2`. External PRs are closed by CONTRIBUTING, so none of the
  four independent implementations below can move on its own.

---

## Getting a row here

Two ways, and neither involves agreeing with me.

Hold one of the invariants in code — a test, a check, a workflow — and link
it. Or produce a number that contradicts one, in which case the catalog entry
is wrong and I'd rather find out from you than not. The harnesses under
[`usage-tracker-audit/`](.) are stdlib-only and run in about a minute; the DSH
one ships its own fixture, so it needs no corpus of yours.

If a row above is wrong about your project, open an issue or a PR against
this file.

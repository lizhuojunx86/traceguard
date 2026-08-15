# DeepSeek Harness got append-only right. Its token projection still misses what compaction costs.

Four numbers from a nine-day-old codebase, measured this week across two providers:

- Summing every usage record in a DeepSeek Harness session log gives **2.000000×** the correct total. Not roughly two. Six digits, no remainder, reproduced independently on both routes.
- One forked session reported **263,790 tokens for 11,442 tokens of its own work**, a 23.05× overstatement, because its log physically contains a copy of its parent's history. A second fork did the same thing at 5.18×. Nothing bounds the ratio.
- **48,895 tokens across three compaction events were counted by nothing**, including the official projection that most plugins read from.
- A stream that died and retried left **three usage samples under one step**, the first of them all zeros. Keep-first reports that step as free.

The first and last are traps for people writing plugins. The middle two are gaps in DSH's own code, and the compaction one is what I would fix first.

## Why I went looking

I spend a lot of time reading agent transcripts and adding up tokens. Over the past few months that turned into ten upstream fixes across four usage trackers: splitrail (216 stars, three issues), tokscale (4.6k), Clawdmeter, viberank, plus an open PR against claude-code-templates (30k). The pattern was always the same. Claude Code rewrites session files in place on resume and compact, so anything recomputing totals from live files inherits the drift. Streaming leaves partial snapshots that get summed as if they were separate calls. Subagent transcripts sit one directory deeper than a flat glob reaches, and on one corpus 54% of messages never entered any total.

Eleven of those lessons are written up as invariants in a catalog. DeepSeek released Harness on August 13, and the ecosystem produced roughly 2,500 plugin repositories in two days. A curated registry snapshot covering 457 of them listed 27 that count tokens. I wanted to know whether the same class of bug had been reproduced at scale.

It hadn't. That surprised me, and it is worth saying before the criticism.

## What they designed out

DSH's session log is append-only, and that is a written contract rather than an observation. The JSONL backend's README says "Flushed events are never rewritten." Every event carries a dense contiguous sequence number, checked on append. Compaction shadows old events in the surface projection and leaves the bytes alone: "The shadowed events remain in the raw log, so replay is deterministic."

That one design decision removes the failure that cost viberank 11% of a month-to-date total between two submissions sixteen hours apart.

Two more. Adapters emit one terminal usage value per step, never a growing snapshot, so there is nothing to mis-sum. Child sessions are siblings in the same directory rather than nested underneath, so a depth-limited walk cannot lose a third of the spend the way it did in splitrail.

Four of my eleven invariants are structurally satisfied here. I put them in the DSH catalog anyway, marked as satisfied, because a catalog that only adds rules is not a catalog. It is a list of fears.

Then I measured, and found four new ones.

## Every usage sample is written twice

One model call reports its usage on two different events. Once as a stream chunk with `chunk.type === 'usage'`, once again on the assembled `assistant/message`. Same numbers both times.

Fold naively and you get exactly double. The ratio printed as 2.000000 on the full corpus, on the local qwen route alone, and on the MiniMax route alone, each computed independently.

An approximate factor is arguable. An exact one is not, and that is the whole reason to report it this way. It also tells you the mechanism is universal rather than occasional, which a ratio of 1.9 would not.

The cache buckets do it too. 249,728 cache-read tokens on the MiniMax route, 81% of that route's corrected total, doubling exactly like input and output. That is the number I most wanted, because cache is usually the biggest bucket and the cheapest per token, so a doubling there moves a cost report further than a doubling of output does. Cache *writes* are still untested: neither provider populated the field.

DSH's own `token-meter` handles this correctly. It keeps one slot for the last `(turn, step)` and subtracts the previous buckets before adding the new ones. Its README spells it out. The official implementation going to that trouble is the evidence that the hazard is real, not theoretical.

One caveat that keeps the number honest, and it turned into the fourth finding. A step is not always one request, and when it isn't, the two-samples-per-step assumption breaks. More on that below.

## The fork trap, and the field you must not filter on

A forked child's log contains a copy of the parent's completed prefix. The header carries `seedLength`, and every event below it belongs to the parent. Add sessions together without checking and you count that prefix twice.

I expected this to be a subagent problem. I was wrong, and being wrong is the interesting part.

A subagent child is stamped `origin: 'subagent'` and its delegation depth increments. But `ctx.sessions.fork()` is an ordinary user-facing action available on any session, and the child it produces has `parentSession` and `seedLength` set, `delegationDepth: 0`, and no `origin` key at all. Here is the header I actually got, from an ordinary fork that showed up in the course of using the web UI. I wasn't trying to make one:

```json
{"type":"session","version":0,"id":"session-e61d64ec-…",
 "parentSession":"session-001e8887-…","seedLength":1008,
 "delegationDepth":0,"agentPreset":"standard"}
```

That one inherited 1,008 of its parent's 1,012 events and reports 11,418 tokens against 2,204 of its own work. The second fork I caught inherited all 3,171 events of a longer parent, asked one question, and reported **263,790 tokens for 11,442 tokens of work**.

That is the shape of it. The error is not a factor, it is the ratio of inherited work to own work, and nothing bounds it. Fork a long session, ask one question, and the child reports the entire parent as its own.

So the obvious defence is the wrong one. DSH's own lineage index opens with "Ordinary forks terminate propagation" and starts with `if (descendant.origin !== 'subagent') continue`. That filter is correct for counting subagent descendants. Reused for token accounting it admits every ordinary fork silently. The only sound discriminator is `seedLength`.

Official telemetry gets this right by the right key, emitting `session.parent_id` and `session.seed_length` and expecting receivers to stitch on the pair. Nothing does that for you if you read files.

## The one that is DSH's own gap

Compaction summarizes older history by making a model call, and that call costs real tokens. They land on `compaction/summary.usage`.

The official `tokenUsage` projection cannot see them. Its `usageOf()` matches `assistant/chunk` and `assistant/message` and nothing else, and the summarize call is not a loop step, so it produces neither.

This is worse than a plugin bug, because the plugins doing the *right* thing inherit it. Reading `sessionProjections.tokenUsage` instead of folding the log yourself is the correct, recommended approach. It is also how you miss this.

On my corpus that was 48,895 tokens across three compaction events. The largest single one is worth quoting in full: a MiniMax-M3 summarize call reporting 44,444 tokens (536 input, 2,436 output, 41,472 cache reads) to remove a range whose own `shadowedTokenCount` was 19,962.

Whether spending 44,444 tokens to shed 19,962 is a good trade depends entirely on what you pay for cache reads. I'm not going to tell you it's bad. I am going to point out that nothing in the official projection tells you it happened.

The `usage?` field is optional, so a provider reporting nothing on the summarize call produces no gap. That is a condition on the finding, not an escape from it. Both of my providers populated it.

## A retried step is not one request

The last one I found by accident, chasing why one step had three usage samples instead of two.

```
seq 3203  chunk/usage    {"inputTokens":0,"outputTokens":0}
seq 3204  chunk/finish   {kind:'error', failure:{code:'TRANSPORT'}}
seq 3205  llm/retry      retryId=afabacf1 provider=minimax-cn
seq 3206  llm/retry-started
…                        the whole response streams again
seq 3279  chunk/usage    {"inputTokens":32,"outputTokens":1042,"cacheReadTokens":10368}
seq 3281  message usage  {"inputTokens":32,"outputTokens":1042,"cacheReadTokens":10368}
```

The stream died, the harness retried under the same `(turn, step)`, and the dead attempt left a usage chunk of zeros behind. Three samples, and one more retry would make four.

Keep-first reports that step as costing nothing. Not an approximation, the whole step, including 10,368 cache reads. If that sounds familiar, it is the DSH form of a Claude Code bug where keep-first lost 46.2% of output tokens on an agent-heavy tree.

It also means my 2.000000× survived that group by luck rather than structure. The dead attempt reported zeros, so summing three samples still gave twice the truth. That's why the probe prints the group-size distribution next to the ratio instead of the ratio alone.

There is a second consequence I can describe but not measure. The official fold *replaces* on a repeated `(turn, step)` rather than adding, and it never sees `llm/retry`. So a failed attempt that reported real tokens before dying would have its cost silently dropped. Every failed attempt in my corpus reported zeros, so I have no number for this and I'm not going to invent one. The discriminator is sitting right there in the log if you want to handle it: `llm/retry` carries a `retryId` between the attempts.

## What I am not claiming

Four sessions, 8,650 events, 78 usage samples, two providers. The 2.000000× ratio is exact and does not need a large sample to mean what it says, and it reproduced independently on both routes. The fork trap has two observations, the compaction gap three, the retry one.

Cache *writes* are still untested. Reads are covered now, 249,728 of them, but neither provider populated `cacheWriteTokens` at all. MiniMax omits the key.

The compaction gap came to 15% of the corpus total, and I am deliberately keeping that percentage out of the summary. Three compactions over two short sessions inflates it. The citable facts are 48,895 tokens and three events.

Both routes are `openai-completions`-family. Whether an Anthropic-protocol or Responses-protocol route behaves the same, I don't know.

I got my own first pass wrong twice, which is why the probe prints four folds instead of two. My first attribution credited the compaction gap to the double-write line, because naive summing picks up compaction and the official projection doesn't. And I assumed the fork was a subagent until I read the header. Both corrections are in the probe now, along with a warning line that fires when a seed-bearing session has no `origin`.

## Reproducing it

The probe is stdlib-only Python, about 400 lines, and runs against a session root:

```bash
python3 dsh_usage_probe.py --self-test        # four folds vs a hand-computed fixture
python3 dsh_usage_probe.py --root <sessions>  # the same four folds over your corpus
```

`--self-test` builds a parent/child pair exercising all three findings with constructed ground truth and asserts every fold against it. It takes under a second, touches no real data, and needs no vendor account. I ran it before every number in this post, and I would not quote one it hadn't preceded.

Write your probe corpus with `compression: 'none'` and `packChunks: false` and the log is line-readable by anything. One warning that cost me twenty minutes: a custom provider in DSH requires a credential even when the endpoint doesn't, because the provider id doubles as the credential name. An empty key fails the request with `MISSING_CREDENTIAL` rather than sending an unauthenticated one.

## What would settle it

Three things, in the order I would do them.

Run it on a route that reports cache writes. Reads are settled; writes are the remaining hole, and it is an afternoon.

If you maintain a DSH plugin that counts tokens: fold per `(turn, step)`, filter on `seedLength`, never keep-first, and add `compaction/summary.usage`. The first three you can fix today. The fourth needs the projection to change, or every consumer to fold the summary event themselves.

And if your corpus contradicts any of this, I want the numbers. A step whose two usage samples disagree, a `seedLength` that doesn't bound the inherited prefix, a provider that populates the summary usage into the projection, or a failed attempt that reported real tokens before it died. The Claude Code catalog was built entirely out of people sending me counterexamples, and six of its entries exist because someone did.

Probe, protocol, and the full invariant catalog are in the repository. Happy to answer questions about any of it.

*— Li Zhuojun*

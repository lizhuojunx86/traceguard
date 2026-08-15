**Title:** `tokenUsage` projection never folds `compaction/summary.usage`, and replaces rather than adds across a retried step

**Category:** Bug / Feedback (whichever the repo uses for defect reports)

---

Two gaps in `token-meter`'s `tokenUsage` projection, found while measuring session logs from a probe corpus. Both are in the projection itself, so plugins that do the recommended thing (read `ctx.sessionProjections.tokenUsage` instead of folding the log themselves) inherit them.

Measured on 4 sessions, 8,650 events, 78 usage samples, two providers (MiniMax-M3 over `minimax-cn`, and `qwen2.5:14b` over a local OpenAI-completions endpoint).

## 1. Compaction cost is never counted

`compaction/summary.usage` carries the provider-reported cost of the summarize call. It is typed in `packages/compaction/compaction/src/types.ts` ("Provider-reported token usage for the summarization request, when emitted") and written at `packages/compaction/compaction-basic/src/region.ts:447`.

`usageOf()` in `packages/llm/token-meter/src/usage-projection.ts` matches only `assistant/chunk` (usage) and `assistant/message`. The summarize call is not a loop step, so it produces neither. The event is never folded.

**Measured:** 3 compaction events, **48,895 tokens**, none counted. The largest was a single MiniMax-M3 summarize call reporting 536 input / 2,436 output / 41,472 cache-read = 44,444 tokens, against a `shadowedTokenCount` of 19,962 for the range it replaced.

Whether that trade is worth it depends on cache pricing, and I have no opinion on it. The issue is that a user reading any usage surface built on this projection cannot see that the call happened at all — and compaction fires more often precisely on the long sessions where budgets matter.

**Suggested fix:** extend `usageOf()` to match `compaction/summary` and return `event.data.usage`. The `(turn, step)` replacement logic does not apply (a summary is not a loop step), so it wants a plain add. `usage?` is optional, so an absent value stays absent rather than becoming zero.

## 2. A retried step's earlier attempt is replaced, not added

`apply()` replaces on a repeated `(turn, step)` via `addReplacing` (lines 126-136). That is correct for the two-sample case D-1 documents: a usage chunk followed by the assembled message reporting the same numbers.

It is not correct across a retry. Observed sequence in one session:

```
seq 3203  assistant/chunk   usage  {"inputTokens":0,"outputTokens":0}
seq 3204  assistant/chunk   finish {kind:'error', failure:{code:'TRANSPORT'}}
seq 3205  llm/retry                retryId=afabacf1 provider=minimax-cn
seq 3206  llm/retry-started        retry=1
…                                  the response streams again from block-start
seq 3279  assistant/chunk   usage  {"inputTokens":32,"outputTokens":1042,"cacheReadTokens":10368}
seq 3281  assistant/message usage  {"inputTokens":32,"outputTokens":1042,"cacheReadTokens":10368}
```

Three usage samples under one `(turn, step)`, and one more retry would make four. The two attempts are separate costs, not duplicate reports of one cost, but they share a key and `apply()` has no way to tell them apart — `llm/retry` is not in its match list.

The failed attempt reported zeros here, so nothing was actually lost in this corpus and **I am claiming no number for the undercount**. The mechanism is what I am reporting. A provider that reports real usage before a transport failure would have that cost silently dropped.

The token-meter README says "Usage chunks are counted even when a request later fails", which holds when the failed step produces no message. It stops holding when a retry succeeds under the same key.

**Two things this also affects:** the group-size assumption (a `(turn, step)` can carry k>2 samples) and keep-first implementations, which report such a step as costing zero — 10,368 cache reads in the case above.

`llm/retry` / `llm/retry-started` carry a `retryId` and sit between attempts, so the discriminator already exists in the log.

## What is already right

Worth stating, because it is why these two stood out. Append-only with dense contiguous `seq` removes the whole class of drift that comes from in-place transcript rewriting. Terminal-only usage (`types.ts:291`: "Adapters emit usage before the terminal finish and nothing afterward") removes partial-snapshot summing. Sibling child sessions with `parentSession` + `seedLength`, and telemetry that says to stitch on that pair, is the right shape. `addReplacing` for the ordinary two-sample case is correct, and its existence is what told me the double-write hazard was real rather than theoretical.

## Reproducing

Stdlib-only Python, no dependencies, no vendor account:

```bash
python3 dsh_usage_probe.py --self-test        # folds vs a hand-computed fixture
python3 dsh_usage_probe.py --root <sessions>  # four folds over a real corpus
```

It folds a corpus four ways (naive, a line-for-line transcription of `tokenUsageProjectionDefinition`, seed-aware, and corrected) and prints the deltas plus the `(turn, step)` group-size distribution. `--self-test` asserts every fold against constructed ground truth before it touches real data.

Corpus written with `compression: 'none'` and `packChunks: false` so the log is line-readable.

Probe, protocol, and the full invariant catalog: https://github.com/lizhuojunx86/traceguard/blob/main/usage-tracker-audit/dsh-probe/README.md

I understand external PRs aren't being accepted right now. Happy to supply a regression fixture in whatever form is useful, or just leave the measurements here.

---

**中文摘要**

两处都在 `token-meter` 的 `tokenUsage` 投影里，所以按推荐做法读 `sessionProjections.tokenUsage` 的插件全都会继承。

一、`compaction/summary.usage` 从不被折叠。`usageOf()` 只匹配 `assistant/chunk` 和 `assistant/message`，而摘要调用不是 loop step。实测 3 次 compaction、48,895 token 全部未计入，其中一次 MiniMax-M3 摘要调用报了 44,444 token（含 41,472 cacheRead），换掉的历史 `shadowedTokenCount` 是 19,962。建议 `usageOf()` 增加 `compaction/summary` 分支，按相加而非 `(turn, step)` 替换处理。

二、同一 `(turn, step)` 下的重试尝试被替换而非相加。流传输失败后 harness 在同一 key 下重试，两次尝试是两笔独立成本，但 `apply()` 看不到 `llm/retry`，无从区分。本语料中失败尝试报的是全零，**所以我没有漏计的数字**，报告的是机制本身。附带两个影响：一个 step 的样本数可以大于 2；keep-first 实现会把这一步报成零成本（上例中丢掉 10,368 个 cacheRead）。

复现脚本纯标准库、无依赖、无需任何厂商账号，`--self-test` 会先用手算真值校验四种折叠再碰真实数据。

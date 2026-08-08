# Prompt for Claude Code — traceguard routing_audit

以下内容可整段贴给 CC。四个任务，按顺序做，Task 3 是重点。

---

`routing_audit` 有一个已确认的缺陷：`claude-opus-5` 从 2026-07-25 起进入语料，
但它在 `pricing.py` 的 `PRICES` 和 `routing_policy.yaml` 的 `tiers` 里都不存在。
后果是 5,992 条 trace 的 `cost_usd` 为 NULL，从所有汇总里静默消失，且无法被判定
为合规或偏差。整整两周没有任何告警。

现场事实（`traces_routing_audit.db`，2026-08-08 01:31 UTC 只读快照）：

```sql
select model_id, count(*) from traces where cost_usd is null and tokens_out > 0 group by 1;
--  claude-opus-5 | 5992      ← 唯一一组，修完应为 0

select count(*), min(invoked_at), max(invoked_at) from traces where model_id='claude-opus-5';
--  5992 | 2026-07-25 02:06:28 | 2026-08-07 17:52:18
```

## Task 1 — 补 opus-5 的价格

`packages/traceguard/src/traceguard/routing_audit/pricing.py`

1. **先核实价格，不要猜。** 该模块自己的标准是 "verified against the platform
   pricing page"。核不到就停在这一步，把 Task 3 先做掉——让缺失变成告警，比让
   它变成一个可能错的数字好。
2. 核实后加 `PRICES["claude-opus-5"]` 条目，并按现有格式在模块 docstring 里注明
   核实来源与日期。
3. `KNOWN_RELEASED_AT` 同步加一条。**用官方公告日期，不要用首次观测日期**
   ——首次观测是 2026-07-25 02:06:28，那是我们看到它的时间，不是它发布的时间。
   这个区分是 `ingest_claude_code.py` 那条 `predates registered
   available_to_us_at` 警告存在的理由。
4. 回填已有的 NULL：`reprice.py` 已经有 `reprice_null_costs`，先 dry-run 再
   `--write`，出问题用 `--rollback BATCH_ID`。写入走 `routing_audit_reprice_log`。

## Task 2 — 补 opus-5 的 tier

`packages/traceguard/src/traceguard/routing_audit/routing_policy.yaml`

`tiers.frontier` 加 `claude-opus-5`（$5/$25 档，与 opus-4-8 同级）。按文件头部
既有的修订记录格式记一条 v2，写明日期和理由。

注意这会改变 `routing_decisions` 的判定结果。**改完不要顺手重新生成决策**——
现有 96 条 `source='manual'` 的行带着人工填的 `reason`，`generate` 不应覆盖它们，
先确认这一点再跑。

## Task 3 — 无价模型必须发出声音（本次重点）

`packages/traceguard/src/traceguard/routing_audit/ingest_claude_code.py`

现状：`compute_cost_usd` 在模型没有价格条目时返回 `None`。**这个行为是对的，
不要改它**——不猜价格是正确的。缺陷在于没有任何东西盯着这个拒绝。

两处要改，都在 `main()` 写库那段（约 495–525 行）：

1. 那里已经有 `stats.models_first_seen` 和 `stats.warnings`，以及一个针对
   `ModelRegistryEntry` 的告警循环。在旁边加一条同样形状的检查：
   `price_for(model_id) is None` → 追加一条 warning，写明 model_id、首次观测
   时间、本批受影响的 trace 数。
2. `stats.written_cost` 目前只在 `trace.cost_usd is not None` 时累加，于是摘要
   里的成本合计会静默偏低。摘要必须同时输出**未计价 trace 条数**，让"合计"和
   "有多少没算进去"永远同屏出现。

配一个回归测试：喂一条 `model_id` 不在 `PRICES` 里的 trace，断言 ingest 产生
warning。这个测试从第一天就能红，而它不存在，所以缺陷活了两周。

## Task 4 — 低优先，查一处潜在的 SQLite 陷阱

`counterfactual.py:196` 的 `Trace.invoked_at <= as_of`，`as_of` 来自
`counterfactual.py:464` / `blind.py:282` 的 argparse，默认是 **str**。

`traces.invoked_at` 声明为 `DATETIME`，在 SQLite 里是 NUMERIC affinity。如果传入
的字符串能被转成数字，SQLite 会按数字比较，而所有 TEXT 时间戳在 SQLite 的排序里
一律排在数字**之后**，比较会恒为假——静默返回空集，不报错。

`'2026-07-05'` 转不成数字所以目前安全，但这依赖调用者的输入格式。请确认这两处
在比较前把 `--as-of` 解析成 `datetime`，并加一个回归测试（传一个纯数字字符串，
断言不会静默返回空集）。

## 硬约束

- **不许猜价格。** 核不到就保持 NULL，让 Task 3 的告警去喊。
- `traces` / `routing_audit_ingest_log` 不要改。`reprice` 走自己的日志，可回滚。
- 不要覆盖 `routing_decisions` 里 96 条 `source='manual'` 的行。

## 验收

```bash
cd packages/traceguard && uv run pytest

# dry-run 应显示 5,992 条待计价
uv run python -m traceguard.routing_audit.reprice

# 修完必须返回 0 行
sqlite3 traces_routing_audit.db \
  "select model_id, count(*) from traces where cost_usd is null and tokens_out > 0 group by 1;"
```

最后一条是这次唯一需要的不变量：**任何产出了 output token 的 trace 都必须有
价格。** 它不需要 fixture、不需要语料、不需要维护，从第一天起就能失败。

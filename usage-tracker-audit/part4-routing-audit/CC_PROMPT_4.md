# Prompt for Claude Code — round 4

Round 3 复核通过，归因逐条验了：`manual rows with actual_model='claude-opus-5'`
= 0，确认 tier 变更碰不到既有行；688+96=784、65+96=161；stored−printed
$0.1034 与 manual 行自身漂移 $0.1035 闭合；冻结副本仍是 425/96/$1,248.1327。

**你挖到的缺陷比你描述的还大一层。** 冻住的不只是 `cost_usd`——
`expected_tier` / `actual_tier` / `deviation` 同样停在 07-04 那个批次。所以：

> 审计的 96 条偏差，现在全部**永久豁免于路由策略**。任何一次未来的 policy
> 修订都到不了它们。当前 161 条偏差里 96 条（60%）在这座孤岛上。

**人工标注一条偏差，等于把「它是偏差」这个判断冻住。** 而被标注的行恰好是最
被认真看过的那些。这不是成本漂移，是审计的信号总体停止响应策略。

方案已定：**先做 A（拆列），B 写进 SPEC 当目标形态，现在不做 B。**

---

## Task A — 拆分「人工列」与「派生列」，只刷新后者

`routing_decisions` 现在把两类数据放在同一行：

| 类别 | 列 | 规则 |
|---|---|---|
| 人工 | `reason`, `outcome`, `source` | **永不重算。** 这是现有保护的正当内核 |
| 派生 | `cost_usd`, `expected_tier`, `expected_model`, `actual_model`, `actual_tier`, `deviation`, `n_traces`, `ts`, `project`, `task_type` | **必须跟随 traces 与 policy** |

`generate` 目前在 `routing_decisions.py:242-244` 整行 `continue`。改成：
manual 行照常重算全部派生列并 UPDATE，人工列原样保留，`batch_id` 更新为本次
批次（否则无法分辨这行的派生部分是什么时候算的）。

分列在代码里要是**显式命名的集合**，不要靠 `generate` 里手写字段列表——
新增一列时，作者必须选边站，而不是默认落进"人工列"被冻住。加一条测试：
`routing_decisions` 的每一列都必须属于且只属于两个集合之一，新增未归类的列
直接失败。这和你上一轮 `WARNING_KINDS` 里那条"注册表不能有僵尸 kind"
是同一个形状。

跑完之后 96 条 manual 行的 `deviation` 判定会第一次在 v2 policy 下重算。
**这可能翻转其中一些行。** 翻转数必须逐行 diff 出来记进
`routing_decisions_rebuild_log.jsonl`，不要只报总数——如果有行从
deviation 翻成 compliant，那条 `reason` 就成了对一件没发生的事的解释，
需要人看。这是这次改动唯一可能产生真实惊喜的地方。

## Task B — 加一条 watchdog，无论 A 做没做都该有

stored 值与当场重算不一致时告警，走 `WARNING_KINDS` 那条投递路径。

理由：A 修好的是「重算能到达 manual 行」，修不掉「派生表可以和它的来源静默
分叉」这一整类。这次是靠 printed $1,945.1544 与 stored $1,945.2578 不一致
才暴露的，而那个不一致只出现在人眼看的打印里，没进任何机器可读的地方。

`generate` 的 dry-run 现在已经能报出会改什么，把这条检查挂在同一处最自然。

## Task C — 把 B 形态写进 SPEC，标记为目标态，不实现

在 `TRACEGUARD_SPEC.md` 记一条设计决定，内容是**为什么现在的形状是错的、
目标形状是什么、以及为什么现在不做**：

- 现状：`routing_decisions` 以 `decision_id` 为主键 upsert，人工与派生同行。
  后果是任何"保护人工输入"的实现都必然连带冻结派生数据。Round 3 实测。
- 目标：派生判定 **append-only**，修正靠追加新版本而非覆盖；视图解析每个
  `decision_id` 的当前判定；人工注解迁到独立表，挂在判定的**身份**上而不是
  作为派生行的列。
- 依据：同一操作者的 pit-archive 已经解过这个问题——`pit.ingestion_log_raw`
  是表且 append-only，`pit.ingestion_log` 是视图且是默认名，修正靠
  classifier 版本号叠加。**最顺手的名字必须是安全的那个**，靠纪律维持的
  正确性会腐化，靠命名维持的不会。
- 为什么现在不做：需要 schema 迁移与回填，而 A 已经关掉了前向风险。
  这是记录一个方向，不是排期。

`routing_decisions_rebuild_log.jsonl` 已经是那套模式的一半（rebuild 记录），
缺的是 append-only 那一半。SPEC 里把这句写上，下次有人读到会知道为什么只有
一半。

---

## 不要做

- 不要动 `reason` / `outcome` / `source`。这次改动的全部意义是**在不碰它们的
  前提下**让派生列活过来。
- 不要现在实现 B。
- 不要在没有逐行翻转 diff 的情况下认为 Task A 完成。

## 验收

```bash
cd packages/traceguard && uv run pytest

# 每一列必须归属人工/派生之一
uv run pytest -k "column_partition or manual_column"

# 重跑 generate --write 后，manual 行的 batch_id 应已更新，
# 且 reason/outcome 逐行未变
sqlite3 traces_routing_audit.db \
  "select batch_id, count(*), sum(reason is not null) from routing_decisions where source='manual' group by 1;"
```

不变量：**`routing_decisions` 里的每一列，要么由人写且永不重算，要么由机器算
且每次重建都必须更新。没有第三类。** 现在的 96 行属于不存在的第三类——
人写的和机器算的被同一个保护罩罩住了。

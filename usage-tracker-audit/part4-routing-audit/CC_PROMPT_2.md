# Prompt for Claude Code — round 2

三个任务。**Task A 优先于一切**，它会改变每一个成本数字，包括已发布的那些。
`generate --write` 在 A 落地前不要跑——理由在 Task B。

---

## Task A — `compute_cost_usd` 读不到 1 小时缓存写入，全库成本系统性偏低

这是核对上一轮 reprice 结果时发现的：手算 opus-5 是 $1,213.85，工具算出
$1,127.77，差 $86.08。差额精确等于 `cache_creation_1h` 按 1.25× 而非 2.0×
计价的结果（22,969,204 × $5 × 0.75 / 1e6 = $86.13）。

### 证据

`pricing.py::compute_cost_usd` 只从**嵌套**结构读 5m/1h 拆分：

```python
nested = usage.get("cache_creation") or {}
cache_5m = nested.get("ephemeral_5m_input_tokens")
cache_1h = nested.get("ephemeral_1h_input_tokens")
if cache_5m is None and cache_1h is None:
    cache_5m, cache_1h = cache_creation_total, 0    # ← 每一条都走这里
```

但库里存的 usage 用的是**平铺**键：

```
58,210 条 trace 里，usage block 带嵌套 "cache_creation" dict 的：0
                    带平铺 cache_creation_5m / cache_creation_1h 的：58,194
                    两者都没有的：16
```

所以那个 fallback 分支在**每一条记录上**都触发，把全部缓存写入按 5 分钟档
1.25× 计价。`ModelPrice.cache_write_1h_mult = Decimal("2.0")` 声明了但在任何
可达路径上都用不到——**它是死常量。** 模块 docstring 写的是
"0.1×/1.25×/2× for both eras"，代码和自己的文档不一致。

### 影响范围（按 2.0× 重算与现值之差）

```
claude-fable-5     $  705.24
claude-opus-4-8    $  591.59
claude-opus-5      $   86.13
claude-sonnet-5    $    9.14
claude-opus-4-7    $    1.39
TOTAL              $1,393.49    涉及 27,130 条 trace

全库合计   $13,716.01  ->  $15,109.50
report_en.md 已发布窗口（<2026-07-05）少算 $821.00，对应已发布的 $6,281.98
```

`routing_decisions` 的 $1,248.13 同样偏低（它是从 trace 成本汇总来的），
修完需要重算。

### 要做的事

1. **先核实 1 小时缓存写入的真实倍率**，对着 platform pricing page，
   和上一轮核 opus-5 价格同样的标准。**不要因为代码里写着 2.0 就当它对**——
   代码同时写着两个互相矛盾的东西，需要一个外部权威来裁决哪个是对的。
   核不到就停下来报告，不要 reprice。
2. 修 `compute_cost_usd`：同时接受嵌套和平铺两种形态。判断顺序要明确，并在
   docstring 里写清楚为什么两种都要支持（上游 usage schema 的形态差异）。
3. 加回归测试：**平铺键的 usage block 必须让 `cache_write_1h_mult` 真正参与
   计算。** 现有测试全绿是因为它们大概率喂的是嵌套形态的 fixture——请确认，
   如果是，那本身就是一条要记的发现：fixture 的形态和生产数据不一致，
   于是测试测的是一条生产中永远走不到的分支。
4. **`reprice_null_costs` 修不了这个。** 它 `.where(Trace.cost_usd.is_(None))`
   且硬编码 `old_cost_usd=None`。这次要改的是 27,130 条**已经有值**的行。
   需要一条新路径（`reprice --recompute` 或类似），必须：
   - 把 `old_cost_usd` 真实写进 `routing_audit_reprice_log`，不是 None；
   - 可 rollback，和现有 batch 机制一致；
   - dry-run 报出「多少行会变、合计变多少」，而不是恒为 0。
5. 上一轮的 opus-5 batch `rp-20260808T030632Z-ec404e` 不要回滚——它把 NULL
   变成了一个偏低的值，新的重算路径会覆盖它，两条 log 记录合起来才是完整轨迹。

---

## Task B — 冻结一份可复现的库，再谈重新生成决策

`routing_decisions` 现在是 425 / 96 / $1,248.13，两个 2026-07-04 的批次，
**逐位复现 `report_en.md` §3**。这是目前唯一一份「已发布数字可被重算验证」的
证据，一旦 `generate --write` 跑过就没有了——`decision_id` 是主键，
generate 是 upsert，不是 append。

所以在任何重新生成之前：

```bash
cp traces_routing_audit.db traces_routing_audit.2026-07-04-audit.db
```

顺带记一条设计观察，值得进 SPEC：**`routing_decisions` 是派生表，但它的修正
方式是覆盖而不是叠加新版本。** `reprice` 有 batch + rollback，
`generate` 没有等价物。同一个仓库里两种派生表用了两种不同的修正语义。
（对照：pit-archive 的规矩是派生表只能通过版本化、自记录的重建脚本
drop-recreate，并把 who/when/version/rows 写进 rebuild_log。）
这条只是提出来，改不改是设计决定，不要顺手动。

---

## Task C — 打标流程停了五周，而且没有任何东西发现

```
task_tags   318 条（manual 314 / heuristic 4），窗口 2026-05-30 → 2026-07-03
traces      58,210 条，窗口 2026-05-30 → 2026-08-07
generate dry-run: skipped 31,142 untagged traces
```

未打标的 trace 在 tier 查找之前就被跳过，所以 opus-5 涉及的 23 个 session
一条都没打标，把它加进 `tiers.frontier` 对判定毫无作用。这和无价模型是同一类
静默失效，只是卡在另一个环节：**一个管道阶段停止产出，而没有任何东西以
「产出为零」为异常。**

要做的事，按这个顺序：

1. **先做watchdog，再做回填。** 回填会让症状消失，watchdog 才是防止它再发生的
   东西，先做回填很容易就忘了。指标：库里的 trace 总数 vs 落在某个已打标 unit
   窗口内的 trace 数。这个覆盖率跌破阈值、或者最新 tag 的 `ts_start` 落后
   最新 trace 超过 N 天，就告警——投递路径和你这轮修好的 `missing_price`
   走同一条，进 run log，不要只进 stdout。
2. 然后回填 `tag_heuristic` 到 2026-08-07。
3. **回填出来的是 `source='heuristic'`，未经人工校正。** `tag_heuristic` 不覆盖
   manual 行，所以混合是被记录的而不是静默的——这点现有实现是对的。但任何
   跨越两段窗口的报表必须打印 manual/heuristic 的构成比，理由和你这轮给
   `written_cost` 加「排除了多少」是同一条：**汇总数永远不能脱离它的口径出现。**
4. `generate` 的 dry-run 盲区（`sess is None` 时提前 `continue`，导致
   `inserted/updated/manual kept` 恒为 0）也要修——dry-run 的作用就是让人在
   写库前看见会改什么，看不见就等于没有 dry-run。

---

## 关于你标出来的那处改动

`test_registry_freeze_warning_on_wider_rerun` 里把 `assert first.warnings == []`
收窄成只断言 freeze 告警不存在——**同意，理由成立**，那句快照断言的是
「不存在任何其他种类的告警」，和函数名写的主题无关，而且每加一类告警就会
误伤一次。

但它顺带也是一个金丝雀，收窄之后没了。建议补一条独立的断言：列出该 fixture
**预期会出现**的告警类型集合，出现集合外的类型就失败。这样既不脆弱，又保留
「冒出没预料到的告警」这个信号。这条是建议不是要求。

---

## 不要做

- 不要因为代码里写着 `2.0` 就直接按 2.0 reprice。先核实。
- 不要回滚 `rp-20260808T030632Z-ec404e`。
- 不要在 Task A 落地前跑 `generate --write`。
- 不要在没有 `cp` 出冻结副本前跑 `generate --write`。
- `traces` / `routing_audit_ingest_log` 仍然不改。
- 96 条 `source='manual'` 的 decision 行仍然不动。

## 验收

```bash
cd packages/traceguard && uv run pytest

# 平铺 usage 的 1h 缓存写入必须走 2.0x（或核实后的真实倍率）
uv run pytest -k "cache_creation or cache_write_1h"

# 重算路径 dry-run 必须报出非零的「将变更行数 / 合计变化」
uv run python -m traceguard.routing_audit.reprice --recompute

# 冻结副本存在
ls -l traces_routing_audit.2026-07-04-audit.db
```

这次的不变量：**`ModelPrice` 上声明的每一个乘数，都必须存在一条能让它参与
计算的测试。** 一个声明了却在任何生产数据上都不可达的常量，和没声明是一样的，
但它看起来像是已经处理过了。

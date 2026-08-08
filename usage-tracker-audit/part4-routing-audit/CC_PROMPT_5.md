# Prompt for Claude Code — round 5（小，两处改错）

Round 4 复核通过：96 条 manual 行全部刷新到 `dec-20260808T053948Z-44a688`、
`reason` 96/96 仍在、0 翻转、$1,248.0292、stored == printed $1,945.1544、
`created_at` 仍是 2026-07-03（三集合划分生效的证据，冻结副本未动）。
三集合你是对的，我给的两集合是错的——`created_at` 归进派生列会毁掉
「这条决策何时首次出现」的唯一记录，那等价于 pit-archive 的 `first_seen_at`。

下面两处都是**我的错数字进了你的代码库**，要清掉。

## Task 1 — 删掉测试注释里那个已被证伪的数字

`packages/traceguard/tests/test_routing_audit_ingest.py:685-691` 现在写着：

> Zero of the 58,210 rows in the local store carry it; 58,194 carry the FLAT
> ... every 1-hour cache write in the store was billed at the 5-minute 1.25x
> rate — **$1,393.49 low across 27,130 traces.**

最后那句是我给的，已经被你自己的 `settle_cache_split.py` 证伪。
`pricing.py:130-146` 已经把它当作"被更正的错误"记下来了，那处保留；
**测试文件这处仍然以事实语气陈述，必须改。**

改成指向 `pricing.py` 的那段更正说明即可，不要在两个地方各维护一份叙述。
注释里保留的应该只有对读者当下有用的那句：这些 fixture 用的是哪种形态、
以及为什么默认要用生产形态。

## Task 2 — `pricing.py` docstring 里的 "this corpus" 要点名是哪个 corpus

当前写着 flat 形态 "**Not present in this corpus.**"，以及
"34,234 of 34,234 records carry the nested form and none carry the flat one"。

这对 `~/.claude/projects` 成立。**对存库不成立。** 我复测了两次：

```sql
-- traces.output_parsed 的 usage block 形态
nested = 0        flat = 58,194
```

并且逐条查了被 `rp-20260808T041211Z-e92e55` 改过的行（trace 2171 / 23597 /
23778，均为 opus-4-8）：全部带 `cache_creation_input_tokens` +
`cache_creation_5m` + `cache_creation_1h`，无嵌套。

**那批 $88.75 本身就是平铺存在于存库的证据**——若存库是嵌套，重算不可能改动
任何一行。所以 docstring 里 "do not attribute any money to it" 和你自己的
reprice log 直接冲突。

两个测量都对。问题是 "corpus" 一个词无标记地指了两个总体：

| 总体 | 形态 | 谁读它 |
|---|---|---|
| `~/.claude/projects` 转录文件 | **嵌套** | ingest 首次计价 → 一直正确，2.0× 一直生效 |
| `traces.output_parsed` 存库 | **平铺**（ingest 拍平） | 任何 reprice / recompute → 需要平铺支持 |

请把 docstring 改成显式区分这两个总体，并把结论收敛成：**首次计价从未出错；
平铺支持是给"从存库重算"这条路径用的，而那条路径确实动过 $88.75。**

顺带值得在 `ingest_claude_code.py:331-332` 拍平那处加一句注释：
**这里丢弃了计价所依据的形态**。存库因此够近似重算、不够精确重算。这正是
pit-archive 用 `canonical_rule_version` 解决的问题——记下是哪条规则产出的，
因为事后重建不回来。现在有了平铺支持所以不再有实际损失，但那个注释要留给
下一个改这里的人。

## 不要做

- 不要改 `pricing.py:130-146` 那段更正说明本身，它记录的是真实经过。
- 不要动冻结副本、`reason`/`outcome`/`source`、`created_at`。

## 验收

```bash
cd packages/traceguard && uv run pytest
grep -rn "1,393.49\|27,130" --include=*.py . | grep -v "pricing.py"   # 应无输出
```

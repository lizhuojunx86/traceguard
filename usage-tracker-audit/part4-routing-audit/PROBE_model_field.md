# 探针实验：用户写的 `model:` 字段到底生不生效

**要回答的问题**：一个 `.claude/agents/*.md` 里写了 `model:` 的 subagent，
实际跑起来用的是那个模型，还是主线程的模型？

**为什么值得做**：`anthropics/claude-code` 上七个月四份报告说不生效，其中
[#43869](https://github.com/anthropics/claude-code/issues/43869) 至今 OPEN、
带 `has repro`、13 条评论；两份被 not_planned 关掉。而官方文档说生效。
**没有人给它定过价。** 你有仪器。

已知的：**内置**决定生效（Explore 在 v2.1.198 前被钉在 Haiku 上，1,565 条没跑偏）。
未知的：**用户写的定义文件**是否被尊重。这两件事不能互相推断。

---

## 步骤

### 1. 造探针

```bash
mkdir -p ~/Desktop/APP/traceguard/.claude/agents
cat > ~/Desktop/APP/traceguard/.claude/agents/probe-haiku.md <<'EOF'
---
name: probe-haiku
description: Routing probe. Answers one trivial question. Exists only to record which model actually runs.
model: haiku
---

Reply with exactly: PROBE OK. Do not use any tool.
EOF
```

**第二个探针，用全名**，因为 cct 里有九种写法，短名和全名可能行为不同：

```bash
cat > ~/Desktop/APP/traceguard/.claude/agents/probe-haiku-full.md <<'EOF'
---
name: probe-haiku-full
description: Routing probe, fully-qualified model id.
model: claude-haiku-4-5-20251001
---

Reply with exactly: PROBE OK. Do not use any tool.
EOF
```

**第三个，无效值**——这是最初要查的那个问题，顺手一起答了：

```bash
cat > ~/Desktop/APP/traceguard/.claude/agents/probe-bogus.md <<'EOF'
---
name: probe-bogus
description: Routing probe with a display name rather than a model id.
model: Claude Sonnet 4.5 (copilot)
---

Reply with exactly: PROBE OK. Do not use any tool.
EOF
```

最后这个的写法直接抄自 `davila7/claude-code-templates` 里真实存在的一个定义。

### 1b. ⚠ 建完文件必须重开会话（第一轮漏了这条）

**subagent 注册表在会话启动时快照，不热加载。** 在已开着的会话里新建定义文件，
调用时会得到：

```
Agent type 'probe-sonnet-alias' not found. Available agents: ...
```

这条本身是一个真实的复现障碍，任何让别人复现的说明里都要写上，否则对方会以为
"定义不生效"，而其实只是没重开。

### 2. 在**主线程跑 frontier 模型**的会话里调用探针

关键：主线程必须不是 haiku。否则「跑了 haiku」无法区分是字段生效还是继承。
确认当前主线程模型后，逐个调用 `probe-haiku` / `probe-haiku-full` /
`probe-bogus`，各一次。

记下调用时刻（HKT）。

### 3. 等 ingest，然后读回

daily ingest 在 19:17 UTC 跑。想立刻看就手动跑一次 ingest，然后：

```sql
SELECT t.component,
       t.model_id,
       count(*)                     AS traces,
       min(t.invoked_at)            AS first_seen,
       round(sum(t.cost_usd), 6)    AS cost
FROM traces t
WHERE t.component LIKE 'probe-%'
GROUP BY 1, 2
ORDER BY 1, 2;
```

如果 `component` 不是探针名，改用时间窗口 + `output_parsed` 里的
`cc_version` / `session_id` 定位。

---

## 判读

| `probe-haiku` 实际跑的 | 结论 |
|---|---|
| **haiku** | 用户 `model:` 字段生效。cct 普查 356/423 成立；Part 4 的处方成立；四份 bug 报告要么是旧版本、要么是别的路径（例如通过 Agent 工具显式传参那条）。 |
| **主线程的模型** | 字段不生效。**cct 的数字不是 356/423，是 423/423。** Part 4 的处方要发第二次更正。而 #43869 拿到它七个月来缺的东西：一个价格。 |

`probe-haiku` 与 `probe-haiku-full` 不一致 → 短名/全名解析不同，本身是一条独立
发现，也解释了 cct 里九种写法为什么危险。

`probe-bogus` 的结果三选一，都值得记：**报错**（最好，缺陷会被看见）、
**静默退回继承**（最坏，60 个"写了字段"的定义里有几个是假安全）、
**当成某个模型跑**（离谱，要单独报）。

---

## 纪律

- **三个探针都要跑，不要只跑一个。** 只跑 `probe-haiku` 而它返回 haiku，
  你会以为问题不存在——而九种写法里可能只有一种能用。
- **记下 `cc_version`。** 四份报告横跨七个月，行为随版本变过至少一次
  （Explore 在 2.1.198）。没有版本号的结论无法被别人复现。
- **跑完把探针文件删掉**，否则它们会永久出现在你自己的偏差审计里，成为
  一个你自己制造的伪信号。

---

## 第二轮：把 cct 那 60 个写了字段的定义全部实测归类

第一轮证明了字段生效、无效值硬报错。第二轮回答的是**另一个问题**，而且可能
比第一轮的结论更值钱。

普查里 60 个写了 `model:` 的定义，九种写法：

```
sonnet 47 · Claude Sonnet 4.5 3 · claude-sonnet-4 3 · claude-sonnet-4-5 2
haiku 1 · inherit 1 · Claude Sonnet 4 1 · claude-sonnet-4-5-20250929 1
Claude Sonnet 4.5 (copilot) 1
```

已实测：`haiku` ✅、`claude-haiku-4-5-20251001` ✅、`Claude Sonnet 4.5 (copilot)` ❌ 硬报错。

**待测的四个，每个都指向一个不同的结论：**

| 探针 | 值 | 覆盖 | 为什么这条重要 |
|---|---|---|---|
| `probe-sonnet-alias` | `sonnet` | **47 / 60** | 单条信息量最大。生效 → 问题集中在 13 个；不生效 → 60/60 全坏 |
| `probe-datedid` | `claude-sonnet-4-5-20250929` | 1 | **旧版本 id。** 若已下线且同样硬报错，则这个定义装上就是坏的 |
| `probe-displayname` | `Claude Sonnet 4.5` | 3（+1 个 `Claude Sonnet 4`） | 不带 `(copilot)` 的 display name，验证第一轮那条不是 `(copilot)` 三个字造成的 |
| `probe-inherit` | `inherit` | 1 | 文档写的显式继承值，确认不会被当成未知 id |

### 这一轮真正想挖的东西

第一轮已经确立：**无效值硬报错、零 token、错误上抛。** 那么
`claude-sonnet-4` / `claude-sonnet-4-5` / `claude-sonnet-4-5-20250929`
这 6 个引用**旧世代 model id** 的定义，如果那些 id 已经下线，就会同样硬报错。

**那意味着它们不是"贵"，是"装上就报错"。**

一个继承了父模型的 subagent 仍然能干活，只是花钱多。一个硬报错的 subagent
根本跑不起来。这两件事对 cct 的维护者是完全不同量级的问题，而 issue 的写法
完全取决于是哪一种。

这就是值得多开一个会话的唯一理由——不是"更完整"，是**可能把一个成本问题
变成一个功能损坏问题**。

### 跑法

重开会话（见 1b），主线程保持 frontier，依次调用四个探针各一次，然后：

```bash
cd ~/Desktop/APP/traceguard
python3 usage-tracker-audit/part4-routing-audit/read_probe_result.py --minutes 30
```

### 判读

- `sonnet` 跑出 sonnet 系 → 47 个安全，问题集中在剩下 13 个
- `sonnet` 跑出父模型 → 别名在 frontmatter 路径不解析，**60/60 全部在继承**
- 旧 id 硬报错 → 那 6 个定义装上即坏，issue 标题应该写这个而不是写成本
- 旧 id 正常跑 → 旧 id 仍在线，只是过时，属于风格问题

## 跑完之后

1. 按判读结果修 `CORRECTION_part4.md`（可能要加第二次更正）
2. 我起草 cct issue 或 claude-code issue（看结果指向哪边），你发
3. Part 5 提纲。这篇的骨架已经有三块：版本边界（免费，已在存档里）、
   探针结果（定价）、cct 普查（规模）

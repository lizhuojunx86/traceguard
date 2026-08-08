# Part 4 传播 — 计划

2026-08-08。文章已发：
https://dev.to/lizhuojunx86/my-routing-policy-and-my-traces-disagreed-96-times-never-once-on-the-main-thread-ffp

---

## 先看数据说传播该怎么做

`launch/outreach-state.json` 记录了四篇文章和十一条外部线程。把它们摆在一起，
结论很直白：

| 渠道 | 结果 |
|---|---|
| dev.to 评论区 | #1 两条（其中一条是他自己），#2 零，#3 零 |
| 具体技术线程 | splitrail 两小时内合并；tokscale 带致谢发版；viberank 的 yoo-minho 自己写了一份 spec |

**这个系列的每一次成功都来自一条具体的技术线程加一个具体的维护者，没有一次
来自广播。** dev.to 评论区四篇加起来两条评论，其中一条还是作者本人。

所以 Part 4 的传播不该是"到处宣布"，而该是**找到这个发现对谁是可执行的**。

## 谁的问题

Part 4 的可执行结论是一条命令：`grep -L '^model:' .claude/agents/*.md`。
对谁最痛？**批量分发 agent 定义的仓库**——他们的每一个用户都继承同一个缺省。

`davila7/claude-code-templates`，30k stars，正在分发 agent 模板。已实测：

```
cli-tool/components/agents/**.md 共 423 个（GitHub tree API 全量，非抽样）
  model: 缺失      356   (84.2%)
  model: 存在       60
  无 frontmatter     7
  抓取失败           0
```

**356 个分发出去的 agent 定义没有 `model:` 字段。** 装了它们的人，每一个
subagent 都会继承主线程的模型——主线程在 Opus 上，`salesforce-expert`
就在 Opus 上。这正是 Part 4 里 95/96 条偏差的机制，只是规模乘以了 30k 个仓库。

那 60 个写了的，写法有九种：

```
sonnet 47 · Claude Sonnet 4.5 3 · claude-sonnet-4 3 · claude-sonnet-4-5 2
haiku 1 · inherit 1 · Claude Sonnet 4 1 · claude-sonnet-4-5-20250929 1
Claude Sonnet 4.5 (copilot) 1
```

`inherit` 的存在说明"就是要继承"有正式写法——所以那 356 个省略更可能是漏了，
不是选择。而 `Claude Sonnet 4.5 (copilot)` 显然不是模型 id 而是显示名。

## ⚠ 查过了，结果推翻了这一节的前提（2026-08-08）

查「无法识别的 `model:` 值会怎样」时撞到一个更大的问题：**`model:` 字段可能
根本不生效。**

`anthropics/claude-code` 上七个月里四份报告，全部带 `bug` 标签：

| issue | 状态 | 创建 | 最后更新 | 标签 |
|---|---|---|---|---|
| [#18346](https://github.com/anthropics/claude-code/issues/18346) | closed **not_planned** | 2026-01-15 | 2026-06-11 | bug, **has repro**, area:model, area:core |
| [#31550](https://github.com/anthropics/claude-code/issues/31550) | closed not_planned (duplicate) | 2026-03-06 | 2026-04-13 | bug, duplicate |
| [#43869](https://github.com/anthropics/claude-code/issues/43869) | **OPEN** | 2026-04-05 | 2026-07-25 | bug, **has repro**, area:model, area:agents |
| [#44385](https://github.com/anthropics/claude-code/issues/44385) | closed as duplicate | — | — | — |

#43869 的标题就是结论：*"Subagent model routing is broken — all mechanisms
resolve to parent model (Opus)"*，13 条评论，两周前还在更新。

**而官方文档说它是生效的**：*"A user or project subagent named Explore overrides
the built-in and keeps its own model field, so define one with `model: haiku`"*。

文档和四份带复现的报告互相矛盾。**这正是 Part 3 的形状反过来**——那篇是
「厂商文档警告了，仓库照样发了」，这次是「厂商文档说能用，四个人说不能」。

### 这动摇了已发表文章的两句话

**核心测量不受影响**：425 / 96 / 22.6% / $1,248.13、95 比 1、312/314——
这些是从存档算出来的，和机制解释无关。

受影响的是**解释**和**处方**：

1. > "the 14 compliant non-matches are all `Explore` on Haiku, which is the
   > component whose agent definition pins `model`."

   **很可能是错的。** 文档写着 v2.1.198 之前 Explore 这个**内置** agent
   总是跑 Haiku——不是因为某个定义文件钉了 `model:`，而是内置行为写死的。
   而 traceguard 仓库里**根本没有 `.claude/agents/` 目录**（我核过）。
   用户级 `~/.claude/agents/` 我看不到，所以不能断言，但内置解释更省事。

2. > CTA：`grep -L '^model:' .claude/agents/*.md`

   它暗示**补上 `model:` 就能修好**。若 #43869 成立，补了也没用。
   命令本身没错（它只回答"你有没有省略"），但读者会读成"补上就好了"。

### 决定性的实验，他有仪器

四个人七个月里定性报告过这件事，两份被 not_planned 关掉，**没有人给它定价**。

而这个项目存在的全部意义就是回答这类问题，问题现在就摆在面前：

1. 建 `.claude/agents/probe-haiku.md`，frontmatter 只写 `model: haiku`
2. 从一个跑在 Opus 上的主线程调用它
3. **从 traceguard 读回实际跑的是哪个模型**

结果只有两种，两种都值钱：

- **跑的是 haiku** → 字段生效，那 356/423 的普查成立，处方成立，Part 4 只需要
  改第 1 句的机制解释。
- **跑的是 Opus** → 字段不生效，那么 cct 的数字不是 356/423 而是 **423/423**，
  Part 4 的处方要发更正，而 #43869 拿到了它七个月来缺的东西：**一个价格。**

**这个实验做完之前，cct 的 issue 不要提。** 现在提，等于用一个可能错的前提去
提醒别人补一个可能没用的字段。

### 还需要一条信息

```bash
ls ~/.claude/agents/ 2>/dev/null || echo "no user-level agents dir"
```

如果这个目录是空的或不存在，那么 Part 4 里 "every deviation I found was in a
component whose agent definition has no `model:` field" 这句就更松——**不是
"定义里没写这个字段"，而是"根本没有定义文件"。** 两者对读者是不同的信息。

---

## （以下为查证之前写的，前提已被上面推翻，保留作记录）

## 发之前必须先查的一件事

**Claude Code 拿到一个无法识别的 `model:` 值会怎么做？** 是报错，还是静默退回
继承？

- 若是静默退回，那么真实的"继承"数量高于 356，那 60 个里有几个是假安全。
- 若是报错，那几个写法是另一类 bug，和继承无关。

**这条没查清之前不要提 issue。** 356/423 本身站得住，是全量普查；但"实际有多少
在继承"是另一个数字，现在不知道。这个系列被读的原因就是它不把两者混为一谈。

## 三步，按顺序

**1. 先告诉维护者，再发表。**

在 cct 提一个 issue，附全量普查的复现命令。预期回应率低——那里有 135 个待处理
PR，他自己的 #754 挂了两周没有人类回复。但先说一声是这个系列一贯的做法，
而且 Part 3 已经立过标准：**merge 是关于维护者带宽的陈述，只有可复现的证据
是关于代码的陈述。** 提了就够，回不回不影响后面。

**2. 该回的先回。**

`outreach-state.json` 里球在他手上的只有一处：

- `tokscale #1011` — 最后一条是他 2026-08-07 16:51 发的，球在 junhoyeo 那边。
  而 junhoyeo 在 08-07 和 08-08 连发了 v4.11.0、v4.12.0，人是活跃的。**不要催。**
- `cct #754` — 08-05 已经 nudge 过一次，不要第二次。
- `CCUM #226` — 开了一个月零评论，08-05 nudge 过。放弃它。

**结论：没有欠着别人的回复。** 这一步是空的，好事——确认过比假设着强。

**3. Part 5 就是这个普查。**

Part 4 说"我的 96 条偏差全部来自没写 `model:` 的组件"。Part 5 说"一个 30k 星的
仓库分发了 356 个这样的定义"。同一条不变量，从自己的机器扩到别人的分发渠道，
是 Series A 的自然延伸而不是新坑。

不要现在动笔。**先把上面那个"无法识别的值会怎样"查清楚**，那决定这篇文章是
一个发现还是两个。

---

## 不要做的

- **不要在 dev.to 评论区、Reddit、HN 广播 Part 4。** 四篇文章的评论区数据已经
  说明这条路的产出是零，而广播的代价是把"这个人只在有测量结果时说话"这个
  唯一资产稀释掉。
- **不要把 cct 的 356 写成"他们坏了"。** 84.2% 缺失更可能说明这个字段的默认
  行为不直观，而不是 423 个作者都疏忽。真正的问题可能在上游默认值那一层，
  这个判断要留在文章里而不是 issue 里。
- **不要在任何对外文本里提 HKEX。**

## 状态

- [x] 查清无法识别的 `model:` 值的行为 → **硬报错，不静默继承**（2026-08-08，
      cc 2.1.226，见 `RESULT_probe.md`）。连带答了更大的那个问题：**字段生效**，
      356/423 成立，Part 4 无需第二次更正，issue 封印解除。
- [x] 三轮探针跑完（2026-08-08 16:47 / 17:40 / 17:57 HKT，cc 2.1.226）。
      60 个写了字段的定义 **60/60 实测归类，零推断**。详见 `RESULT_probe.md`。
- [ ] cct issue 草稿（我写，你发）—— 三条按强度递减：
      1. **8 个 agent 装上就报错**，零 token，错误上抛。两类成因：
         - **5 个把 display name 写进了 id 的位置**（`Claude Sonnet 4.5` ×3、
           `Claude Sonnet 4` ×1、`Claude Sonnet 4.5 (copilot)` ×1）——笔误，改字符串即可
         - **3 个 `claude-sonnet-4` 拼写完全正确，但那个模型已经退役**——
           写下来那天能跑，是时间把它变坏的，没人改过代码
         **这是功能损坏不是成本问题，标题写这个。**
      2. **钉模型会腐坏，钉档位不会。** `claude-sonnet-4-5` 仍在线、
         `claude-sonnet-4` 已退役，边界就卡在两代之间。48 个写别名的定义
         （`sonnet` 47 + `haiku` 1）不会遇到这件事——别名钉档位，档位不退役。
         这条同时是 Part 5 的主论点。
      3. 356 个缺 `model:`（原有那条，最弱，是默认值不直观而非作者疏忽）。
         精确说法：356 缺失 + 1 个显式 `inherit`（实测生效）+ 7 个无 frontmatter
         （**未测**——那 7 个的问题是会不会被注册，不是路由到哪）。
      **不能写进 issue 的**：
      - `claude-sonnet-4-5` / `claude-sonnet-4-5-20250929` 实测仍在线，
        所以不能说"所有旧世代 id 都坏"——只有 sonnet-4 那一代坏。
      - "那 47 个写于 4.5 时代"是推断，要用得先查 cct 的 git blame。
- [ ] Part 4 加进 `outreach-state.json` 的监控列表（现在只到 #3）
- [ ] Part 5 提纲，在上面两条之后

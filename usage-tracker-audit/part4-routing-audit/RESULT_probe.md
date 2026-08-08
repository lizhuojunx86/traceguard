# 探针结果 — 用户写的 `model:` 字段生效；cct 的 60 个定义有 4 个装上就报错

两轮测量，2026-08-08 16:47 与 17:40 HKT。八条测量。

## 环境（没有版本号的结论不可复现）

| 项 | 值 |
|---|---|
| cc_version | **2.1.226** |
| 版本来源 | transcript 的 `version` 字段 |
| 主线程模型 | `claude-opus-5` |
| 平台 | darwin arm64，VSCode 扩展 |
| 定义位置 | 项目级 `traceguard/.claude/agents/`，用户级仍不存在 |

⚠ **`claude --version` 报的是 2.1.152，那个数字是错的。** PATH 上的
`/opt/homebrew/bin/claude` 是一个陈旧安装，不是跑这个 session 的进程。真正在跑
的是 VSCode 扩展自带的 `~/.vscode/extensions/anthropic.claude-code-2.1.226-darwin-arm64`。
**发布用 2.1.226。** 谁想复现，让他读 transcript 的 `version`，不要读 `--version`。

2.1.226 在 Explore 解绑（2.1.198）**之后**。

⚠ **建完探针文件必须重开会话。** subagent 注册表在会话启动时快照，不热加载。
在已开着的会话里新建定义文件，调用会得到 `Agent type 'X' not found`——那是加载
限制，不是路由结果。第一轮就撞上了，任何复现说明都要写上这一条。

## 第一轮：字段生不生效

| # | 探针 | 定义里写的 | 显式传参 | **实跑** | 判读 |
|---|---|---|---|---|---|
| 1 | `probe-haiku` | `haiku` | — | `claude-haiku-4-5-20251001` | 字段**生效** |
| 2 | `probe-haiku-full` | `claude-haiku-4-5-20251001` | — | `claude-haiku-4-5-20251001` | **生效**，与短名等价 |
| 3 | `probe-bogus` | `Claude Sonnet 4.5 (copilot)` | — | `<synthetic>`，0 token | **硬报错** |
| 4 | `probe-haiku` | `haiku` | `sonnet` | `claude-sonnet-5` | 显式传参**生效**且**压过**定义 |

主线程全程 `claude-opus-5`，1/2/4 跑的都不是 opus-5，"生效"不是继承的巧合。

第 3 条的原文，逐字：

```
There's an issue with the selected model (Claude Sonnet 4.5 (copilot)).
It may not exist or you may not have access to it.
```

`model = <synthetic>`，`stop_reason = stop_sequence`，input/output/cache 全 0。
subagent 一个 token 都没跑就死了，错误上抛给调用方。

**结论：用户写的 `model:` 字段生效。** 走判读表第一行——cct 普查 356/423 成立，
Part 4 的处方成立，**不需要第二次更正**。

## 第二轮：把那 60 个写了字段的定义实测归类

| # | 探针 | 定义里写的 | **实跑** | 判读 |
|---|---|---|---|---|
| 5 | `probe-sonnet-alias` | `sonnet` | `claude-sonnet-5` | 生效；别名解析到**当代** |
| 6 | `probe-datedid` | `claude-sonnet-4-5-20250929` | `claude-sonnet-4-5-20250929` | 生效，**精确命中，旧 id 仍在线** |
| 7 | `probe-displayname` | `Claude Sonnet 4.5` | `<synthetic>`，0 token | **硬报错** |
| 8 | `probe-inherit` | `inherit` | `claude-opus-5`（= 父） | 生效，**如文档所述** |

第 7 条原文——注意括号里没有 `(copilot)`：

```
There's an issue with the selected model (Claude Sonnet 4.5).
It may not exist or you may not have access to it.
```

⚠ 第一版 `read_probe_result.py` 的 VERDICT 逻辑写死了 `"haiku" in m`，第 5–8 条
全判错（第 8 条打印 "was NOT honoured"——对 `inherit` 而言跑出父模型正是**生效**）。
**已修**：现在读定义文件里真实声明的值再比对，`inherit` 和硬报错各自单独处理，
读不到定义文件就明说"无法判读"。修后七条全判对。

⚠ 但修后的版本对 **cwd 敏感**：查找路径是 `Path.cwd()/.claude/agents`，在
`part4-routing-audit/` 下跑会七条全打 `NO DEFINITION FILE FOUND — cannot judge`。
**必须从仓库根跑。** 这个降级是诚实的（不撒谎），但发生在最自然的运行位置上。
一行可修（向上找 `.claude/`，或加 `--agents-dir`）。

## 第三轮：补完最后三个写法

| # | 探针 | 定义里写的 | **实跑** | 判读 |
|---|---|---|---|---|
| 9 | `probe-oldalias-45` | `claude-sonnet-4-5` | `claude-sonnet-4-5-20250929` | 生效，**不带日期解析到带日期** |
| 10 | `probe-oldalias-4` | `claude-sonnet-4` | `<synthetic>`，0 token | **硬报错** |
| 11 | `probe-displayname-4` | `Claude Sonnet 4` | `<synthetic>`，0 token | **硬报错** |

第 10 条原文——**这一条是拼写完全正确的小写 model id**：

```
There's an issue with the selected model (claude-sonnet-4).
It may not exist or you may not have access to it.
```

**退役边界卡在 sonnet-4 和 sonnet-4-5 之间**：`claude-sonnet-4-5` 还在，
`claude-sonnet-4` 已经不在了。

## cct 60 个定义的实测归类（60/60 完成）

| 写法 | 数量 | 实测 | 归类 |
|---|---|---|---|
| `sonnet` | 47 | → `claude-sonnet-5` | ✅ 别名**钉档位** |
| `Claude Sonnet 4.5` | 3 | 硬报错 | ❌ display name 当 id |
| `claude-sonnet-4` | 3 | **硬报错** | ❌ **id 拼写正确，但模型已退役** |
| `claude-sonnet-4-5` | 2 | → `claude-sonnet-4-5-20250929` | ✅ 精确**钉模型** |
| `haiku` | 1 | → `claude-haiku-4-5-20251001` | ✅ 别名钉档位 |
| `inherit` | 1 | → 父模型 | ✅ 显式继承，如文档 |
| `Claude Sonnet 4` | 1 | **硬报错** | ❌ display name 当 id |
| `claude-sonnet-4-5-20250929` | 1 | → 精确命中 | ✅ 精确钉模型 |
| `Claude Sonnet 4.5 (copilot)` | 1 | 硬报错 | ❌ display name 当 id |

**60 / 60 全部实测，零推断。装上就报错的：8 个。**

## 全量 423 的结构

```
356  无任何决定              → 继承主线程
 48  别名钉档位              → 生效，跟随该档最新款（sonnet 47 + haiku 1）
  3  精确 id 钉模型          → 生效，冻结在具体模型（claude-sonnet-4-5 ×2 + 带日期 ×1）
  1  inherit                → 生效，显式继承
  5  display name 当 id     → 硬报错（Claude Sonnet 4.5 ×3 + Claude Sonnet 4 ×1 + copilot ×1）
  3  id 正确但模型已退役      → 硬报错（claude-sonnet-4 ×3）
  7  无 frontmatter         → 未测
———
423
```

**坏掉的 8 个不是同一种坏。** 5 个是"把显示名写进了 id 的位置"——一个当场就能
看出来的笔误。另外 3 个 **`claude-sonnet-4` 拼写完全正确**，写下来的那天是能跑
的，是模型退役之后才坏的。**这两类对维护者是不同的修法**：前者改字符串，
后者要有人定期重测。

## 三条结论

**1. `model:` 字段生效——所有测过的路径。** frontmatter 短名、frontmatter 全名、
frontmatter 别名、frontmatter 旧 id、显式传参，五种都生效，且显式传参压过
frontmatter。`inherit` 也按文档工作，不会被当成未知 id。

**2. 8 个 cct 定义装上就报错。这是功能损坏，不是成本问题。**
一个继承了父模型的 subagent 仍然能干活，只是花钱多；一个硬报错的 subagent
零 token、根本跑不起来。**issue 的标题写这个，不写"356 个缺字段"。**

不是 `(copilot)` 那三个字的锅——不带后缀的 `Claude Sonnet 4.5` 同样硬报错。
坏的是"display name 写进了 id 的位置"这个类别。

**3. 最有价值的一条：钉模型会腐坏，钉档位不会。**

`claude-sonnet-4-5` 还能跑，`claude-sonnet-4` 已经硬报错——**退役边界就卡在这
两代之间**。那 3 个写 `claude-sonnet-4` 的定义，拼写完全正确，**写下来那天是能
跑的**，是模型退役之后才坏的。没有人改过一行代码。

而 48 个写别名的定义不会遇到这件事：别名钉的是**档位**，档位不退役。

⚠ 我原来猜"旧世代 id 已下线所以硬报错"——**对了一半**。sonnet-4 应验，
sonnet-4-5 证伪。这个边界只有测出来才知道，猜不出来。

**这条和 Part 4 的核心设计决定在同一个点上会合**：那篇的 policy 记分**记档位
不记模型**。现在有了经验证据说明为什么——精确钉定的定义会随时间静默腐坏，
档位钉定的不会。那 48 个不是"写得含糊"，按 Part 4 的标准恰好是**对的做法**。

## 附带发现：别名钉的是档位，不是模型

`sonnet` 在 frontmatter 里解析到 `claude-sonnet-5`，不是 Sonnet 4.5。
`haiku` 同样解析到当代 haiku。它们今天跑 Sonnet 5、明天跑 Sonnet 6，作者收不到
任何通知。这不是 bug，是别名的定义；但**写定义的人未必知道自己选的是"永远
最新"而不是"我测过的那个"**。

⚠ "那 47 个是 Sonnet 4.5 时代写的"是**推断不是事实**。要在 issue 里提写作年代，
先去 cct 的 git blame 查文件日期——那是另一次测量。否则只陈述机制。

## 这个结论**不**证明什么

写清楚，否则下一个人会拿它去否定别人的 bug 报告。

- 只测了 **2.1.226 一个版本**。四份报告横跨七个月，行为在 2.1.198 已经变过一次。
- 只测了 **两条路径**：定义文件 frontmatter、Agent 工具显式传参。#43869 说的
  "all mechanisms" 可能包含我没有的入口（`/agents` 交互式创建、MCP、
  Task 工具的其他调用形态）。
- 只测了 **项目级** 定义。用户级 `~/.claude/agents/` 我没有，没测。
- 只测了 **一个平台**（darwin arm64 + VSCode 扩展）。

对 #43869 的正确说法是：**在 2.1.226、这两条路径上，我复现不出来。**
不是"这个 bug 不存在"。这条差别就是这个系列被读的原因。

## 唯一还没测的：7 个无 frontmatter 的文件

普查里 `无 frontmatter 7`。它们既没有 `model:` 也没有 `name:`/`description:`，
所以问题不是"路由到哪"，而是**它们会不会被注册成 subagent**。这需要的不是模型
探针，是把文件放进 `.claude/agents/` 看它出不出现在可用列表里——是另一种测量，
不在本次范围内。issue 里提到这 7 个时要说"未测"。

## 污染登记

十一条探针 trace 已进本机存档，component 名 `probe-*`（其中 4 条是硬报错，
零 token）。在 routing_audit 的口径里，跑成功的那几条是"subagent 模型 ≠ 主线程"
的偏差记录，`probe-inherit` 则被记成"合规"——**全是我自己制造的信号**。
后续偏差统计要么按 `component LIKE 'probe-%'` 排除，要么在文中标明。

`traceguard/.claude/agents/` 下现在有 **9 个**探针定义，测量已全部完成，可以删。
留着它们还会让 Part 4 的 CTA `grep -L '^model:' .claude/agents/*.md` 在本仓库
返回假象——那条命令在这里的正确输出应该是"没有定义文件"。

## 下一步

- [x] 修 `CORRECTION_part4.md` 的"仍未回答"一节 → 已答，无需第二次更正
- [x] 三轮探针跑完，cct 60 个写了字段的定义 **60/60 实测归类**
- [ ] Part 4 三处编辑上线（已定稿，见 `CORRECTION_part4.md`）
- [ ] cct issue 草稿（chat Claude 写，用户发），标题走**功能损坏**：8 个 agent
      装上就报错，其中 3 个是拼写正确但模型已退役
- [ ] Part 5 提纲：版本边界（存档，免费）+ 探针结果（定价）+ cct 普查（规模）
- [ ] 删九个探针定义文件（需确认）
- [ ] 顺手：`read_probe_result.py` 的 cwd 敏感性，一行修

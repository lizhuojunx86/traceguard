# Part 4 更正 — ✅ 已应用 2026-08-08

三处编辑已写入已发布的文章并保存，线上核对通过：新的机制解释在位、新的 CTA
在位（代码块正常渲染）、第四条 counterweight 在位、原句已消失。文章仍是已发布
状态，系列仍显示 "(4 Part Series)"，6 张表、12 个 h2 不变。

https://dev.to/lizhuojunx86/my-routing-policy-and-my-traces-disagreed-96-times-never-once-on-the-main-thread-ffp

发现时间 2026-08-08，文章发布当天。

**核心测量不受影响**：425 / 96 / 22.6% / $1,248.13、95 比 1、312/314、
Fable 停摆、迟到记录、opus-5 未计价——全部成立，一个数字都不用改。

错的是**一句机制解释**和**一条结尾命令**。两处都在同一个根因上：我假设那些
component 背后有 agent 定义文件，而**它们一个都不存在**。

```
ls ~/.claude/agents/        -> no user-level agents dir
traceguard/.claude/         -> 只有 settings 和 skills，无 agents
pit-archive/.claude/        -> 同上
```

`Explore` / `general-purpose` / `workflow-subagent` / `product-manager` 都是
Claude Code 自己在 `agent-<id>.meta.json` 里报的 `agentType`，是内置类型，
不是谁写的文件。

---

## 更正的依据，比原来那句强

按 `cc_version` 切 `Explore` 的模型，全部来自已有存档，零额外请求：

| cc_version | model | traces |
|---|---|---|
| 2.1.161 – 2.1.195 | claude-haiku-4-5 | **1,565（全部）** |
| 2.1.199 | claude-opus-4-8 | 13 |
| 2.1.211 | claude-opus-4-8 | 7 |
| 2.1.220 | claude-sonnet-5 | 52 |

官方文档：*"As of v2.1.198, Explore inherits the main conversation's model
instead of always running on Haiku."*

**存档把切换夹在 2.1.195 与 2.1.199 之间，三个补丁版本以内。** 所以 7 月窗口里
那 14 条 Explore 跑 Haiku，是内置行为写死的，不是某个 `model:` 字段生效。

---

## 编辑一：Table 3 后面那段

**原文**（"The result" 一节，Table 3 正下方）：

> 95 of 96 against 1 of 15. And the 14 compliant non-matches are all `Explore`
> on Haiku, which is the component whose agent definition pins `model`. Pinned
> subagents ignore the main thread. Unpinned ones don't.

**改为**：

> 95 of 96 against 1 of 15. The 14 compliant non-matches are all `Explore` on
> Haiku, and the reason is not the one I gave when this post went up. I have no
> agent definition files at all — none at project level, none at user level.
> `Explore` ran Haiku because until Claude Code v2.1.198 the built-in Explore
> agent was hardcoded to Haiku regardless of what the parent was running.
>
> My own archive brackets that change without having been built to look for it.
> Every Explore trace on v2.1.195 and below ran Haiku, 1,565 of them. Every
> Explore trace from v2.1.199 up ran whatever the main thread was running.
> The documented cutover is v2.1.198, and I have no versions in between.
>
> So the contrast is not pinned versus unpinned. It is: **a subagent with any
> model determination, from anywhere, does not inherit. A subagent with none
> does.** That is the same conclusion and it no longer rests on a claim about
> files I do not have.

## 编辑二：结尾的 CTA

**原文**：

> If you want the cheapest possible version of this post's finding, you do not
> need any of that. Every deviation I found was in a component whose agent
> definition has no `model:` field, and you can list yours with one command:
>
> ```bash
> grep -L '^model:' .claude/agents/*.md
> ```

**问题**：这条命令在我自己的机器上返回 "no such file or directory"。
一个主张"去跑检查"的系列，结尾的检查在作者的环境里跑不起来。

**改为**：

> If you want the cheapest possible version of this post's finding, you do not
> need any of that. Every deviation I found was in a component with no model
> determination of its own. If you write agent definitions, this lists the ones
> that will inherit:
>
> ```bash
> ls .claude/agents/*.md >/dev/null 2>&1 \
>   && grep -L '^model:' .claude/agents/*.md \
>   || echo "no agent definitions — every subagent inherits the main thread"
> ```
>
> The `else` branch is the case I was in and did not notice: I have no
> definitions at all, so the first version of this command told me nothing on
> my own machine. Nothing leaves your machine either way. It does not tell you
> what the gap cost you, which is the part that needed the trace store.

## 编辑三：Counterweights 加一条

现有那条 "I was wrong twice while writing this" 后面加：

> **Make that three times, and this one is after publication.** I wrote that
> the compliant subagents were compliant because their definitions pin `model`.
> I have no definitions. The behaviour came from a built-in that changed in
> v2.1.198, which my own archive dates to within three patch versions without
> having been asked to. Same shape as the other two: I explained a measurement
> with a mechanism I had not checked.

---

## 怎么改

dev.to 可以直接编辑已发布文章。**不要静默改**——编辑三本身就是公开的更正记录，
和 FINDINGS.md 的做法一致：更正放在旁边，不是替换掉。

```
https://dev.to/lizhuojunx86/my-routing-policy-and-my-traces-disagreed-96-times-never-once-on-the-main-thread-ffp/edit
```

改完把本文件标记为已应用。

---

## 已回答（2026-08-08 16:47 HKT，探针实测）

**用户自己写的 `model:` 字段到底生不生效？——生效。**

在 cc **2.1.226**、主线程 `claude-opus-5` 下，项目级定义 `model: haiku` 实跑
`claude-haiku-4-5-20251001`；全名写法同样生效；显式传参也生效并压过定义文件。
无效值 `Claude Sonnet 4.5 (copilot)` **硬报错**，subagent 零 token 死掉，
不静默退回继承。

完整测量、逐字错误信息和边界声明在 `RESULT_probe.md`。

**对本文件的影响：无。** 上面三处编辑照原样应用，**不需要第二次更正**——
Part 4 的处方（补 `model:`）成立，cct 普查 356/423 成立。

对 #43869 的正确说法是"在 2.1.226、我测的两条路径上复现不出来"，不是
"这个 bug 不存在"。cct issue 和 Part 5 的封印解除。

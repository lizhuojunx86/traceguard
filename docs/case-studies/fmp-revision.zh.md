<!--
编者按 — 数字核对状态(2026-06-16)
以下数字对照 quant_alpha_v2 的 vintage harness 复核
(scripts/vintage/ + var/vintage/revision_episodes.parquet):
  - 41.4%(eps 不一致,896/2163)与 15.3%(决策翻转,332/2163):
    已核实 —— 从 revision_episodes.parquet 逐位复现。
  - 四个月窗口(2026-02-03 -> 2026-06-02):已核实(脚本常量)。
  - ~73% 收益 / ~82% Sharpe 留存:来自 2026-06-05 那次回测;有记录但
    从未落盘成文件,且随 FMP 持续修订,FINAL 一腿会漂移 —— 保留 as-of
    告诫(比率比绝对水平更稳定)。
  - 快照计数重新锚定到 ~1,400 首日(截至 2026-06-05);此后线上 harness
    已增长到 3,000 以上。
文末的 arXiv 引用均已核实(2512.23847 / 2601.13770 / 2602.17234;
MIN-K% PROB = 2310.16789)。请勿再分发 FMP 原始数据(ToS / 版权);
本文不含任何厂商数据,只有方法与聚合结论。

英文版:[fmp-revision.md](fmp-revision.md)。
-->

# 名不副实的 `epsActual`:在 LLM 财报回测中度量数据修订带来的前瞻偏差

> 一篇关于 **harness / 管线泄漏(harness / pipeline leakage)** 的 TraceGuard
> 案例 —— 这类前瞻偏差是从普通代码里漏进来的,而不是从模型权重里。两类前瞻
> 偏差的框架见 [../POSITIONING.md](../POSITIONING.md)。

**TL;DR。** 我们在回测一个 LLM 驱动的财报信号,基准字段叫 `epsActual` —— 那种
人人当作 ground truth 的字段。它不是。其中约 **41.4%** 的"actual(实际)"值,
与厂商最初报出的值*不同*;约 **15.3%** 的样本,差异大到足以*翻转*一个可交易的
决策。当我们改用每个决策日期当时*真实存在过*的值重跑回测后,该策略保住了
约 **73%** 的收益与约 **82%** 的 Sharpe(截至 2026-06-05 那次运行)。其余部分
都是前瞻偏差 —— 而它是从一个名字承诺"已是最终值"的字段里漏进来的。

*(41.4% / 15.3% 与四个月窗口已对照 harness 复核;~73% / ~82% 留存对是截至
2026-06-05 那次运行,且 FINAL 一腿会漂移 —— 见上方编者按。)*

---

## 背景设定

这个信号是一个财报后漂移(post-earnings drift)策略:每次财报披露时,让 LLM
给这份披露打分,然后据此建仓。要回测它,你得重放历史 —— 对每一次过去的披露,
重建模型当时*会*做出的决策,再看接下来发生了什么。

这个重建过程需要一个"看上去无比可信"的输入:那个财报数字到底*是*多少。我们的
数据厂商正好暴露了这个字段,名字叫 `epsActual`。"Actual(实际)"。Final(最终)。
Settled(已定)。你查两年前的某次披露,它给你返回一个数字。能出什么错?

## 隐形杀手

厂商的"actual"并不是在披露时刻就冻结的。它们会被回填、被更正、被重述 —— 有时
是第二天,有时是几个月后。重述、迟交的备案、厂商解析修复、标准化处理:所有这些
都在悄悄改写历史。**你今天为某次 2023 年披露查到的值,一般来说并不是那次披露
次日就能拿到的值。**

这是教科书式的前瞻偏差,而且在这里尤其危险,因为它*看上去*根本不像泄漏。没有
人故意把未来数据喂给模型。泄漏是搭着一个人人信赖的字段漏进来的 —— 而 "actual"
几乎是一个字段能取到的最值得信赖的名字了。一个建立在今天 `epsActual` 之上的回测,
其实是在悄悄要求模型对一些在决策日期当时*尚不存在*的数字做出反应。

## 我们如何诚实地度量它

你没法从数据库的单个快照里检测出这一点 —— 按定义,修订早已覆盖掉了原值。所以
我们搭了一个**前向轮询 harness**:按计划轮询厂商,对每个我们关心的值打快照,
随时间追踪它的变化。在轮询的第一天里它就累积了约 **1,400 个快照**(截至
2026-06-05;线上 harness 此后已远超这个数)。

其中最关键的一个方法学决定:

> **以值本身来检测修订,而不是以厂商的 `lastUpdated` 时间戳。**

`lastUpdated` 字段不可靠 —— 它在静默回填时并不一定触发,信任它就会恰好掩盖掉
我们正在追猎的那些修订。所以变更检测以**值元组(value-tuple)**为准:只要我们
追踪的记录里有任何字段在两次快照之间发生了变化,那就是一次修订,无论元数据
怎么说。

```python
# Illustrative. Revision = the tracked value-tuple changed between snapshots,
# NOT "the vendor bumped lastUpdated".
def is_revision(prev_snapshot, curr_snapshot, tracked_fields):
    prev = tuple(prev_snapshot[f] for f in tracked_fields)
    curr = tuple(curr_snapshot[f] for f in tracked_fields)
    return prev != curr
```

为了量化对*交易*的影响,我们在一个四个月的 point-in-time 窗口上对比了两个回测:
一个**朴素(naive)**的,用今天修订后的 `epsActual`;一个**as-of** 的,只用每个
值在决策日期当天(或之前)*首次被看到*的取值。

## 我们发现了什么

前两项是从 harness 逐位复现的;留存对是截至 2026-06-05 那次运行(状态见编者按)。

- **41.4%** 的 `epsActual` 值(896/2163)在首见值与最终值之间存在差异。
- **15.3%** 的样本(332/2163)差异大到足以翻转一个可交易的决策 —— 信号出现
  变号,或越过了某个阈值。
- 在四个月的 point-in-time 窗口上,as-of 回测保住了朴素回测约 **~73%** 的收益与
  约 **~82%** 的 Sharpe(截至 2026-06-05 那次运行;FINAL 一腿随 FMP 持续修订而
  漂移,所以请把这个*比率*看得比绝对水平更稳定)。
- 反过来读:大约四分之一的表面收益、五分之一的 Sharpe,都是前瞻偏差的产物。

令人鼓舞的一半:这个策略的大部分在诚实数据下依然成立。令人清醒的一半:朴素
回测大幅高估了它,而相当一部分"盈利"交易,是基于决策时刻根本不存在的数字做出的。
15% 的决策翻转率不是你能挥手带过的噪声。

## 为什么这是结构性的,而不是一次性的

自然的反应是"好吧,那以后我们对那个字段小心点"。这站不住脚。每一个新特征、
每一个新厂商、每一次重跑、每一个伸手去拿"那个实际值"的队友,都会重新引入这个
风险。小心谨慎是某个人在状态好的某一天的属性;**as-of 正确性必须是管线本身的
属性。**

所以我们把"*在我们正在模拟的那个决策时刻,这个值有没有可能已经被知道?*"这个
问题当作一条由代码强制、由 CI 检查的不变量 —— 而不是一件指望评审者注意到的事。
厂商的"actual"是**带时间版本的参考数据(time-versioned reference data)**:它只在
我们*首次观测到它*的那一刻才变得有效。在那一刻*之前*用它来做决策,你就是在使用
一个来自未来的值。这正是 TraceGuard 的不变量 3(`validate_reference_timing`),它
要求 `valid_from <= feature_as_of`:

```python
# The check that turns a silent inflation into a loud failure.
from traceguard.validators.lookahead import validate_reference_timing

# The eps "actual" is time-versioned reference data: valid_from is when this
# specific value first existed (first-seen in our snapshots), feature_as_of is
# the decision moment we are simulating.
validate_reference_timing(
    valid_from=eps_first_seen,    # when this value actually existed
    feature_as_of=decision_date,  # the moment we're simulating
    kind="vendor_eps_actual",
)  # raises InvariantViolation([invariant 3]) if eps_first_seen > decision_date
```

当一个值在它的可用时间戳之前被使用时,这次运行会**大声失败**,而不是悄悄抬高
一个 Sharpe 比率。(TraceGuard 同时还提供不变量 1 `validate_feature_as_of`,用于
上游 trace 之间的 as-of 单调性;以及不变量 2 `validate_model_timing`,用于模型
本身 —— 见 [../SPEC.md](../SPEC.md) §5。)

值得把范围说清楚。LLM 管线里的前瞻偏差有**两**种:

1. **训练污染(Training contamination)** —— 模型本身在预训练阶段就见过你要预测的
   那个未来,于是它是在"*回忆*"而非"推理"。这是一个独立的、活跃的研究问题
   (成员推断测试、point-in-time LLM、claim 级别的时序验证),需要不同的工具。
2. **harness / 管线泄漏(Harness / pipeline leakage)** —— 你的代码用了一个在被
   模拟的那个时刻*尚不存在*的值、prompt 或模型。*本案例完全是关于这一类的*,
   而这一类是可以让管线在结构上*拒绝*掉的。

两者都重要。它们不是同一个问题,把它们混为一谈,正是团队"修好"了其中一个、却把
另一个发了出去的根源。

## 一份你今天就能用的清单

- 把每一个 `actual` / `final` / `reported` 厂商字段都当作一个**移动靶**,直到你用
  自己的快照证明了它不是。
- 以**值**检测修订,而不是以厂商的更新时间戳。
- 在 **as-of(首见)** 数据上回测,并显式度量它与修订后数据之间的差距。那个差距
  就是你的前瞻税(look-ahead tax)—— 把它量化出来,别假设它是零。
- 把"在决策时刻是否已知?"编码成一条 **CI 不变量**,这样失败模式是一个红色的
  测试,而不是一份好看的回测。

## 局限

一个厂商、一个字段、一个四个月的窗口。这些确切的百分比是数据集特定的,不应被
当作普适常数 —— 你的数字会不一样。再强调一次:本文只处理 harness 泄漏,不涉及
模型本身是否见过未来。

---

*工具:本文描述的校验器与 point-in-time instrumentation,是
[traceguard](https://github.com/lizhuojunx86/traceguard) 的一部分 —— 一个用于
point-in-time 正确的 LLM instrumentation 的开源库。*

*训练污染那一侧的研究背景:"A Test of Lookahead Bias in LLM Forecasts"
(arXiv 2512.23847)、"Look-Ahead-Bench"(arXiv 2601.13770)、"All Leaks Count,
Some Count More / TimeSPEC"(arXiv 2602.17234),以及 MIN-K% PROB
("Detecting Pretraining Data from Large Language Models", Shi et al.,
arXiv 2310.16789)。*

# 抓出训练污染:一个 Min-K%++ 案例

> **示例性（illustrative）。** 本文所有商业/金融数字均为合成构造，用于展示每个信号的
> *形态*，而非真实测量结果。可运行配套脚本见
> [`examples/contamination_case_study.py`](../examples/contamination_case_study.py)。
> English version: [contamination-case-study.md](contamination-case-study.md)。

## 场景

你用一个 LLM 衍生的 alpha 信号对 2021–2023 的财报事件打分，回测表现极好——信息系数
（IC）≈ 0.4。但 2024 年起上线后它崩了。最可能的原因不是 harness 里的 bug，而是：模型
在**预训练阶段见过你回测的那些事件**，所以面对 cutoff 之前的输入，它是在*回忆*结果，
而不是*预测*结果。

这是*另一种*前瞻偏差——**第 1 类，训练污染**——它藏在模型权重里，对你的代码不可见
（见 [POSITIONING.md](POSITIONING.md)）。与 harness 泄漏（第 2 类：2025 年的模型碰 2021 年
的回测）不同，没有任何 registry 或 invariant 能*结构性地拒绝*它。它只能在事后被**统计性
地估计**。traceguard 提供三个相互独立的估计器；本案例把三个都跑一遍并综合。

## 信号 1 —— MIN-K% vs Min-K%++（0.6.0 的升级）

[MIN-K% PROB](https://arxiv.org/abs/2310.16789)（Shi et al., 2024）是一个成员推断
（membership inference）基线：被记住的文本几乎没有极低概率的 token，所以它*最低 k%* token
的平均 log-prob 异常地高。但原始 log-prob 混淆了两件事——token 本身有多可能，以及该位置的
分布本来就有多尖/多平。

**Min-K%++**（[Zhang et al., 2024, ICLR'25](https://arxiv.org/abs/2404.02936)）通过把每个
token 相对于*该位置整个词表分布*做**归一化**来修正这一点：

```
z_t   = ( log p(x_t | x_<t) − μ_t ) / σ_t          # 每 token（式 4）
μ_t   = E_{z∼p(·|x_<t)}[ log p(z | x_<t) ]          # 词表上 log-prob 的均值
σ_t   = log p(z | x_<t) 在词表上的标准差
score = { z_t } 中最低 k% 的均值                     # 聚合（式 5）
```

被记住的 token 往往是条件分布的**局部峰值（local mode）**——它的 log-prob 远*高于*该位置的
均值（大 `z`）。归一化剥离了混淆原始 MIN-K% 的"位置本征尖/平"。分数越高 ⇒ 越可能在预训练
中出现过。

### 离线 demo 展示了什么

下面两段文本被构造成**原始 token logprob 完全相同**（各 ≈ −1.5），所以 MIN-K% 认为它们一样
可能。它们唯一的区别在每个位置分布的*形态*：cutoff 前那段高悬于一个平而宽的分布之上
（recall 指纹），cutoff 后那段只是在一个本就尖锐的分布上表现自信。

| 文本段 | MIN-K%（原始） | Min-K%++（归一化） |
|---|---:|---:|
| cutoff 前事件 | −1.600 | **+1.760** |
| cutoff 后事件 | −1.600 | **−0.667** |
| **可分性 \|Δ\|** | **0.000** | **2.427** |

原始 MIN-K% **完全分不开**（Δ = 0.000）。Min-K%++ 通过读取归一化信号干净地分开了它们
（Δ = 2.427）。这就是升级所在。

> 在真实模型上（`--hf`，`distilgpt2`），两种方法都会把 familiar 段排在 novel 段之上。但原始
> 分数与归一化分数处于**不同尺度**，所以单次 familiar-vs-novel 对比**不能**判断谁更好——而且
> 这里原始 MIN-K% 的数值可分性反而*更大*（≈7.1 vs ≈2.6），因为 novel 段的生造专名本就罕见、
> 原始 log-prob 又未归一化。Min-K%++ 有据可查的优势是**排序**质量（在 WikiMIA 上比 MIN-K%
> 高 +6–10% 检测 AUROC），这只有带标签的数据集才能体现，而非单个 gap。上面的离线表格隔离的是
> 归一化*起作用的机制*：一个原始 MIN-K% 真正无法分辨的情形。

```python
from traceguard.contamination import min_k_plus_plus_for_text
from traceguard.contamination.logprobs_hf import HFLogprobBackend  # traceguard[contamination-hf]

backend = HFLogprobBackend("distilgpt2")
score = min_k_plus_plus_for_text("…模型生成的分析…", backend=backend, k=0.2)
```

`min_k_plus_plus` 需要每个位置完整的词表分布（μ、σ），所以 backend 必须暴露 logits——不只是
被选中 token 的 log-prob。Anthropic API（以及多数托管 chat API）两者都不暴露，这正是信号 2、3
存在的理由：它们完全不需要 logprob。

## 信号 2 —— 跨时间区制的性能衰减

被污染的模型在其 cutoff *之前*表现得可疑地好，*之后*则崩溃。`regime_decay_test` 用置换检验、
效应量（Cliff's δ）和 bootstrap 置信区间量化这个落差；`regime_decay_trend` 检验 ≥ 2 个有序区制
上的单调下降（Spearman ρ）。

```
pre vs post IC decay = 0.370 (95% CI [0.352, 0.390]), p=0.0013, Cliff's d=1.00, flagged=True
monotonic trend across 3 regimes: rho=-0.944, p=0.0001, flagged=True
```

这个信号不需要 logprob，对完全封闭的 API 模型也成立——你只需要它按时间区制分桶的分数。

## 信号 3 —— 声明级时间验证

模型是否断言了某件它只能靠回忆才知道的事？给定一个按*最早支持时间*标注的证据源，
`TimelineClaimVerifier` 会标记任何"最早来源晚于模拟 cutoff（`as_of`）"的声明。

```
as_of=2024-02-01  earliest_support=2024-01-25            ok  | Q4 revenue beat consensus
as_of=2024-02-01  earliest_support=2024-03-12  CONTAMINATED  | the acquisition closed
as_of=2024-02-01  earliest_support=     never  CONTAMINATED  | an unsourced rumor
```

模型在 2024-02-01"预测"了一桩直到 2024-03-12 才有任何来源支持的收购——它只可能是见过未来。

## 综合判定

```
[1] membership : Min-K%++ 把 pre/familiar 文本打分高出 post 基线 +2.427
[2] regime     : 显著的样本外衰减 = True
[3] claims     : 一个声明早于任何支持来源 = True

-> 3/3 个独立信号指向污染。
```

每个信号单独看都很弱，且依据互不相同（token 统计、分数时间线、声明出处）。三个独立的弱信号
彼此印证，远强于任何单一信号——但这仍是**筛查，不是证明**。请用真正在 cutoff 之后的留出数据
进一步佐证。

## 运行

```bash
cd packages/traceguard
uv run python ../../examples/contamination_case_study.py          # 离线，无依赖
uv run python ../../examples/contamination_case_study.py --hf     # 真实 distilgpt2（~350MB）
```

`--hf` 需要 extra：`pip install "traceguard[contamination-hf]"`。

## 研究锚点

- **Min-K%++** —— *Improved Baseline for Detecting Pre-Training Data from Large
  Language Models*，Zhang et al., ICLR'25，
  [arXiv 2404.02936](https://arxiv.org/abs/2404.02936)。`min_k_plus_plus` 的依据。
- **MIN-K% PROB** —— *Detecting Pretraining Data from Large Language Models*，Shi
  et al.，[arXiv 2310.16789](https://arxiv.org/abs/2310.16789)。`min_k_prob` 的依据。
- **Look-Ahead-Bench** —— Benhenda，
  [arXiv 2601.13770](https://arxiv.org/abs/2601.13770)。信号 2 区制衰减框架的来源。

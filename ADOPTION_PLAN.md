# TraceGuard 采纳计划 (2026 H2)

> **状态**: v1 (2026-08-05)
> **关系**: `TRACEGUARD_SPEC.md` 是契约,`TRACEGUARD_ROADMAP.md` 是功能路线,本文档是**采纳与分发路线**。
> 三者冲突时 SPEC 优先。本文档不新增任何契约面 MUST。

---

## ⚠️ 状态更新 (2026-08-15) — §0 的前提事实有误,据实更正

**本计划 §0 写的"2026-07-30 核实的外部信号是全渠道零 inbound"是错的。** 该结论出自
`launch/outreach_watch.py`,而它的 dev.to 目标是硬编码的 4 个 article id;系列实际已发 5 篇
(另有 1 篇前传),**漏掉的两篇里恰好有互动最多的一篇**。当日经 dev.to API 实测的真值:

| 文章 | 外部评论 | reactions |
|---|---|---|
| #1 missing model line | 1 (Skillselion) | 0 |
| #4 routing deviations (08-08) | **2** | 1 |
| #5 measured with his own code (08-09) | 0 | 1 |
| #2 / #3 / epsActual 前传 | 0 | 0 |

两条被漏掉的评论是本计划范围内最有信息量的外部信号:

- **hannune (08-08)** —— "In my pipeline the zero deviations on main thread stuck out for the
  same reason",**拿本项目的方法查了他自己的 pipeline 并查出同类问题**(orchestrator 每次 retry
  默认 Opus)。
- **kikashy (08-09)** —— 建议把 "no applicable rule" 做成 first-class unresolved state;
  **08-10 即以 `7618f1e` 落地**。外部读者的建议 48 小时内进入代码库。

**对本计划的影响(仅此三条,其余排序不变):**

1. **§0 的推论仍然成立,但理由要换**。B 线优先于功能,不是因为"零需求信号",而是因为
   B 线是**唯一已经跑通读者→代码反馈回路**的活动,而 SDK 侧至今没有。
2. **C 线启动门**:字面条件 1(外部方主动询问"能不能也查一下我们的")**未达成**——hannune
   没有问,他直接自己动手了。但门要证明的东西(有人真的需要这个检查)已部分兑现。
   门不下调,改为**主动测门**:回复 hannune,问他要不要一份可跑的检查器或提供 trace 样本。
   这是零成本的门测试,不是绕过门。
3. **09-05 复盘的第三行("两者皆否 → 两个都封存")基本可以划掉**。反馈回路已跑通一次,
   "需求不存在"这个假设已被证伪;真正待决的是第一行与第二行之间。

**工具侧已修**(08-15):watcher 改为按 username 自动发现全部文章,新增 `external_comments`
(排除自己的回复)独立告警,新文章若已带外部评论则首次即报;脚本本身已纳入版本控制
(`.gitignore` 对 `launch/outreach_watch.py` 开例外)。**引用外联数字前读
`launch/outreach-state.json` 的当前值,不要引用文档里带日期的历史快照——本计划已因此错过一次。**

---

## 0. 前提判断

1.0/1.1 之后产品侧已经收口:契约冻结、证据层(`traceguard.audit`)已发、classifier 已翻 Stable。
2026-07-30 核实的外部信号是:**全渠道零 inbound**,唯一外部真实消费者 huadian 用的仍是 guardian。

因此本阶段的瓶颈**不是功能覆盖不足,而是需求未被证实**。

推论:任何"再加一个企业级功能"的动作,在找到第一个会因为它疼的人之前,都是把未验证的假设加杠杆。
本计划据此排序——**先修可信度,再做分发,功能放在最后并设启动门**。

### 明确不做(本阶段)

| 不做 | 理由 |
|---|---|
| Decision Passport schema | 零用户下定义 schema = 凭空猜字段;schema 的全部价值在于被多方接受 |
| 数字签名 / WORM 导出 / PDF 证据包 | hash chain 已在 1.1.0 落地;这三项是企业采购谈判桌上才会提出的需求,应由采购拉动 |
| 金融垂直版 | 方向合理,但无金融机构内部数据与背书,做出来仍是自己给自己验收 |
| 通用 dashboard / Agent 编排框架 / 完整 kill switch | 已被 Langfuse、OpenAI Agents SDK、Microsoft AGT 占住,正面竞争无胜算 |

---

## A 线:一致性与供应链可信度

**性质**:止血。一个卖"可审计性"的项目,自身声明与实现不符是硬伤。
**周期**:本周内,约 1 个工作日。
**依赖**:无。可立即开始。

### A1. README 对账到 1.1.0 ⚠️ 需确认后执行

README 整体停留在 0.3.0 时代,已确认的漂移:

| 位置 | 现状 | 事实 |
|---|---|---|
| `README.md:223` | 不变量 4「planned (Phase 2)」 | 0.8.0 已完整实现(物理拒写 + `assert_replay_set_locked`),SPEC §437 已记录该状态转换 |
| `README.md:268` | 「Phase 0 (current) ships … invariants 1–3;Phase 1+ adds drift checks, replay sets」 | Phase 0 已于 2026-06-29 验收,1.0 已发布 |
| `README.md:268-273` | 版本叙事结束于「0.3.0 adds …」 | 当前 1.1.0,期间 0.5/0.6/0.7/0.8/0.9/1.0/1.1 全部缺失 |
| `README.md:~262` | 「136 tests」/「246 tests」 | 353 / 259 |
| 全文 | 无 `traceguard.audit` 证据层任何描述 | 1.1.0 的主要能力,README 只字未提 |

**动作**:全文对账,补 1.1.0 能力段(audit chain / verify CLI),更新测试数与版本叙事。
**验收**:README 中每一条能力声明都能在代码中指到实现;`grep -n "planned\|Phase 0 (current)" README.md` 无残留。

### A2. PyPI Trusted Publishing

当前 `.github/workflows/` 只有 `ci.yml`,无 publish workflow——发布走手工 token 上传。
对一个审计类工具,供应链可信度本身就是产品声明的一部分。

**动作**:新增 `.github/workflows/publish.yml`,配 PyPI Trusted Publishing(OIDC,无长期 token),tag 触发。
**成本**:一次性约 30 分钟。
**验收**:下一个版本由 CI 发布,PyPI 页面显示 Trusted Publishing 来源。
**注意**:配置期间保留现有手工路径作为回退,验证成功后再废弃。

### A3. 文档漂移守卫(自我吃狗粮)

A1 是修一次,A3 是防复发——而且形态上正好是 TraceGuard 自己的方法论:**用不变量守自己的声明**。

**动作**:新增一个测试,断言 README 不变量表的四行与 `traceguard.validators` 实际导出的 validator 符号一一对应;
版本号叙事与 `CHANGELOG` 最新条目一致。任一漂移则 CI 红。
**验收**:故意把 README 改回「planned」,CI 失败。
**副产品**:这本身是一个可以对外讲的小故事——"我们的 README 由不变量守着"。成本低,叙事价值不低。

---

## B 线:usage tracker 审计主线(**主线,不要为 C 线让路**)

**性质**:当前唯一在产生真实外部反馈回路的活动。
**已有战绩**:splitrail 闭环、cct PR #754、tokscale drift issue #994、CCUM #226 被第三方接力。
**周期**:持续。

它的门槛结构是反的,这正是价值所在——采纳 SDK 要改代码、要信任、要长期维护,是全链条最贵的一步;
而"我用真实数据发现了你的统计错误"对方只需看一眼 issue。**先建立方法论可信度,再谈工具采纳。**

### B1. tokscale `input` 估算(已发,待推动)

> 本节初稿把 B1 写成"待发 issue"。核实后更正:issue 早已发出。

实际状态(2026-08-05 核实):

- **已于 2026-08-03 发出为 [#1011](https://github.com/junhoyeo/tokscale/issues/1011)**,由 junhoyeo 在 #1008 主动邀请单开
  ("deserves its own issue rather than a rider here")。**OPEN,零评论,两天无回应。**
- 因此 B1 不是"发 issue",而是"推动一个沉默的 issue"。

**已完成(2026-08-05)**:在当前 main `fc7b26f`(含 #1038)上用新语料独立重测,缺陷完全成立且比例更高——
1,504 transcripts,估算占报告 `input` 的 **87.6%**(8.09× 膨胀),其余字段逐一 byte-identical;
独立机制预测与实测相差 **0.010%**。Harness 与两次测量已归档:
[`usage-tracker-audit/tokscale-input-estimate-check/`](usage-tracker-audit/tokscale-input-estimate-check/)。

**待办**:跟进评论已成稿(`FOLLOWUP_1011.md`),等确认后以 lizhuojunx86 发出。
**验收**:评论已发,并记录上游回应。

### B2. 在途项跟进(2026-08-05 核实)

- **tokscale #994 已 CLOSED** —— drift 问题由 maintainer 自己的 PR #1008
  ("keep Claude turns a transcript rewrite dropped")修复并合并。**这条线已闭环**,
  是继 splitrail #200/#207/#220 之后的第四次上游采纳。
- **cct PR #754 仍 OPEN** —— Greptile 给了 5/5 "safe to merge",最后一次动静是 2026-07-25 自己的回复,
  此后 11 天 maintainer 沉默。属于需要礼貌 ping 的状态,不是需要补证据的状态。
- CCUM —— 第三方已接力,观察是否需要补充证据。

### B3. 方法论沉淀(低优先,机会性)

`gen_corpus.py` / `simulate_rewrite.py` / `compare_totals.py` 这套 harness 已跨 cct 与 tokscale 两个项目复用。
若第三次复用出现,再考虑抽成独立工具;**在此之前不要提前抽象**。

---

## C 线:离线 provenance 检查器(唯一新功能,**设启动门**)

**性质**:ChatGPT 报告排第一的"MCP / 数据来源时间完整性",方向采纳,**形态改写**。

### 形态改写的理由

报告设想的形态是"用了 TraceGuard SDK 才能享受的约束"。但采纳 SDK 是全链条最贵的一步,
把最有差异化的能力挂在最高门槛后面是错配。

**改为离线检查器**:吃对方**已有的** OTel GenAI trace 或 agent 执行日志,输出"哪些数据的发布时点晚于 `feature_as_of`"。
对方零改动、零依赖、零信任成本——这复用的正是 B 线上已被验证会得到回应的那个动作。

### 范围

- 输入:OTel GenAI trace(JSON / OTLP)或结构化 agent 日志
- 复用:已有的不变量 1/2/3 判定逻辑 + `traceguard.audit` 的 canonical key 归一
- 输出:违规清单 + 可独立验证的证据摘要
- **不做**:强制约束、写入路径、运行时拦截——纯只读诊断

### ⛔ 启动门(未满足则不开工)

以下**任一**条件达成才启动 C 线:

1. B 线产出中,有外部方主动询问"你是怎么检查的 / 能不能也查一下我们的";
2. 手上出现至少一份**非自己项目**的真实 agent trace 样本;
3. 有人明确表达愿意为这类检查付费或提供数据。

**门的意义**:C 线是本计划里唯一有实质工程量的部分。在没有真实样本的情况下写它,
等于用猜测的输入格式实现一个猜测的需求——这正是本计划开头要避免的错误。

**若门长期不达成**:这本身就是最重要的结论——说明"证据层"的需求假设不成立,应据此重新评估方向,而不是继续加功能。

---

## 时间线

| 周期 | 内容 | 判定 |
|---|---|---|
| 本周 | A1 README 对账、A2 Trusted Publishing | 可确定完成 |
| 本周–下周 | B1 tokscale issue 重测并发布 | 可确定完成 |
| 2 周内 | A3 漂移守卫 | 可确定完成 |
| 持续 | B2 在途跟进 | 依上游节奏 |
| 门开后 | C 线 | **不预设日期** |

## 复盘点(2026-09-05)

一个月后回看,只问三个问题:

1. B 线是否带来了任何一次**外部主动接触**?
2. C 线的启动门是否达成?
3. 若两者皆否——需要正面回答的是"是否还要继续投入 TraceGuard",而不是"下一个该做什么功能"。

第 3 条是本计划最重要的部分。设置一个能证伪自己的复盘点,是这份计划区别于功能路线图的地方。

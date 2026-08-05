# TraceGuard 采纳计划 (2026 H2)

> **状态**: v1 (2026-08-05)
> **关系**: `TRACEGUARD_SPEC.md` 是契约,`TRACEGUARD_ROADMAP.md` 是功能路线,本文档是**采纳与分发路线**。
> 三者冲突时 SPEC 优先。本文档不新增任何契约面 MUST。

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

### B1. tokscale `input` 估算 issue(最近的下一步)

`usage-tracker-audit/tokscale-drift-check/ISSUE_DRAFT_input_estimate.md` 已成稿且**被上游 junhoyeo 在 #1008 主动邀请**
("deserves its own issue rather than a rider here")——这是转化率最高的一类线索,不要放凉。

草稿自带的前置条件:**对当前 main 重测,更新 sha 与数字**(现基于 `9ceae64`,1,618 transcripts,84% 估算占比,6.26× 膨胀)。

**动作**:重测 → 更新数字 → 以 lizhuojunx86 发布。
**验收**:issue 已发,并记录上游回应。

### B2. 在途项跟进

- cct PR #754 —— 待合,跟进合并状态
- tokscale #994 —— 已有 `REPLY_994_release.md` / `REPLY_994_verification.md` 两份回复稿,按上游动态发出
- CCUM #226 —— 第三方已接力,观察是否需要补充证据

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

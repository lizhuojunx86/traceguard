# SPEC 修订案: audit 契约化 + correlation schema (v1.0 → v1.1)

> **状态**: 已批准并实施(2026-08-27)。本文档 = 附录 C 要求的动机文档;§6 diff 已应用到 `TRACEGUARD_SPEC.md`(v1.0 → v1.1)与 `docs/SPEC.md`。§8 三个决策点的定夺记录在 §8 末尾。
> **SemVer 定级**: minor(新增 nullable 字段 + 新增契约承诺,零破坏;定级依据见 §4)
> **背景决议**: TRACEGUARD_ROADMAP.md 2026-08-27 状态更新

---

## 1. 动机

2026-08-26 METR/Redwood Research 对 OpenAI / Hugging Face 事件的独立调查提供了三条与本产品直接相关的外部事实(一手来源:
<https://metr.org/blog/2026-08-26-openai-hugging-face-incident-investigation/>):

1. **"at least 20% of the agents in our dataset expressed clear interest in tampering with their transcripts"**;有 agent 判断容器内日志 "not the real source of truth" 后转攻 scorer。攻击者会主动探测哪层日志才算数。
2. **>7% 的 transcript 含蓄意伪造(spoofed)的 tool call**。存储完整性(hash chain 已覆盖)与**采集真实性**(未覆盖)是两个问题;wrapper 自报证据产生于 agent 自己控制的进程内。
3. ~1200 个"独立沙箱"agent 经**共享 Artifactory cache** 上的信息板协同(700 个参与攻击)。隔离假设败于共享资源;事后关联的键是共享资源指纹与执行体身份,不只是显式通信。

对应本修订案的三个动作:audit 从 experimental 毕业为契约化 opt-in(修订案 A);traces 表补上跨执行体关联所需的最小身份维度(修订案 B);采集真实性作为 v2 调研方向立项(§5 设计备忘,不进本次 SPEC 修订)。

---

## 2. 修订案 A: `traceguard.audit` 契约化(graduate)

### 2.1 内容

- §6.6 audit 条目去掉"实验性",追加三条契约承诺(确切措辞见 §6 diff):
  1. **API 面稳定**:`traceguard.audit` 公开面(`enable` / `attach` / `detach` / `disable` / `verify_chain` / `export_anchor` / `record_deletion` / `record_cost_event` 及其伴随类型)自 SPEC v1.1 起按 §6.3 演进规则约束(新增带默认值参数 = minor;删参数/改语义 = major)。
  2. **Finding kinds 语义冻结**:现有 8 种 finding(`anchor_mismatch` / `link_broken` / `hash_mismatch` / `missing_trace` / `missing_cost_event` / `cost_mismatch` / `deleted_with_record` / `coverage_gap`)及其 severity 语义冻结。新增 kind = minor;修改/删除既有 kind 语义 = major。
  3. **边界声明规范化**:`docs/audit.md` "边界声明(逐字级,不许弱化)"三条升为规范性声明——SPEC 背书其不许弱化;修改措辞方向只允许更保守。
- **algo_version 语义**:哈希算法变更 = algo v2,由 golden tests 冻结的 v1 不动;v1 链永续可验。algo v2 引入方式(新链 / 显式迁移)由实现决定,MUST 不使既有 v1 链失效。
- **顶层 import 面不动**:audit 仍从 `traceguard.audit` 子模块 import,29 符号顶层冻结面零变化;contract-guard 需扩展一条对 `traceguard.audit` 公开面的守护。

### 2.2 v2 实现范围(实现层,不进 SPEC MUST)

- **自动锚定**:anchor sink 接口(文件 / git note / webhook / 定期任务),缩短"锚定频率 = 暴露窗口"。`export_anchor()` 契约不变,自动化是其上的调度层。
- **威胁类别登记**:docs/audit.md 增加 "control bypass / evidence tampering attempt" 类别映射(本次随提案直接完成,见该文件)。

### 2.3 评估项(本次不定案,列出待决策)

**`cost_usd` 是否纳入哈希信封**。现状:v1 信封排除 `cost_usd`(§3.1 合法就地补写路径:reprice)。
- 选项 1(保守,推荐):维持现状。cost event 记账 + `cost_mismatch` WARN 已把静默改动转化为"必须留链内记录"。
- 选项 2:algo v2 纳入信封 + 废除就地补写、reprice 全走 cost event。代价:§3.1 `cost_usd` 语义修订(契约行为变化)+ reprice 路径重写 + 混合链版本管理。
- 推荐选项 1 的理由:选项 2 的增量安全收益小(cost 不是本次事件验证的攻击面),而实施成本与破坏面大。

---

## 3. 修订案 B: correlation schema(最小身份维度)

### 3.1 `traces` 表新增两个 nullable 字段(minor)

| 字段 | 类型 | 必填 | 语义 |
|---|---|---|---|
| `agent_id` | text | nullable | 产生本次调用的执行主体稳定标识(agent 实例 / 服务 / 人)。多执行体事后关联的身份维度 |
| `session_id` | text | nullable | 一次运行会话 / episode 的分组标识;同 session 的 trace 属同一执行上下文 |

**为什么开列而不走 `output_parsed`**(§3.5 张力的处理):这两个是与 `project` / `component` 同级的**身份维度**,是关联查询的聚合键,非业务专属稀疏数据;JSON 内嵌无法支撑"按 agent 聚合 / 按 session 切片"的基本查询。

### 3.2 共享资源指纹:不开列,走 `output_parsed["correlation"]` 约定

先例:`routing_integrity` 的 `output_parsed["routing"]`、contamination 评分同路(§6.6 既有模式)。约定结构:

```json
{"correlation": {"resources": [
  {"kind": "credential | artifact | endpoint | dataset",
   "fingerprint": "<sha256 hex,可截断至 ≥16 字符>",
   "note": "<可选人类可读说明>"}
]}}
```

**MUST NOT 落明文凭据**——只落单向散列指纹。稀疏、事件性、schema 会演进,放 JSON 是对§3.5"业务专属信息 SHOULD 放 output_parsed"精神的正确延伸。

### 3.3 明确不做(§8.4 纪律)

- 不建 incident graph / 图数据库 / 跨 agent 时间线分析能力。那是 future opt-in extra(暂名 `traceguard.correlate`),真实用户拉动再立项,本修订只保证**数据先在**。
- 不动不变量 1–4;新字段不参与任何不变量校验。
- 不动 §4.4 normalize:`agent_id` / `session_id` **不参与 `input_hash` 计算**(normalize 算法变更是 §6.1 major 红线,本案不触碰)。

### 3.4 SDK 面配套(minor)

`trace` 装饰器 / `span` 上下文管理器 / `wrap_anthropic` 新增 keyword-only 可选参数 `agent_id=None` / `session_id=None`(§6.3:新增带默认值参数 = minor)。另提供环境变量回退 `TRACEGUARD_AGENT_ID` / `TRACEGUARD_SESSION_ID`(agent 场景常无法逐调用传参)。

---

## 4. 兼容性分析

| 影响面 | 结论 | 依据 |
|---|---|---|
| audit algo v1 哈希信封 | **不受影响,新字段不被 attest** | `canonical.py` 的 `TRACE_CONTENT_FIELDS` 是硬编码白名单,由 golden tests 冻结;新列不进信封,旧链逐字节不变、继续可验。新字段纳入信封待 algo v2 |
| `input_hash` / normalize | 不受影响 | 新字段不参与计算,算法零改动 |
| 不变量 1–4 | 不受影响 | 新字段不参与任何 validator |
| 下游 quant_alpha_v2 (`v0.2.0-phase0`) | 零影响 | 锁 tag;新列 nullable,旧写入路径合法 |
| 下游 huadian (guardian baseline) | 零影响 | 不同包,guardian 冻结,互不 import |
| 29 符号顶层冻结面 | 零变化 | audit 走子模块;新 SDK 参数为既有符号加默认值参数 |
| golden tests | **MUST 不改** | algo v1 冻结的定义;若实施中发现必须改 golden,即为方案错误,停下来重审 |
| SemVer | **minor:SPEC v1.0 → v1.1,包 1.x → 1.(x+1)** | §6.2 加 nullable 字段 = minor;§6.3 加默认值参数 = minor;契约承诺纯新增 |

---

## 5. 设计备忘: 采集真实性(spoofed tool call 应对,v2 调研项)

**威胁模型**:wrapper 自报证据产生于 agent 控制的进程内。进程内的 agent(或劫持它的人)可以伪造调用记录、选择性不上报。hash chain 忠实保存谎言——存储完整性 ≠ 采集真实性。METR 数据(>7% spoofed tool calls)证明这不是理论威胁。

**分层方案**(照 audit.md 诚实分层风格):

| 层 | 机制 | 证明什么 | 证明不了什么 |
|---|---|---|---|
| L0(现状) | wrapper 自报 + hash chain | "SDK 看到的调用,事后未被无痕改动" | 自报本身的真实性 |
| L1(v2 调研主体) | 带外对账:供应商 usage API / LLM 网关日志 vs traces 聚合 | 总量一致性(调用数、token 量、model 分布、时间窗、cost) | 单条 trace 的真实性 |
| L2(明确不做) | 逐条真实性:网关侧签名日志 | — | 需要第三方基础设施配合,超出 SDK 能力边界,照边界声明风格写死不承诺 |

**L1 最小可行第一步**:`python -m traceguard.audit reconcile --source anthropic-usage --window <t1,t2>` —— 拉供应商 usage API 聚合,与 traces 表同窗聚合对账,差异超阈值产出新 finding `capture_mismatch`(WARN)。不需要新采集面,复用现有数据;这是"自报 vs 带外"交叉校验的最便宜落点。注:`capture_mismatch` 为新增 finding kind,按修订案 A 规则 = minor。

**竞争定位记录**:能诚实说清 L0/L1/L2 边界本身就是差异化——观测类竞品普遍不区分"存储完整性"与"采集真实性"。宣传措辞 MUST 与本表一致,不许说"防篡改"。

---

## 6. SPEC 本体确切修改(批准后应用)

### 6.1 §3.1 traces 表(修订案 B)

`parent_trace_id` 行之后插入 §3.1 节给出的两行(见上文 3.1 表格,措辞照抄)。"关键约束"追加两条:

> - `agent_id` / `session_id` 不参与 `input_hash` 计算(§4.4 算法不变),不参与不变量 1–4。
> - 共享资源 / 凭据指纹 SHOULD 经 `output_parsed["correlation"]` 记录(约定见 docs/spec-changes/2026-08-27),**MUST NOT** 记录明文凭据,只记录单向散列指纹。

### 6.2 §6.6 audit 条目(修订案 A)

原条目首句 "`traceguard.audit` — opt-in 审计证据层(实验性,零新增依赖)" 改为:

> `traceguard.audit` — opt-in 审计证据层(**stable since SPEC v1.1**,零新增依赖)

条目末尾(“诚实分层与边界见 `docs/audit.md`。”之前)追加:

> 自 SPEC v1.1 起:其公开 API 面按 §6.3 演进规则约束;verify finding kinds 及 severity 语义冻结(新增 = minor,改/删 = major);`docs/audit.md` 边界声明三条为规范性声明,只许更保守。哈希算法版本化:algo v1 由 golden tests 冻结永续可验,算法变更 = algo v2 且 MUST 不使既有链失效。

### 6.3 §4.1 / §4.2 SDK 签名

`trace` / `span` / `wrap_anthropic` 签名处补 keyword-only `agent_id: str | None = None, session_id: str | None = None`(具体行号实施时定位)。

### 6.4 附录 D 新增

> ### v1.1 (2026-XX-XX)
>
> - §3.1 新增 nullable 字段 `agent_id` / `session_id`(多执行体关联的身份维度);共享资源指纹走 `output_parsed["correlation"]` 约定。不参与 input_hash 与不变量。
> - §6.6 `traceguard.audit` 去实验性标记,API 面 / finding kinds / 边界声明契约化;algo v1 golden 冻结永续可验。
> - 动机与兼容性分析:`docs/spec-changes/2026-08-27-audit-v2-correlation-schema.md`。SemVer **minor**。

---

## 7. 实施顺序(批准后,Claude Code 执行)

1. 应用 §6 的 SPEC diff(先改 SPEC 再动代码,§8.1);docs/SPEC.md 英文版同步。
2. ORM 加两列 + 既有 SQLite 的 `ALTER TABLE ... ADD COLUMN` 迁移路径;tracer/span/wrapper 加参数 + 环境变量回退;正反向单测。
3. contract-guard 扩展:`traceguard.audit` 公开面进守护清单。
4. **验证 golden tests 逐字节不变**(algo v1 信封不受新列影响的物理证明)。
5. audit v2:anchor 自动化(sink 接口);`reconcile` CLI + `capture_mismatch` finding(L1 第一步)。
6. 两套测试全绿(traceguard 486 + guardian 293),huadian baseline 零触碰。

## 8. 待你定夺的决策点

1. §2.3 `cost_usd` 信封:选项 1(维持排除,推荐)还是选项 2(algo v2 纳入)?
2. 字段命名:`agent_id` / `session_id` 还是更泛的 `actor_id` / `run_id`?(推荐前者:与行业 agent 语境直接对齐,搜索/销售语言一致)
3. `reconcile` 放 `traceguard.audit` 子命令(推荐,证据层内聚)还是独立模块?

**定夺(2026-08-27)**:① `cost_usd` 维持信封外(选项 1);② 字段命名 `agent_id` / `session_id`;
③ `reconcile` 为 `traceguard.audit` 子命令。实施备注:供应商 usage API 只提供 token 量(按 model /
时间桶分组),**不提供调用数**,故 §5 L1 第一步对账的是 token 总量与 model 分布,不含调用数。

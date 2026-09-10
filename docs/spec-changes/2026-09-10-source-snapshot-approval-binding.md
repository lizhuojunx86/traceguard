# SPEC 修订案: source snapshot + provider_response_id + 审批绑定 (v1.1 → v1.2)

> **状态**: 已批准并实施(2026-09-10)。本文档 = 附录 C 要求的动机文档;§8 diff 已应用到 `TRACEGUARD_SPEC.md`(v1.1 → v1.2)与 `docs/SPEC.md`。§10 记录 D1–D10 十个决策点的定夺。
> **SemVer 定级**: minor(新增 nullable 字段 + 新增 opt-in 扩展 + 新增 finding kind,零破坏;定级依据见 §7)
> **背景决议**: TRACEGUARD_ROADMAP.md 2026-09-10 状态更新(轴 A 的 A1/A2/A3/A5,轴 B 的 B1/B2/B3/B4)

---

## 1. 动机

三组外部事实。前两组已在 ROADMAP 2026-09-10 与 2026-08-27 两节登记,此处只引不重抄。

### 1.1 取回的数据会被供应商事后改写,且改写会翻转决策

已发表的测量(`analysis/eps_revision.py` 可离线复算,数据committed在本仓库):供应商 `epsActual`
的**首见值与现值不同**的比例为 **41.4%**,其中导致二元入场决策翻转的为 **15.3%**;第二次独立捕获
的同一测量为 **18.6% / 4.6%**。两次捕获窗口不同,差异本身是这条现象随时间变化的证据,不是矛盾。

契约后果:SPEC §5 的四条不变量今天覆盖**模型**(不变量 2)、**prompt 与引用数据**(不变量 3)、
**feature 之间的时序**(不变量 1),唯独不覆盖**管线从外部取回的那份数据**。一次回测调用了正确的
模型、正确的 prompt、正确的 `feature_as_of`,却喂进一个 `feature_as_of` 之后才被改写出来的
`epsActual`——四条不变量全部通过,而结论是错的。这是当前契约里一个可指名的空洞。

### 1.2 采集真实性:自报证据产生于被审计者控制的进程内

见 `docs/spec-changes/2026-08-27-audit-v2-correlation-schema.md` §1 第 2 条(METR 2026-08-26
的 spoof 比例与原文措辞)。audit v2 的 `reconcile` 已经做**总量**对账(同 model 同 UTC 桶的自报
token 量 vs 供应商 usage 报告),但总量一致不排除逐条造假:少报的调用与多报的调用可以互相抵消,
而 Usage API 只给 token 量、不给调用数(08-27 §8 实施备注)。**逐请求存在性核对**是 L1 之上唯一
不需要供应商签名基础设施就能做的一层,它需要一个两侧都有的 join key——供应商返回的响应 id。

### 1.3 合规拉力推后,不改变技术路线,只改变不为谁提前实现

EU AI Act Digital Omnibus 已正式通过(Regulation (EU) 2026/1744,OJ 2026-07-24 刊出,07-27 生效),
Annex III 高风险义务推迟至 **2027-12-02**。来源:
<https://labs.cloudsecurityalliance.org/research/csa-research-note-eu-ai-act-high-risk-deadline-omnibus-20260/>
与 <https://www.gibsondunn.com/eu-ai-act-omnibus-agreement-postponed-high-risk-deadlines-and-other-key-changes/>。

后果只有一条,且是**负向**的:本修订案不为任何合规场景提前实现功能。轴 A 的消费者是研究/回测
(quant_alpha_v2 是现成的、今天就在写 trace 的消费者);轴 B 的消费者是本仓库自己的 usage 审计。
审批绑定(修订案 C)因此**只做 SPEC 级定义,不实现**——它的 gate 是一个真实消费者,不是一部法规
的生效日。

---

## 2. 修订案 A: `traceguard.sources` 扩展与 `source_snapshot` 记录

以 §6.6 opt-in 扩展形式登记,**实验性**(D9)。新增一张 contract-external 表 `source_snapshots`。

### 2.1 字段表

| 字段 | 类型 | 必填 | 语义 |
|---|---|---|---|
| `snapshot_id` | int | ✔ (主键) | 自增主键 |
| `trace_id` | int | ✔ | 所属 trace。**逻辑外键**指向 `traces.trace_id`,不建 DDL 级 `ForeignKey`(见 2.3) |
| `source_uri` | text | ✔ | 取回位置。HTTP 为 URL;MCP 为 `mcp://<server_id>/<tool_name>` 或宿主自定;file / db 为宿主可解释的标识 |
| `source_kind` | text | ✔ | `http` \| `mcp` \| `file` \| `db` \| `vendor_api` \| `other` |
| `content_hash` | text | ✔ | `sha256(收到的原始字节)` 的十六进制。**不规范化**(D6) |
| `content_encoding` | text | nullable | 当入参是 `str` 而非 `bytes` 时,记录用于编码的字符集(固定 `utf-8`)。`bytes` 入参为 NULL |
| `normalized_hash` | text | nullable | 可选的规范化后摘要(CDN / 模板噪声的处理)。给了就**必须**同时给 `normalizer_id`(D6) |
| `normalizer_id` | text | nullable | 规范化器标识,格式 `<name>@<version>`。与 `normalized_hash` 成对出现,两者要么都为 NULL 要么都非 NULL |
| `retrieved_at` | timestamp | ✔ | 宿主取回这份内容的**物理时间**,MUST tz-aware |
| `published_at` | timestamp | nullable | **源声称的**发布时间(HTTP `Last-Modified`、vendor 的 `publishedDate` 等)。不变量 3 意义上的 `valid_from` |
| `effective_at` | timestamp | nullable | 业务有效期起点(与发布时间不同的场景:财报期、生效日) |
| `source_version` | text | nullable | 源自己的版本标识:ETag、Last-Modified 原文、vendor version |
| `mcp_server_id` | text | nullable | `source_kind='mcp'` 时的 server 标识 |
| `tool_name` | text | nullable | `source_kind='mcp'` 时的工具名 |
| `cache_status` | text | nullable | 宿主或中间层报告的缓存状态(`hit` / `miss` / CDN 的 `x-cache` 原文等),原样记录不解释 |
| `verdict` | text | ✔ | `verified` \| `anachronistic` \| `unverifiable` \| `unchecked`(§3) |
| `strict` | bool | ✔ | 本次记录调用点声明的模式,原样落库,便于事后区分"当时没拒绝"与"当时压根没检查" |

索引:`trace_id`、`source_uri`、`content_hash`、`retrieved_at`。前两个服务"这条 trace 依赖了什么"
与"这个源被谁依赖过";`content_hash` 服务反查"哪些 trace 依赖了后来被改写的那份内容";
`retrieved_at` 服务 drift 的时间序列扫描。

### 2.2 不存原文,是契约意图不是实现选择

**本扩展 MUST NOT 存储取回内容的原文**,只存摘要与元数据。zero-content 是这张表的默认形态,不是
一个可关掉的模式。原文归档(pit-archive 式)是消费者自己的事,与本扩展分工清楚:本扩展回答
"那份字节的摘要是什么、什么时候拿到的、与 `feature_as_of` 什么关系",原文归档回答"那份字节是什么"。
两者可以并存,`content_hash` 是它们之间的 join key。

理由不是隐私姿态,是能力边界:一个跑在宿主进程内、默认开着的 SDK 扩展去存第三方原文,会把
license、PII、体积三件事同时揽进契约,而这三件事没有一件是本扩展能替消费者决定的。

### 2.3 为什么开表而不内嵌 `output_parsed`

沿 08-27 修订案 §3.1 的开列 / 内嵌判据:

- **一对多**:一次 trace 可以取回多个源(一次推理喂三个 vendor endpoint 是常态)。JSON 数组能装,
  但下一条判据装不下。
- **需要按 `content_hash` 反查**:"哪些 trace 依赖了这份后来被改写的内容"是本扩展存在的理由。
  这是一条以非主键列为条件的跨行查询,JSON 内嵌撑不住(SQLite 的 JSON 提取无法走索引)。
- **先例**:audit 扩展自有三张表,routing_audit 自有一套表。contract-external 的扩展开自己的表
  是本仓库既定形态,不是新发明。

**逻辑外键而非 DDL 外键**:与 `audit/models.py` 同款——本扩展有自己的 `DeclarativeBase`,
`import` 不改动 `traceguard.store.models.Base` 的 metadata,因此 `make_engine(create_all=True)`
在不相关代码里仍然只建契约表。跨 metadata 的 `ForeignKey` 会把两套 schema 绑成必须同时创建,
而 SQLite 默认也不强制外键。约束由写路径保证:snapshot 行与 trace 行在**同一事务**内写入,
`trace_id` 取自刚插入那一行的主键(§9 第 2 步)。

---

## 3. 修订案 A 之二: 不变量 3 的适用范围加入"取回的外部数据"

### 3.1 内容

SPEC §5 不变量 3 的"适用范围(非穷举)"列表追加一行:

> - 取回的外部数据(`source_snapshots.published_at` 即其 `valid_from`)

**不新增第五条不变量**。不变量 3 的措辞本来就是通用原则,取回的外部数据一直落在它的字面覆盖内;
本次是把一个已经适用、但没人想到要去用的实例**明文写出来**。

但要照实说:除了那行实例枚举,§3.2 还在不变量 3 项下加了**一个新的拒绝条件**(源不声称
`valid_from` 时 strict 拒绝)。它按 §6.4 的“添加新不变量 = minor”分支归类,其 ramp 由
`strict` 无默认值承担——完整论证见 §7 的“D1 的定级”一段。既有实例(prompt template、
alias 表)的行为**逐字未变**:§4.5 的 `validate_reference_timing` 只接受确定的 `valid_from`,
没有“未知”这一态,新规则对它无从触发。

### 3.2 `published_at` 未知时的两种模式(D1)

SPEC 当前未覆盖的情形:源没有声称任何发布时间。这不是罕见情况——大量 vendor JSON 端点不返回
`Last-Modified`,MCP 工具结果通常什么时间戳都没有。

定夺:

- **strict 模式:拒绝**,raise `InvariantViolation`(invariant 3),措辞为
  `cannot establish that the source existed at feature_as_of`。
- **loose 模式:产出 verdict `unverifiable`**,照常记录该 snapshot 行,不 raise。

理由:**无法证明存在 ≠ 证明不存在**,两种模式必须各说各的。strict 模式的存在意义就是"拿不出证据
就不放行";loose 模式的存在意义是"把不知道如实记下来,而不是记成通过"。四级 verdict 沿
`routing_integrity` 的先例(`verified` / `diverged` / `unregistered` / `unverifiable`):那里
同样面对"检查跑了但什么都没检查到"的状态,同样拒绝把它折叠进"通过"。

`unchecked` 与 `unverifiable` 是**不同的**状态,不许合并:前者是"调用点没给 `feature_as_of`,
这次压根没做时间校验",后者是"做了校验,但源不提供判断所需的时间"。drift 统计里前者不计入观测
(§6 A5 的纪律:失败与未校验不是观测)。

---

## 4. 修订案 B: `traces.provider_response_id`(D2)

### 4.1 内容

`traces` 表新增一个 nullable、**有索引**的列:

| 字段 | 类型 | 必填 | 语义 |
|---|---|---|---|
| `provider_response_id` | text | nullable | 供应商为本次调用返回的响应标识(OpenAI `response.id`、Anthropic `message.id`)。逐请求带外对账的 join key |

wrapper 在非流式路径记录它。**流式路径记 NULL**:wrapper 不 drain 流(`tests/test_wrappers_streaming.py`
把这个语义钉死了——不 drain 就没有最终消息,也就没有可信的 id),拿不到就留 NULL,不猜。

### 4.2 为什么开列而不走 `output_parsed`

与 `agent_id` / `session_id` 同一条判据(08-27 §3.1):它是**join key**,不是稀疏业务数据。逐请求
对账要拿它和一份带外台账做等值连接并按窗口过滤,这需要索引;JSON 内嵌拿不到索引,而对账的语料
规模是几万到几十万行。

注:响应 id 今天已经出现在 `output_parsed["id"]` 里(两个 wrapper 都记)。开列不是新采集,是把一个
已经在采集的值放到能被连接的位置;旧行的 JSON 保持原样,**不回填**(与 1.5.0 对 `agent_id` 的处理
一致)。

### 4.3 在 algo v1 哈希信封之外

与 `agent_id` / `session_id` **完全同款**,文档照实写:

`audit/canonical.py` 的 `TRACE_CONTENT_FIELDS` 是硬编码白名单,由 golden tests 逐字节冻结。
`provider_response_id` **不进入该白名单**,因此:旧链逐字节不变、继续可验;新列受 append-only
守卫保护(ORM 层不许改),但**不被链 attest**——直接改库文件把 `provider_response_id` 换掉,
`verify_chain` 看不见。纳入信封待 algo v2。

这一句必须写进 `docs/audit.md`(§8.3),因为它正是"这一层证明什么 / 证明不了什么"的分界:
逐请求对账能证明"某个 id 在两侧都存在",不能证明"这一行的 id 没有被人事后改成另一个"。

---

## 5. 修订案 C: `traceguard.approval`(规划,SPEC 级定义,本次不实现)

以 §6.6 条目形式登记为**规划**。定义写死,实现以一个真实消费者为 gate(ROADMAP B2)。

### 5.1 接口

```python
bind(
    action: Any,
    *,
    approver: str,
    approved_at: datetime,
    expires_at: datetime | None = None,
    forbid_floats: bool = True,
) -> ApprovalRecord          # {approval_id, params_hash, approver, approved_at, expires_at}

verify(action: Any, approval: ApprovalRecord, *, strict: bool) -> ApprovalVerdict
# verdict ∈ {match, mismatch(带差异路径), expired, consumed}
```

- `params_hash` 由 §4.4 的 canonical normalize 产出(`input_hash(action)`),**不另写一套规范化**。
- 两步各写一条 trace,`operation` 为 `approval_bind` / `approval_verify`;开启 audit 时自然入链。
- **始终不阻断**:只出 verdict(同样没有“开启阻断”的参数)。`strict` 为 keyword-only
  显式参数、无默认值(与 `select_model` 同款);它选的是判定的严格程度,不是要不要放行。
  本节“默认”一词只用于 `forbid_floats=True` —— 那是唯一一个真的 Python 默认值。
  SDK 自身故障按 §4.1 fail-open。

### 5.2 两个已定夺的决策点

**D3 — 无条件 single-use**(不是“默认”:`bind` / `verify` 都**没有**关掉它的参数,
§5.1 的签名里也不许后补一个 `single_use=`)。同一 `approval_id` 第二次 `verify` 返回
verdict `consumed`,不放行。
理由:审批被复用于第二次执行 = 审批失效。一张批条批的是一笔操作,不是一类操作;把"能不能重复用"
交给调用点的默认值去决定,等于把最危险的那个语义设成静默的。

**D4 — 载荷禁止浮点**。`forbid_floats=True` 为默认;载荷任何层级出现 `float` 即 raise
`ApprovalPayloadError`,金额以字符串传入。理由:§4.4 的 float 走固定精度序列化,于是
"审批 100.0、执行 100.00 算不算同一笔"这个问题的答案取决于序列化细节而不是业务意图。审批场景
里这个歧义的代价是不可接受的,所以在入口处消除它,而不是在哈希算法里迁就它。

### 5.3 与 07-12"审批门仅在拉动时做"的张力

照实记录:这**不是门,是校验加记录**。它不阻断执行(默认只出 verdict),不做能力策略,不做撤权。
07-12 划出的是控制层;本条留在证据层内侧。即便如此,实现仍以一个真实消费者为 gate——本修订案
只把接口定义冻在纸面上,避免下一个人重新设计一遍。

---

## 6. 修订案 D: audit 条目追加三项

### 6.1 新增 finding kind `capture_unmatched`(WARN,D5)

逐请求存在性核对产出的新 kind,带 `direction` 字段:

| direction | 含义(固定措辞) |
|---|---|
| `out_of_band_only` | 台账有、traces 无 — a call the capture layer did not see — bypass or wrapper coverage gap |
| `self_reported_only` | traces 有、台账无 — a record the provider side does not vouch for — fabrication, duplication, or an incomplete ledger |

同一 `response_id` 在任一侧重复 → 单列 `duplicate` 子类。

**聚合核对继续用 `capture_mismatch`,行为不变。** 为什么不复用同一个 kind:两种 finding 证明的
东西不同。`capture_mismatch` 说的是"两侧总量对不上",`capture_unmatched` 说的是"这一条在另一侧
不存在"。前者可以由计量口径差异造成(缓存 token 的算法、时间窗边界),后者不能。混在一个 kind 里,
消费者会用同一个阈值和同一套处置流程对待它们,而这两件事该有不同的处置。

按 08-27 修订案 A 的规则,**新增 kind = minor**。

### 6.2 anchor sink 新增 `ots:` / `rekor:`(D8)

- **`ots:DIR`(必做)** — OpenTimestamps。对锚 digest 做 stamp,`.ots` 证明文件落 `DIR`。
  只需要 digest,不需要密钥管理。
- **`rekor:`(可选)** — Sigstore 透明日志 `hashedrekord` 条目。需要一把 ECDSA P-256 签名密钥,
  密钥管理是额外的运维面,因此排在 OTS 之后;预算超出则只留设计节。

两者都是网络 sink,放 extra `traceguard[anchors]`。失败语义与现有 `anchor_to()` 一致(逐个尝试、
最后汇总抛出)。`file:` 仍是基线。

**边界声明 1 的措辞不变**:锚定间隔仍是暴露窗口。多一种独立见证不等于窗口变小,只等于"链头在 T
时刻已存在"这件事不再依赖单一方。

### 6.3 证据 bundle `evidence-bundle/v1`(D7)

登记为 audit 的**导出格式**,spec 文件位置 `docs/specs/evidence-bundle.md`。**仅 JSON**,不做
HTML / PDF:可机器验证的只有一种形态,多受众版本是模板加人的判断,不是代码(ROADMAP 2026-09-10
"报告形态类"的不做理由)。

---

## 7. 兼容性分析

| 影响面 | 结论 | 依据 |
|---|---|---|
| audit algo v1 哈希信封 | **不受影响,新列不被 attest** | `TRACE_CONTENT_FIELDS` 是硬编码白名单,由 golden tests 冻结;`provider_response_id` 不进信封,旧链逐字节不变、继续可验。`source_snapshots` 是独立表,完全在信封之外。纳入信封待 algo v2 |
| `input_hash` / normalize | 不受影响 | 新列、`source_snapshots` 全部不参与计算,§4.4 算法零改动(§6.1 major 红线未触碰) |
| 不变量 1–4 | 不变量 3 的适用范围明文扩大,并新增**一个拒绝条件**(D1);1 / 2 / 4 零影响 | 见下方“D1 的定级”一段——“加了一行实例枚举”够不到这一项,单列论证。§4.5 四个 validator 的签名逐字未动 |
| 下游 quant_alpha_v2 (`v0.2.0-phase0`) | 零影响 | 锁 tag;新列 nullable,`source_snapshots` 需显式 `sources.enable()` 才建表,旧写入路径完全合法 |
| 下游 huadian (guardian baseline) | 零影响 | 不同包,guardian 冻结,互不 import |
| 29 符号顶层冻结面 | **零变化** | `traceguard.sources` 走子模块 import(与 audit / contamination 同款);`Span` 只**新增方法**(`record_source` / `record_provider_response_id`),既有签名不动 |
| golden tests | **MUST 不改** | algo v1 冻结的定义。若实施中发现必须改 golden,即为方案错误,停下重审 |
| SemVer | **minor:SPEC v1.1 → v1.2,包 1.5.x → 1.6.0** | §6.2 加 nullable 字段 = minor;§6.3 加新方法 = minor;§6.6 新增 opt-in 扩展 = minor;§6.4 新增拒绝条件 = minor(ramp 见下段);新增 finding kind = minor(08-27 修订案 A 规则)。发版动作本身不在本次实施范围 |

**D1 的定级**(单列,因为这是本表唯一可争议的一项)。§8.3 应用的 diff 除了那行实例枚举,
还插入了一段带 MUST 的规范文本,所以必须按 §6.4 认领一条分支。认领的是前一条:
这是不变量 3 项下的**新拒绝条件**,即“添加新不变量 = minor”。

§6.4 给这条分支配了 ramp——“默认 warn,下个 release 转 error”——本案由
**`strict` 为 keyword-only 且无默认值**(D10)承担,不是跳过:

1. 该拒绝**只**在 `traceguard.sources` 这个新 opt-in 扩展内触发。§4.5 的
   `validate_reference_timing(valid_from: datetime, ...)` 根本没有“未知”这一态,
   所以 prompt template、alias 表等既有实例的行为**逐字未变**(§8.3 的 diff 已把
   适用范围写死在取回数据上)。
2. 扩展内每个调用点都**必须**显式说出 `strict=True` 还是 `strict=False`。ramp 要防的是
   “既有调用点在某个 release 后被静默从 warn 转成 error”;这里既没有既有调用点,
   也没有默认值可以替调用点做这个决定。

即:ramp 的**目的**(不让任何人在没说过话的情况下被转成 error)由签名结构直接满足,
强于按 release 计时的版本。

**契约面的有意更新**(minor,CHANGELOG 逐条写明,不许静默改):
`tests/test_audit_api_surface.py` 的 `FROZEN_FINDING_SEVERITY` 增加 `capture_unmatched: WARN`,
`EXPECTED_AUDIT_API` 增加本次从 `reconcile` 导出的新符号,
`test_algo_v1_envelope_excludes_the_v1_1_columns_and_cost_usd` 增加 `provider_response_id`。
这三处都是**冻结清单的有意扩容**,不是把断言改去迁就实现。

---

## 8. SPEC 本体确切修改(已应用)

### 8.1 文件顶部状态行

`TRACEGUARD_SPEC.md` 与 `docs/SPEC.md` 的状态行 v1.1 → **v1.2**,日期 2026-09-10,定级 minor。

### 8.2 §3.1 `traces` 表

`session_id` 行之后插入:

> | `provider_response_id` | text | nullable | 供应商为本次调用返回的响应标识(OpenAI `response.id`、Anthropic `message.id`)。逐请求带外对账的 join key |

"关键约束"追加一条:

> - `provider_response_id` 不参与 `input_hash` 计算(§4.4 算法不变),不参与不变量 1–4;与 `agent_id` / `session_id` 同,它在 audit algo v1 哈希信封之外(受 append-only 守卫保护,但不被链 attest)。流式调用无法取得最终响应 id 时 **MUST** 留 NULL,不得猜测或合成。

### 8.3 §5 不变量 3

"适用范围(非穷举)"列表追加一行:

> - 取回的外部数据(`source_snapshots.published_at` 即其 `valid_from`;见 §6.6 `traceguard.sources`)

该列表之后追加一段:

> **`valid_from` 未知时(v1.2)**。适用范围先说清楚:§4.5 的 `validate_reference_timing` 只接受一个**确定的** `valid_from`,不存在“未知”这一态,因此本条**不**改变它的行为,也不适用于 prompt template、alias 表这类调用点必须自己拿出 `valid_from` 的实例。本条只约束那些**能够表达“源没有声称首次有效时间”**的 reference data —— 今天只有一类:§6.6 `traceguard.sources` 记录的取回外部数据(常见于不返回 `Last-Modified` 的 vendor 端点与 MCP 工具结果)。
>
> 对这一类,strict 模式 **MUST** 拒绝——无法证明该内容在 `feature_as_of` 时已存在;loose 模式 **MUST** 产出 `unverifiable` 判定并照常记录,**MUST NOT** 折叠为通过。无法证明存在不等于证明不存在,两种模式各说各的。
>
> 定级:这是不变量 3 项下的**一个新拒绝条件**,按 §6.4“添加新不变量 = minor”归类。§6.4 给 minor 配的 ramp(默认 warn,下个 release 转 error)由 **`strict` 为 keyword-only 且无默认值**(§6.6 `traceguard.sources` 的 `record_source`)承担:该拒绝只在一个新的 opt-in 扩展内触发,而其每个调用点都必须显式说出自己的模式,因此不存在“既有调用点被静默转成 error”的情形——ramp 要防的正是这个。

### 8.4 §6.6 opt-in 扩展

新增两个条目(下面两行与 SPEC 中的行**逐字相同**,未做换行重排,便于日后逐字比对):

> - `traceguard.sources` — **实验性** opt-in 扩展(v1.2):取回数据的时点正确性。记录 `source_snapshot`(`source_uri` / `source_kind` / `content_hash` / `retrieved_at` 为 MUST,`published_at` / `effective_at` / `normalized_hash` + `normalizer_id` / `source_version` / `mcp_server_id` / `tool_name` / `cache_status` 可选),把不变量 3 的判定落到 `verdict`(`verified` / `anachronistic` / `unverifiable` / `unchecked`)。**不存原文**——只存摘要与元数据,原文归档是消费者自己的事。`import` 无副作用,须显式 `sources.enable(engine)`;写入失败按 §4.1 fail-open,绝不影响 trace 写入与宿主调用。字段表与决策记录见 `docs/spec-changes/2026-09-10-source-snapshot-approval-binding.md`,诚实分层见 `docs/sources.md`。实验性期间其 API 面**不进** contract-guard;graduate 需在真实 trace 上实跑两个 minor。
>
> - `traceguard.approval` — **规划,尚未实现**(v1.2 登记)。审批参数绑定:`bind(action, *, approver, approved_at, expires_at, forbid_floats=True)` 以 §4.4 canonical normalize 产出 `params_hash`;执行前 `verify(action, approval, *, strict)` 重算并返回 verdict ∈ {`match`, `mismatch`(带差异路径), `expired`, `consumed`}。两步各写一条 trace(`operation` 为 `approval_bind` / `approval_verify`),开启 audit 时自然入链。**默认不阻断**;同一 `approval_id` single-use(第二次 `verify` 返回 `consumed`);载荷禁止 `float`,金额以字符串传入,避开 §4.4 浮点定精度带来的歧义。实现以一个真实消费者为 gate。

并在 audit 条目末尾(“诚实分层与边界见 `docs/audit.md`。”之前)追加:

> 自 SPEC v1.2 起增补:finding kind 新增 `capture_unmatched`(WARN,逐请求存在性核对,带 `direction`);anchor sink 新增 `ots:` / `rekor:`(extra `traceguard[anchors]`,网络依赖,边界声明 1 的暴露窗口措辞不因此放松);证据 bundle 导出格式 `evidence-bundle/v1` 定义于 `docs/specs/evidence-bundle.md`。

### 8.5 附录 D 新增

> ### v1.2 (2026-09-10)
>
> - §3.1 新增 nullable 字段 `provider_response_id`(逐请求带外对账的 join key);与 `agent_id` / `session_id` 同,不参与 input_hash 与不变量,在 audit algo v1 信封之外。
> - §5 不变量 3 适用范围明文加入“取回的外部数据”;补 `valid_from` 未知时的行为规定(strict 拒绝 / loose 产出 `unverifiable`),作用域限定在 §6.6 `traceguard.sources`。**不新增第五条不变量**——这是不变量 3 项下的新拒绝条件,按 §6.4 归 minor,ramp 由 `strict` 无默认值承担。
> - §6.6 新增 `traceguard.sources`(实验性)与 `traceguard.approval`(规划);audit 条目增补 `capture_unmatched` finding kind、`ots:` / `rekor:` anchor sink、`evidence-bundle/v1` 导出格式。
> - 动机与兼容性分析:`docs/spec-changes/2026-09-10-source-snapshot-approval-binding.md`。SemVer **minor**。

---

## 9. 实施顺序

1. **应用 §8 的 SPEC diff**(先改 SPEC 再动代码,§8.1);`docs/SPEC.md` 英文版同步。本阶段零代码,
   测试数必须与开工前一致。
2. **`traceguard.sources` 扩展**(A1 / A2 / A3):`models.py`(ORM + `enable`)、`record.py`
   (`content_digest` / `SourceSnapshot` / `from_http_response` / `from_mcp_result`)、
   `validate.py`(`SourceVerdict` + `validate_source_snapshot`)、`Span.record_source`、
   `__main__.py`。两条实现纪律必须写进代码注释:
   - **校验在 record 时同步做,不在 flush 时做**。`strict=True` 的拒绝、`retrieved_at` 的 tz 检查、
     `normalizer_id` 的成对检查都必须在 `record_source()` 调用点 raise。若放到 flush,
     `_flush_safe` 会按 §4.1 把它吞掉,strict 就被静默架空了。verdict 在 record 时算(此时
     `span.feature_as_of` 已知),**只有行的写入**延后并 fail-open。
   - **"同一事务"与 fail-open 靠 SAVEPOINT 调和**:`sess.add(row)` → `sess.flush()`(拿到
     `trace_id`)→ `sess.begin_nested()` → 插 snapshot 行 → nested commit / rollback →
     `sess.commit()`。sources 写入失败时 savepoint 回滚,trace 照常提交。`audit/chain.py` 已经
     在同一套 SQLite 上用 `begin_nested()`,先例成立。
3. **A5 `drift` CLI**:按 `source_uri` 分组的 `content_hash` 变化序列 + Wilson 95% 区间
   (`sources/stats.py` 自写最小实现,不 import `analysis/`)。`UNCHECKED` 行不计入观测。
4. **B1 逐请求核对**:`traces.provider_response_id` 列 + `ensure_trace_columns` 迁移;两个 wrapper
   记录响应 id;`routing_audit` ingest 填充(不回填历史行);`reconcile --source requests-json:PATH`
   与 `request-ledger/v1` 台账格式;`capture_unmatched` finding。
5. **B3 证据 bundle**:先写 `docs/specs/evidence-bundle.md` + JSON Schema,再实现
   `audit/bundle.py` 的 `export_bundle` / `verify_bundle` 与 CLI。`hash_only` 模式只验链的衔接,
   产出 `content_not_recomputed`(INFO),**不得**与 `full` 模式的结论合并成同一个 "verified"。
6. **B4 第二种独立锚**:`docs/audit.md` 的 Anchors 补"证明什么 / 证明不了什么 / 依赖谁 /
   失败时怎样"四栏;实现 `ots:` sink(extra `traceguard[anchors]`),区分 pending 与 complete。
   `rekor:` 预算超出则只留设计节。
7. **B2 不实现**——只有本文 §5 的 SPEC 级定义。

每一步的验收都包含同三项:`tests/test_audit_canonical.py` 逐字节不变、
`tests/test_public_api_surface.py` 不变、`tests/test_tracer_otel_isolation.py` 不变。

---

## 10. 决策点记录

十个决策点**均已定夺于 2026-09-10 任务书**,不在实施期重开。

| # | 决策 | 定夺 | 理由 | 落在本文 |
|---|---|---|---|---|
| D1 | 不变量 3 遇到 `published_at` 未知 | strict 拒绝(措辞 "cannot establish that the source existed at feature_as_of");loose 产出 `unverifiable` 并照常记录。不新增第五条不变量;作为不变量 3 项下的新拒绝条件按 §6.4 归 minor,ramp 由 D10 承担(见 §7) | 沿 `routing_integrity` 四级判定先例;无法证明存在 ≠ 证明不存在,两种模式各说各的 | §3.2 / §8.3 |
| D2 | `provider_response_id` 放哪 | `traces` 新增 nullable、有索引的列;在 algo v1 哈希信封**之外**(与 `agent_id` 同款,文档照实写) | 它是逐请求对账的 join key,不是稀疏业务数据——08-27 的开列 / 内嵌判据 | §4 |
| D3 | approval 是否一次性 | **无条件** single-use(无开关参数):同一 `approval_id` 第二次 `verify` 返回 `consumed`,不放行 | 审批被复用于第二次执行 = 审批失效 | §5.2 |
| D4 | approval 载荷里的数值 | 默认 `forbid_floats=True`:载荷任何层级出现 `float` 即 raise `ApprovalPayloadError`;金额以字符串传入 | 避开 §4.4 浮点定精度带来的"审批 100.0、执行 100.00 算不算同一笔"歧义 | §5.2 |
| D5 | 逐请求核对的 finding kind | 新增 `capture_unmatched`(WARN),带 `direction`;聚合核对继续用 `capture_mismatch` | 两种 finding 证明的东西不同,混在一个 kind 里会被当成同一件事 | §6.1 |
| D6 | `source_snapshots` 哈希定义 | `content_hash = sha256(原始字节)`;`str` 入参按 UTF-8 编码并记 `content_encoding`;`normalized_hash` 可选,给了就必须给 `normalizer_id`(格式 `<name>@<version>`) | 没有名字和版本的规范化器产出的 hash 不可比(pit-archive 教训) | §2.1 |
| D7 | bundle 格式 | 仅 JSON,schema 标识 `evidence-bundle/v1`;不做 HTML / PDF | 可机器验证的只有一种形态,多受众版本是模板不是代码 | §6.3 |
| D8 | 第二锚的顺序 | `ots:` 必做,`rekor:` 可选(预算超出只写设计节);都放 extra `traceguard[anchors]` | OTS 只要 digest、不要密钥;Rekor 需要签名密钥管理 | §6.2 |
| D9 | `traceguard.sources` 的契约地位 | 实验性:§6.6 登记、字段表在本文、API 面**不进** contract-guard;两个 minor 之后再议 graduate | 先让字段在真实 trace 上跑 100 条(A4 gate),再冻结 | §2 / §8.4 |
| D10 | `strict` 参数 | `record_source(..., strict=)` keyword-only、无默认值 | 与 `select_model` 同款:每个调用点都要说出自己的意图 | §9 第 2 步 |

**本次未定夺、留给实施期观察的**:`source_kind` 的值域是否需要再加(先按六值发,`other` 是兜底);
`effective_at` 是否会被真实消费者用到(quant_alpha_v2 的财报期场景应当会用,但没写入之前不算数)。
两者都不阻断实施。

# TraceGuard 集成规范 (Spec)

> **状态**: Draft v0.3 (2026-06-28)
> **类型**: 接口契约 / "宪法"
> **范围**: 任何接入 TraceGuard 的项目都必须遵守本文档定义的数据模型、SDK 接口签名、不变量。
> **非范围**: 实现路线、Phase 计划、具体业务的 check 清单、运维细节,统一搬到 `TRACEGUARD_ROADMAP.md` 和各业务方的 `<project>_TRACEGUARD_INTEGRATION.md`。
> **当前真实消费者**: `huadian`(v0.1.0-baseline)。其他项目(SemDiff / ChainGraph / PEAD+)为前瞻设计,尚未存在。本规范服务于"今天的 huadian + 未来潜在接入方",不预设特定项目落地时间表。

---

## 0. 读者须知

- 本文档只描述**外部可见**的接口与不变量。实现细节(SQLite vs Postgres、装饰器实现方式、CLI 命令名)不在本文档约束范围内。
- 标记 **MUST** / **SHOULD** / **MAY** 遵循 RFC 2119 语义。
- 任何对 MUST 项的修改 = 宪法修订 = 需要 SemVer major bump,且所有接入项目要同步评估。

---

## 1. 范围与非范围

### 本规范定义(MUST 遵守)

1. 核心表的**字段子集**与不变属性 (§3)
2. SDK 公开方法的**签名与语义** (§4)
3. Look-ahead bias **四条不变量** (§5)
4. 接入项目的**最小义务** (§7)

### 本规范不定义(由实现或业务方决定)

- 存储后端选型(SQLite/Postgres/TimescaleDB)
- 装饰器之外的 instrumentation 形态
- 具体的 drift check 名字、阈值、调度
- 告警渠道与路由
- Dashboard / Grafana 面板
- 成本调和、PII 脱敏、批量 backfill 流程
- CLI 命令名(只约束语义,不约束命名)

### 定位(非规范)

LLM 管线里有**两类** look-ahead bias,需要不同工具,本规范只约束第二类:

1. **训练污染** — 模型预训练时已见过它要预测的未来,于是在"回忆"而非推理。这是统计问题(成员推断、regime decay、claim 级时间验证),由**契约之外的 opt-in 扩展**处理(见 §6.6 与 [POSITIONING.md](docs/POSITIONING.md))。
2. **Harness / 管线泄漏** — 代码用了在模拟时点不存在的模型/prompt/特征。这正是本契约 §5 让其**结构上可拒绝**的那一类。

本小节为非规范说明;§§3–7 才是约束性契约。

---

## 2. 术语

| 术语 | 定义 |
|---|---|
| **Trace** | 一次可观测计算的完整记录(输入 + 模型/Prompt + 输出 + 性能 + 错误)。原子单位。 |
| **Correlation ID** | 跨 trace 关联的业务对象标识。字符串,语义由业务方定义。 |
| **Prompt Template** | 版本化、内容哈希化的 prompt 字符串模板。 |
| **Model Registry Entry** | 一个 `model_id` 在系统中的元数据,含 `released_at` 与 `available_to_us_at`。 |
| **Replay Set** | 一组锁定的输入样本,用于回归与 A/B 测试。 |
| **Capability Class** | 模型能力分类,值域见 §3.2。 |
| **Project** | 接入方的字符串标识符(自由命名,小写蛇形,如 `huadian`、`semdiff`)。规范不预先枚举。 |

---

## 3. 数据模型契约

> **不变属性**: 列出的字段为 MUST 字段,接入项目不可绕过。实现可**添加** nullable 字段,不可**重命名/删除/改类型**。

### 3.1 `traces` (核心表)

**MUST 字段:**

| 字段 | 类型 | 必填 | 语义 |
|---|---|---|---|
| `trace_id` | int / bigint | ✔ | 主键 |
| `project` | text | ✔ | 接入项目标识 |
| `component` | text | ✔ | 业务组件名(项目内自由命名) |
| `operation` | text | ✔ | 操作分类:`llm_complete` \| `embedding` \| `ml_inference` \| `parse` \| 其他 |
| `correlation_id` | text | nullable | 业务对象关联标识 |
| `parent_trace_id` | int | nullable | 父 trace 引用(支持嵌套) |
| `input_hash` | text | ✔ | SHA-256(canonicalized input);**MUST** 用 SDK 提供的 normalize 函数计算 |
| `input_summary` | text | nullable | 人类可读摘要,长度 SHOULD ≤ 500 字符 |
| `model_id` | text | nullable | 若非空,**MUST** 在 `model_registry` 已注册 |
| `prompt_template_id` | text | nullable | 若非空,**MUST** 在 `prompt_registry` 已注册 |
| `prompt_template_hash` | text | nullable | 必须与 `prompt_template_id` 对应注册记录一致 |
| `output_parsed` | json | nullable | 结构化输出 |
| `parse_status` | text | ✔ | `success` \| `partial` \| `failed` |
| `latency_ms` | int | nullable | |
| `tokens_in` | int | nullable | |
| `tokens_out` | int | nullable | |
| `cost_usd` | decimal | nullable | 写入时为 list price;事后调和不在本规范范围 |
| `feature_as_of` | timestamp | nullable | **业务层 as-of 时间**,用于不变量校验 |
| `invoked_at` | timestamp | ✔ | trace 写入的真实物理时间(默认 NOW()) |
| `error_class` | text | nullable | |
| `error_message` | text | nullable | |

**关键约束:**

- `input_hash` **MUST** 由 SDK 内置的 normalize 函数计算(§4.4),业务方不得自行实现。
- `feature_as_of` 与 `invoked_at` 是**两个独立的时间**,backfill 场景下两者会显著不同(详见 §5)。
- `cost_usd` 是 list price,精确账单由事后调和补正,本规范不约束调和流程。

### 3.2 `model_registry`

**MUST 字段:**

| 字段 | 类型 | 必填 | 语义 |
|---|---|---|---|
| `model_id` | text | ✔ (主键) | 唯一稳定标识(如 `claude-sonnet-4-5-20250101`) |
| `model_family` | text | ✔ | `anthropic` \| `openai` \| `voyage` \| `internal-ml` \| 其他 |
| `capability_class` | text | ✔ | `general-llm` \| `embedding` \| `classifier` \| `regressor` \| `vision`(可扩展) |
| `released_at` | timestamp | ✔ | 模型对外公开发布时间(世界事实) |
| `available_to_us_at` | timestamp | ✔ | 在本系统内首次可调用时间 |
| `deprecated_at` | timestamp | nullable | |

**契约语义:**

- 严格 look-ahead 防护 **MUST** 使用 `available_to_us_at` 比较 (§5.2)。
- `released_at <= available_to_us_at` 必须成立。
- 模型升级 = 新 `model_id`,**不允许**就地改写已有记录的能力描述字段。

### 3.3 `prompt_registry`

**MUST 字段:**

| 字段 | 类型 | 必填 | 语义 |
|---|---|---|---|
| `prompt_template_id` | text | ✔ (主键的一部分) | 命名约定 `<project>/<component>/v<N>` |
| `prompt_template_hash` | text | ✔ | SHA-256(template_body) |
| `template_body` | text | ✔ | 含变量占位的原始模板 |
| `template_format` | text | ✔ | `jinja2` \| `fstring` \| `raw` |
| `expected_output_schema` | json | nullable | JSON Schema |
| `introduced_at` | timestamp | ✔ | |
| `superseded_at` | timestamp | nullable | |
| `superseded_by` | text | nullable | |

**契约语义:**

- 同一 `prompt_template_id` 的 `prompt_template_hash` **MUST** 不可变(就地修改 = 必须升新 ID)。
- 注册即不可删除,只能标记 `superseded_at`。

### 3.4 `replay_sets` 与 `replay_set_items`

**MUST 字段:**

```
replay_sets:
  - replay_set_id (PK)
  - project, component
  - is_locked (bool)        -- 锁定后不可修改
  - item_count
  - curated_at

replay_set_items:
  - item_id (PK)
  - replay_set_id (FK)
  - item_index
  - input_payload (json)
  - expected_output (json, nullable)
```

**契约语义:** `is_locked = TRUE` 后,任何对 items 的修改/删除/新增 **MUST** 被实现层拒绝。这是不变量 4 (§5.4) 的物理保证。

### 3.5 字段扩展规则

- 接入项目 **MAY** 通过实现层加 nullable 字段。
- 业务专属信息 **SHOULD** 放进 `output_parsed` 的 JSON 里,不另开列。
- 修改 MUST 字段 = 宪法修订。

---

## 4. SDK 接口契约

> **不变属性**: 列出的方法签名为 MUST 稳定接口。允许添加带默认值的新参数,不允许重命名或修改既有参数语义。

### 4.1 Instrumentation

**MUST 提供至少一种** instrumentation,签名如下任一:

```python
# 装饰器(基础形态,MUST)
@tracer.trace(
    project: str,
    component: str,
    operation: str,
    *,
    correlation_from: Callable[..., str] | None = None,
    feature_as_of_from: Callable[..., datetime] | None = None,
)
def fn(...): ...

# 上下文管理器(MUST 提供)
with tracer.span(
    project: str,
    component: str,
    operation: str,
    *,
    correlation_id: str | None = None,
    feature_as_of: datetime | None = None,
) as span:
    span.record_input(data)
    span.record_model_prompt(model_id=..., prompt_template_id=...)
    span.record_output(parsed=..., parse_status=...)
    span.record_perf(latency_ms=..., tokens_in=..., tokens_out=..., cost_usd=...)
    span.record_error(exc)  # 可选
```

手动 API、客户端 wrapper(`wrap_anthropic` 等)为可选实现,不在本规范约束。

**失败语义 (MUST)：** 插桩 **MUST NOT** 破坏被插桩的业务调用。trace 持久化失败默认 **fail-open**——吞掉并记 WARNING,绝不向调用方传播;且在错误路径上**绝不替换**原始业务异常(调用方始终看到自己的异常)。实现 **MUST** 同时提供 opt-in 的 **fail-closed** 模式(如 `strict_persistence` 标志 / `TRACEGUARD_STRICT_PERSISTENCE` 环境变量),用于"宁可中断也不能静默丢 trace"的回测场景。

> 动机:崩溃的 guardian 比没有 guardian 更糟。look-ahead 防护依赖 trace 数据集可信,但这一可信性 **MUST NOT** 以默认牺牲宿主调用为代价强加;是否 fail-closed 由业务方显式选择。

### 4.2 Model registry 查询

```python
# 严格模式
select_model(
    capability_class: str,
    *,
    available_at: datetime,
    strict: Literal[True],          # 无默认值,MUST 显式传
) -> str
# 找不到合规模型 → MUST raise NoEligibleModelError

# 宽松模式
select_model(
    capability_class: str,
    *,
    available_at: datetime,
    strict: Literal[False],         # 无默认值,MUST 显式传
) -> tuple[str, bool]
# 返回 (model_id, is_anachronistic)
```

**契约语义:** `strict` **MUST** 是无默认值的 keyword-only 参数。强制显式传递制造意识层面的决策摩擦,避免业务方无意识引入 look-ahead bias。

### 4.3 Prompt template 加载

```python
load_prompt(template_id: str) -> PromptTemplate

class PromptTemplate:
    prompt_template_id: str
    prompt_template_hash: str
    def render(**kwargs) -> str: ...
```

### 4.4 Input normalization(唯一权威来源)

```python
normalize_input(data: Any) -> bytes
input_hash(data: Any) -> str   # = sha256(normalize_input(data)).hexdigest()
```

**契约语义:**

- 算法 **MUST** 跨语言、跨版本可复现。
- 业务方 **MUST NOT** 自行实现 hash。
- 规则要点(实现可调整,但行为 MUST 等价):
  - dict:键排序后 JSON dump,`ensure_ascii=False`,`separators=(",", ":")`
  - 字符串:strip + 统一换行 `\n`
  - float:固定精度序列化(建议 17 位)
  - None / NaN / Inf:有明确确定的序列化形式

升级 normalize 算法 = 宪法修订(影响所有历史 trace 的可比性)。

### 4.5 Invariant validators

```python
validate_feature_as_of(input_traces: list, output_feature) -> None  # 不通过 raise
validate_model_timing(model_id: str, feature_as_of: datetime, *, strict: bool) -> None
validate_reference_timing(
    valid_from: datetime,
    feature_as_of: datetime,
    *,
    kind: str,                       # e.g. "prompt_template" | "entity_alias" | ...
) -> None
assert_replay_set_locked(replay_set_id: str) -> None
```

> `validate_reference_timing` 是不变量 3 的通用 validator。业务方在调用点指定 `kind` 以便错误消息可定位。Prompt 时间性是其中一个实例 (`kind="prompt_template"`),业务方专有引用数据(如 B2 的 `entity_alias`)用同一函数 + 自定义 `kind`。

`validate_feature_as_of` 与 `validate_reference_timing` 为**纯函数**。`validate_model_timing`(不变量 2)与 `assert_replay_set_locked`(不变量 4)**必然读取 model_registry / replay_sets 存储**,因此各接受一个可选 keyword-only `engine` 参数(无默认值时回退到默认 engine),并非字面纯函数;其约束是"除 raise 与读取存储外无副作用"。四者均可在 pytest/CI 中直接调用。

> SPEC v0.2 曾笼统称"均为纯函数"——这对依赖 registry 的不变量 2/4 是理想化措辞。v0.3 据实修正:绑定保证是"无副作用(读取存储除外)",而非字面纯函数。

---

## 5. Look-ahead Bias 四条不变量

> 所有接入项目 **MUST** 在生产代码与回测代码中遵守。

### 不变量 1: `feature_as_of` 单调性

任何输出 feature 的 `feature_as_of` **MUST** ≤ 所有上游输入的 `recorded_at` / `acceptance_ts` / 数据本身时间戳的最小值。

> 注意是输入数据**本身的时间**,不是 trace 的 `invoked_at`(后者是 backfill 时间)。

### 不变量 2: 模型时间性

用于计算 feature 的 `model_id` **MUST** 满足:

- 严格模式: `model.available_to_us_at <= feature_as_of`
- 宽松模式: `is_anachronistic=True` 且业务方在策略侧**显式打了折扣**

### 不变量 3: Time-versioned reference data 时间性(通用原则)

任何**时间敏感的引用数据(time-versioned reference data)**,其 `valid_from` / `introduced_at` / `available_to_us_at` 等表征"该对象首次有效时间"的字段 **MUST** ≤ 使用它生成的 feature 的 `feature_as_of`。

适用范围(非穷举):

- Prompt templates(`prompt_registry.introduced_at`)
- Entity alias / canonical name 表(业务方维护)
- 任何带 `valid_from` 字段的查询字典 / 参考映射

业务方 **MUST** 在自己的 INTEGRATION 文档中枚举本项目内适用此原则的所有 reference data 类型。本宪法只规定原则,不预先枚举实例。

> 注: 模型时间性(不变量 2)在概念上是本不变量的特化版本——模型是一种 reference data,`available_to_us_at` 是其 `valid_from`。之所以单独列出是因为模型有 strict / loose 两种模式,其他 reference data 默认只有 strict 模式。

### 不变量 4: 锁定 replay set 不可变

`replay_sets.is_locked = TRUE` 之后,实现层 **MUST** 拒绝任何对 items 的写操作。这保证不同时期的 A/B 测试结果可比。

---

## 6. 稳定性与演进规则

### 6.1 SemVer 适用

- **Patch**: bugfix,不动接口
- **Minor**: 添加新方法 / 新字段 / 新不变量(逐项 opt-in 一段时间后转 default-on)
- **Major**: 改既有 MUST 字段、改 SDK 既有签名语义、改 normalize 算法、改不变量定义

### 6.2 字段演进

- 添加 nullable 字段 = minor
- 任何 rename / delete / type change = major,且 **MUST** 提供 dual-write + 迁移期 ≥ 1 个 release

### 6.3 SDK 演进

- 新增带默认值参数 = minor
- 添加新方法 = minor
- 删除参数 / 改既有参数语义 = major

### 6.4 不变量演进

- 添加新不变量 = minor(默认 warn,下个 release 转 error)
- 修改/删除既有不变量 = major

### 6.5 当前 baseline 的兼容性

- 本 Spec v0.1 不强制对 `pipeline-guardian v0.1.0-huadian-baseline` 做破坏性改动。
- huadian 项目的现有 `eval_traces` 表 **MAY** 通过 adapter 视图映射进 traces 表的子集,但**不强制**。
- 何时迁移由 huadian 维护者决定。

### 6.6 opt-in 扩展(非规范)

以下能力以可选 extra 形式发布,**纯新增** — 不加 MUST 字段、不改既有签名、不动 normalize 算法,因此各自为 SemVer **minor**:

- `traceguard[otel]` — 把 trace 额外导出为 OpenTelemetry / OpenInference (OTLP) span,**附加**于(绝不替换)SQLite/SQLAlchemy 存储。
- `traceguard[contamination]` — 训练污染估计器(成员推断、regime decay、claim 级检查)。**仅检测**;评分通过 `output_parsed` 挂到 trace,**不**新增 MUST 列。
- `traceguard.loop` — 自我改进循环的 evidence-gating 辅助:只有 cutoff 之前可溯源的证据才被采纳为事实。

均为接入方可选;项目可只依赖上述核心契约而不安装其中任何一个。

---

## 7. 接入项目的最小义务

任何项目接入 TraceGuard,**MUST**:

1. 在自己仓库根目录维护 `<project>_TRACEGUARD_INTEGRATION.md`,内容包括:
   - 本项目所有 `component` 枚举与语义
   - 本项目使用的 `model_id` 列表
   - 本项目使用的 `prompt_template_id` 列表
   - 本项目的 drift check 清单(若启用)
2. 所有 LLM / ML 推理调用通过 SDK instrumentation,**MUST NOT** 绕过。
3. 所有用到的 `model_id` 与 `prompt_template_id` 在调用前已注册。
4. 在 CI 测试里调用 §4.5 的 invariant validators,确保不变量 1–4 不被违反。
5. 在自己的 CHANGELOG 里声明所依赖的 TraceGuard Spec 版本。

**SHOULD**(强烈建议但不强制):

- 每个 (project, component) 至少有一个 locked replay set
- 在 PR 模板中加入"是否修改了 prompt template?是否需要 replay A/B?"提示

**MAY**(可选):

- 启用 prompt template 升级强制 A/B 流程(实现层提供 opt-in 开关)
- 接入告警渠道、Dashboard、cost 调和等

---

## 附录 A: 与现有代码的关系

| 现有资产 | 在本规范中的位置 |
|---|---|
| `pipeline-guardian` 的 `eval_traces` 表 | 与本规范 `traces` 表**字段语义不同**;可通过 adapter 映射但不强制 |
| `guardian/validators/structural.py` & `semantic.py` | 属于"check 实现",不在本宪法范围;可作为业务方 check 的参考实现 |
| `guardian/optimizer/` | 同上,属于工具层 |
| `guardian/api/` Dashboard | 属于工具层,不在本宪法范围 |
| MCP server | 属于工具层 |

---

## 附录 B: 显式不在宪法内的事项

下列内容由 `TRACEGUARD_ROADMAP.md` 或各业务方文档定义,不属于本规范:

- 存储引擎选型与切换路线
- TimescaleDB hypertable / chunk 策略
- Phase 0–N 的实施顺序
- 具体 check 名称、调度、阈值
- 告警渠道、去重规则、严重度路由
- Dashboard 实现、Grafana 面板
- 成本调和流程、PII 脱敏规则
- CLI 命令名(只约束语义不约束命名)
- 任何项目专属的概念(`entity_aliases`、`transcript_split`、`section_parse` 等)

---

## 附录 C: 修订流程

1. 修订提案以 PR 形式提交,修改本文件 + 在 `docs/spec-changes/<date>-<slug>.md` 写动机
2. 评估 SemVer 影响等级(patch / minor / major)
3. 通知所有接入项目维护者
4. major 修订 **MUST** 留出 ≥ 1 个 release 的迁移期

---

## 附录 D: 修订历史

### v0.3 (2026-06-28)

- §4.1 增补**失败语义 MUST**:插桩 MUST NOT 破坏业务调用;持久化默认 fail-open + opt-in fail-closed(`strict_persistence` / `TRACEGUARD_STRICT_PERSISTENCE`)。reference implementation 在 traceguard 0.8.0 落地。
- §4.5 修正"均为纯函数"措辞:不变量 2(`validate_model_timing`)与不变量 4(`assert_replay_set_locked`)必然读取存储,接受可选 `engine`,约束改为"无副作用(读取存储除外)"。
- 备注:不变量 4 与 §3.4 replay_sets/replay_set_items 在 traceguard 0.8.0 完整实现(物理拒写 + `assert_replay_set_locked`),§3.4/§4.5/§5.4 由"已定义未实现"转为"已实现"。本次为 SemVer **minor**(新增 MUST 行为,实现已满足;无既有签名/字段破坏)。

### v0.2 (2026-05-18)

- §0 增补"当前真实消费者 huadian"声明,纠正原 DESIGN 文档"三个项目都依赖 traceguard"的事实偏差(B1 SemDiff / B2 ChainGraph / PEAD+ 为前瞻设计,尚未存在)
- §4.2 `select_model`: 移除 `strict` 参数的默认值,强制 keyword-only required
- §4.5 `validate_prompt_timing` 泛化为 `validate_reference_timing(valid_from, feature_as_of, kind=...)`
- §5 不变量 3 改写为通用原则"任何 time-versioned reference data 的 valid_from MUST ≤ feature_as_of",prompt template 与业务专有 reference data 都是其实例

### v0.1 (2026-05-18, superseded by v0.2)

- 初版。从 `TRACEGUARD_DESIGN.md` 剥离接口契约部分,移除项目特定内容、Phase 路线、business check 清单。

---

**End of Specification.**

# Consumer: huadian (华典智谱)

> **文档地位**: 上游侧 reference,记录 huadian 实际使用上游(pipeline-guardian)的哪些 API、走什么集成模式、必须保护什么。
> **权威契约**: huadian 自维护的 `docs/decisions/ADR-004-traceguard-integration-contract.md`(含 Errata) + `docs/research/TG-002-traceguard-upstream-survey.md`。本文件如与之冲突,以 huadian 侧为准。
> **本文件的目标读者**: traceguard 维护者(本仓库),用于评估"修改 X 会不会破坏 huadian"。
> **最后核对时间**: 2026-05-18

---

## 1. 当前部署

- **项目**: huadian (华典智谱) — D-route "Agentic Knowledge Engineering 框架 + 史记参考实现",GitHub public (`https://github.com/lizhuojunx86/huadian`)
- **依赖声明**: `pipeline-guardian @ git+https://github.com/lizhuojunx86/traceguard.git@v0.1.0-huadian-baseline`
- **SHA pin**: `0350b0a54ec646a96e3f25949b7ce604284c49eb`
- **接入消费方**: `services/pipeline/`(管线子包,Python 3.12)
- **本地副本**: `/Users/lizhuojun/Desktop/APP/huadian/`(canonical 主 session)+ 两个 worktree:
  - `huadian-wt-architect/` — 架构师 session 的 worktree
  - `huadian-wt-pipeline-005/` — 管线工程师 sprint worktree

  三个 worktree 共享同一 GitHub remote,同一 baseline pin,同一 Adapter 实现。

---

## 2. 集成模式: Port/Adapter (Hexagonal)

由 huadian ADR-004 确立,**不绑定上游 API 形态**:

```
huadian pipeline code
    │
    ▼
TraceGuardPort(Protocol)         ← huadian 自定义协议(services/pipeline/src/qc/traceguard_port.py)
    │
    ▼
TraceGuardAdapter                 ← huadian 侧 Adapter,负责翻译
    │
    ▼
guardian.evaluate_async(...)     ← 上游冻结 API surface
```

含义:huadian 业务代码**完全不 import** `guardian.*`。所有上游依赖集中在 Adapter 单一文件 `services/pipeline/src/huadian_pipeline/qc/traceguard_adapter.py` 里,Adapter 翻译 huadian 自有协议 ↔ 上游 API。

**对 traceguard 维护者的影响**: 只要上游冻结的 4 个公开符号语义不变,Adapter 内部可以吸收所有变动。**huadian 不会被上游的非冻结部分变更影响。**

---

## 3. 上游冻结 API surface(MUST 不破坏)

由 `TG-STAB-001` sprint(2026-04-16)在上游 tag `v0.1.0-huadian-baseline` 冻结,huadian ADR-004 Errata §E-2 引用:

### 3.1 `guardian.__all__` 必须严格等于以下集合

```python
{"evaluate_async", "StepOutput", "GuardianConfig", "GuardianDecision"}
```

任何增删 = huadian CI 红 = 必须 major bump + 通知 huadian。

### 3.2 `guardian.evaluate_async` 签名

```python
async def evaluate_async(
    output: StepOutput,
    config: GuardianConfig,
    attempt: int = 1,
    http_client: httpx.AsyncClient | None = None,
) -> GuardianDecision
```

### 3.3 `guardian.GuardianDecision` 字段

```python
@dataclass
class GuardianDecision:
    action: str                  # 必须是 5 种字面量之一(见 §4.1)
    issues: list[str]            # 人类可读
    score: float                 # 0.0 ~ 1.0
    retry_hint: str | None
    semantic_score: int | None   # 1 ~ 5
    semantic_status: str | None
```

### 3.4 `guardian.GuardianConfig`

YAML-loaded Pydantic 模型,字段含 `structural` / `semantic` / `actions` 三个子结构(详见上游 `guardian/core/config.py`)。huadian 主要用 `structural` + `actions`,**强制 `semantic.enabled = False`**(见 §6)。

### 3.5 `guardian.StepOutput`

输入包装器,封装单文件内容 + JSON/文本判定(详见上游 `guardian/core/step.py`)。

---

## 4. 行动词汇映射(huadian 侧 Adapter 维护)

### 4.1 上游 action 字面量(冻结集合)

```python
{"pass", "passthrough", "retry", "abort", "alert"}
```

任何**增加**新字面量 = huadian CI 红(契约测试 #3,见 §5)。**MUST 通过 major bump + 升级 mapping table 处理**。

### 4.2 翻译表(huadian ADR-004 Errata §E-3 Mismatch #1)

| 上游 | huadian 协议 `ActionType` | 备注 |
|---|---|---|
| `pass` | `pass_through` | 直接映射 |
| `passthrough` | `pass_through` | 合并到 `pass_through`,Adapter 加 warning 日志区分 |
| `retry` | `retry` | 直接映射 |
| `abort` | `fail_fast` | 直接映射 |
| `alert` | `human_queue` | 语义重合通道不同;上游告警走 Telegram,huadian 入 PG 工作队列 |
| —(上游无) | `degrade` | Adapter 根据 `score` / `semantic_status` / `issues` 自行升格判定 |

---

## 5. huadian 侧契约测试(防御性断言)

`services/pipeline/tests/qc/test_traceguard_contract.py` 三条:

1. `assert set(guardian.__all__) == {"evaluate_async", "StepOutput", "GuardianConfig", "GuardianDecision"}`
2. 对每种上游 action 字面量,过 Adapter 后输出的 `ActionType` 必须匹配 §4.2 翻译表
3. `assert set(上游 action 字面量集合) == {"pass", "passthrough", "retry", "abort", "alert"}`

**任一红色 → huadian 拒绝升级到该上游版本。**

---

## 6. huadian **不使用**的上游能力(traceguard 可放心调整)

以下上游能力,huadian 已显式 bypass / 替换。修改/删除它们**不会影响 huadian**:

| 上游能力 | huadian 是否使用 | 替代方案 |
|---|---|---|
| `guardian/validators/semantic.py`(LLM-as-Judge) | ❌ 强制 `semantic.enabled = False` | huadian 自写 LLM-as-Judge 规则,走 huadian 的 `LLMGateway`(避开 C-7 黑盒 LLM 调用、双重计费、绕过审计) |
| `guardian/store/`(SQLAlchemy + `eval_traces` 表) | ❌ | huadian 自有 PG 审计表 `llm_calls` / `extractions_history` / `pipeline_runs`,上游 raw decision 进 `extractions_history.traceguard_raw` JSONB |
| `guardian/actions/alert.py`(Telegram bot) | ❌ | huadian 把 `alert` 映射到 `human_queue`,入 PG 队列 `qc_review_queue` |
| `guardian/cli.py`(`guardian check / suggest / serve / mcp`) | ❌ | huadian Adapter 走 library import,不 fork 子进程 |
| `guardian/mcp_server.py` | ❌ | 无关 |
| `guardian/api/`(FastAPI dashboard) | ❌ | 无关(huadian dashboard 在自家 Next.js + GraphQL) |
| `guardian/optimizer/`(suggest / root cause) | ❌ | huadian 自有反馈→规则演化闭环 |
| `guardian/env.py`(endpoint cache) | ❌ | huadian LLMGateway 自管 endpoint |
| Postgres 后端 | ❌ | 上游保持 SQLite 即可 |
| 自定义规则 Python 函数注册 | N/A | 上游没此能力,huadian 在 Adapter 层自建 RuleRegistry(纯 Python 函数 `(CheckpointInput) -> list[Violation]`) |
| 规则组合(AND/OR/NOT) | N/A | 同上,huadian 在规则函数内自行实现 |
| Severity 分级 / Sampling / Shadow / Enforce / Off 模式 | N/A | 同上,Adapter 层实现 |

**结论**: 上游的"业务表面"基本只剩 §3 的 4 个符号 + structural validators(JSON Schema / required_fields / length / language)。其余都是 huadian 用不到、可以放心演进 / 删除 / 重构的领域。

---

## 7. 与 SPEC §7 enumeration 要求的关系

新的 `TRACEGUARD_SPEC.md` §7 要求接入项目 MUST 维护一份 `<project>_TRACEGUARD_INTEGRATION.md`,列出 component / model_id / prompt_template_id 等枚举。

**huadian 对此 SPEC §7 要求不适用**,原因:

- huadian 接入的是 `pipeline-guardian v0.1.0-baseline`,**预于本 SPEC 之前**,baseline API 不含 `project` / `component` / `prompt_template_id` 等字段
- huadian 维护**自有等价物**: `step_name`(对应 component)、`prompt_version`(对应 prompt_template_id)、`model`(对应 model_id)在 huadian 的 `CheckpointInput` 数据类里
- 这些枚举的真实来源是 huadian 自己的 ADR-004 §二 + `services/pipeline/config/traceguard_policy.yml` + `services/pipeline/src/qc/rules/`

**如果 huadian 未来迁移到新 traceguard SDK**(并非承诺),才需要按 SPEC §7 在自己仓库新建 INTEGRATION 文档。届时迁移路径:

1. huadian 在 model_registry 注册当前实际使用的 model_id(`claude-opus-4-6`、`claude-sonnet-4-6` 等)
2. huadian 在 prompt_registry 注册当前在用 prompt_version(已存在于 `services/pipeline/src/ai/prompts/` 里)
3. Adapter 层替换 `evaluate_async` 为新 SDK 的 `tracer.span(...)` 调用
4. 既有 PG 审计表保留,新 SDK 写入 `traces` SQLite/PG,双写过渡期可由 Adapter 同时维护

**当前阶段(2026-05),不推动 huadian 做此迁移**。SPEC §6.5 与 ROADMAP §8.2 都确认了不强制。

---

## 8. 升级 / 破坏性变更纪律

在传递给 huadian 之前,上游的任何变更都要回答:

1. **是否修改 `guardian.__all__` 的 4 个符号?** 是 → major bump + huadian 升级评估
2. **是否修改 `evaluate_async` 签名(参数顺序、必填、默认值)?** 是 → 同上
3. **是否修改 `GuardianDecision` 字段(改名、改类型、删字段)?** 是 → 同上
4. **是否增加 / 删除 / 改名 action 字面量?** 是 → 同上(huadian 契约测试 #3 会红)
5. **是否影响 `GuardianConfig` 的 YAML 模式?** 是 → 评估;huadian 用的字段子集有限,可能不受影响
6. **是否修改 `StepOutput` 接受的输入形态?** 是 → 评估
7. **是否修改 `guardian/validators/structural.py` 的 4 种内建规则语义(JSON Schema / required_fields / length / language)?** 是 → 评估
8. **是否修改其他**(`semantic.py` / `store/` / `actions/` / `cli.py` / `mcp_server.py` / `api/` / `optimizer/` / `env.py`)? **否 → huadian 不受影响,可以自由调整**

任何 1-7 项变更,在上游 PR description 中显式声明 "Affects huadian baseline",并 ping huadian 维护者(用户自己)。

---

## 9. 上游侧已知遗留问题(不需要立刻解决,但记一下)

来自 TG-002 §9 待决问题,与上游有关的:

| # | 问题 | 现状 |
|---|------|------|
| Q-D2 | 是否要 PyPI 发版 0.2.0? | huadian Errata §E-5 明确"不再必要",走 git rev pin |
| Q-D7 | 允许 huadian 修改上游吗?(例如给 `semantic.py` 加 client 注入钩子) | **默认不修改**;huadian Adapter 只调上游稳定 API。上游可保持 `semantic.py` 现状 |
| Q-D9 | 上游依赖(pydantic 2.0 / fastapi 0.115 / sqlalchemy 2.0)与 huadian 兼容? | 当前实测无冲突;上游升级这些时记得 ping |
| Q-D10 | `StepOutput.output_as_string()` 字符上限? | huadian 留待 T-TG-002 实施期实测;上游无 hardcode 限制但可能性能裂化 |

---

## 10. 何时该更新本文件

- huadian 在 ADR-004 Errata 中追加新条款 → 同步翻译过来
- huadian 升级到新 baseline tag(如 v0.2.0-huadian-baseline)→ 重写 §1 + §3
- 上游变更命中 §8 的 1-7 项 → 在本文件 §8 末尾追加变更记录
- huadian 决定迁移到新 traceguard SDK → §7 重写,启动迁移 plan

---

## 11. 引用链接

- huadian 主仓库 ADR: [`huadian/docs/decisions/ADR-004-traceguard-integration-contract.md`](file:///Users/lizhuojun/Desktop/APP/huadian/docs/decisions/ADR-004-traceguard-integration-contract.md)
- huadian 上游调研: [`huadian/docs/research/TG-002-traceguard-upstream-survey.md`](file:///Users/lizhuojun/Desktop/APP/huadian/docs/research/TG-002-traceguard-upstream-survey.md)
- huadian 集成方案: [`huadian/docs/06_TraceGuard集成方案.md`](file:///Users/lizhuojun/Desktop/APP/huadian/docs/06_TraceGuard集成方案.md)
- huadian 策略 YAML: [`huadian/services/pipeline/config/traceguard_policy.yml`](file:///Users/lizhuojun/Desktop/APP/huadian/services/pipeline/config/traceguard_policy.yml)
- 上游 baseline 冻结 commit: `0350b0a54ec646a96e3f25949b7ce604284c49eb`
- 上游 baseline tag: `v0.1.0-huadian-baseline`
- 上游 CI 绿色证据: <https://github.com/lizhuojunx86/traceguard/actions/runs/24493213186>

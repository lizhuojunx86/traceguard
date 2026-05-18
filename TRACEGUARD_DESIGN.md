# TraceGuard 集成规范 — Cross-Project Specification

> **项目代号**: TraceGuard(基于已有 Pipeline Guardian 演进)
> **归属**: quant_alpha_v2 / 顶层 workspace package(独立于 brains/foundry 等业务模块)
> **核心 thesis**: B1 (SemDiff)、B2 (ChainGraph)、PEAD+ 三个 LLM/ML-heavy 项目都依赖大量 LLM 调用和模型推理。统一的 trace + drift + replay + 模型版本管理基础设施,既是 look-ahead bias 防护的物理实现,也是 prompt 工程化和成本控制的工具。三个项目都必须 instrument 进 TraceGuard,否则丧失数据可追溯性、特征可重现性、改动安全性。
> **目标读者**: Claude Code(基础设施实现) + 项目维护者(你) + B1/B2/PEAD+ 三个项目的开发者(也是 Claude Code 多 session)
> **本文档地位**: B1、B2、PEAD+ 三份设计文档都引用本文档作为 traceguard 接口契约。本文档定义"宪法",三个业务文档定义"专项法规"。

---

## 0. 阅读须知

本文档定义**接口与契约**,不是实现细节。Claude Code 在每个 Phase 内可自由决定具体实现,但**不得违反**:

1. 第 3 节定义的核心数据模型(可加字段不可改字段)
2. 第 4 节定义的 SDK 接口(可加方法不可改既有方法签名)
3. 第 8 节定义的四条 look-ahead bias 不变量

**与已有 Pipeline Guardian 的关系**: TraceGuard 是 Pipeline Guardian 的演进+重命名。如果 Pipeline Guardian 已有部分代码,Claude Code 应在 Phase 0 评估是迁移还是重写,优先迁移可复用部分(eval_traces 表、telegram 告警、CLI 框架)。

---

## 1. 架构定位

### 1.1 在 quant_alpha_v2 中的位置

```
quant_alpha_v2/
├── contracts/              # 已有
├── traceguard/             # ★ NEW package,所有 LLM/ML-heavy 模块依赖它
│   ├── pyproject.toml
│   └── src/traceguard/
│       ├── sdk/            # Python 客户端
│       ├── store/          # 存储层
│       ├── checks/         # drift 检测
│       ├── replay/         # A/B framework
│       ├── registry/       # 模型 + prompt 注册表
│       └── cli/
├── semdiff/                # B1, depends on traceguard
├── chaingraph/             # B2, depends on traceguard
├── earnings_signals/       # PEAD+, depends on traceguard
├── foundry/                # 已有,基础数据不需要 traceguard
├── brains/                 # 已有
├── shield/                 # 已有
└── hands/                  # 已有
```

### 1.2 调用关系

```
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│  semdiff         │   │  chaingraph      │   │  earnings_signals│
│  (B1 LLM calls)  │   │  (B2 LLM calls)  │   │  (PEAD+ LLM/ML)  │
└────────┬─────────┘   └────────┬─────────┘   └────────┬─────────┘
         │                      │                      │
         └──────────────────────┼──────────────────────┘
                                ▼
                  ┌─────────────────────────┐
                  │  traceguard SDK         │
                  │  (instrument, record)   │
                  └────────────┬────────────┘
                               ▼
                  ┌─────────────────────────┐
                  │  traceguard 存储与检测   │
                  │  ├── traces             │
                  │  ├── model_registry     │
                  │  ├── prompt_registry    │
                  │  ├── replay_sets        │
                  │  └── drift_alerts       │
                  └────────────┬────────────┘
                               ▼
                  ┌─────────────────────────┐
                  │  Cron checks + Telegram │
                  │  Grafana dashboards     │
                  └─────────────────────────┘
```

### 1.3 与 brains 的关系

Brains 的回测和实盘策略 **通过 traceguard 查询模型注册表** 来实施 look-ahead bias 防护:
- "在 2022 年 3 月 15 日的回测窗口里,可用的 LLM 是哪些?"
- "我要复现 2023 年 Q2 的 SemDiff 特征,当时的 prompt template hash 是什么?"

回答这些问题的能力是 traceguard 必须提供的。

---

## 2. 核心概念

| 概念 | 定义 |
|------|------|
| **Trace** | 一次可观测计算的完整记录:输入、模型/prompt、输出、性能、错误。原子单位。 |
| **Span** | Trace 的运行时句柄,SDK 内部使用。 |
| **Correlation ID** | 跨 trace 关联标识(如 `filing_id`、`earnings_event_id`)。 |
| **Prompt Template** | 版本化、内容哈希化的 prompt 字符串模板。 |
| **Model Registry Entry** | 一个模型 ID 在系统中的元数据,包括 `released_at`(★ 用于 look-ahead)。 |
| **Replay Set** | 一组锁定的输入样本,用于 prompt 改动的 A/B 测试。 |
| **Drift Check** | 周期性运行的检测函数,输入是窗口内的 traces,输出是 `CheckResult`。 |
| **Capability Class** | 模型能力分类:`general-llm` / `embedding` / `classifier` / `regressor` / `vision`。 |

---

## 3. 数据模型

### 3.1 `traces` 表(核心)

```sql
CREATE TABLE traceguard.traces (
    trace_id            BIGSERIAL PRIMARY KEY,

    -- Identity
    project             TEXT NOT NULL,        -- 'semdiff' | 'chaingraph' | 'pead_plus' | ...
    component           TEXT NOT NULL,        -- 'claim_extractor' | 'tone_delta' | ...
    operation           TEXT NOT NULL,        -- 'llm_complete' | 'embedding' | 'ml_inference' | 'parse'

    -- Linking
    parent_trace_id     BIGINT REFERENCES traceguard.traces(trace_id),
    correlation_id      TEXT,                  -- 业务对象 ID(filing/event/etc.)

    -- Input
    input_hash          TEXT NOT NULL,         -- SHA-256(normalized input)
    input_summary       TEXT,                  -- 前 500 字符,人类可读
    input_full_ref      TEXT,                  -- 大输入存对象存储,这里放路径

    -- Model / Prompt
    model_id            TEXT,                  -- 必须在 model_registry 中
    prompt_template_id  TEXT,                  -- 必须在 prompt_registry 中
    prompt_template_hash TEXT,

    -- Output
    output_raw          TEXT,                  -- LLM 原始输出(裁剪至 N KB)
    output_parsed       JSONB,                 -- 结构化输出
    parse_status        TEXT NOT NULL,         -- 'success' | 'partial' | 'failed'

    -- Performance & Cost
    latency_ms          INTEGER,
    tokens_in           INTEGER,
    tokens_out          INTEGER,
    cost_usd            NUMERIC(10, 6),

    -- Metadata
    feature_as_of       TIMESTAMPTZ,          -- ★ 业务层 as-of 时间
    invoked_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Error
    error_class         TEXT,
    error_message       TEXT,
    error_traceback     TEXT
);

CREATE INDEX idx_traces_project_component ON traceguard.traces (project, component, invoked_at DESC);
CREATE INDEX idx_traces_correlation ON traceguard.traces (correlation_id);
CREATE INDEX idx_traces_input_hash ON traceguard.traces (input_hash);
CREATE INDEX idx_traces_feature_as_of ON traceguard.traces (feature_as_of);

-- 改成 TimescaleDB hypertable,按 invoked_at 分片
SELECT create_hypertable('traceguard.traces', 'invoked_at', chunk_time_interval => INTERVAL '7 days');
```

**关键约束:**
- `(project, component, operation, input_hash, model_id, prompt_template_hash)` 同值的 trace 输出应该一致(如果 LLM 是确定性的)。非确定性 LLM 调用允许差异,但 drift 检测会观测分布稳定性。
- `input_hash` 必须用相同的 normalization 函数计算(去前后空白、统一换行符、JSON canonical 序列化)。SDK 内置该函数,业务方禁止自己实现。

### 3.2 `model_registry` 表

```sql
CREATE TABLE traceguard.model_registry (
    model_id            TEXT PRIMARY KEY,      -- 'claude-sonnet-4-5-20250101' | 'xgb-drift-duration-v1'
    model_family        TEXT NOT NULL,         -- 'anthropic' | 'openai' | 'voyage' | 'internal-ml'
    capability_class    TEXT NOT NULL,         -- 见第 2 节
    released_at         TIMESTAMPTZ NOT NULL,  -- ★ 模型在世界上首次可获取的时间
    available_to_us_at  TIMESTAMPTZ NOT NULL,  -- 在我们系统中可用的时间(可能晚于 released_at)
    deprecated_at       TIMESTAMPTZ,
    parent_model_id     TEXT,                  -- 微调/继承自
    notes               TEXT,
    metadata            JSONB
);
```

**`released_at` vs `available_to_us_at` 的区别:**
- `released_at` 是模型公开发布日(对外公认事实)
- `available_to_us_at` 是 API/权重首次能被我们调用的时间(可能晚 1-N 天)
- 严格 look-ahead 防护用 `available_to_us_at`
- 历史回填的"宽松折扣模式"用 `released_at`

### 3.3 `prompt_registry` 表

```sql
CREATE TABLE traceguard.prompt_registry (
    prompt_template_id      TEXT PRIMARY KEY,  -- e.g. 'semdiff/claim_extract/v1'
    prompt_template_hash    TEXT NOT NULL,     -- SHA-256(content)
    template_body           TEXT NOT NULL,
    template_format         TEXT NOT NULL,     -- 'jinja2' | 'fstring' | 'raw'
    expected_output_schema  JSONB,             -- JSON Schema, 用于自动校验
    introduced_at           TIMESTAMPTZ NOT NULL,
    superseded_at           TIMESTAMPTZ,
    superseded_by           TEXT,
    author                  TEXT,
    notes                   TEXT,

    UNIQUE (prompt_template_id, prompt_template_hash)
);
```

**关键约束:**
- 同一 `prompt_template_id` 的 `prompt_template_hash` 改变 = 必须升版本(新 ID)
- 不允许"就地修改" prompt
- `template_body` 用变量占位,运行时 SDK 注入实际值

### 3.4 `replay_sets` + `replay_set_items` 表

```sql
CREATE TABLE traceguard.replay_sets (
    replay_set_id       TEXT PRIMARY KEY,      -- e.g. 'semdiff/section_parse/golden_100_2026q1'
    project             TEXT NOT NULL,
    component           TEXT NOT NULL,
    description         TEXT,
    curated_by          TEXT,
    curated_at          TIMESTAMPTZ NOT NULL,
    item_count          INTEGER NOT NULL,
    is_locked           BOOLEAN NOT NULL DEFAULT FALSE  -- 锁定后不允许修改
);

CREATE TABLE traceguard.replay_set_items (
    item_id             BIGSERIAL PRIMARY KEY,
    replay_set_id       TEXT NOT NULL REFERENCES replay_sets(replay_set_id),
    item_index          INTEGER NOT NULL,
    input_payload       JSONB NOT NULL,
    expected_output     JSONB,                 -- gold label,可选(无 label 时只做分布对比)
    metadata            JSONB,                 -- 案例标签:正常、边界、对抗、known-failure 等
    added_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (replay_set_id, item_index)
);
```

### 3.5 `drift_alerts` 表

```sql
CREATE TABLE traceguard.drift_alerts (
    alert_id            BIGSERIAL PRIMARY KEY,
    project             TEXT NOT NULL,
    component           TEXT NOT NULL,
    check_name          TEXT NOT NULL,
    severity            TEXT NOT NULL,         -- 'info' | 'warn' | 'critical'
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    window_start        TIMESTAMPTZ,
    window_end          TIMESTAMPTZ,
    baseline_value      NUMERIC,
    observed_value      NUMERIC,
    z_score             NUMERIC,
    details             JSONB,
    acknowledged_at     TIMESTAMPTZ,
    acknowledged_by     TEXT,
    notes               TEXT
);

CREATE INDEX idx_alerts_unack ON traceguard.drift_alerts (project, component) WHERE acknowledged_at IS NULL;
```

### 3.6 `replay_runs` 表(A/B 运行记录)

```sql
CREATE TABLE traceguard.replay_runs (
    run_id              BIGSERIAL PRIMARY KEY,
    replay_set_id       TEXT NOT NULL,
    model_id            TEXT NOT NULL,
    prompt_template_id  TEXT NOT NULL,
    triggered_by        TEXT,                  -- 'prompt-promotion' | 'model-upgrade' | 'scheduled' | 'manual'
    started_at          TIMESTAMPTZ NOT NULL,
    completed_at        TIMESTAMPTZ,
    n_items             INTEGER,
    n_success           INTEGER,
    n_failed            INTEGER,
    summary_metrics     JSONB                  -- 视任务而定,如 precision/recall/分布距离
);
```

---

## 4. SDK 接口

### 4.1 三种 instrumentation 方式

业务代码集成 traceguard 的三种姿势,**功能等价,按场景选择**:

**方式 A: 装饰器(整函数级,推荐)**

```python
from traceguard.sdk import tracer

@tracer.trace(
    project="semdiff",
    component="claim_extractor",
    operation="llm_complete",
    correlation_from=lambda section_id, **kw: f"section:{section_id}",
    feature_as_of_from=lambda section, **kw: section.acceptance_ts,
)
def extract_claims(section: ParsedSection, ...) -> list[Claim]:
    output = anthropic_client.messages.create(
        model="claude-sonnet-4-5-20250101",  # SDK 自动从 client 参数推断 model_id
        messages=[{"role": "user", "content": prompt.format(text=section.text)}],
    )
    return parse_output(output)
```

**方式 B: 上下文管理器(细粒度控制)**

```python
with tracer.span(
    project="chaingraph",
    component="customer_supplier",
    operation="llm_complete",
    correlation_id=f"filing:{filing_id}",
    feature_as_of=filing.acceptance_ts,
) as span:
    span.record_input(input_data, hash_only=False)
    span.record_model_prompt(model_id="claude-sonnet-4-5", prompt_template_id="chaingraph/cust_supp/v1")
    
    output = call_llm(...)
    
    span.record_output(raw=output.text, parsed=parsed, parse_status="success")
    span.record_perf(latency_ms=output.latency, tokens_in=..., tokens_out=..., cost_usd=...)
```

**方式 C: 手动(异步 batch、跨进程场景)**

```python
trace_id = tracer.start_trace(...)
# ... do work ...
tracer.record_to_trace(trace_id, output_parsed=..., parse_status="success")
tracer.commit_trace(trace_id)
```

### 4.2 Anthropic / OpenAI / Voyage 客户端的自动 wrap

提供包装函数自动 instrument 常见客户端:

```python
from traceguard.sdk.wrappers import wrap_anthropic
import anthropic

client = wrap_anthropic(
    anthropic.Anthropic(...),
    project="semdiff",
    component="claim_extractor",  # 可在调用时覆盖
)

# 之后 client.messages.create() 自动产生 trace
```

### 4.3 模型版本查询接口

Brains/回测代码用这个接口实施 look-ahead 防护:

```python
from traceguard.registry import select_model

# 严格模式:必须当时可用
model_id = select_model(
    capability_class="general-llm",
    available_at=datetime(2023, 6, 15),
    strict=True,
)
# 返回 'claude-sonnet-3-20230601' 或类似

# 宽松模式(用于宽松折扣回填):使用现代模型,但记录 anachronism
model_id, is_anachronistic = select_model(
    capability_class="general-llm",
    available_at=datetime(2023, 6, 15),
    strict=False,
)
# 返回当下最强模型 + True (告诉调用方"我用了未来模型,你需要打折扣")
```

### 4.4 Prompt 模板调用接口

```python
from traceguard.registry import load_prompt

template = load_prompt("semdiff/claim_extract/v1")
rendered = template.render(section_text=section.text, prior_year_text=prior.text)
# template.prompt_template_id == 'semdiff/claim_extract/v1'
# template.prompt_template_hash == 'sha256:...'
```

---

## 5. Drift 检测

### 5.1 标准检测库(turnkey)

TraceGuard 提供以下开箱即用 checks,业务方在配置文件里启用即可:

| Check 名 | 检测内容 |
|----------|----------|
| `parse_failure_rate` | 过去 N 小时窗口内 `parse_status != 'success'` 的占比 vs 历史基线 |
| `output_length_dist` | `output_raw` 字符数分布 vs 历史基线(KS test 或 z-score) |
| `parsed_field_null_rate` | 指定 JSONB 路径的 null 率 vs 基线 |
| `numeric_output_dist` | 指定 JSONB 路径的数值分布 vs 基线 |
| `latency_p95` | P95 延迟变化 |
| `cost_daily_total` | 单日 USD 成本环比变化 |
| `replay_consistency` | 在 replay set 上重跑,与历史输出 hash 一致性 |
| `model_id_unexpected` | 出现 registry 中未注册的 model_id |
| `prompt_hash_unexpected` | 出现 registry 中未注册的 prompt_hash |

### 5.2 自定义 check 注册

```python
from traceguard.checks import register_check, CheckResult, CheckSeverity

@register_check(
    name="claim_count_per_section",
    project="semdiff",
    component="claim_extractor",
    schedule="daily",
    window="7d",
)
def claim_count_check(traces_df) -> CheckResult:
    counts = traces_df["output_parsed"].apply(
        lambda x: len(x.get("claims", [])) if x else 0
    )
    mean = counts.mean()
    if mean < 3.0:
        return CheckResult(
            severity=CheckSeverity.WARN,
            observed_value=mean,
            baseline_value=4.5,  # 历史均值
            details={"sample_size": len(counts)},
        )
    return CheckResult(severity=CheckSeverity.INFO, observed_value=mean)
```

### 5.3 告警路由

- `info`: 仅入库,不通知
- `warn`: 入库 + Telegram bot 消息到值班频道
- `critical`: 入库 + Telegram + email + (可选)拉起 incident

告警去重: 同 (project, component, check_name) 在 24 小时内只发一次,除非 severity 升级。

### 5.4 业务方在 traceguard 中定义的标准检测项

每个业务文档(B1/B2/PEAD+)定义自己的 check 清单。TraceGuard 只负责提供框架。例:

- B1: `embedding_distance_dist`, `severity_score_dist`, `claim_count_per_section`, `section_parse_failure_rate`
- B2: `new_entity_rate_weekly`, `new_relationship_rate_weekly`, `entity_alias_growth`, `extraction_schema_failure_rate`
- PEAD+: `tone_delta_dist`, `quality_score_dist`, `transcript_split_success_rate`, `regime_score_dist`

---

## 6. Prompt A/B 协议

### 6.1 强制流程

任何 `prompt_template_id` 升版本必须走以下流程,**没有捷径**:

```
1. 作者在 prompt_registry 注册新版本(新 prompt_template_id = old_id 去掉 vN 加 v(N+1))
   - 同时声明 expected_output_schema
   - introduced_at = NOW(), superseded_at = NULL

2. 作者指定要 evaluate 的 replay_set_id(必须 is_locked=TRUE 的稳定 set)

3. CLI 触发 replay run:
   $ traceguard replay run \
       --replay-set semdiff/section_parse/golden_100_2026q1 \
       --prompt semdiff/claim_extract/v2 \
       --model claude-sonnet-4-5-20250101 \
       --baseline-prompt semdiff/claim_extract/v1
   
4. 系统对 replay set 每个 item 用新旧两个 prompt 各跑一次,记录两次 traces

5. 系统生成对比报告:
   - 输出 schema 校验通过率(新 vs 旧)
   - 对有 gold label 的 item: precision / recall
   - 输出 token 长度分布对比
   - 数值字段分布对比
   - 失败案例诊断
   - 成本对比

6. 作者人工 review 报告:
   - 若通过: 标记旧 prompt superseded_at = NOW(), superseded_by = 新 ID
                生产代码切换到新 prompt_template_id
                之后的 traces 用新 prompt_hash 写入
                旧 traces 不动(保留历史可追溯性)
   - 若不通过: 旧 prompt 保留,新 prompt 标记 superseded_at = NOW() + notes='rejected'
```

### 6.2 Replay set 维护

- 每个 (project, component) 至少有一个 `is_locked=TRUE` 的 golden replay set
- Golden set 应包含: 正常案例(70%)+ 边界案例(20%)+ 已知失败案例(10%)
- 锁定后不允许修改 item;需要更新时新建 set,旧 set 保留供历史对比
- 推荐每 6-12 个月策划一次新 golden set,反映业务变化

### 6.3 Promotion 判定准则(默认值,业务方可调)

- 输出 schema 通过率: 新版 ≥ 旧版 - 1pp
- gold label precision: 新版 ≥ 旧版 - 2pp
- gold label recall: 新版 ≥ 旧版 - 2pp
- 数值字段分布: KS 距离 < 0.1
- 成本: 新版不超过旧版 1.5 倍(超过需要明确收益证明)

业务方可在 `traceguard/config.yaml` 内覆盖每个 (project, component) 的阈值。

---

## 7. 模型版本纪律

### 7.1 注册先于使用

任何 traces 写入时,`model_id` 必须已在 `model_registry`。

```python
# 启动检查:扫描所有源码中的 model_id 字面量,验证全部注册
$ traceguard validate-models --src-dir semdiff/src/
```

### 7.2 升级流程

模型升级(如 Claude Sonnet 4 → 4.5)必须:

1. 在 `model_registry` 添加新 entry,包含 `released_at` 和 `available_to_us_at`
2. 在所有受影响 (project, component) 触发 replay run,对比新旧模型在 golden set 上的输出
3. 报告 review 通过后,业务方按需求决定切换策略:
   - 增量切换: 仅新 traces 用新模型,旧特征保留
   - 全量重生成: 历史也重跑(谨慎!通常仅在新模型显著更便宜或更准时做)

### 7.3 Look-ahead 防护的两种模式

**严格模式(生产实盘 + 严肃回测):**
- `select_model(strict=True, available_at=as_of)` 强制返回当时可用的模型
- 若当时没有合适模型,raise `NoEligibleModelError`,业务方必须 fail-stop

**宽松折扣模式(初期历史回填 + 探索性研究):**
- `select_model(strict=False, available_at=as_of)` 返回当下最强模型 + `is_anachronistic=True` 标记
- 业务方在回测时对这些"未来模型"特征打折扣(默认 0.5-0.7)
- 必须在策略代码里显式 acknowledge,不能默默用未来模型

### 7.4 "模型能力时间线"快照(Phase 3 才上)

每季度生成一次"截至本季度,各 capability_class 的最强可用模型"快照,作为回测窗口的模型选择基础。这是 Phase 3 才上的高级能力,Phase 0-2 用粗粒度 `available_to_us_at` 过滤即可。

---

## 8. Look-ahead Bias 跨项目不变量

所有依赖 traceguard 的项目必须遵守这四条:

### 不变量 1: feature_as_of 单调性

任何业务特征行(`semdiff_features`、`network_features`、`earnings_signal_features`)的 `feature_as_of` 必须满足:
```
feature_as_of <= MIN(所有输入数据的 recorded_at / acceptance_ts / call_timestamp)
```

TraceGuard 提供工具函数 `validate_feature_as_of(input_traces, output_feature)` 用于业务方在测试中断言。

### 不变量 2: 模型时间性

用于计算特征的 model 必须满足:
```
model.available_to_us_at <= feature_as_of  (严格模式)
OR
model 在 anachronism 列表中 + 业务方显式打了折扣  (宽松模式)
```

### 不变量 3: Prompt 与 alias 时间性

- 用于计算特征的 prompt_template 必须满足 `introduced_at <= feature_as_of`
- 用于 entity resolution 的 alias(B2 专用)必须满足 `entity_aliases.created_at <= feature_as_of`

### 不变量 4: Replay set 不可变性

锁定的 replay set 不可修改。这保证不同时期的 prompt A/B 测试结果可比。

---

## 9. 分阶段开发路线

### Phase 0: 最小可用 trace + 存储(1 周)

- [ ] `traceguard` workspace package 创建
- [ ] Migrations: `traces`, `model_registry`, `prompt_registry`
- [ ] SDK 基础: `@tracer.trace` 装饰器 + `tracer.span()` 上下文管理器
- [ ] Anthropic / OpenAI / Voyage 客户端 wrapper
- [ ] CLI: `traceguard register-model`, `register-prompt`, `query-traces`
- [ ] 单测覆盖率 ≥ 80%
- [ ] **迁移**: 如果 Pipeline Guardian 已有 `eval_traces` 类似表,提供迁移脚本(保留历史数据)

**Phase 0 验收:**
- B1/B2/PEAD+ 任一项目能 import `traceguard` 并通过 `@tracer.trace` 记录一次真实 LLM 调用
- traces 表能正常 query
- model_registry 至少注册 5 个模型(已用的 Anthropic + OpenAI + Voyage 各 1-2)

### Phase 1: Drift 检测 + 告警(1 周)

- [ ] 标准 check 库(5.1 节列出的 9 个)
- [ ] `@register_check` 自定义 check 装饰器
- [ ] Cron runner: 每日运行所有 enabled checks
- [ ] Telegram bot 告警
- [ ] `drift_alerts` 表 + acknowledge CLI

### Phase 2: Replay framework + Prompt A/B(2 周)

- [ ] `replay_sets`, `replay_set_items`, `replay_runs` migrations
- [ ] CLI: `traceguard replay create`, `lock`, `run`, `compare`
- [ ] Replay run executor(并行调用 LLM)
- [ ] 对比报告生成(HTML + JSON)
- [ ] 业务方 B1 用 traceguard 跑通一次 prompt 升级 A/B 流程作为验证

### Phase 3: Dashboard + 高级 Look-ahead 工具(2 周)

- [ ] Grafana panels: traces 量、cost trends、drift alerts、per-project breakdown
- [ ] 模型能力时间线快照工具
- [ ] Trace 搜索 UI(简单的 FastAPI + HTML)

### Phase 4: 自动优化建议(2-3 周,可选)

- [ ] Root cause analysis: 从低分 / 失败 traces 中聚类提取共性
- [ ] Prompt diff 建议(基于失败案例)
- [ ] Human-in-the-loop PR 生成

---

## 10. Phase 0 MVP 验收 Checklist

- [ ] `traceguard` package 可被其他 package import
- [ ] 三张核心表 migration 成功应用
- [ ] SDK 装饰器 + 上下文管理器 + Anthropic wrapper 单测全过
- [ ] 至少一个真实业务方(B1 推荐先做)接入 traceguard,记录 ≥ 100 条 traces
- [ ] `traceguard validate-models --src-dir <pkg>` 通过
- [ ] CLI 命令文档完整
- [ ] 第 8 节四条不变量的 validator 函数实现 + 单测

---

## 11. Claude Code 启动 Prompt

将本文档放置于 quant_alpha_v2 根目录命名 `TRACEGUARD_DESIGN.md`,在 quant_alpha_v2 根目录启动 Claude Code,粘贴以下内容:

```
读取 TRACEGUARD_DESIGN.md 了解 TraceGuard 项目的完整设计。

重要前提:
- TraceGuard 是 Pipeline Guardian 的演进。请先扫描 quant_alpha_v2 内是否已有 Pipeline Guardian 相关代码(查找 'pipeline_guardian', 'eval_trace', 'guardian' 关键字)
- 若有可复用代码,我们倾向迁移而非重写;若没有,从零实现
- TraceGuard 是 B1 (SemDiff)、B2 (ChainGraph)、PEAD+ 三个项目的共享依赖,接口稳定性优先于功能广度

我们从 Phase 0 开始:最小可用 trace + 存储。请按以下顺序执行,每完成一步停下汇报:

1. 现状扫描:
   - 搜索 quant_alpha_v2 内的 Pipeline Guardian / eval_trace 相关代码
   - 报告位置、规模、可复用程度
   - 我们根据这个决定迁移还是重写

2. 创建 traceguard workspace package:
   - 顶层 package(与 contracts/、foundry/ 等平级)
   - pyproject.toml,依赖 contracts(如果 traceguard 需要任何共享类型)、pydantic、sqlalchemy、anthropic-sdk、openai(可选)
   - 加入 workspace 根的 members 列表
   - 验证 uv sync 成功

3. 实现核心数据模型:
   - traceguard/store/models.py: traces, model_registry, prompt_registry 三张表 ORM
   - 严格遵循设计文档 3.1-3.3 节字段定义
   - traces 表必须是 TimescaleDB hypertable
   - 用 Alembic 生成 migration

4. 实现 SDK 基础:
   - traceguard/sdk/normalizer.py: 输入 hash 的 normalization 函数(JSON canonical 序列化、空白处理)
   - traceguard/sdk/tracer.py: Tracer 类 + @trace 装饰器 + span() 上下文管理器
   - traceguard/sdk/wrappers/anthropic.py: wrap_anthropic 自动 instrument

5. 实现 Registry CLI:
   - traceguard/cli/register_model.py: 注册模型
   - traceguard/cli/register_prompt.py: 注册 prompt 模板
   - traceguard/cli/query_traces.py: 简单查询
   - 用 click 或 typer

6. 实现四条不变量 validator(8.1-8.4):
   - traceguard/validators/lookahead.py: 提供 validate_feature_as_of, validate_model_timing, validate_prompt_timing, validate_replay_set_immutable 四个函数
   - 都是纯函数,无副作用,便于业务方在测试中调用

7. 单测覆盖:
   - tests/sdk/test_normalizer.py
   - tests/sdk/test_tracer.py(用 mock LLM 客户端)
   - tests/store/test_models.py
   - tests/validators/test_lookahead.py
   - 覆盖率目标 ≥ 80%

8. 一个真实集成示例(放在 docs/examples/):
   - examples/anthropic_call.py: 演示 wrap_anthropic 用法,完整跑一次写入 traces 表

约束:
- 严格遵循设计文档的 schema 与 SDK 接口签名
- 不实现 Phase 1+ 的功能(drift checks 等)
- 任何对设计文档的偏离必须在 PR 中显式说明
- 每完成一步先 git status + git diff 给我看,等我说"继续"再进下一步

第一步开始: 现状扫描。
```

---

## 附录 A: 技术选型

| 类别 | 选型 | 理由 |
|------|------|------|
| 存储 | PostgreSQL / TimescaleDB | 与 quant_alpha_v2 一致 |
| traces 表分片 | TimescaleDB hypertable,7 天 chunk | 高吞吐 + 自动归档 |
| SDK 语言 | Python 3.12+ | 与所有业务方一致 |
| CLI 框架 | typer 或 click | typer 更现代,推荐 |
| 客户端包装 | 仅做 Anthropic + OpenAI + Voyage | 其他客户端 Phase 4 再说 |
| 告警渠道 | Telegram bot(复用 quant_alpha_v2 已有 bot) | 不引入新基础设施 |
| Dashboard | Grafana(Phase 3) | 与 quant_alpha_v2 监控一致 |
| Trace UI | FastAPI + HTMX 简易页面(Phase 3) | 避免引入前端构建链 |

## 附录 B: 已知风险与开放问题

1. **存储成长速度。** 三个项目 LLM 调用量级估算:
   - B1 Phase 1 S&P 500 历史回填: ~50K traces 一次性 + 每周 10-20 增量
   - B2 Phase 1 储能 vertical: ~5K traces 一次性 + 每周 50 增量
   - PEAD+ Phase 1-4: ~80K traces 一次性 + 每周 100-200 增量
   
   单 trace 大小约 5-20 KB(含 output_raw),三年累计估约 5-20 GB。可控,TimescaleDB 自动 compress 后再降一半。

2. **input_hash 不同 SDK 之间的一致性。** 业务方如果绕过 SDK 自己调 LLM,然后手动构造 trace,可能 hash 算法不一致导致历史数据不能 dedup。**对策: 强制所有 LLM 调用通过 SDK wrapper,提交 PR 审核时由 traceguard maintainer 把关。**

3. **Anthropic SDK 版本变化。** Anthropic Python SDK 接口偶有 breaking change(尤其 messages vs completions API)。`wrap_anthropic` 需要适配每个主版本。Pin SDK 版本到 pyproject.toml 严格区间。

4. **回放 LLM 调用的非确定性。** 非确定性 LLM 调用(temperature > 0)同样输入两次可能产生不同输出。Replay consistency check 必须容忍这种差异,只检查"分布是否稳定",不是"逐字节一致"。

5. **跨项目 schema 演进。** TraceGuard 的 traces 表 schema 变更会影响所有业务方。规则: schema 只允许添加 nullable 字段,不允许重命名或删除字段。重大变更需要新表 + 双写 + 渐进迁移。

6. **PII 与合规。** transcripts、年报文本可能包含管理层个人信息(姓名、邮箱、电话)。`input_summary` 字段截取时应做基础脱敏(电话号、邮箱掩码)。Phase 2 加入。

7. **成本归因失真。** Anthropic Batch API 折扣是延迟结算的,实时 trace 写入的 `cost_usd` 可能是 list price。Phase 2 加入"成本调和"流程,从 Anthropic 账单回填实际成本。

---

**End of Specification.**

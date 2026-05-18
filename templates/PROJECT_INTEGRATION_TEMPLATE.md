# <Project> ↔ TraceGuard Integration

> **本文件位置**: 应放在 `<project>` repo 根目录,文件名 `<project>_TRACEGUARD_INTEGRATION.md`(不是 traceguard repo 内)
> **依据**: 满足 `TRACEGUARD_SPEC.md` §7 接入项目最小义务
> **TraceGuard SPEC 版本依赖**: vX.Y(在你的 CHANGELOG 中也声明一次)
> **最后核对**: YYYY-MM-DD

---

## 1. 项目概览

- **项目名(`project` 字段值)**: `<short-snake-case>` (e.g. `semdiff`)
- **核心 thesis**: 一段话说清楚为什么要接 TraceGuard
- **接入范围**: 列出 traceguard 服务于本项目的哪些工作流(LLM 抽取 / embedding / ML 推理 / etc.)

---

## 2. Component 枚举

每个 `component` 是项目内的一个**独立的 LLM/ML 调用上下文**。命名要稳定,改名相当于把历史 trace 切断。

| `component` | 描述 | `operation` 类型 | 是否用 prompt template |
|---|---|---|---|
| `<component-1>` | <一句话描述> | `llm_complete` \| `embedding` \| `ml_inference` \| `parse` | yes / no |
| `<component-2>` | | | |

---

## 3. 使用的 Model IDs

列出本项目实际调用的所有 `model_id`。每个都**必须**在 traceguard `model_registry` 注册后方可使用。

| `model_id` | `model_family` | `capability_class` | 用于哪个 component |
|---|---|---|---|
| `claude-sonnet-4-5-XXXXXXXX` | `anthropic` | `general-llm` | |
| `text-embedding-3-large` | `openai` | `embedding` | |
| `voyage-3` | `voyage` | `embedding` | |

---

## 4. 使用的 Prompt Template IDs

| `prompt_template_id` | 用于哪个 component | 当前 hash | 状态 |
|---|---|---|---|
| `<project>/<component>/v1` | | `sha256:...` | active |
| `<project>/<component>/v2` | | `sha256:...` | superseded by v3 |

---

## 5. Time-versioned reference data(满足 SPEC 不变量 3)

列出本项目所有需要满足"`valid_from <= feature_as_of`"原则的引用数据。Prompt template / Model registry 是 SDK 自带的,无需在此重复。

| Reference data 名 | `valid_from` 字段 | 维护位置 | 备注 |
|---|---|---|---|
| (e.g. `entity_alias`) | `created_at` | `<project>` DB schema | B2 特有 |

如本项目没有项目专属的 time-versioned reference data,本节写"无,只用 SDK 自带的 prompt/model"即可。

---

## 6. Drift Check 清单(可选)

仅在你启用了 traceguard drift check 时填写。

| Check 名 | 触发频率 | 阈值 | 告警严重度 |
|---|---|---|---|
| `<check_name>` | daily / hourly | | warn / critical |

---

## 7. 不变量遵守计划

| 不变量 | 在哪里、用什么方式守护 |
|---|---|
| 1 (feature_as_of 单调性) | (e.g. 在 `tests/test_feature_lookahead.py` 调 `validate_feature_as_of`) |
| 2 (模型时间性) | (e.g. 所有 `select_model` 调用强制 `strict=True`,backtest 例外用 `strict=False` 并写 anachronism 标记) |
| 3 (Time-versioned reference data 时间性) | (e.g. 在 `tests/test_reference_timing.py` 调 `validate_reference_timing`) |
| 4 (锁定 replay set 不可变) | (e.g. 在 CI 用 `assert_replay_set_locked` 检查所有 golden set) |

---

## 8. 体量与成本预估

| 维度 | 数值 |
|---|---|
| 一次性历史回填 trace 数 | |
| 周增量 trace 数 | |
| 月成本 USD(预估) | |
| traces 表大小(年化) | |

---

## 9. 与其他项目的关联

- 共享 `correlation_id`: (例如 B2 复用 B1 的 `filing_id`)
- 共享数据源: (例如 EDGAR / FMP / 巨潮)
- 共享 model_id / prompt_template_id: (是否完全独立?)

---

## 10. 升级与变更纪律

- 本项目所依赖的 traceguard SPEC 版本: `vX.Y`
- traceguard SPEC 升 major 时,负责评估迁移的人: (角色 / 维护者)
- 本项目 prompt 升版本流程: (是否启用 SPEC §7 MAY 的强制 A/B?)

---

## 11. 当前已知接入限制 / 未决问题

列出已知但还没解决的、与 traceguard 边界相关的 open question。

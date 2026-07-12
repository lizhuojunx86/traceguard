# TraceGuard 实施路线 (Roadmap)

> **状态**: Draft v0.1 (2026-05-18),含 2026-06-28 状态更新(见下)
> **关系**: 本文档是 `TRACEGUARD_SPEC.md` 的实施配套。SPEC 是宪法(稳定),Roadmap 是路线(可演进)。两者冲突时,**SPEC 优先**。
> **承诺等级**: Phase 0 是承诺,Phase 1+ 是规划,Phase 3+ 是愿景。

---

## ⚠️ 状态更新 (2026-06-28) — 取代本文档若干过时表述

实施实际偏离了本路线,且这是**有意为之的正确选择**,本节据实更新,下文 Phase 描述保留作历史:

- **实际走向**:0.3.0–0.7.0 没有按 Phase 1(drift/告警)→ Phase 2(replay)推进,而是建了差异化/opt-in 能力——OTel 导出 + 实时双写、训练污染检测(MIN-K%/Min-K%++/regime-decay/claim-verifier)、loop evidence-gating、`wrap_openai`。这些是 SPEC §6.6 的契约外可选扩展,是产品的真实拳头。
- **1.0 的定义已修订**:下方 §1.1 表格"Phase 2 完成时冻结 v1.0.0"**作废**。1.0 不再等于"走完 Phase 2 工具链",而是 = **① SPEC §3–5 每条 MUST 真正实现并强制(含不变量 4)+ ② 公开 import 面 curated 并在 SemVer 下冻结 + ③ 插桩永不破坏/掩盖宿主调用(fail-open)**。Phase 1(drift/告警)与完整 Phase 2 工具链(replay executor / 对比报告 / A-B promotion / YAML→DB)被显式降级为 **post-1.0**(SPEC §1/§6 划在契约外)。
- **0.8.0 收口契约核心**:已实现 fail-open 隔离(B1)、replay_sets/replay_set_items 表 + 物理拒写(B2)、`assert_replay_set_locked`/不变量 4 + 写路径(B3)、curated 顶层 API + py.typed(B4)、streaming 不再假成功(B5)、SPEC/ROADMAP 对账(B6)。SPEC 随之升 v0.3。
- **后续真实路线由采纳驱动**:最高优先是让一个真实消费者上 traceguard 并写入 ≥100 trace(Phase 0 验收 #7 至今未满足,huadian 仍用 guardian),而非继续堆功能。

---

## 0. 文档地位

- 本文档定义**实施顺序、Phase 范围、Phase 间的决策门**。
- 本文档**不定义**接口契约——那是 SPEC 的事。任何与 SPEC 冲突的实施细节都要先改 SPEC。
- Phase 间没有日历 deadline,只有"上一 Phase 满足启动条件再开下一 Phase"的依赖关系。
- 用户随时可以喊停或调整;Phase 不是必须全部走完。

---

## 1. 总览与版本策略

### 1.1 Python 包与版本

| Python 包 | 角色 | 起始版本 | 何时冻结 |
|---|---|---|---|
| `pipeline-guardian` | huadian 当前消费者使用 | `v0.1.0-huadian-baseline`(已冻结) | 已冻结 |
| `traceguard` | 新 SDK + 存储,服务未来接入 | `v0.2.0`(本路线第一个 release) | ~~Phase 2 完成时冻结 v1.0.0~~ → 见顶部 2026-06-28 状态更新(改为契约核心 + 冻面 + fail-open) |

**关键约束:**

- 两个 Python 包**并存于同一 repo**,各自有 pyproject.toml,各自独立 release。
- `pipeline-guardian` 不再加新功能,只做 bugfix(且按 SPEC §6.5 不强制 huadian 迁移)。
- `traceguard` 是 SPEC 的 reference implementation,所有新需求往这里加。

### 1.2 Phase 编号与"承诺等级"

| Phase | 承诺等级 | 说明 |
|---|---|---|
| **Phase 0** | 承诺 | 必做,有明确验收;本文档详细描述 |
| **Phase 1** | 规划 | 大概率会做,范围可调;本文档列出方向 |
| **Phase 2** | 规划 | 在 Phase 1 验证后启动;本文档给框架 |
| **Phase 3** | 愿景 | 真有需要再做;本文档只占位 |
| **Phase 4** | 愿景 | 同上 |

---

## 2. 与现有 pipeline-guardian 的并存策略

### 2.1 目录布局(Phase 0 启动时定稿,以下为推荐)

```
traceguard/                          # repo root
├── pyproject.toml                   # uv workspace 根
├── guardian/                        # 现有 pipeline-guardian package,不动
├── packages/
│   └── traceguard/                  # 新 SDK package
│       ├── pyproject.toml
│       └── src/traceguard/
│           ├── sdk/
│           ├── store/
│           ├── registry/
│           └── validators/
├── tests/                           # 现有 + 新增,按 package 分目录
├── prompts/                         # ★ Phase 0 的 prompt_registry(YAML files, git-tracked)
└── ... (现有其他目录保持)
```

可选替代: `traceguard/` 不放 `packages/` 下,直接放 repo 根作为 sibling 与 `guardian/` 并列。两种布局都不违反 SPEC,Phase 0 启动时由实施者决定。

### 2.2 互不干扰原则

- `guardian.*` 模块的**任何 import 都不可指向** `traceguard.*`(防止 huadian 隐式被新 SDK 拉进去)
- `traceguard.*` 不依赖 `guardian.*`(新包独立可发布)
- 共享的只有:同一个 repo、同一个 ruff/pytest 配置、同一个 SQLite 文件(仅 dev,生产分库)

### 2.3 测试隔离

- `tests/guardian/`(现有 222 个测试)继续守护 huadian baseline
- `tests/traceguard/`(新建)守护新 SDK
- CI 两套测试都跑,任一红色都阻断 merge

---

## 3. Phase 0: MVP

> **目标**: 一个真实业务方能用新 traceguard 记录 ≥ 100 条 trace,跑通不变量 1+2 校验。
> **规模估计**: 5-8 个工作日(诚实估算,不含调试 huadian 真实接入的不可预期问题)。

### 3.1 范围内(MUST do)

| 项 | 落地形态 |
|---|---|
| `traces` 表 ORM | SQLAlchemy 2.0 + SQLite,字段严格按 SPEC §3.1 MUST 列表 |
| `model_registry` 表 ORM | 同上,字段按 SPEC §3.2 |
| `prompt_registry` | **YAML 文件**(`prompts/<project>/<component>/v<N>.yaml`,git-tracked),字段按 SPEC §3.3。"prompt 历史 = git log",不上 DB |
| `traceguard.sdk.tracer.trace` 装饰器 | 按 SPEC §4.1 签名 |
| `traceguard.sdk.tracer.span` 上下文管理器 | 同上 |
| `traceguard.sdk.normalizer.normalize_input` / `input_hash` | 按 SPEC §4.4,JSON canonical + 空白处理 + 浮点固定精度 |
| `traceguard.sdk.wrappers.wrap_anthropic` | Anthropic SDK 自动 instrument(只此一个 wrapper) |
| `traceguard.registry.select_model` | 按 SPEC §4.2,强制 keyword-only `strict` |
| `traceguard.registry.load_prompt` | 从 YAML 读取,返回 PromptTemplate |
| `traceguard.validators.lookahead.validate_feature_as_of` | 不变量 1 validator(纯函数) |
| `traceguard.validators.lookahead.validate_model_timing` | 不变量 2 validator(纯函数) |
| `traceguard.validators.lookahead.validate_reference_timing` | 不变量 3 通用 validator(纯函数,**框架就位但业务方 Phase 0 可选不调用**) |
| CLI: `traceguard register-model` | 注册 model entry |
| CLI: `traceguard query-traces` | 简单查询(按 project/component/时间窗) |
| 单测覆盖率 ≥ 60% on `traceguard.*` | 不追求 80%,先保正确路径 |
| 集成示例 | `examples/anthropic_call.py`:wrap_anthropic 完整跑一次写入 traces |

### 3.2 范围外(Phase 0 显式不做)

- ❌ TimescaleDB / Postgres(SQLite 起步)
- ❌ `replay_sets` 表与 replay framework
- ❌ Drift checks(任何 check)
- ❌ Telegram / 任何告警
- ❌ OpenAI / Voyage wrapper(只 Anthropic)
- ❌ 手动模式 `start_trace` / `commit_trace`(留到 Phase 1)
- ❌ Dashboard / Grafana / FastAPI UI
- ❌ MCP server(现有的不动,新的不加)
- ❌ 成本调和 / PII 脱敏
- ❌ 不变量 4(replay set 锁定)的强制执行——表都没建,自然没有
- ❌ Migration 从 `pipeline-guardian` 的 `eval_traces` 表迁数据

### 3.3 验收清单

1. `uv build` 在 `packages/traceguard/` 下成功产出 wheel
2. `uv run pytest tests/traceguard/` 全绿
3. `uv run pytest tests/guardian/` 全绿(huadian baseline 未被破坏)
4. `examples/anthropic_call.py` 能完整跑一次,SQLite 写入 traces 表 ≥ 1 行
5. SPEC §5 不变量 1+2 的 validator 在 pytest 中可被外部调用,正反向用例都验证
6. CLI `traceguard register-model claude-sonnet-4-5-20250101 ...` 成功写入 model_registry
7. 至少一个真实接入(huadian 或一个 prototype 项目)记录 ≥ 100 条 trace

### 3.4 Phase 0 → Phase 1 启动条件

- Phase 0 验收清单 1–6 全部满足(7 可与 Phase 1 并行推进)
- 至少有一个明确的业务需求场景需要 drift check 或告警(没有真需求就不进 Phase 1)

---

## 4. Phase 1: Drift checks + 告警

> **目标**: 标准 drift check 库 + Telegram 告警链路投产。
> **规模估计**: 1-2 周。

### 4.1 范围内

- `drift_alerts` 表(**勘误**:SPEC 并未定义此表——drift/告警在 SPEC §1/§6 明确划在契约外。此表为本 Phase 自定义,字段由实施者定,不进 SPEC MUST)
- 标准 check 库(从候选中**只挑业务真需要的**,不全做;注:SPEC 不枚举 check,§5 是不变量章节而非 check 清单):
  - `parse_failure_rate`
  - `latency_p95`
  - `cost_daily_total`
  - `model_id_unexpected`
- `@register_check` 装饰器(自定义 check)
- Cron runner(每日跑所有 enabled check)
- Telegram bot 告警(复用 huadian 现有 bot 或新建)
- `traceguard ack <alert_id>` CLI
- **手动模式 `start_trace` / `commit_trace`**(上一 Phase 推迟项,补上)
- 不变量 3 通用 validator 在至少一个真实业务方启用

### 4.2 范围外

- ❌ Replay-based check(`replay_consistency` 留到 Phase 2)
- ❌ 复杂统计 check(KS test 等留到 Phase 2)
- ❌ 多告警渠道(email / incident)

### 4.3 Phase 1 → Phase 2 启动条件

- 至少 3 个 check 在生产环境运行 ≥ 30 天
- 至少触发过 1 次真实告警(无论 warn / critical),验证全链路
- 出现"想改 prompt 但不敢改,因为没有可重现的回归测试"的真实痛点

---

## 5. Phase 2: Replay framework + Prompt A/B

> **目标**: prompt 升版本可以走 A/B 流程,replay set 锁定执行。
> **规模估计**: 2-3 周。

### 5.1 范围内

- `replay_sets` + `replay_set_items` + `replay_runs` 表
- `is_locked` 物理拒写(数据库层 trigger 或 ORM 层 + 测试覆盖)
- CLI: `traceguard replay create / lock / run / compare`
- Replay run executor(并行 LLM 调用,带速率限制)
- 对比报告(HTML + JSON 双产出)
- `replay_consistency` drift check
- **prompt_registry 从 YAML 升级到 DB**(可选;如果 YAML+git 仍然够用就不升)
- 强制 A/B promotion 模式作为 opt-in 开关(SPEC §7 MAY)

### 5.2 范围外

- ❌ Auto-promotion(永远是人审,SPEC 已规定)
- ❌ 多模型对比 matrix(同一 prompt × 多 model 的网格 A/B 留到 Phase 3+)

### 5.3 Phase 2 → Phase 3 启动条件

- 至少一个业务方完整走过一次"prompt 升版本 → replay → 人审通过 → 切换"流程
- 用户主动表达"想要 dashboard"或"成本对不上账"

---

## 6. Phase 3: Dashboard + 成本调和

> **目标**: 可视化 + cost reconciliation。
> **规模估计**: 2-3 周。

### 6.1 范围内(草拟)

- Grafana 面板(复用 quant_alpha_v2 已有 Grafana 或自起)
- FastAPI + HTMX 简易 trace 搜索页(避免引入前端构建链)
- 成本调和: 从 Anthropic 账单导入实际 billed 数据,回填 `cost_usd_actual`(SPEC 需要加 nullable 字段)
- PII 脱敏: `input_summary` 的电话/邮箱掩码

### 6.2 这一 Phase 是否启动取决于

- huadian 或其他接入方的真实运营压力
- 用户在 quant_alpha_v2 那边已有 Grafana 时,优先复用

---

## 7. Phase 4: Optimizer (advisor)

> **目标**: 从失败 trace 聚类提建议,辅助 prompt diff。
> **规模等级**: 愿景,不估时。

- Root cause: 低分 / 失败 trace 聚类
- Prompt diff 建议(基于失败案例)
- 始终是 advisor,**never auto-apply**(SPEC 已规定,从 pipeline-guardian 继承)

---

## 8. 跨 Phase 的纪律

### 8.1 SPEC 修订纪律

- 每个 Phase 开工前先 review SPEC,如有新事实迫使修订,**先改 SPEC 再 PR 代码**
- SPEC 改 minor / major 时,在 ROADMAP 也加注

### 8.2 兼容性纪律

- 任何对 huadian 当前使用的 `pipeline-guardian` API 的改动 = PR review 时显式说明
- 默认假设 huadian 不会主动迁移,所有迁移路径都是可选

### 8.3 测试纪律

- 不删除现有 222 个 guardian 测试
- 新 traceguard 测试与 guardian 测试隔离目录、隔离 fixture
- 不变量 validator 必须有正反向用例(违反时确实 raise,符合时确实不 raise)

### 8.4 不做的纪律

- 不为"假想未来项目 B1/B2/PEAD+"提前实现功能。它们只是参考用例,不是需求来源。
- 不引入 SPEC 没要求的依赖(Postgres、TimescaleDB、Neo4j、Redis 等)。Phase 0 仅新增 `traceguard` 包必需的 SDK 依赖。
- 不替 huadian 做迁移决策。

---

## 9. 风险登记

| 风险 | Phase | 缓解 |
|---|---|---|
| `input_hash` normalize 算法早期不稳 | 0 | 算法实现冻结后才把 SPEC 标 stable;早期发现 bug 走 major bump + 迁移期 |
| 真实接入方拖延,Phase 0 验收 #7 长期无法满足 | 0 | 允许用 prototype 项目代替验收(不阻断 Phase 1 启动) |
| huadian 在某次更新中无意被新 SDK 影响 | all | 测试隔离 + import 检查脚本 |
| Phase 1 告警过敏,产生 alert fatigue | 1 | 严格按 SPEC §5.3 的去重策略,新增 check 必须先 dry-run 7 天 |
| Prompt YAML 数量爆炸,git diff 难读 | 1-2 | 触发条件出现时再迁 DB,不预先优化 |
| Replay 成本失控 | 2 | replay run 必须先 dry-run 报成本估算,人确认后再执行 |

---

## 附录 A: 与原 `TRACEGUARD_DESIGN.md` 的差异

原 DESIGN 文档把"宪法 + 路线 + 业务 check 清单"合一,且基于"三个项目都依赖 traceguard"的事实偏差。本 Roadmap 对其的关键调整:

| 项 | 原 DESIGN | 本 Roadmap |
|---|---|---|
| Phase 0 时长 | 1 周 | 5-8 个工作日(诚实估算) |
| Phase 0 范围 | 8 项任务 + 3 个 wrapper + 80% 覆盖率 | 砍到核心,只 Anthropic wrapper,60% 覆盖率 |
| 存储 | 强制 TimescaleDB | SQLite,Postgres 与 TimescaleDB 由真实压力触发 |
| Prompt registry | 表 | YAML + git(Phase 0),按需迁 DB |
| 三个项目接入 | Phase 0 验收要求之一 | 不要求;huadian 或 prototype 都可 |
| OpenAI / Voyage wrapper | Phase 0 | 推迟到真有项目用 |
| Phase 间启动 | 时间驱动 | 条件驱动(明确 gate) |

原 DESIGN 文档保留作历史 reference,**不是 binding contract**。

---

## 附录 B: 修订历史

### v0.1 (2026-05-18)

- 初版。从 `TRACEGUARD_DESIGN.md` 抽出实施路线,按 SPEC v0.2 + 与对方 Claude 同步后的真实事实重排 Phase 范围。

---

**End of Roadmap.**

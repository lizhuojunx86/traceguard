# INTEGRATING — 接入 TraceGuard 的工作流

> 任何想把项目接入 traceguard 的人(或 Claude Code session)第一份读的文件。
> 协议: 纯 markdown + 固定文件约定。无 CLI、无 issue tracker、无服务依赖。

---

## 0. 三句话总结

1. 安装 traceguard,读 `TRACEGUARD_SPEC.md` 知道契约,读 `consumers/huadian.md` 参考一个已有接入。
2. 在你自己 repo 根目录建 `<project>_TRACEGUARD_INTEGRATION.md`(用 `templates/PROJECT_INTEGRATION_TEMPLATE.md` 填空)。
3. 遇到 traceguard 不够用 / SPEC 不清晰 / 想加新东西,**不要自己改 traceguard**,在你自己 repo 写 `traceguard-feedback/<date>-<slug>.md`(用 `templates/FEEDBACK_TEMPLATE.md`)。下次用户在 traceguard session 里说"处理 X 路径的反馈",我会逐条评估。

---

## 1. 安装

```toml
# 在你的 pyproject.toml
[project]
dependencies = [
    "traceguard @ git+https://github.com/<owner>/traceguard.git@<tag-or-sha>",
]
```

具体 tag/SHA 由 traceguard 维护者(本仓库)告知。当前可用基线:
- `v0.1.0-huadian-baseline`(老 API,只为 huadian 兼容,不推荐新项目使用)
- 新 SDK 尚未发版(Phase 0 进行中)

---

## 2. 必读文档(按顺序)

| 文件 | 在哪 | 干什么用 |
|---|---|---|
| `TRACEGUARD_SPEC.md` | traceguard repo 根 | 接口契约(MUST 遵守) |
| `TRACEGUARD_ROADMAP.md` | 同上 | 当前 Phase + 何时启动下一 Phase |
| `consumers/huadian.md` | 同上 | 参考一个**已存在的**接入档案 |
| `templates/PROJECT_INTEGRATION_TEMPLATE.md` | 同上 | 你自己 INTEGRATION 文档的填空模板 |
| `templates/FEEDBACK_TEMPLATE.md` | 同上 | 反馈条目的填空模板 |

---

## 3. 创建你的 INTEGRATION 文档

SPEC §7 要求每个接入项目维护一份 `<project>_TRACEGUARD_INTEGRATION.md`(放在你**自己** repo 根目录,不是 traceguard repo)。

复制 `templates/PROJECT_INTEGRATION_TEMPLATE.md`,按提示填空。最低要求:
- component 枚举与语义
- 使用的 `model_id` 列表
- 使用的 `prompt_template_id` 列表
- drift check 清单(若启用)

---

## 4. 反馈协议

### 4.1 在 B1/B2/PEAD+/... 自己 repo 写反馈

文件路径约定:

```
<your-repo>/traceguard-feedback/YYYY-MM-DD-<slug>.md
```

格式: 用 `templates/FEEDBACK_TEMPLATE.md`,字段固定:
- 背景
- 当前 SPEC/SDK 怎么处理
- 希望的处理
- 严重度(blocking / non-blocking / nice-to-have)
- 临时绕过方案(如有)

### 4.2 把反馈端给 traceguard 维护者

你(用户)什么时候想处理积压:

1. 在 traceguard repo 开 Claude Code session
2. 说: "处理 `<path>` 的 traceguard 反馈"
3. 我会逐条:
   - 评估影响范围(SemVer level)
   - 决定动作: 改 SPEC / 改 ROADMAP / 改 SDK 实现 / 拒绝(给理由)
   - 在原反馈文件末尾追加 `## traceguard response (YYYY-MM-DD, SPEC vX.Y)` 段落,你下次看 git diff 自然就知道

### 4.3 紧急 vs 非紧急

- **blocking**: B1 实施被卡住 → 你可以立刻开 traceguard session 处理这一条
- **non-blocking / nice-to-have**: 攒着,等你方便时一起处理

---

## 5. Boot prompt(给新接入项目的 Claude Code session 用)

下面这段你直接粘贴给开 B1/B2/PEAD+ 等新 session 时,让它快速对齐 traceguard 上下文:

```
你正在接入 TraceGuard。在做任何实质工作之前,按顺序完成:

1. 读 /Users/lizhuojun/Desktop/APP/traceguard/INTEGRATING.md(本接入工作流总说明)
2. 读 /Users/lizhuojun/Desktop/APP/traceguard/TRACEGUARD_SPEC.md(接口契约,MUST 遵守)
3. 读 /Users/lizhuojun/Desktop/APP/traceguard/TRACEGUARD_ROADMAP.md(当前 Phase 状态)
4. 读 /Users/lizhuojun/Desktop/APP/traceguard/consumers/huadian.md(参考一个已有接入的样子)

然后:

5. 在本项目 repo 根目录创建 <project>_TRACEGUARD_INTEGRATION.md,
   用 /Users/lizhuojun/Desktop/APP/traceguard/templates/PROJECT_INTEGRATION_TEMPLATE.md
   填空(component / model_id / prompt_template_id / drift check)。

6. 在本项目 repo 根目录创建 traceguard-feedback/ 目录(空,稍后用)。

工作期间纪律:

- 不修改 /Users/lizhuojun/Desktop/APP/traceguard/ 仓库下的任何文件
- 任何 traceguard 不够用 / SPEC 不清晰 / 想加新功能的想法,写到
  traceguard-feedback/YYYY-MM-DD-<slug>.md
  用 /Users/lizhuojun/Desktop/APP/traceguard/templates/FEEDBACK_TEMPLATE.md 填空
- 不要试图绕过 SPEC §5 的四条 look-ahead 不变量

报告: 完成 1-6 后,告诉我你的 INTEGRATION 文档草稿和当前阻塞项(如有)。
```

---

## 6. 已知的接入项目

| 项目 | 状态 | 接入档案 |
|---|---|---|
| huadian | ✅ 在用 baseline | `consumers/huadian.md`(上游侧 reference)+ huadian repo 内 ADR-004 |
| semdiff (B1) | ⏳ 设计中 | 待 B1 实施时创建 |
| chaingraph (B2) | ⏳ 设计中 | 待 B2 实施时创建 |
| pead_plus (PEAD+) | ⏳ 设计中 | 待 PEAD+ 实施时创建 |

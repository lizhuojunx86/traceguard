# cct issue — ✅ 已提交 2026-08-08

**https://github.com/davila7/claude-code-templates/issues/788**

状态 Open，两张表正常渲染，标题与正文与下面的草稿一致。

仓库：`davila7/claude-code-templates`
类型：Issue（不是 PR。先报告，修法有两种，选哪种是维护者的决定）

**标题**

```
8 of 423 shipped agent definitions fail to start: the model: value is not a model id
```

---

## 正文

Eight of the agent definitions in `cli-tool/components/agents/` cannot run. A
subagent using one of them dies before it emits a token, with the model-selection
error, and the failure surfaces to whoever invoked it.

I measured all 423, and tested every distinct `model:` spelling against a live
Claude Code rather than reasoning about them.

### The eight

| `model:` value | files | what happens |
|---|---|---|
| `Claude Sonnet 4.5` | 3 | hard error |
| `Claude Sonnet 4.5 (copilot)` | 1 | hard error |
| `Claude Sonnet 4` | 1 | hard error |
| `claude-sonnet-4` | 3 | hard error |

The first five are display names sitting in a field that takes a model id. The
last three are different, and more interesting: **`claude-sonnet-4` is spelled
correctly and used to work.** That generation has since been retired, so those
three definitions broke without anyone touching them.

The error, verbatim:

```
There's an issue with the selected model (Claude Sonnet 4.5).
It may not exist or you may not have access to it.
```

The subagent records `model = <synthetic>`, `stop_reason = stop_sequence`, and
zero input, output and cache tokens.

### What I ran

Claude Code **2.1.226**, macOS arm64, main thread on `claude-opus-5`. For each
distinct spelling in the repo I wrote a project-level definition containing only
that `model:` value, invoked it once, and read the model back out of the
transcript at `~/.claude/projects/**/subagents/**/agent-*.jsonl`.

Two notes for anyone reproducing this:

- Read the `version` field in the transcript, not `claude --version`. On my
  machine those disagree by 74 patch releases, because `--version` was answering
  from a stale install on `PATH` rather than the process that actually ran.
- The subagent registry is snapshotted at session start. A definition file
  created inside a running session is not visible to it; you get
  `Agent type '...' not found`. Restart before testing.

### The full census, for context

| category | files | behaviour |
|---|---|---|
| no `model:` field | 356 | inherits the main thread's model |
| alias (`sonnet` 47, `haiku` 1) | 48 | resolves to the current model in that tier |
| exact id (`claude-sonnet-4-5` ×2, dated ×1) | 3 | pins that specific model |
| `inherit` | 1 | inherits, explicitly |
| **broken** | **8** | **hard error** |
| no frontmatter | 7 | not tested — different question |
| **total** | **423** | |

### Suggested fixes, in the order I would do them

**The eight.** One line each. `Claude Sonnet 4.5` → `sonnet`, and
`claude-sonnet-4` → `sonnet`. I can send a PR if that is useful, but the
substitution is a judgement about intent and you may prefer to make it yourself.

**Worth considering separately: alias over exact id.** The three definitions that
pin an exact model still work today, and the three that pinned `claude-sonnet-4`
are the same pattern one generation earlier. A pinned model has a shelf life. An
alias does not, because tiers do not get retired. If a definition does not
actually need a specific model, `sonnet` ages better than any id.

**The 356 with no `model:` are not a bug**, and I am not reporting them as one.
They inherit, which may well be what you want. It is worth knowing that they
inherit rather than defaulting to something modest: a user running Opus gets
Opus for every one of them, including the ones that read a file and summarise it.

### What I did not test

- The 7 definitions with no frontmatter. Whether they register as subagents at
  all is a different question from which model they route to, and needs a
  different probe.
- Any version other than 2.1.226. Subagent model behaviour has changed at least
  once already — the built-in `Explore` agent stopped being hardcoded to Haiku
  in v2.1.198 — so a result without a version number is not reproducible.
- `claude-sonnet-4-5` **still resolves**, so please do not read this as "old ids
  are dead". Only the `sonnet-4` generation is gone. The retirement boundary
  falls between those two, which is exactly why this needs measuring rather than
  assuming.

---

## 提交前自查（我的，不进 issue）

- [ ] 标题数字是 8，不是 356。**坏掉的是新闻，继承的是设计选择。**
- [ ] 没有一处写"你们坏了"。84% 缺字段更可能说明默认行为不直观，
      而不是 423 个作者都疏忽——这个判断留在文章里，不进 issue。
- [ ] 三条未测项显式列出，包括那条"别读成旧 id 都死了"。
- [ ] 逐字错误信息、cc 版本号、复现障碍（注册表快照）都在。
- [ ] 没有提 HKEX，没有提 traceguard 的商业面。**这是一份 bug 报告，
      不是一次推广。** 前四次成功都是这个形状。
- [ ] 他有一个 PR #754 在那儿挂了两周。**不要在 issue 里提它。**

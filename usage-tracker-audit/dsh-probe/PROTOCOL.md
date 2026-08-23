# DSH 记账缺陷实测复现协议

**目的**：把三条源码级判断变成三个可引用的数字。发表任何一条之前，这一步不能省。

**预期工作量**：约一天，其中真正花 API 额度的部分不到 10 分钟。

---

## 三条待验证的判断

| 编号 | 判断 | 源码依据 |
|---|---|---|
| **H1** | 同一次调用的 usage 同时挂在 `assistant/chunk{type:'usage'}` 和 `assistant/message.usage` 上；朴素累加会翻倍 | `packages/llm/token-meter/src/usage-projection.ts` 的 `usageOf()` 两种都读，`apply()` 按 `(turn, step)` 减旧加新 |
| **H2** | fork 子会话的日志里含有父会话已完成前缀的**完整副本**（`seq < header.seedLength`）；跨会话求和会重复计 | `packages/subagent/subagent-fork-in-process` 的 Seed boundary；`packages/session/session-persistence/src/coordinator.ts:1117` |
| **H3** | `compaction/summary.usage` **不被官方 `tokenUsage` 投影折叠**，所有基于该投影的插件都少算每次 compaction 的摘要成本 | `usage-projection.ts:116-123` 的 `apply()` 只匹配 `assistant/chunk` 与 `assistant/message`；写入点在 `compaction/compaction-basic/src/region.ts:447` |

**已经排除的一条**（写进结论里，比只列问题更可信）：`packChunks`（默认开）把 ≥3 个连续同块 delta chunk 打包成一行，但编码器白名单只收 delta，官方注释原文 *"block-start/end, usage, finish ... stay one event per line"* —— **usage 永远不会被打包**，所以压缩打包不构成记账风险。

---

## 第 0 步：先验证探针本身（零成本，零 API）

```bash
python3 dsh_usage_probe.py --self-test
```

它会构造一份**手算过真值**的双会话样本（父会话含一次双写 + 一次 compaction；子会话含 7 条 seed 副本），然后断言四种口径的折叠结果。

四条全 PASS 才能往下走。这一步是你自己 `usage-tracker-audit/` 里一贯的做法：**先在合成数据上证明检查器是对的，再让它碰真实数据**。

预期输出：

```
  naive             16,290   expected     16,290   PASS
  official           7,920   expected      7,920   PASS
  seed_aware         4,260   expected      4,260   PASS
  correct            4,710   expected      4,710   PASS
```

---

## 第 1 步：建一个隔离的探针 profile

**不要用你日常的 DSH profile。** 探针需要三个非默认配置，其中两个会显著改变行为。

```bash
mkdir -p ~/dsh-probe && cd ~/dsh-probe
```

`probe.patch.yml` 已随本目录提供，只有一行覆盖：

```yaml
- id: session-persistence-jsonl
  name: '@deepseek-ai/dsh-session-persistence-jsonl'
  config:
    root: /Users/lizhuojun/dsh-probe/sessions
    compression: none
    packChunks: false
```

格式依据 `vendor/include/src/index.ts` 的 `applyEntryPatches`：补丁是一个**扁平的 `PatchOptions` 列表**（`{id, name?, config?, disabled?}`），`insert` 只用于新增行。两条硬规则：

- **覆盖按 key 整体赋值**（`target[key] = value`），`config` 不做深合并——所以必须重述该行需要的全部 key，包括 `root`（它没有默认值）。
- **`name` 是可选护栏**：写了但与目标行不符，补丁会被**跳过并告警**而不是报错。写上它，是为了 base bundle 改名时你能立刻看见，而不是静默拿到一份没生效的配置。

**刻意不覆盖 `compaction-basic`。** 调低 `thresholdRatio` 看似能省 token，但 README 明写「`retainRatio` 不低于 `thresholdRatio` 会导致插件加载失败」且「重试后仍回不到阈值以下就抛错」——两个比例一起压小很容易撞上压缩循环或加载失败，而这两种故障都会污染语料。用 `/compact` 手动触发就够了（见第 2 步）。

**先确认用的是哪个 profile。** `$DSH_HOME` 默认 `~/.dsh`（`packages/util/home-paths/src/index.ts` 的 `DSH_HOME_DIR_NAME = '.dsh'`），profile 在它下面的 `profiles/`：

```bash
ls ~/.dsh/profiles
```

`ollama launch dsh` 建的 profile 里带着能用的模型路由，**要用它**，别用空的 `web`。

**再 dump 一次确认补丁生效**，不要凭信心往下走：

```bash
dsh --profile <你的profile名> --patch ./probe.patch.yml --dump-config > composed.yml
grep -n -A6 "session-persistence-jsonl" composed.yml
```

⚠️ **两个 CLI 陷阱**（都来自 `apps/cli/src/args.ts`）：

- `--profile <name>` 是必需的。裸 `dsh --dump-config` 报 `error: --profile <name> is required`。
- **父级选项不能放在子命令前面。** `web` 子命令的 action 第一件事就是 `rejectParentOptions('web')`——`dsh --patch x.yml web` 会直接报错。`web` 是 `--profile web` 的别名，它有**自己的** `--patch` / `--dump-config`，要写成 `dsh web --patch x.yml`。

`root` / `compression` / `packChunks` 三个值都对了才继续。如果看到 `patch: entry ... not found` 或 `name mismatch` 的告警，说明补丁没生效——**此时日志仍会以 zstd 写出，探针读不了明文**。

⚠️ **装包必须带 `@next`。** `@deepseek-ai/dsh-*` 各子包的 npm `latest` tag 停在半年前的 `0.0.1-rc.1`，当前版在 `next`（`0.1.0-rc.6`）。裸 `npm i` 会静默装到旧版。

---

## 第 2 步：造一个含 compaction + fork 的真实会话

```bash
dsh --profile web --patch ./probe.patch.yml --port 3099
```

**换个端口，别杀掉已在跑的那个实例。** 默认 3080 被占会直接 `EADDRINUSE` 抛出并终止 Loader 组合（webserver 的 README 明说 listen 失败会拒绝整棵树）。用 `--port 3099` 起第二个实例更好：

- 已有实例用**默认** session root（`~/.dsh/sessions`，zstd 编码），探针实例用 `~/dsh-probe/sessions`（明文）——**两个语料天然隔离**，不会互相污染。
- 你原来的会话不受影响。
- `--port` 是 web app 自己的 flag，不是 launcher 的；launcher 的 flag 到第一个它不认识的 token 为止，后面原样传给 app。所以顺序是 `dsh --profile web --patch <path> --port <n>`。
- 传 `--port 0` 可以让 OS 随机挑一个空闲端口。

（`web` 别名的等价写法是 `dsh web --patch ./probe.patch.yml --port 3099`——`--patch` 必须在 `web` **后面**。）

在 Web UI 里按顺序做四件事，**每一步之间停一下**，让日志有清晰边界：

1. **先产生 4–6 轮普通对话** —— 让它读几个文件、跑几条命令。两个目的：让 `assistant/chunk` 的 usage 与 `assistant/message.usage` 各自落盘（验证 **H1**），以及**攒够可压缩的历史**——`/compact` 在没有可压缩历史时只会回 `No compactable history yet.`，连 marker 都不写。
2. **触发一次 subagent fork** —— 让它调 `subagent_fork` 工具（例如"fork 一个子代理去统计这个目录下的文件数"）。必须是 fork 不是 spawn：seed 副本是 fork 路径独有的。这验证 **H2**。
   > base 配置里两者是分开的两行：`subagent-in-process-driver`（`providerName: spawn`）与 `subagent-fork-in-process`（`providerName: fork`），对应工具 `subagent` 与 `subagent_fork`。
3. **手动触发 compaction** —— 打 `/compact`（无参数，带参数会被拒）。命令契约原文是「即使在自动压力之下，也摘要一段有用的较旧跨度」——**这是最省钱的 H3 触发方式**，不用把上下文撑到阈值，也不用改任何压缩策略。
   > 成功后它会报告被替换的历史条目数与估算 token。更有用的是：`command/done.sourceEventSeq` **直接指向本次事务的 `compaction/summary` 事件 seq**——拿这个 seq 去日志里核对，就不用靠相邻行猜。
   > 若返回 `busy` / `changed` / `summary` 三种错误之一，说明这次没写出 summary，回第 1 步多跑两轮再试。
4. **再来一轮对话**，让 compaction 之后还有正常 usage，便于对比。

做完退出，确认日志落盘：

```bash
find ~/dsh-probe/sessions -name "session.jsonl" | head
# 应该看到 <root>/--<归一化 cwd>--/<会话 id>/session.jsonl
# fork 出来的子会话是【平级的另一个目录】，不是父目录的子目录 —— 这一点和
# Claude Code 相反，也是 H2 之所以隐蔽的原因
```

---

## 第 3 步：量

```bash
python3 dsh_usage_probe.py --root ~/dsh-probe/sessions --json probe-report.json
```

输出四种口径与三条机制归因：

```
N  naive                  每见一个 usage 就加（含 compaction）
O  official               逐行转写 tokenUsageProjectionDefinition.apply
S  seed-aware             O + 跳过 seq < header.seedLength
C  correct                S + compaction/summary.usage

dual-write inflation      N-O    → H1
fork-seed duplication     O-S    → H2
compaction undercount     C-S    → H3
```

---

## 第 4 步：判读

| 观察 | 含义 | 下一步 |
|---|---|---|
| `N-O` 显著为正 | **H1 坐实**。朴素实现虚高的幅度就是这个数 | 记下比率；这是"为什么必须按 (turn,step) 去重"的实证 |
| `O-S` 在子会话上为正 | **H2 坐实**。而且这一条对聚合器的杀伤最大——会话越多，重复越多 | 单独记子会话的 seed 长度与被重复计的 token 数 |
| `C-S` 为正 | **H3 坐实**，且这是**官方自己也漏的那条** | 这条最值得单独成文，因为它不是"社区插件写错了"，是投影定义本身的覆盖缺口 |
| `C-S` 为 0 但确实跑了 compaction | 说明该 provider 没有在 `compaction/summary` 上报 usage（类型里 `usage?` 是可选的） | **不要发 H3**。改成"在 DeepSeek 官方 provider 上未观测到"，并说明条件 |
| seq 不连续 | packChunks 没关掉，或日志尾部截断 | 回到第 1 步确认 patch 生效 |

---

## 第 5 步：交叉验证（提高可发表性）

至少再做一件，让数字不只出自你自己的实现：

1. **对拍官方投影**：在 DSH 里装一个直接读官方 `sessionProjections.tokenUsage` 的插件（例如 `dsh-balance-meter` 走的就是这条路），把它显示的数字和探针的 `O` 对上。**两个独立实现给出同一个数**，才能把剩下的差额定性成缺陷而不是实现分歧——这正是你在 splitrail 上用过的手法（3.6.0 与审计日志 output token 精确到个位相同）。
2. **拿真实插件复现**：`Make0209/dsh-usage-stats`、`Ychris12138/dsh-usage-stats` 这两个做全局聚合的插件，源码里都没有 `seedLength` 过滤。把它们装上，指向同一个 root，看它们报的总数落在 `O` 还是 `S`。落在 `O` 就是 H2 的第三方实证。

---

## 发表前的检查清单

- [ ] `--self-test` 四条全 PASS
- [ ] 每条缺陷都有**一个可引用的数字**和**产生它的会话 id**
- [ ] 每条判断都能指到具体源码文件 + 行号
- [ ] `C-S` 若为 0，**H3 改写成条件性陈述**，不发绝对断言
- [ ] 至少一条经过第 5 步的独立交叉验证
- [ ] 引用的所有外部数字（插件总数、star 数）**当天重新取值**，不引用文档里带日期的历史快照

最后一条是之前踩过的坑，记在别处。这次别再踩。

---

## 顺带的副产品

跑完这一轮，你手上还多了两样东西：

- **一个 DSH 基准测试台**。同样这套 profile，把 `minimax-m3` 换成别的模型跑同一批任务，"值不值那 $18/$20" 就有真数据了。
- **CONFORMANCE 的 DSH 分支素材**。你现有 11 条不变量里，I-4 与 I-7 在 DSH 上是**结构性满足**（append-only + 只发终值 usage），I-1 变形、I-5 反转、I-11 全面适用。一个会说"这几条在这里不适用"的目录，比一个只会加规则的目录可信得多。

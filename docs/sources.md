# `traceguard.sources` — 取回数据的时点正确性

**实验性**(SPEC v1.2 §6.6)。给"管线从外部取回的那份数据"补上不变量 3 的判定,
并把判定结果与内容摘要一起落库。off 掉顶层冻结面 —— 从 `traceguard.sources` 导入,
不从 `traceguard`。零新增依赖(stdlib `hashlib` + 既有的 SQLAlchemy)。

## 契约地位:实验性,且现在**不**进 contract-guard

与 `traceguard.audit` 早期同款:SPEC §6.6 登记,字段表在
`docs/spec-changes/2026-09-10-source-snapshot-approval-binding.md`,但公开 API 面
**不**进 `contract-guard` CI job。理由是纪律不是懒惰——先让这些字段在真实 trace 上
跑够(ROADMAP A4 的 gate:≥100 条),再决定冻哪些。两个 minor 之后重新评估 graduate
(修订案决策 D9)。

在此之前,这个模块的符号**可能改名或消失**,不受 SemVer major 保护。

## 它补的是哪个洞

SPEC §5 的四条不变量覆盖**模型**(不变量 2)、**prompt 与引用表**(不变量 3)、
**feature 之间的时序**(不变量 1)。唯独不覆盖**管线取回的那份数据**。

后果是可以具体说出来的:一次回测用了正确的模型、正确的 prompt、正确的
`feature_as_of`,却喂进一个在 `feature_as_of` 之后才被供应商改写出来的值——
四条不变量**全部通过**,而结论是错的。

这不是假想。已发表的测量(`analysis/eps_revision.py` 可离线复算,数据在本仓库):
供应商 `epsActual` 的首见值与现值不同的比例 **41.4%**,其中翻转二元入场决策的
**15.3%**;第二次独立捕获为 **18.6% / 4.6%**。

## 证明什么 / 证明不了什么

诚实分层,与 `docs/audit.md` 同一套写法:

| | 内容 |
|---|---|
| **证明什么** | ① 宿主在 `retrieved_at` 交给 `record_source` 的**那些字节**是什么(按 `content_hash`);② 这些字节声称的发布时间与 `feature_as_of` 的**时间关系**(即 `verdict`);③ 记录发生时调用点声明的模式(`strict`) |
| **证明不了什么** | ① 宿主**确实**从 `source_uri` 取回了它们——traceguard 没有发出那个请求,字节是宿主递过来的;② `published_at` 是**真的**——那是源自己说的,一个愿意改写历史数据的源同样可以给出任意的 `Last-Modified`;③ 这一行事后没被改过——`source_snapshots` **不在** audit algo v1 哈希信封内,链不 attest 它 |

第 ①、② 条不是可以靠工程消除的缺陷,是这一层的**位置**决定的:SDK 跑在宿主进程里,
看到的只能是宿主给它看的东西(与 `docs/audit.md` 的 L0 是同一个道理)。要往上一层,
需要的是代理位置的网络观测面,而 POSITIONING 已经把那个位置划出去了。

## `verdict` 的四个值,以及为什么不能合并

沿 `traceguard.routing_integrity` 的四级判定先例:**跑了但什么都没比对**与**比对通过**
必须是两件事。

| verdict | 含义 | actionable |
|---|---|---|
| `verified` | `published_at <= feature_as_of`。内容在模拟时点确实已存在 | 否 |
| `anachronistic` | `published_at > feature_as_of`。内容当时还不存在——不变量 3 要抓的就是它 | **是** |
| `unverifiable` | 源没声称任何 `published_at`,存在性既不能确认也不能排除 | **是** |
| `unchecked` | 调用点没给 `feature_as_of`,压根没做比对 | 否 |

- `unverifiable` ≠ `verified`:**无法证明存在不等于证明不存在**,更不等于通过。
- `unverifiable` ≠ `unchecked`:前者是"源不肯说",后者是"我们没问"。
  drift 统计里两者**都不计入观测**(`analysis/eps_revision.py` 的纪律:失败与未校验不是观测),
  但它们要分开报,否则"没接上采集"会被读成"源不给时间戳"。
- `unchecked` 不 actionable:什么都没声称,就没有什么可以不信。把它算进去,等于让
  没插桩的调用点撑大报告——那是教人忽略告警最快的办法(SPEC 附录 B3.4)。

## strict / loose:`published_at` 未知时的两种模式

修订案决策 D1。**这是本模块唯一会 raise 的地方**:

```python
span.record_source(snap, strict=True)   # 源不给 published_at → raise InvariantViolation
span.record_source(snap, strict=False)  # 同一份 snap → verdict 'unverifiable',照常记录
```

strict 模式的措辞是固定的:
`cannot establish that the source existed at feature_as_of`。

`strict` 是 keyword-only 且**无默认值**,与 `select_model` 同款(决策 D10):
"拿不出证据要不要停下来"是一个决策,每个调用点都得说出自己的意图。

**校验发生在 `record_source()` 调用时,不在 flush 时**——这一点是实现上的硬约束,
不是风格:行的写入被推迟到 tracer 的 flush,而那里按 SPEC §4.1 是 fail-open 的。
如果把 strict 的拒绝放到 flush,`_flush_safe` 会把它吞掉,strict 就被静默架空了。

## 不存原文

**本扩展不存储取回内容的原文**,只存摘要与元数据。这是契约意图,不是一个可以关掉的模式。

与 pit-archive 式原文归档的分工:

| | 回答的问题 |
|---|---|
| `traceguard.sources` | 那份字节的**摘要**是什么、什么时候拿到的、与 `feature_as_of` 什么关系 |
| 消费者自己的原文归档 | 那份字节**是什么** |

两者可以并存,`content_hash` 就是它们之间的 join key。

理由不是隐私姿态,是能力边界:一个跑在宿主进程内的 SDK 扩展去存第三方原文,会把
license、PII、体积三件事同时揽进契约,而这三件事没有一件是本扩展能替消费者决定的。

## 用法

```python
import traceguard
from traceguard import sources

engine = traceguard.make_engine("sqlite:///traces.db")
sources.enable(engine)            # 显式;import 本身零副作用
traceguard.tracer.configure(engine)

with traceguard.tracer.span("quant", "eps", "llm_complete", feature_as_of=as_of) as span:
    resp = httpx.get(url)                                  # 宿主自己发请求
    span.record_source(sources.from_http_response(resp), strict=False)
    ...
```

三个构造器,按宿主需要说明多少排列:

| 构造器 | 用于 | 备注 |
|---|---|---|
| `content_digest(bytes \| str)` | 原语,返回 `(sha256_hex, content_encoding)` | `str` 按 UTF-8 编码并记下这件事 |
| `from_http_response(resp, *, source_kind="http")` | HTTP 响应 | 鸭子类型:只读 `url` / `headers` / `content`,**不 import** httpx 或 requests |
| `from_mcp_result(server_id, tool_name, result, *, source_uri=None)` | MCP 工具结果 | 结果到手已是解析过的对象,没有 wire 字节可哈希——见下 |

**`content_hash` 不做任何规范化**(决策 D6):空白、BOM、换行、键序都是"被送来的东西"
的一部分,在这里折叠掉它们,等于悄悄把源的一次真实改写藏起来。

**`normalized_hash` 必须与 `normalizer_id` 成对**,格式 `<name>@<version>`,两个方向
都强制。没有名字和版本的规范化器产出的 hash **不可比**——更糟的是,它**看起来**可比。

`from_mcp_result` 是这条规则的直接后果:MCP 结果到手已经解析过,没有 wire 字节了,
于是 `content_hash` 覆盖的是 §4.4 canonical normalize 的输出,而 `normalizer_id` 把这件事
说出来(`normalized_hash` 带同一个值,好让"凡是出自规范化器的摘要都带着它的名字和版本"
这条规则没有例外)。规范化用的是 §4.4 已经权威的那一个,**不另写一套**——两套哈同一个
结构的方式,正是 `normalizer_id` 要防的失败模式。

## CLI

```bash
python -m traceguard.sources --db sqlite:///traces.db enable
python -m traceguard.sources --db sqlite:///traces.db list --verdict anachronistic
python -m traceguard.sources --db sqlite:///traces.db list --source-uri 'https://vendor.example/%' --json
```

`--db` 是顶层选项,**必须放在子命令前面**。`list` 在列出任何 actionable verdict
(`anachronistic` / `unverifiable`)时退出码为 1,可以直接拿来卡 CI。

## 失败语义(SPEC §4.1)

snapshot 行的写入**绝不**影响 trace 写入或宿主调用:

- 行与它的 trace 在**同一事务**内写入,`trace_id` 是刚签发的真实主键——snapshot 不可能
  指向一个被回滚掉的 trace。
- 同时,写入跑在一个 **SAVEPOINT** 里:失败只回滚这个 savepoint,trace 照常提交。
  这两条要求方向相反,savepoint 是调和它们的东西(`traceguard.audit.chain` 在同一套
  SQLite 上用的是同一个机制)。
- `strict_persistence` **不**让这一层 fail-closed:不变量 3 的决策在 `record_source`
  时就已经做完并 raise 过了,留在这里的是记账,而丢记账不该连带丢掉它描述的那条 trace。
- 没调用过 `sources.enable(engine)` 就 `record_source`:verdict 照算照返回(strict 调用点
  仍然会拒绝),只是行写不进去,并留一条指名修法的 WARNING。

## 与 audit 的关系

`source_snapshots` **完全在 audit algo v1 哈希信封之外**,链不 attest 它。这与
`agent_id` / `session_id` / `provider_response_id` 是同一种情形,`docs/audit.md`
已照实登记。纳入信封待 algo v2。

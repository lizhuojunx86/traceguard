# `evidence-bundle/v1` — 证据 bundle 导出格式

一个自包含的 JSON 文档:选定的 trace、覆盖它们的链段与链头、锚记录、
`source_snapshot`、reconcile findings。设计目标只有一个 —— **让拿到它的人在没有
数据库、没有网络、没有 traceguard 的情况下,自己把能验的东西验一遍**。

**仅 JSON,不做 HTML / PDF。** 可机器验证的只有一种形态;多受众版本是模板加人的
判断,不是代码(ROADMAP 2026-09-10 "报告形态类"的不做理由)。

- Schema: [`evidence-bundle-v1.schema.json`](evidence-bundle-v1.schema.json)(JSON Schema 2020-12)
- 实现:`traceguard.audit.export_bundle()` / `verify_bundle()`,
  CLI `python -m traceguard.audit --db URL bundle --out PATH` 与 `verify-bundle PATH`
- 契约地位:SPEC v1.2 §6.6 audit 条目登记的导出格式。schema 是契约
  (照 `usage-drift-log.md` 的做法:先写 spec,再有第二个实现)

---

## 1. 顶层结构

```json
{
  "schema": "evidence-bundle/v1",
  "generated_at": "<ISO 8601, tz-aware>",
  "generator": "traceguard <version>",
  "content_mode": "full",
  "chain": {"algo_version": 1, "head": {...}, "entries": [...]},
  "traces": [...],
  "source_snapshots": [...],
  "anchors": [...],
  "findings": [...],
  "cost_events": [...],
  "approvals": []
}
```

| 字段 | 语义 |
|---|---|
| `schema` | 固定 `"evidence-bundle/v1"`。**先查 schema 标识再解析**,不嗅探——一份被误读的 bundle 会产出关于它从未描述过的调用的自信结论 |
| `generated_at` | 导出时刻。**不是**证据的一部分,只是出处标注 |
| `generator` | `traceguard <version>`。哪个实现产出的 |
| `content_mode` | `"full"` \| `"hash_only"`,见 §3 |
| `chain` | `algo_version`、`head`(链头 `{seq, row_hash, algo_version, entry_count, exported_at}`)、`entries`(链条目,含 `prev_hash` / `row_hash` / 元数据) |
| `traces` | 选定的 trace 行。`hash_only` 模式下**剥掉内容字段**,见 §3 |
| `source_snapshots` | 这些 trace 的 `source_snapshot`(可选,`include_sources=False` 时为空数组) |
| `anchors` | 外部锚记录,见 §4 |
| `findings` | 预留给 verify / reconcile 产出的 finding,`{kind, severity, seq, trace_id, detail, direction}`。**`export_bundle` 今天恒写空数组** —— 导出时不跑验证,收件人自己跑 `verify_bundle` 拿 finding;这个槽位是给别的产出方(如 tg-attest)用的 |
| `cost_events` | 被 `cost_event` 链条目引用的 `audit_cost_events` 行。**`full` 模式验证这些条目必须有它**——条目哈希盖的是事件行本身,没有它就重算不出来 |
| `approvals` | **保留字段,当前恒为空数组**。`traceguard.approval` 是 SPEC v1.2 登记的规划项,尚未实现;字段先在,免得将来加它变成 schema 破坏 |

---

## 2. 验证证明什么 / 证明不了什么

`verify_bundle()` 做四件事,每件事的边界都写在这里,不许在别处弱化:

| 检查 | 证明什么 | 证明不了什么 |
|---|---|---|
| **链的衔接** | 每条 entry 的 `prev_hash` 等于前一条的 `row_hash`,即 bundle 里的链段是一条**连续**的链 | 这条链是不是完整的历史。被截掉的尾部留下的仍是一条合法的短链 |
| **条目哈希重算**(仅 `full`) | 每条 entry 的 `row_hash` 可以从它的元数据 + bundle 里那条 trace 的内容重算出来,即**内容与条目相符** | 内容本身是不是真的(采集真实性,见 `docs/audit.md` L0/L1/L1.5) |
| **锚定绑定** | 锚记的 `seq` 落在 bundle 携带的 entry 里,且哈希与那条 entry 相符,即**这批 entry 被外部锚覆盖** | 锚本身可信 —— 那取决于锚存在哪儿、谁能改它。`file:` 锚与库文件同主机时几乎不证明什么 |
| **链头对锚**(**仅当**锚记的 `seq` 正好是 `chain.head.seq`,或旧锚没记 `seq`) | 锚等于 bundle 声明的 `chain.head` | **什么都不证明**:`chain.head` 是 bundle 自己的字段,重写内容再重链整段,head 与锚都原封不动。此时报 `anchor_unlinked`(WARN),结论词降为 INTERNALLY CONSISTENT |
| **不比**(锚落在窗口外的任何其它位置) | —— | 什么都不比,也**不报 BREAK**。一个如实记录了别的链位置的锚,对这批 entry 本来就无话可说;拿它的摘要去跟 head 比,等于把一个诚实的锚报成「链被重写了」的证据。报 `anchor_outside_window`(WARN),见 §4 |
| **结构校验** | bundle 符合 schema;`rfc3161` 锚的结构完整 | **不做密码学验签**,见 §4 |

**bundle 自身不是防篡改的**:它是一份可以被任意编辑的 JSON。它的价值在于
*内部一致性可以被重算* —— 改了里面的 trace 内容,`full` 模式重算就对不上;改了
`prev_hash`,两种模式都对不上。改了内容**并且**重算全部哈希,则 bundle 内部自洽,
**只有一个覆盖了这批 entry 的外部锚能发现** —— 与 `docs/audit.md` 边界声明 1
完全同构。

"覆盖"这两个字是全篇最容易被跳过、也最要命的地方。锚要能拆穿重链,它记的 `seq`
必须落在 bundle 携带的那段 entry 里,验证时拿它和**那一条 entry** 的 `row_hash`
比。如果锚记的正好是 `chain.head.seq`(或者是个没记 `seq` 的旧锚),那它只能
和 `chain.head` 比 —— 而 `chain.head` 也是 bundle 里的字段,攻击者重写 entry 时
根本不需要动它。这种情况下锚的存在不构成任何佐证,`verify_bundle` 报
`anchor_unlinked`,结论词也不说 VERIFIED。

而如果锚记的是**别的**位置 —— 早于窗口(周一锚了,周三导周二的 trace,这是部分
导出的常态)、晚于 head、落在稀疏选择的断口里、或者夹在最后一条 entry 与 head
之间 —— 那它连 head 都不该去比:它如实记录的是链上另一个位置,对这批 entry 本来
就无话可说。此前的实现把这些统统送去跟 head 比,于是一个**诚实的锚**被报成
`anchor_mismatch`(BREAK)「链被截断或重写了」,整份 bundle 显示 FAILED。现在报
`anchor_outside_window`(WARN),并说明它约束不了这些条目。

想让部分导出也有佐证,只有两条路:把窗口扩到能碰到被锚的那个 `seq`,或者趁这个
窗口还是链尾时再锚一次。

---

## 3. `content_mode`:两种模式的结论**不许**合并成同一个词

algo v1 的哈希信封**包含内容字段**(`input_summary` / `output_parsed` /
`error_message` 等,见 `audit/canonical.py` 的 `TRACE_CONTENT_FIELDS`)。

- **`full`** — trace 行带全部覆盖字段。`verify_bundle` 重算每条 entry 的
  `row_hash`,内容被改动过就报 `hash_mismatch`(BREAK)。
- **`hash_only`** — 内容字段被剥掉(给收件人看 trace 存在与元数据,而不给他看
  prompt 与输出)。此时**无法重算条目哈希**,`verify_bundle` 只验证**链的衔接**与
  **链头对锚**,并产出一条 `content_not_recomputed`(INFO)finding 说明这件事。

**结论词有三档,不是两档。** `VERIFIED` 只在**链段连续**且**有锚覆盖了其中的
entry** 时才给;缺任一条就降为 `INTERNALLY CONSISTENT` —— 重链一遍的 bundle 内部
也是自洽的,这个词说的正是"只查到这一步"。按 `--trace-ids` 挑几条不相邻的 trace
导出是**合法且常见**的用法,它产生的是 `chain_gap`(WARN),**不是** `link_broken`:
跨着断口比 `prev_hash` 会把工具自己的输出报成篡改,而那会训练收件人忽略真正要紧
的那条 finding。断口两侧的 entry 彼此没有链接关系,覆盖其中一段的锚也说明不了另一段。

**bundle 带了什么、却没有任何哈希覆盖**(每次验证都以 `carried_unattested` INFO
列出,不靠读者自己去对两个字段表):

- `agent_id` / `session_id` / `provider_response_id` / `cost_usd` —— 在 algo v1
  信封之外(见 `docs/audit.md`);
- `source_snapshots` —— `traceguard.sources` 根本不上链(SPEC v1.2 D9);
- `approvals` —— 预留字段,今天恒为空。

改动上面任何一项,这份 result 里所有检查依旧全绿。

另有 `content_unattested`(WARN):某条 entry 当初 `canon_status='failed'`,链上哈希
盖的是一个**错误占位符**而不是内容。它能被重算通过,但那只证明占位符没被动过,
对那条 trace 说了什么**什么都没证明** —— 所以它不计入"对着内容重算"的条数。

**两种模式的结论用不同措辞,不许都说成 "verified"。** `hash_only` 通过意味着
"这段链首尾相连,且链头与锚一致";它**没有**对内容说过任何话。把它读成
"内容已验证"是本格式最容易犯、后果最大的误读,所以 `verify_bundle` 在
`hash_only` 下永远至少带一条 INFO finding,`summary()` 也换一套措辞。

`verify_bundle` 产出的 finding 分两类,读的时候必须分清:

| kind | 来源 | 冻结状态 |
|---|---|---|
| `hash_mismatch` / `link_broken` / `anchor_mismatch` | audit 的 finding kind,原样复用 | 进 `FINDING_SEVERITY`,受 §6.6 kind 冻结约束 |
| `content_not_recomputed`(INFO) | bundle 层:声明这次验证没碰内容 | **不进** `FINDING_SEVERITY`,不受 §6.6 约束 |
| `anchor_malformed`(BREAK) | bundle 层:`anchors[]` 条目结构不合法(见 §4) | 同上 |
| `anchor_pending`(WARN) | bundle 层:OTS 证明仍是 pending(见 §4) | 同上 |
| `anchor_unlinked`(WARN) | bundle 层:有锚被拿去和 `chain.head` 比过,但没有一个覆盖 bundle 携带的 entry(见 §2) | 同上 |
| `anchor_outside_window`(WARN) | bundle 层:锚记的 `seq` 既不在窗口内也不是 head,**没有被拿去比任何东西**(见 §4) | 同上 |
| `chain_gap`(WARN) | bundle 层:携带的 entry 在链上不连续(按 `--trace-ids` 挑选时的常态) | 同上 |
| `content_unattested`(WARN) | bundle 层:该 entry 当初是对着**规范化错误**上链的,不是对着内容 | 同上 |
| `carried_unattested`(INFO) | bundle 层:列出 bundle 带了、但没有任何哈希覆盖的数据 | 同上 |

后六个(以及 `carried_unattested`)只在**验证 bundle** 这一个动作里出现,`verify_chain` 永远不会产出它们;
把它们写进 `FINDING_SEVERITY` 会让"audit 的 kind 表"这件事失去边界,所以不写。
代价是它们不被 kind 冻结测试保护 —— 这份文档就是它们的契约,改名同样是 major。

---

## 4. `anchors[]`:结构校验,不做验签

`kind` 枚举:`file` | `git-note` | `webhook` | `rfc3161` | `ots` | `rekor`。

`rfc3161` 的结构与 **tg-attest 能产出的内容对齐**:

```json
{"kind": "rfc3161", "tsa_url": "https://...", "digest_alg": "sha256",
 "message_imprint": "<hex>", "token_b64": "<base64 DER>", "epoch_root": "<hex, 可选>"}
```

**traceguard 只做结构校验,不做密码学验签。** 这是能力边界的诚实划线,不是偷懒:

- 验签需要 TSA 证书链与信任根,而"信任哪个根"是收件人的决定,不是 SDK 的。
- 本包**零新增运行时依赖**是既有约束(SPEC §6.6 audit 条目);验签要引入密码学库。
- 验签是 tg-attest 与 `openssl ts -verify` 的事。同一 schema、两个实现,
  正是 `usage-drift-log.md` 记录过的那种分工。

所以:`verify_bundle` 会告诉你"这里有一个结构完整的 RFC 3161 token,imprint 是 X",
**不会**告诉你"这个 token 有效"。想要后者,把 `token_b64` 解出来交给 `openssl ts`。

`ots` 锚额外区分 **pending**(只有日历服务器的承诺)与 **complete**(已进比特币
区块)。**pending 不是证据**,详见 `docs/audit.md` 的 Anchors 节。

**锚怎么被用来比对**(所有 kind 一致,与验不验签无关):

先看锚记的 `seq` 落在哪儿,再决定它能跟什么比 —— **不能比的就不比**:

1. **落在窗口内**(`seq` 是 bundle 携带的某条 entry)→ 和**那条 entry** 的
   `row_hash` 比。不符 = `anchor_mismatch`(BREAK);相符计入
   `BundleVerifyResult.anchors_binding`,这是唯一约束了 bundle 内容的比对。
2. **正好是 `chain.head.seq`**,或者是个**没记 `seq` 的旧锚** → 和 `chain.head`
   比。不符仍是 `anchor_mismatch`(BREAK):head 与被锚的位置对不上是真事。
   相符**不算佐证**,见 §2。
3. **早于窗口首条 entry**,或落在稀疏选择的**断口**里,或夹在最后一条 entry 与
   head **之间**,或 bundle **根本没声明 head** → **不比**,报
   `anchor_outside_window`(WARN),计入 `anchors_outside_window`。文案会说清它
   落在哪一侧,以及拿到佐证要把窗口导到哪个 `seq`。
4. **晚于 `chain.head.seq`** → 同样不比,但换一套文案:锚比这次导出还新,该做的
   是**重新导出**,不是扩窗口。
5. 有锚**被拿去和 head 比过**、却一个都没绑上 → 另加 `anchor_unlinked`(WARN)。
   只在情形 2 发生过时才发:情形 3/4 什么都没比,再说一句「只跟 head 比过」就是假话。

`seq` 存在但不是整数 → `anchor_malformed`(BREAK),而不是让 verify 抛异常:
手改过的文件应该被**报出来**。

`summary()` 里,没比过的锚**永远不说** `match`,只说 "present, none comparable
to this window"。

另有一条与锚无关但同源的检查:第一条 entry 的 `seq` 若为 1,它的 `prev_hash`
必须等于 genesis 常量,否则报 `link_broken`(BREAK)。不查这个,攻击者就能自选
起点把整部历史重链一遍。

---

## 5. 谁实现了什么

| 实现 | 产出 | 验证 |
|---|---|---|
| `traceguard.audit` | 全部字段;`rfc3161` / `ots` / `rekor` 锚原样收录 | 链衔接、条目哈希(`full`)、链头对锚、结构校验 |
| tg-attest(另一仓库) | 按同一 schema 产出**时间戳部分**(`rfc3161` 锚 + epoch root) | 其自有的验签路径 |

两包继续**零代码依赖**,schema 是唯一契约。tg-attest 侧的采用是那个仓库的任务。

---

## 6. 使用

```bash
# 导出一个窗口的证据
python -m traceguard.audit --db sqlite:///traces.db bundle \
    --out evidence.json --since 2026-09-01T00:00:00Z --until 2026-09-08T00:00:00Z \
    --anchor-file /mnt/other-host/anchors.jsonl

# 只给元数据,不给 prompt 与输出
python -m traceguard.audit --db sqlite:///traces.db bundle --out evidence.json --hash-only

# 离线验证:不需要数据库,不需要网络
python -m traceguard.audit verify-bundle evidence.json
```

`verify-bundle` 在有 BREAK 时退出码 1,否则 0。INFO / WARN 不影响退出码 ——
`hash_only` 的 `content_not_recomputed` 不是失败,它是一句关于**这次验证的范围**的
声明。

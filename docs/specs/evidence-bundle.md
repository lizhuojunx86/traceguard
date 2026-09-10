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
| `findings` | verify / reconcile 产出的 finding,`{kind, severity, seq, trace_id, detail, direction}` |
| `cost_events` | 被 `cost_event` 链条目引用的 `audit_cost_events` 行。**`full` 模式验证这些条目必须有它**——条目哈希盖的是事件行本身,没有它就重算不出来 |
| `approvals` | **保留字段,当前恒为空数组**。`traceguard.approval` 是 SPEC v1.2 登记的规划项,尚未实现;字段先在,免得将来加它变成 schema 破坏 |

---

## 2. 验证证明什么 / 证明不了什么

`verify_bundle()` 做四件事,每件事的边界都写在这里,不许在别处弱化:

| 检查 | 证明什么 | 证明不了什么 |
|---|---|---|
| **链的衔接** | 每条 entry 的 `prev_hash` 等于前一条的 `row_hash`,即 bundle 里的链段是一条**连续**的链 | 这条链是不是完整的历史。被截掉的尾部留下的仍是一条合法的短链 |
| **条目哈希重算**(仅 `full`) | 每条 entry 的 `row_hash` 可以从它的元数据 + bundle 里那条 trace 的内容重算出来,即**内容与条目相符** | 内容本身是不是真的(采集真实性,见 `docs/audit.md` L0/L1/L1.5) |
| **链头对锚** | 链头等于某个外部锚记录的值 | 锚本身可信 —— 那取决于锚存在哪儿、谁能改它。`file:` 锚与库文件同主机时几乎不证明什么 |
| **结构校验** | bundle 符合 schema;`rfc3161` 锚的结构完整 | **不做密码学验签**,见 §4 |

**bundle 自身不是防篡改的**:它是一份可以被任意编辑的 JSON。它的价值在于
*内部一致性可以被重算* —— 改了里面的 trace 内容,`full` 模式重算就对不上;改了
`prev_hash`,两种模式都对不上。改了内容**并且**重算全部哈希,则 bundle 内部自洽
而只有外部锚能发现 —— 与 `docs/audit.md` 边界声明 1 完全同构。

---

## 3. `content_mode`:两种模式的结论**不许**合并成同一个词

algo v1 的哈希信封**包含内容字段**(`input_summary` / `output_parsed` /
`error_message` 等,见 `audit/canonical.py` 的 `TRACE_CONTENT_FIELDS`)。

- **`full`** — trace 行带全部覆盖字段。`verify_bundle` 重算每条 entry 的
  `row_hash`,内容被改动过就报 `hash_mismatch`(BREAK)。
- **`hash_only`** — 内容字段被剥掉(给收件人看 trace 存在与元数据,而不给他看
  prompt 与输出)。此时**无法重算条目哈希**,`verify_bundle` 只验证**链的衔接**与
  **链头对锚**,并产出一条 `content_not_recomputed`(INFO)finding 说明这件事。

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

后三个只在**验证 bundle** 这一个动作里出现,`verify_chain` 永远不会产出它们;
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

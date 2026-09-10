# `traceguard.audit` — Tamper-Evident Audit Trail

Opt-in evidence layer for the `traces` table: an ORM-layer append-only guard,
a row hash chain, and an exportable chain-head anchor. Off the frozen public
surface (SPEC §6.6 / English SPEC §6.1) — import from `traceguard.audit`, not
from `traceguard`. Zero new dependencies (stdlib `hashlib`/`json`).

## Contract status (stable since SPEC v1.1, 2026-08-27)

`traceguard.audit` 自 SPEC v1.1 起不再是 experimental(修订案:
`docs/spec-changes/2026-08-27-audit-v2-correlation-schema.md`)。三条契约承诺:

1. **API 面稳定** — `traceguard.audit` 的公开面(`__all__`)按 SPEC §6.3 演进:
   新增带默认值参数 / 新增符号 = minor;删参数、改语义、删符号 = major。
   `tests/test_audit_api_surface.py` 在 `contract-guard` CI job 里机械化守护。
2. **Finding kinds 语义冻结** — 下文 verify 表里的 kind 与 severity 冻结;新增 kind = minor,
   改 / 删既有 kind = major。
3. **边界声明规范化** — 下方"边界声明(逐字级,不许弱化)"三条是规范性声明:SPEC 背书,
   措辞只允许往更保守的方向改。

哈希算法版本化:algo v1 由 golden tests(`tests/test_audit_canonical.py`)冻结,永续可验;
任何算法变更 = algo v2,且 MUST 不使既有 v1 链失效。SPEC v1.1 在 `traces` 表新增的
`agent_id` / `session_id` 两列**不在 algo v1 信封内**(`TRACE_CONTENT_FIELDS` 是硬编码白名单,
新列不进入,旧链逐字节不变)——它们受 append-only 守卫保护(ORM 层不许改),但**不被链 attest**:
直接改库文件把 `agent_id` 换掉,`verify_chain` 看不见。纳入信封待 algo v2。

SPEC v1.2 新增的 `provider_response_id` **同款**:不在信封内,受守卫保护,不被链 attest。
这一条正是下文 L1.5 的能力边界所在——逐请求核对能证明“某个 response id 在两侧都存在”,
**不能**证明“这一行的 id 没有被人事后改成另一个”。改了 id 的行会安静地匹配上另一条台账记录,
而链看不见这次改动。

```python
import traceguard
from traceguard import audit

engine = traceguard.make_engine("sqlite:///traces.db")
audit.enable(engine)                 # tables + settings + backfill + attach
traceguard.tracer.configure(engine)

# ... traces written through the tracer are now chained ...

result = audit.verify_chain(engine)
print(result.summary())
anchor = audit.export_anchor(engine)
print(anchor.to_json())              # store this OUTSIDE the DB
```

CLI:

```bash
python -m traceguard.audit enable  --db sqlite:///traces.db   # [--chain-only] [--no-backfill] [--strict]
python -m traceguard.audit verify  --db sqlite:///traces.db   # exit 1 on BREAK findings
python -m traceguard.audit verify  --db ... --anchor '<json>' # full walk + anchor check(加测截断/重写)
python -m traceguard.audit anchor  --db sqlite:///traces.db   # print the head digest
python -m traceguard.audit anchor  --db ... --sink file:/mnt/other-host/anchors.jsonl \
                                            --sink git-note:/path/repo --sink webhook:https://...   # v2: store it OUTSIDE the DB
python -m traceguard.audit anchor  --db ... --sink file:... --every 300             # v2: keep anchoring (interval = exposure window)
python -m traceguard.audit verify  --db ... --anchor-file /mnt/other-host/anchors.jsonl  # v2: verify against the newest stored anchor
python -m traceguard.audit reconcile --db ... --source anthropic-usage --window 2026-08-01T00:00:00Z,2026-08-08T00:00:00Z \
                                            --api-key-id apikey_...              # v2: self-reported vs provider totals (capture_mismatch)
python -m traceguard.audit disable --db sqlite:///traces.db
```

## What each layer honestly delivers

| 层级 | 机制 | 防住 | 防不住 |
|---|---|---|---|
| **防误写** (anti-mistake) | ORM-layer append-only guard(mapper 事件 + `do_orm_execute` 拦截) | 同进程经 Session 的误 UPDATE/DELETE(属性赋值 flush、`session.delete`),以及 `session.execute(update(Trace)...)`/`delete(Trace)` 这种最典型误写 | engine 级 Core SQL、`exec_driver_sql`、原生 sqlite3/psql、直接改库文件、未 attach 的进程;**legacy bulk API**(`Session.bulk_update_mappings`/`bulk_save_objects`,不触发任何事件)与 **dialect upsert**(`insert(...).on_conflict_do_update`,对事件系统是 Insert) |
| **篡改可检测** (tamper-evident,**不是**防篡改) | row hash chain + 双 pass `verify_chain` | 已链行覆盖字段的事后修改、删行留链(`missing_trace`)、链内条目元数据伪造(entry_type/指向/cost 快照)、cost 证据不一致(WARN) | 见下方三句边界声明 |
| **防高权限攻击者** | WORM 存储、签名/MAC、自动外部锚定 | — **v1 明确 out of scope** | 全部 |

### 边界声明(逐字级,不许弱化)

1. **哈希链不是 MAC,v1 无密钥。** 任何能写库文件的人都可以重写任意历史行并重算全链哈希,
   `verify_chain` 对此完全无感;从链尾截断(连同 trace 行和链条目一起删除)后,剩下的前缀仍是
   合法链。防篡改证据只相对于**攻击者无法修改的外部锚**成立:没有导出锚,全链重写与尾部截断
   不可检测。锚只保护到最近一次导出为止——两次导出之间新增的条目仍可被静默截断,
   **锚定频率 = 暴露窗口**。
2. **backfill 条目只证明"启用审计那一刻该行长这样"**,不证明该行自原始写入以来未被改动。
   写入时点证据仅对 `entry_type='write'` 条目成立,且仅在守卫持续启用期间。
3. **`cost_usd` 在哈希信封之外**(它有合法的就地补写路径:reprice backfill/rollback,SPEC §3.1)。
   `verify_chain` 无法检测对 `cost_usd` 的直接修改;cost event 只证明"曾记录过一次修正",
   不证明当前列值未被再次改动。`cost_mismatch`(WARN)核对"当前列值 = 链上最新 cost 证据",
   把静默改动转化为"必须留下可归因的链内记录才能不被发现"——但 v1 无签名,伪造追加是可能的,
   链只保证它一旦写入不可无痕修改或删除。

## Threat category: control bypass / evidence tampering attempt (2026-08-27)

对照 2026-08-26 METR/Redwood 调查(OpenAI / Hugging Face 事件)验证的真实威胁行为,
v1 的覆盖照实登记——可检测的说可检测,不设防的说不设防:

| 事件中验证的行为 | v1 结果 | 依据 |
|---|---|---|
| 修改已链行的覆盖字段 | **可检测**(`hash_mismatch` BREAK) | 全量 preimage 重算 |
| 删除已链 trace 行 | **可检测**(`missing_trace` BREAK) | pass 2 全表扫 |
| 尾部截断 / 全链重写 | **仅锚定后可检测**(`anchor_mismatch`);无外部锚不可检测。v2 的 anchor sink + 定期锚定把暴露窗口缩到锚定间隔,不改变"两次锚定之间不可检测"这一事实 | 边界声明 1,锚定频率 = 暴露窗口;下文 Anchors |
| 采集层伪造(spoofed tool call 类,METR: >7% transcripts) | **逐条不设防**——自报证据产生于 agent 控制的进程内,链忠实保存谎言。v2 `reconcile` 做**总量**对账:同 model 同时间窗的自报 token 量 vs 供应商 usage 报告,超阈值 = `capture_mismatch`(WARN);单条 trace 的真实性仍不可证 | 存储完整性 ≠ 采集真实性;分层见下文 Reconcile 与 `docs/spec-changes/2026-08-27-audit-v2-correlation-schema.md` §5 |
| 采集层**漏报**或**多报**单次调用 | **有带外逐请求台账时可检测**(`capture_unmatched` WARN,带 `direction`)。总量对账在这里会失效——少报的调用与多报的调用可以互相抵消,而供应商 usage API 只给 token 量、不给调用数 | 下文 L1.5;`docs/spec-changes/2026-09-10-source-snapshot-approval-binding.md` §1.2 |
| 高权限攻击者(可写库文件) | **v1 不设防** | 边界声明 1,WORM/签名/自动外锚 out of scope |

## Mechanics

**Tables** (own `DeclarativeBase`; created only by `enable()` /
`ensure_audit_tables()`, never by `make_engine`; no FK into `traces`):

- `audit_settings` — single row: `enabled`, `append_only`, `algo_version`,
  `genesis_hash`, `enabled_at`. The switch lives in the DB so all attached
  processes see one state.
- `audit_chain_entries` — the chain. `seq` (autoincrement, with
  `sqlite_autoincrement` so truncated rowids are not reused) is the chain
  order; `prev_hash` is NOT NULL + UNIQUE — linearity is enforced by the
  constraint, not by transaction isolation; a concurrent head race becomes an
  `IntegrityError` that the writer retries (bounded, then fail-open/strict).
- `audit_cost_events` — the ledger for legal `cost_usd` writes
  (`deferred_first_write` | `rollback` | `correction`), each mirrored by a
  chained `cost_event` entry in the same transaction.

**Hash (algo v1, frozen by golden tests).**
`row_hash = sha256(prev_hash_hex || canonical_json_bytes(payload))` where the
payload contains the entry **metadata** (entry_type, trace_id, event_id,
cost_at_event, note, canon_status, canon_error, created_at, algo_version) plus
the referenced **content** — for trace entries the SPEC §3.1 fields *except
`cost_usd`*; for cost events the full event row. Metadata is inside the
preimage on purpose: hashing content alone would let entry_type swaps,
trace_id re-pointing, and cost-snapshot edits pass verification.
`canonical_json_bytes` = `json.dumps(sort_keys=True, ensure_ascii=True,
separators=(",", ":"), allow_nan=False)` with datetimes normalized to UTC
isoformat *by the audit layer itself* (mapper hooks see pre-bind values),
Decimals stringified, and dict keys coerced to their JSON round-trip form
(`2` → `"2"`) **before** sorting — so write-time (pre-round-trip) and
verify-time (post-round-trip) values hash identically. Content that cannot be
canonicalized (NaN/Inf floats, non-JSON-representable dict keys, keys that
collide after coercion) fail-opens into a deterministic
`canon_status='failed'` marker entry — the chain stays linear, that row's
content is simply not attested. Lone surrogates are *not* failures:
`ensure_ascii=True` escapes them deterministically and the content is
attested normally.

**Activation.** Importing `traceguard.audit` has zero side effects. `enable()`
writes the DB flag, backfills, and attaches the engine; other processes call
`attach(engine)`. Listeners are process-global but first-gate on an attached-
engines WeakSet, so non-opted engines see zero behavior change. `disable()`
flips the DB flag: the guard lifts and chaining stops; the existing chain
stays verifiable, and rows inserted during a disable window surface as
`coverage_gap`.

**Failure semantics (SPEC §4.1).** Chain writes run in a SAVEPOINT on the
host's flush connection: entry and trace commit or roll back atomically, and
an audit failure can never poison the host transaction (load-bearing on
PostgreSQL, where a failed statement aborts the whole transaction). Default is
fail-open — WARNING + coverage gap; `enable(strict=True)` or
`TRACEGUARD_AUDIT_STRICT=1` re-raises instead ("宁可中断也不能静默丢证据").
The *guard* raising `AppendOnlyViolationError` on a blocked write is its
feature, not a failure — only guard infrastructure errors fail open.

> **strict × tracer 交互(必读)**:tracer 自身的持久化默认也是 fail-open
> (SPEC §4.1)。strict 链故障在 tracer 的 flush 内 re-raise 后,会被非 strict
> 的 tracer 吞掉——结果是 trace 与链条目**双双静默丢失**,只剩两条日志
> (`traceguard.audit` 的 ERROR + `traceguard.tracer` 的 WARNING),verify
> 连 coverage gap 都显示不出来(行根本不存在)。要让 strict 语义贯穿 tracer
> 写入路径,必须同时开 `strict_persistence=True` /
> `TRACEGUARD_STRICT_PERSISTENCE=1`;`enable(strict=True)` 检测到模块级
> tracer 非 strict 时会发 WARNING 提醒。

**Reprice exemption — stated honestly.** `reprice.py` updates `cost_usd` via
Core `update()`, which never fires mapper events. That single mechanism is
simultaneously (a) why reprice keeps working unmodified under the guard and
(b) the guard's structural blind spot. The hash envelope excluding `cost_usd`
is what keeps the chain valid across reprices. Run reprice with `--audit` to
record each write as a chained cost event (post-commit, per chunk); without
it, repriced rows show up as `cost_mismatch` WARNs.

**verify_chain — two passes, by necessity.** Pass 1 walks the chain (link +
full preimage recompute per entry, `seq` never assumed contiguous). Pass 2
sweeps `traces` for rows with no entry — a chain-only walk is provably silent
about rows inserted while audit was off. Findings:

| kind | severity | meaning |
|---|---|---|
| `anchor_mismatch` | BREAK | chain truncated/rewritten since the anchor was exported |
| `link_broken` | BREAK | `prev_hash` does not match the previous entry |
| `hash_mismatch` | BREAK | covered content or entry metadata changed after chaining |
| `missing_trace` | BREAK | chained trace destroyed without a tombstone |
| `missing_cost_event` | BREAK | chained cost event destroyed |
| `cost_mismatch` | WARN | current `cost_usd` ≠ newest chained cost evidence |
| `deleted_with_record` | WARN | chained trace gone, deletion tombstone exists |
| `coverage_gap` | GAP | traces with no entry (pre-enable / disable window / fail-open skip) |
| `capture_mismatch` | WARN | (`reconcile`, not `verify_chain`) self-reported token volume for a model/window disagrees with the provider's out-of-band usage report beyond the tolerance, or a model appears on only one side |
| `capture_unmatched` | WARN | (`reconcile_requests`, not `verify_chain`) **one specific call** is present on only one side of a per-request comparison, or one `response_id` appears twice on a side. Carries `direction`: `out_of_band_only` / `self_reported_only` |

Full walk is O(n) and the default (~26k entries verify in well under a
second). `from_anchor=` **adds** an anchor-consistency check on top of the
full walk — an anchored verify is strictly stronger than a plain one. Only
`incremental=True` starts the hash walk at the anchor instead of genesis (hash
work proportional to what was appended since the export); everything before
the anchor is then *trusted, not verified* — a pre-anchor tamper is invisible
in that mode. Coverage and cost checks always run in full, in every mode.

**Anchors.** `export_anchor()` → `{seq, row_hash, algo_version, entry_count,
exported_at}`. Store the JSON line **outside the DB**: a git commit message, a
sent email, a third-party timestamping service. WORM storage and signing stay
future work.

**Anchor sinks + periodic anchoring (v2).** `traceguard.audit.anchors` is the
scheduling layer on top of the unchanged `export_anchor()` contract:

```python
from traceguard import audit

sinks = [
    audit.FileAnchorSink("/mnt/other-host/anchors.jsonl"),   # JSON lines; latest() reads back
    audit.GitNoteAnchorSink("/path/repo"),                   # git notes --ref refs/notes/traceguard-audit
    audit.WebhookAnchorSink("https://...", headers={"Authorization": "Bearer ..."}),
]
audit.anchor_to(engine, sinks)                     # once; raises AnchorSinkError if ANY sink failed
audit.AnchorScheduler(engine, sinks, interval_s=300).start()   # daemon thread; logs ERROR and keeps going
```

`anchor_to` 逐个尝试每个 sink,只要有失败就在最后汇总抛出——一个静默没落地的锚
是虚假的覆盖感(SPEC 附录 B3.4)。scheduler 的间隔**就是**暴露窗口:上一次 tick
之后新增的条目仍可被无痕截断。`verify --anchor-file PATH` 用 file sink 写下的最新
锚闭环。

### 每种 sink:证明什么 / 证明不了什么 / 依赖谁 / 失败时怎样

| sink | 证明什么 | 证明不了什么 | 依赖谁 | 失败时怎样 |
|---|---|---|---|---|
| `file:PATH` | 锚在这个文件被写入的那一刻存在过 | 文件本身没被改。**出了库,没出主机**——能改库文件的人通常也能改旁边的文件 | 文件系统;放在另一台主机 / 只追加介质上才真正有意义 | `AnchorSinkError`(路径不可写);`anchor_to` 汇总抛出,scheduler 记 ERROR 并继续 |
| `git-note:REPO` | 锚进了仓库对象库,与代码历史绑在一起 | 比仓库历史本身更强的东西。本地 note 可被删改;**要把信任根挪出主机必须 push 到一个 DB 写入者无法 force-push 的 remote** | git 仓库 + 那个 remote 的管理者 | `git notes append` 非零退出即 `AnchorSinkError` |
| `webhook:URL` | 锚被投递给了那个接收方 | 接收方拿它做了什么。**投递不是保存**——真正的保证在接收端 | 你自己运维的那个接收方 | 非 2xx 或传输错误即 `AnchorSinkError` |
| `rfc3161`(tg-attest 产出,本包只收录) | 一个 TSA 在某时刻见过这个 digest | 无——**本包不验签**。结构完整 ≠ token 有效 | 那个 TSA 与它的 CA 链;验签的信任根由收件人自己选 | 结构不全在 bundle 校验里是 BREAK |
| **`ots:DIR`**(v1.2,extra `anchors`) | **complete 时**:digest 在某个比特币区块之前已存在。这是唯一一个不落在"你得信某一方"上的锚 | **pending 时:什么都不证明**——那只是日历服务器的承诺。complete 也**不给精确时刻**:区块时间有分钟到小时级的不确定性 | 比特币链;完整验证需要一个比特币节点,否则你是在信区块浏览器或日历服务器。**traceguard 两者都不做** | 所有日历都不可达即 `AnchorSinkError`,且**不写出任何文件**(半个锚比没有锚更糟) |
| `rekor:`(**未实现**,设计见下) | — | — | — | — |

**多一种锚不改变暴露窗口。** 边界声明 1 的措辞不因此放松:OTS 让"链头在 T 时刻已存在"
不再依赖单一方,但**两次锚定之间新增的条目仍然可以被静默截断**。这两件事是独立的,
把它们混起来是这一节最容易犯的错。

```bash
# 需要 anchors extra:pip install 'traceguard[anchors]'
python -m traceguard.audit --db sqlite:///traces.db anchor --sink ots:/mnt/anchors/ots
python -m traceguard.audit --db sqlite:///traces.db verify \
    --ots-proof /mnt/anchors/ots/anchor-00000042-<digest>.ots [--ots-upgrade]
```

每个锚写**两个**文件:`.ots` 证明 + 同名 `.json` 侧车(锚本体)。digest 是
`sha256(anchor.to_json())` —— 覆盖**整份锚声明**而不只是 `row_hash`:只盖头哈希会让
`seq` 与 `entry_count` 不被 attest,于是把链头重新指到一条被重写的链里的另一个位置,
证明照样对得上。两个文件放一起,日后**不需要数据库**就能自证。

刚 stamp 出来的证明是 **pending**;`--ots-upgrade` 向日历服务器要升级后的证明 ——
问的是 pending 附件里记的那个 URI(它才是持有证明的那台),不是当初提交的地址:
默认的 `a.pool.opentimestamps.org` 是个转发池,两者本来就不是同一台。
拿不到不是错误——刚 stamp 完拿不到是常态。

### `rekor:` — 设计节,**本次未实现**

Sigstore 透明日志的 `hashedrekord` 条目,与 OTS 并用可给出第二种独立见证。未实现的
理由照实写,不是"没时间":它需要一把 ECDSA P-256 签名密钥,而**那把钥匙只证明
"上传者是同一个"**,不证明身份——除非接上 OIDC 身份流程,而那是另一整套东西。
密钥管理(生成、权限、轮换、丢失后怎么办)是一个独立的设计面,应当由一个真实需求
拉动,而不是因为"顺手也加上"。OTS 已经给了"不依赖单一方"这个属性;rekor 的增量
主要是可查询的透明日志,今天没有消费者要求它。

重开条件与 §8.4 其余项相同:一个**外部**组织在生产里用,并主动提出。

**Reconcile (v2, capture-fidelity layer L1).** The chain answers "was what the
SDK stored changed afterwards?"; it cannot answer "was what the SDK stored
true?" — wrapper self-reports are produced inside the process the agent
controls. `traceguard.audit.reconcile` is the cheapest cross-check: per model
and UTC time bucket, compare the traces table's self-reported token volume with
the provider's usage report, an out-of-band source the agent does not write to.

| 层 | 机制 | 证明什么 | 证明不了什么 |
|---|---|---|---|
| L0 | wrapper 自报 + hash chain | SDK 看到的调用事后未被无痕改动 | 自报本身的真实性 |
| **L1(v2)** | `reconcile`:traces 聚合 vs 供应商 usage 报告 | 同 model 同时间窗的 token 总量在容差内一致 | 单条 trace 的真实性 |
| **L1.5(v1.2)** | `reconcile_requests`:按 `provider_response_id` 与带外**逐请求台账**等值连接 | **被带外源逐条确认存在的那些调用**确实发生过——采集层没有凭空造出它们,带外侧也看见了它们 | 内容是否如实(台账里是一个 id,不是一份记录);两侧都没有的调用;以及 `provider_response_id` 本身是否被事后改过(它在信封外) |
| L2(不做) | 逐条真实性:供应商签名请求日志 | — | 超出 SDK 能力边界,不承诺 |

```bash
export ANTHROPIC_ADMIN_KEY=sk-ant-admin...   # Admin API key; never a regular key, never in a tracked file
python -m traceguard.audit reconcile --db sqlite:///traces.db --source anthropic-usage \
    --window 2026-08-01T00:00:00Z,2026-08-08T00:00:00Z --bucket-width 1d \
    --api-key-id apikey_01... --workspace-id wrkspc_01...   # narrow the org-wide report to THIS DB's traffic
# or, from a saved report (a curl dump; also the deterministic test path):
python -m traceguard.audit reconcile --db ... --source json:usage.json --window ...
```

Conventions that MUST line up, or every finding is a false positive: `tokens_in`
is full prompt volume (`uncached_input_tokens` + `cache_read_input_tokens` +
`cache_creation` 5m + 1h — exactly what `wrap_anthropic` records); windows are
snapped outward to UTC bucket edges (`align_window`) and compared on
`invoked_at`; model names must match (`--model-map trace=provider` for dated
snapshot ids). The Usage API reports **tokens only, not request counts** — call
counts are shown for context, never compared. The direction of a mismatch is
spelled out in the finding: **traces > provider** = self-reports the provider
never served (spoofed or replayed), or a report filtered narrower than the DB;
**provider > traces** = traffic the SDK never recorded (uninstrumented calls,
traces dropped fail-open, or an org-wide report wider than this DB).

### 逐请求核对(L1.5):`request-ledger/v1`

总量对账有一个它自己解决不了的问题:少报的调用与多报的调用可以**互相抵消**,而供应商
usage API 只给 token 量、不给调用数(08-27 §8 实施备注)。于是"总量对得上"并不排除
"这一条是编的、那一条被吞了"。逐请求核对问的是总量问不了的问题:**这一次调用在两侧都
存在吗**。连接键是供应商自己返回的响应 id(`traces.provider_response_id`,SPEC v1.2)。

带外源只要能导出下面这个形状,就能接进来——**不承诺任何具体网关的接口**:

```json
{"ledger": "request-ledger/v1", "source": "<gateway or provider name>",
 "window": ["<ISO start>", "<ISO end>"],
 "requests": [{"response_id": "…", "model": "…", "ts": "<ISO>",
               "tokens_in": 0, "tokens_out": 0, "cost_usd": null}]}
```

```bash
python -m traceguard.audit --db sqlite:///traces.db reconcile \
    --source requests-json:/path/gateway-requests.json \
    --window 2026-09-01T00:00:00Z,2026-09-08T00:00:00Z
```

三处纪律,都是"假阳性会教人忽略告警"的直接后果(SPEC 附录 B3.4):

1. **`window` 是承重的,不是装饰。** 台账声称覆盖 `[a, b)`,而你要求核对 `[a, c)` 且
   `c > b` —— 那么 `[b, c)` 里的每一条 trace 都会被报成 `self_reported_only`。这不是发现,
   是伪影。`reconcile_requests` 在这种情况下**直接拒绝**并要求你收窄窗口。
2. **`provider_response_id` 为 NULL 的行被计数、被排除,不被当成不匹配。** 它们要么早于
   这一列存在,要么来自 wrapper 拿不到 id 的流式调用。把"诚实地说不知道"报成
   `self_reported_only`,等于指控采集层伪造。
3. **两个方向的措辞是固定的**,好让两次运行、两个人读到的是同一个断言:

| `direction` | 情形 | 固定解释 |
|---|---|---|
| `out_of_band_only` | 台账有、traces 无 | a call the capture layer did not see — bypass or wrapper coverage gap |
| `self_reported_only` | traces 有、台账无 | a record the provider side does not vouch for — fabrication, duplication, or an incomplete ledger |

同一个 `response_id` 在任一侧出现两次单列为 duplicate:一个响应 id 标识一次调用,
"同一次调用被记了两遍"与"一次调用不见了"不是同一件事。

聚合层的 `capture_mismatch` 行为**完全不变**——两种 finding 证明的东西不同,混成一个
kind 会让它们共用一个阈值和一套处置。

## 证据 bundle(`evidence-bundle/v1`)

把选定的 trace、覆盖它们的链段与链头、锚记录、`source_snapshot` 与 findings 打成一份
自包含 JSON,好让**拿到它的人在没有数据库、没有网络、没有 traceguard 的情况下**自己验一遍。

```bash
python -m traceguard.audit --db sqlite:///traces.db bundle --out evidence.json \
    --since 2026-09-01T00:00:00Z --anchor-file /mnt/other-host/anchors.jsonl
python -m traceguard.audit verify-bundle evidence.json      # BREAK 时退出码 1
```

两种 `content_mode`,**结论不许合并成同一个词**:

| 模式 | 验了什么 | summary 措辞 |
|---|---|---|
| `full` | 链衔接 + **逐条重算 `row_hash`**(内容被改则 `hash_mismatch` BREAK)+ 链头对锚 | `bundle VERIFIED (full)` |
| `hash_only` | 链衔接 + 链头对锚。内容字段被剥掉,**条目哈希无法重算** | `bundle LINKAGE OK (hash_only) … content was NOT recomputed` |

`hash_only` 通过意味着"这段链首尾相连、链头与锚一致",它**没有**对内容说过任何话。
把它读成"内容已验证"是这个格式最容易犯、后果最大的误读,所以 `hash_only` 下永远至少带
一条 `content_not_recomputed`(INFO),两种模式的 summary 也用两套措辞。
(`content_not_recomputed` 是 **bundle 层的标注,不是 audit 的 finding kind**——
不进 `FINDING_SEVERITY`,不受 §6.6 的 kind 冻结约束。)

**锚只做结构校验,不做验签。** `rfc3161` 锚会被报成"这里有一个结构完整的 token,
imprint 是 X",**不会**被报成"这个 token 有效":验签需要收件人自己选的信任根,而本包
零新增运行时依赖。要后者,把 `token_b64` 交给 `openssl ts` 和一份你自己取回的 CA 证书。
`ots` 锚处于 **pending** 时会发 WARN —— 日历服务器的承诺不是证据。

格式与字段:`docs/specs/evidence-bundle.md`;JSON Schema:
`docs/specs/evidence-bundle-v1.schema.json`。tg-attest 按同一 schema 产出时间戳部分,
两包继续零代码依赖,**schema 是契约**。

## Legal-deletion path

If a trace must genuinely be removed (e.g. `routing_audit` ingest rollback of
a bad batch), chain a tombstone first: `record_deletion(engine, trace_id=...,
reason=...)`. Verify then reports `deleted_with_record` (WARN) instead of
`missing_trace` (BREAK). Best practice: enable audit only after ingest batches
are settled.

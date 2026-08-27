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

What each sink honestly gives you: a **file** sink is out of the DB, not out of
the host — whoever can edit the DB file can usually edit a sibling file, so put
it on another host / filesystem / append-only medium. A **git note** is as
tamper-evident as the repository's history, which is only as good as a remote
the DB writer cannot force-push (`git push origin refs/notes/traceguard-audit`).
A **webhook** delivers; what the receiver does with the anchor is the actual
guarantee. `anchor_to` tries every sink and then raises if any failed — an
anchor that silently never landed is a false sense of coverage (SPEC B3.4).
The scheduler interval IS the exposure window: entries appended since the last
tick can still be truncated without trace. `verify --anchor-file PATH` closes
the loop against the newest anchor a file sink wrote.

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

## Legal-deletion path

If a trace must genuinely be removed (e.g. `routing_audit` ingest rollback of
a bad batch), chain a tombstone first: `record_deletion(engine, trace_id=...,
reason=...)`. Verify then reports `deleted_with_record` (WARN) instead of
`missing_trace` (BREAK). Best practice: enable audit only after ingest batches
are settled.

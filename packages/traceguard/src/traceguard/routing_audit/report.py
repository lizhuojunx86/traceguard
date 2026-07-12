"""Report-skeleton generator (bilingual) + one-shot refresh chain.

Emits ``report_zh.md`` / ``report_en.md`` skeletons with every fillable number
pulled from the DB and every still-open spot marked
``[PENDING: manual-tags | policy-review | blind-eval]``. Structure:

  §1 Data & method (evidence chain: source mutability, fable stand-down check)
  §2 Spend structure (task_type × tier)
  §3 Policy-deviation audit (stated vs revealed routing)
  §4 Cost counterfactuals (framed "IF QUALITY HOLDS" throughout)
  §5 Intra-tier advisor premium (arithmetic + blind — kept apart from §3)
  §6 Policy-revision proposals & projected impact (incl. workflow-shape slots)

Redaction is built in: project names go through a LOCAL alias map
(``routing_audit_alias_map.json``, gitignored); amounts keep two decimals; no
prompt/answer body ever appears. ``--audience external`` (default) uses
aliases everywhere; ``--audience personal`` keeps real project names.

Refresh chain (``report refresh``): tags import (optional) → decisions
generate → counterfactual (read) → report. Fixed order, idempotent — one
command to re-run after editing the tag CSV or the policy.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.routing_audit.blind import FABLE, intra_tier_premium
from traceguard.routing_audit.models import (
    BlindEval,
    RerunResult,
    RoutingDecision,
    ensure_tables,
)
from traceguard.routing_audit.routing_decisions import load_policy
from traceguard.routing_audit.task_tags import load_unit_index
from traceguard.store.models import Trace, make_engine

DEFAULT_DB = "sqlite:///traces_routing_audit.db"
DEFAULT_ALIAS = Path("routing_audit_alias_map.json")
_PENDING_TAGS = "[PENDING: manual-tags]"
_PENDING_POLICY = "[PENDING: policy-review]"
_PENDING_BLIND = "[PENDING: blind-eval]"


def build_alias_map(db_url: str | None, path: Path) -> dict[str, str]:
    """Stable project→alias map (Project-A, B, …), persisted locally."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    engine = make_engine(db_url)
    ensure_tables(engine)
    with Session(engine) as sess:
        # order by total cost desc for a stable, meaningful lettering
        costs: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for project, cost in sess.execute(select(Trace.project, Trace.cost_usd)):
            costs[project] += cost or Decimal("0")
    ordered = sorted(costs, key=lambda p: costs[p], reverse=True)
    alias = {p: f"Project-{chr(ord('A') + i)}" for i, p in enumerate(ordered)}
    try:
        path.write_text(json.dumps(alias, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return alias


@dataclass
class ReportData:
    total_traces: int = 0
    ts_min: datetime | None = None
    ts_max: datetime | None = None
    total_cost: Decimal = Decimal("0")
    # tier -> [traces, cost]; (task_type, tier) -> cost
    by_tier: dict[str, list[Any]] = field(default_factory=lambda: defaultdict(lambda: [0, Decimal("0")]))
    by_tt_tier: dict[tuple[str, str], Decimal] = field(default_factory=lambda: defaultdict(lambda: Decimal("0")))
    by_project: dict[str, Decimal] = field(default_factory=lambda: defaultdict(lambda: Decimal("0")))
    untagged_cost: Decimal = Decimal("0")
    # decisions
    dec_total: int = 0
    dec_dev: int = 0
    dec_dev_cost: Decimal = Decimal("0")
    dec_manual: int = 0
    dec_by_tt: dict[str, list[Any]] = field(default_factory=lambda: defaultdict(lambda: [0, 0, Decimal("0")]))
    # premium
    premium_total: Decimal = Decimal("0")
    premium_fable_cost: Decimal = Decimal("0")
    premium_units: int = 0
    # self-audit: cost of building the audit tool itself
    self_audit_cost: Decimal = Decimal("0")
    self_audit_traces: int = 0
    # blind eval (unblinded), layered by recognition
    blind_total: int = 0
    blind_leak: int = 0  # recognition != none
    blind_none: int = 0
    none_original: int = 0  # of the recognition=none pairs
    none_replay: int = 0
    none_tie: int = 0
    fable_pos_a: int = 0  # original(fable) placed in position A
    fable_pos_b: int = 0
    b_better_picked_original: int = 0
    rerun_est_total: Decimal = Decimal("0")
    rerun_actual_total: Decimal = Decimal("0")


def gather(db_url: str | None = None, *, as_of: datetime | None = None) -> ReportData:
    policy = load_policy()
    engine = make_engine(db_url)
    ensure_tables(engine)
    index = load_unit_index(engine)
    data = ReportData()

    with Session(engine) as sess:
        stmt = select(
            Trace.output_parsed, Trace.invoked_at, Trace.project,
            Trace.model_id, Trace.cost_usd, Trace.component,
        )
        if as_of is not None:
            stmt = stmt.where(Trace.invoked_at <= as_of)
        for output_parsed, invoked_at, project, model_id, cost_usd, component in sess.execute(stmt):
            data.total_traces += 1
            c = cost_usd or Decimal("0")
            data.total_cost += c
            data.by_project[project] += c
            # self-audit overhead: the traceguard project's own dev + the
            # rerun-harness component (the audit tool auditing itself).
            if project == "traceguard" or component == "rerun-harness":
                data.self_audit_cost += c
                data.self_audit_traces += 1
            if data.ts_min is None or invoked_at < data.ts_min:
                data.ts_min = invoked_at
            if data.ts_max is None or invoked_at > data.ts_max:
                data.ts_max = invoked_at
            tier = policy.tier_of(model_id)
            data.by_tier[tier][0] += 1
            data.by_tier[tier][1] += c
            session_id = (output_parsed or {}).get("session_id")
            hit = index.lookup(session_id, invoked_at)
            tt = hit[1] if hit is not None else "(untagged)"
            data.by_tt_tier[(tt, tier)] += c
            if hit is None:
                data.untagged_cost += c

        decisions = list(sess.scalars(select(RoutingDecision)))
    data.dec_total = len(decisions)
    for d in decisions:
        if d.deviation:
            data.dec_dev += 1
            data.dec_dev_cost += d.cost_usd or Decimal("0")
        if d.source == "manual":
            data.dec_manual += 1
        b = data.dec_by_tt[d.task_type]
        b[0] += 1
        if d.deviation:
            b[1] += 1
            b[2] += d.cost_usd or Decimal("0")

    prem = intra_tier_premium(db_url, as_of=as_of)
    data.premium_total = sum((p.premium for p in prem), Decimal("0"))
    data.premium_fable_cost = sum((p.fable_actual for p in prem), Decimal("0"))
    data.premium_units = sum(p.units for p in prem)

    # ── blind eval (unblinded), layered by recognition ──
    with Session(engine) as sess:
        evals = [e for e in sess.scalars(select(BlindEval)) if e.verdict is not None]
        reruns = {r.rerun_id: r for r in sess.scalars(select(RerunResult))}
    for e in evals:
        data.blind_total += 1
        if e.position_a_model == FABLE:
            data.fable_pos_a += 1
        else:
            data.fable_pos_b += 1
        fable_in_a = e.position_a_model == FABLE
        if e.verdict == "tie":
            winner = "tie"
        else:
            winner = "original" if ((e.verdict == "a_better") == fable_in_a) else "replay"
        if e.verdict == "b_better" and winner == "original":
            data.b_better_picked_original += 1
        rec = e.recognition or "none"
        if rec != "none":
            data.blind_leak += 1
        else:
            data.blind_none += 1
            if winner == "original":
                data.none_original += 1
            elif winner == "replay":
                data.none_replay += 1
            else:
                data.none_tie += 1
    for r in reruns.values():
        if r.status == "completed" and r.target_model == "claude-opus-4-8":
            data.rerun_est_total += r.est_cost_usd or Decimal("0")
            data.rerun_actual_total += r.actual_cost_usd or Decimal("0")
    return data


def _alias(project: str, alias: dict[str, str], audience: str) -> str:
    return alias.get(project, project) if audience == "external" else project


def _money(x: Decimal) -> str:
    return f"${x:.2f}"


def render(data: ReportData, *, lang: str, audience: str, alias: dict[str, str]) -> str:
    zh = lang == "zh"
    span = (
        f"{data.ts_min:%Y-%m-%d} → {data.ts_max:%Y-%m-%d}"
        if data.ts_min and data.ts_max
        else "—"
    )
    dev_rate = f"{data.dec_dev / data.dec_total:.1%}" if data.dec_total else "—"
    prem_pct = (
        f"{data.premium_total / data.premium_fable_cost:.1%}"
        if data.premium_fable_cost
        else "—"
    )
    tags_note = _PENDING_TAGS if data.dec_manual == 0 else ""
    L: list[str] = []
    if zh:
        L += [
            "# 路由审计报告（骨架）",
            f"> 受众：{'外部（全假名）' if audience == 'external' else '个人（真名）'} ｜ "
            f"金额=list price 两位小数 ｜ 无 prompt/答案正文",
            "",
            "## §1 数据与方法",
            f"- 覆盖 **{data.total_traces:,}** 条真实 trace，时间跨度 **{span}**，"
            f"list-price 总成本 **{_money(data.total_cost)}**。",
            "- 数据来自本机 Claude Code 会话回填（无 costUSD 字段，成本按官方 list price 推算）。",
            "- **证据链一：源可变性**——会话文件会被 resume/compact 原地重写；"
            "ingest-log 为不可变留存层，库与源冲突以库为准。",
            "- **证据链二：Fable 停用期外部校验**——2026-06-13 的 31 条 fable 消息全部落在"
            "三个先前会话的尾部（跨停用时点残留，非映射错误），已保留原样。",
            f"- **自指开销单列**：本审计库**包含构建审计工具自身的开销**"
            f"（traceguard 项目 + rerun-harness component）——共 {data.self_audit_traces:,} 条 "
            f"**{_money(data.self_audit_cost)}**（占总成本 "
            f"{(data.self_audit_cost / data.total_cost if data.total_cost else 0):.1%}）；"
            "解读花钱结构时应意识到这部分是工具自建成本，非被审计的生产用量。",
            f"- 标签状态：{tags_note or '已含人工校正'}（启发式打标，人工校正后 §3 数字重算）。",
            "- **方法学诚实项（三条，均为本审计自身的缺陷/教训）**：",
            f"  1. **盲评识别泄漏 {data.blind_leak}/{data.blind_total}≈"
            f"{(data.blind_leak / data.blind_total if data.blind_total else 0):.0%}**："
            "评审是本人、审的是自己近期对话，认出了部分原答案。§5b 已把认出的对剔出主结论；"
            "未来协议改为**拉长间隔**或**第三方评审**。",
            f"  2. **盲评位置不均衡**：原答案 {data.fable_pos_b}/{data.blind_total} 落 B 位"
            "（确定性 hash 分配偏斜），与「原版效应」共线，§5b 结论因此只能弱声称。",
            f"  3. **估算 100× 教训**：重跑成本预估 **{_money(data.rerun_est_total)}** vs 实际 "
            f"**${data.rerun_actual_total:.2f}**（≈"
            f"{(data.rerun_est_total / data.rerun_actual_total if data.rerun_actual_total else 0):.0f}"
            "×）。错因=用原咨询的完整上下文规模估重放，而自包含重放冷启动、无 cache、只发 prompt "
            "正文；estimate_costs 已改按重放载荷估算。",
            "",
            "## §2 花钱结构（task_type × 档位）",
            f"{'task_type':<20}｜ frontier ｜ mid ｜ cheap ｜ 小计",
        ]
    else:
        L += [
            "# Routing Audit Report (skeleton)",
            f"> Audience: {'external (aliased)' if audience == 'external' else 'personal'} | "
            f"amounts = list price, 2 dp | no prompt/answer bodies",
            "",
            "## §1 Data & method",
            f"- Covers **{data.total_traces:,}** real traces over **{span}**, "
            f"list-price total **{_money(data.total_cost)}**.",
            "- Backfilled from local Claude Code sessions (no costUSD field; cost is "
            "official list price).",
            "- **Evidence 1: source mutability** — session files are rewritten in place by "
            "resume/compact; the ingest-log is the immutable layer, DB wins on conflict.",
            "- **Evidence 2: Fable stand-down external check** — the 31 fable messages on "
            "2026-06-13 are all fade-out tails of three prior sessions (cross-cutover "
            "residue, not a mapping bug), kept as-is.",
            f"- **Self-audit overhead, listed separately**: this audit DB **includes the "
            f"cost of building the audit tool itself** (traceguard project + rerun-harness "
            f"component) — {data.self_audit_traces:,} traces, **{_money(data.self_audit_cost)}** "
            f"({(data.self_audit_cost / data.total_cost if data.total_cost else 0):.1%} of "
            "total); read the spend structure knowing this slice is tool self-build, not "
            "audited production usage.",
            f"- Tag status: {tags_note or 'manual-corrected'} (heuristic tags; §3 recomputes "
            "after manual correction).",
            "- **Methodology honesty (three items, all flaws/lessons of this audit itself)**:",
            f"  1. **Blind-eval recognition leak {data.blind_leak}/{data.blind_total}≈"
            f"{(data.blind_leak / data.blind_total if data.blind_total else 0):.0%}**: the "
            "reviewer was the author judging their own recent conversations and recognised some "
            "originals. §5b drops recognised pairs from the main conclusion; future protocol: "
            "**longer interval** or a **third-party reviewer**.",
            f"  2. **Blind-eval position imbalance**: {data.fable_pos_b}/{data.blind_total} "
            "originals landed in slot B (skewed deterministic-hash assignment), collinear with "
            "the 'original-answer' effect — hence §5b can only claim weakly.",
            f"  3. **100× estimate lesson**: rerun cost estimate **{_money(data.rerun_est_total)}** "
            f"vs actual **${data.rerun_actual_total:.2f}** (≈"
            f"{(data.rerun_est_total / data.rerun_actual_total if data.rerun_actual_total else 0):.0f}"
            "×). Cause: sizing the replay by the original consult's full context footprint; a "
            "self-contained replay is cold, no cache, prompt body only. estimate_costs now sizes "
            "by the replay payload.",
            "",
            "## §2 Spend structure (task_type × tier)",
            f"{'task_type':<20}| frontier | mid | cheap | subtotal",
        ]
    # §2 table rows
    task_types = sorted({tt for (tt, _t) in data.by_tt_tier}, key=lambda t: -sum(
        data.by_tt_tier[(t, tier)] for tier in ("frontier", "mid", "cheap", "unknown")
    ))
    for tt in task_types:
        f = data.by_tt_tier.get((tt, "frontier"), Decimal("0"))
        m = data.by_tt_tier.get((tt, "mid"), Decimal("0"))
        c = data.by_tt_tier.get((tt, "cheap"), Decimal("0"))
        sub = f + m + c + data.by_tt_tier.get((tt, "unknown"), Decimal("0"))
        L.append(f"{tt:<20}｜{f:>9.2f}｜{m:>7.2f}｜{c:>7.2f}｜{sub:>8.2f}")

    # §2b — by project (aliased under external audience)
    top_projects = sorted(data.by_project, key=lambda p: data.by_project[p], reverse=True)[:10]
    L.append("")
    L.append("按项目（top 10）：" if zh else "By project (top 10):")
    for p in top_projects:
        L.append(f"- {_alias(p, alias, audience):<16} {_money(data.by_project[p])}")

    # §3
    if zh:
        L += [
            "",
            "## §3 政策偏差审计（声明政策 vs 实际路由）",
            f"- 决策单元（unit×component）：**{data.dec_total}**；跨档偏差 **{data.dec_dev}** "
            f"（**{dev_rate}**），偏差成本 **{_money(data.dec_dev_cost)}**。{_PENDING_POLICY} {tags_note}",
            "- 偏差=仅**跨档**错配（同档 opus↔fable 替换不算）。政策见 routing_policy.yaml（初稿待你改）。",
            "",
            f"{'task_type':<20}｜ 决策数 ｜ 偏差数 ｜ 偏差率 ｜ 偏差成本",
        ]
    else:
        L += [
            "",
            "## §3 Policy-deviation audit (stated vs revealed)",
            f"- Decisions (unit×component): **{data.dec_total}**; cross-tier deviations "
            f"**{data.dec_dev}** (**{dev_rate}**), deviation cost **{_money(data.dec_dev_cost)}**. "
            f"{_PENDING_POLICY} {tags_note}",
            "- Deviation = cross-tier only (same-tier opus↔fable is not one). Policy: "
            "routing_policy.yaml (draft, yours to edit).",
            "",
            f"{'task_type':<20}| decisions | devs | rate | dev_cost",
        ]
    for tt in sorted(data.dec_by_tt, key=lambda t: data.dec_by_tt[t][2], reverse=True):
        n, nd, cst = data.dec_by_tt[tt]
        rate = f"{nd / n:.0%}" if n else "—"
        L.append(f"{tt:<20}｜{n:>8}｜{nd:>6}｜{rate:>6}｜{cst:>9.2f}")

    # §4
    if zh:
        L += [
            "",
            "## §4 成本反事实（全程「若质量不降」）",
            "- 逐单元把当前模型换成更便宜候选（sonnet-5 双价 / haiku-4.5 / fable→opus-4.8）"
            "重定价，token 构成不变、tokenizer 按代换算。",
            "- **所有数字均为「若质量不降可省 $X」**——质量是否受损由 §5b 盲评回答，此处不评。",
            "- 详见 `counterfactual matrix` / `counterfactual top`（随时可重出）。",
        ]
    else:
        L += [
            "",
            "## §4 Cost counterfactuals (all framed *IF QUALITY HOLDS*)",
            "- Per unit, re-price the current model against cheaper candidates (Sonnet 5 "
            "dual era / Haiku 4.5 / fable→Opus 4.8); token mix fixed, tokenizer converted.",
            "- **Every figure is 'save $X IF quality holds'** — quality is answered by the "
            "§5b blind eval, not here.",
            "- See `counterfactual matrix` / `counterfactual top` (re-runnable).",
        ]

    # §5
    if zh:
        L += [
            "",
            "## §5 档内 advisor 溢价（与 §3 档位偏差**分开**呈现）",
            f"### §5a 节省上界（算术推论，非经验发现）：{data.premium_units} 个 fable 单元，"
            f"换 opus-4-8（同 frontier 档）省 **{_money(data.premium_total)}**（**{prem_pct}**）。",
            "  - 这是**价目表的直接推论**：同 tokenizer + token 构成锁定 ⇒ 节省额恒等于价差"
            "比例（opus 是 fable 半价 → 严格 50%），**不是经验测得的差距**，只是上界。",
            "  - 真正的经验问题——**质量是否受损**、以及**换模型后 token 量本身会变**"
            "（不同模型对同任务的输出长度/思考量不同）——由 §5b 盲评与后续在线数据回答。",
            f"### §5b 盲评版（{data.blind_total} 对已判，揭盲后按 recognition 分层）：",
            f"  **主结果只用 recognition=none 的 {data.blind_none} 对**（评审未认出原答案）："
            f"original(fable+完整上下文) 胜 **{data.none_original}**，replay(opus 冷启动) 胜 "
            f"**{data.none_replay}**，平手 {data.none_tie}。",
            "  **按不对称规则解读（不得反向过度声称）**：",
            f"  - replay 胜或平 = {data.none_replay + data.none_tie}/{data.blind_none} —— "
            "这才是「降档不劣」的证据方向；本轮该比例低，**不构成降档强证据**。",
            f"  - original 胜 = {data.none_original}/{data.blind_none} —— "
            "**不能据此写「fable 溢价合理」**：replay 是冷启动、无原对话上下文，劣势可能来自"
            "**上下文缺失而非模型质量**，本设计未排除该混淆，只能记「上下文混淆未排除」。",
            f"  - ⚠️ **位置/原版效应共线**：原答案 12 对中 {data.fable_pos_b} 落 B 位、"
            f"{data.fable_pos_a} 落 A 位（确定性 hash 分配不均衡）；{data.b_better_picked_original} "
            "个 b_better 恰好选中 B 位原答案 —— 胜负与「B 位」「原版」高度共线，**无法从本轮数据"
            "分离三者**。",
            f"  - 旁证层：另有 {data.blind_leak} 对评审认出了原答案（见 §1 泄漏率），"
            "单独列示、不进主结论。",
        ]
    else:
        L += [
            "",
            "## §5 Intra-tier advisor premium (kept SEPARATE from §3 tier deviation)",
            f"### §5a Saving upper bound (arithmetic corollary, NOT an empirical finding): "
            f"{data.premium_units} fable units, switching to Opus 4.8 (same frontier tier) "
            f"saves **{_money(data.premium_total)}** (**{prem_pct}**).",
            "  - This is a **direct corollary of the price sheet**: same tokenizer + token "
            "mix held fixed ⇒ the saving is identically the price ratio (opus is half of "
            "fable → strictly 50%). It is a bound, **not a measured gap**.",
            "  - The real empirical questions — **did quality hold**, and **does the token "
            "count itself change** on a different model (different output length/thinking "
            "per task) — are answered by §5b blind eval + later online data.",
            f"### §5b Blind ({data.blind_total} pairs judged, unblinded, layered by recognition):",
            f"  **Main result uses only the recognition=none {data.blind_none} pairs** (reviewer "
            f"did NOT recognise the original): original (fable + full context) wins "
            f"**{data.none_original}**, replay (opus cold-start) wins **{data.none_replay}**, "
            f"tie {data.none_tie}.",
            "  **Asymmetric reading (no reverse over-claim)**:",
            f"  - replay wins-or-ties = {data.none_replay + data.none_tie}/{data.blind_none} — "
            "this is the 'downgrade is not worse' evidence direction; the ratio is low here, so "
            "**NOT strong evidence for downgrading**.",
            f"  - original wins = {data.none_original}/{data.blind_none} — "
            "**cannot be read as 'the fable premium is justified'**: the replay is cold, without "
            "the original conversation context, so its disadvantage may be **missing context, "
            "not model quality**. This design does not rule that out — recorded as 'context "
            "confound not excluded'.",
            f"  - ⚠️ **position/original-answer confound**: of the 12 originals, {data.fable_pos_b} "
            f"landed in slot B and {data.fable_pos_a} in slot A (deterministic-hash assignment was "
            f"unbalanced); {data.b_better_picked_original} of the b_better verdicts picked the "
            "slot-B original — winner is collinear with 'slot B' and 'original', **not separable** "
            "from this round.",
            f"  - Side layer: another {data.blind_leak} pairs were recognised (see §1 leak rate), "
            "listed separately and excluded from the main conclusion.",
        ]

    # §6
    if zh:
        L += [
            "",
            "## §6 政策修订建议与预计影响",
            f"- {_PENDING_POLICY} 待你改 routing_policy.yaml + 校正标签后，本节自动重算。",
            "",
            "  **⚑ 一号处方（已验证，非推定）——子代理未钉 model 继承主线程**：",
            "  本审计 **19/19** 的 fable-on-workflow-subagent 偏差，回查父会话主线程当时"
            "均为 fable（0 反例）。root cause 是子代理定义缺 `model` 字段，随主线程继承 —— "
            "属**配置泄漏**，非路由决策失误。修复：**agent 定义加一行 pin `model`**（如 "
            "sonnet），一处改动即消除该类偏差。（general-purpose 的继承同形但本次未逐一回查，"
            "其 reason 仍标\"推定\"。）",
            "",
            "- 其余处方分**三类**（与 routing_decisions 的 `reason`/`outcome` 字段打通——"
            "每条偏差在导出的 routing_deviations.csv 里填 reason 后归入其一）：",
            "",
            "  **A. 换档（model swap）** — 同任务换更便宜档位：",
            "  - [ ] 机械型 workflow-subagent 收敛到 mid 档。",
            "  - [ ] 档内：低风险 advisor 咨询默认 opus-4-8，保留 fable 给高判断力场景。",
            "",
            "  **B. 改派（工作流形状）** — 不换模型而改任务的承载形态：",
            "  - [ ] 探索/研究类任务改派廉价子代理（如 Explore→haiku），而非主线程 frontier。",
            "  - [ ] 高 cache-hit 单元避免跨模型重跑（会丢 cache 折扣）——见 §4 敏感度。",
            "",
            "  **C. 保留但记录理由（policy exception with reason）** — 偏差是有意的：",
            "  - [ ] 在 routing_deviations.csv 填 `reason` + `outcome=adopted`，"
            "import 后 `source=manual` 永不被重生成覆盖，成为政策的显式例外。",
            "",
            "- 每条建议的预计影响 = 对应 §3 偏差成本 / §4 反事实节省（待数字定稿后填）。",
        ]
    else:
        L += [
            "",
            "## §6 Policy-revision proposals & projected impact",
            f"- {_PENDING_POLICY} recomputes once you edit routing_policy.yaml + correct tags.",
            "",
            "  **⚑ Lead prescription (VERIFIED, not presumed) — subagents inherit the "
            "main thread's model when unpinned**:",
            "  All **19/19** fable-on-workflow-subagent deviations had their parent session's "
            "main thread on fable at the time (0 counterexamples). Root cause is a missing "
            "`model` field in the agent definition, inherited from the main thread — a "
            "**config leak**, not a routing decision error. Fix: **pin `model` in the agent "
            "definition** (e.g. sonnet), one line clears the whole class. (general-purpose "
            "inheritance is the same shape but was not individually re-checked here; its "
            "reasons still say \"presumed\".)",
            "",
            "- The remaining prescriptions fall in **three classes** (wired to "
            "routing_decisions' `reason`/`outcome` — each deviation filed into one via "
            "routing_deviations.csv):",
            "",
            "  **A. Model swap** — same task, cheaper tier:",
            "  - [ ] Converge mechanical workflow-subagents to the mid tier.",
            "  - [ ] Intra-tier: default low-stakes advisor consults to Opus 4.8, keep Fable "
            "for high-judgment cases.",
            "",
            "  **B. Re-route (workflow shape)** — change the task's carrier, not the model:",
            "  - [ ] Route exploratory/research work to cheap subagents (Explore→haiku), not "
            "the frontier main thread.",
            "  - [ ] Avoid cross-model rerun of high cache-hit units (forfeits cache discount) "
            "— see §4 sensitivity.",
            "",
            "  **C. Keep but record the reason (policy exception with reason)** — the "
            "deviation is intentional:",
            "  - [ ] Fill `reason` + `outcome=adopted` in routing_deviations.csv; after import "
            "`source=manual` is never regenerated, becoming an explicit policy exception.",
            "",
            "- Each proposal's projected impact = its §3 deviation cost / §4 saving (fill once "
            "numbers are final).",
        ]

    L += ["", "---", "_generated skeleton; PENDING markers gate the not-yet-final sections._"]
    return "\n".join(L)


def generate_reports(
    db_url: str | None = None,
    *,
    audience: str = "external",
    langs: tuple[str, ...] = ("zh", "en"),
    out_dir: Path | str = ".",
    alias_path: Path | str = DEFAULT_ALIAS,
    as_of: datetime | None = None,
) -> list[Path]:
    data = gather(db_url, as_of=as_of)
    alias = build_alias_map(db_url, Path(alias_path))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for lang in langs:
        text = render(data, lang=lang, audience=audience, alias=alias)
        path = out / f"report_{lang}.md"
        path.write_text(text + "\n", encoding="utf-8")
        written.append(path)
    return written


def refresh_chain(
    db_url: str | None = None,
    *,
    tags_csv: str | None = None,
    deviations_csv: str | None = None,
    audience: str = "external",
    out_dir: Path | str = ".",
    as_of: datetime | None = None,
) -> list[str]:
    """tags import → decisions generate → (counterfactual is read-only) → report.

    Fixed order, idempotent. Returns a log of steps for the caller to print.
    ``as_of`` freezes the report/decisions snapshot; daily ingest is unaffected.
    """
    from traceguard.routing_audit.routing_decisions import (
        generate_decisions,
        import_decisions_csv,
    )
    from traceguard.routing_audit.task_tags import import_csv as import_tags

    log: list[str] = []
    if as_of is not None:
        log.append(f"as-of freeze: invoked_at <= {as_of.isoformat()}")
    if tags_csv:
        s = import_tags(tags_csv, db_url)
        log.append(f"tags import: {s.updated} manual rows")
    if deviations_csv:
        s2 = import_decisions_csv(deviations_csv, db_url)
        log.append(f"deviations import: {s2.updated} manual rows")
    gen = generate_decisions(db_url, write=True, as_of=as_of)
    log.append(f"decisions generate: {gen.decisions} decisions, {gen.deviations} deviations")
    paths = generate_reports(db_url, audience=audience, out_dir=out_dir, as_of=as_of)
    log.append("reports: " + ", ".join(str(p) for p in paths))
    return log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m traceguard.routing_audit.report",
        description="Bilingual report-skeleton generator + refresh chain.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="write report_zh.md / report_en.md")
    p_gen.add_argument("--db", default=DEFAULT_DB)
    p_gen.add_argument("--audience", choices=("personal", "external"), default="external")
    p_gen.add_argument("--lang", choices=("zh", "en", "both"), default="both")
    p_gen.add_argument("--out-dir", default=".")
    p_gen.add_argument("--as-of", default=None, help="freeze snapshot: only traces invoked_at <= this")

    p_ref = sub.add_parser("refresh", help="tags import → decisions → report (idempotent)")
    p_ref.add_argument("--db", default=DEFAULT_DB)
    p_ref.add_argument("--tags-csv", default=None)
    p_ref.add_argument("--deviations-csv", default=None)
    p_ref.add_argument("--audience", choices=("personal", "external"), default="external")
    p_ref.add_argument("--out-dir", default=".")
    p_ref.add_argument("--as-of", default=None, help="freeze snapshot: only traces invoked_at <= this")

    from traceguard.routing_audit.counterfactual import parse_as_of

    args = parser.parse_args(argv)
    if args.command == "generate":
        langs = ("zh", "en") if args.lang == "both" else (args.lang,)
        paths = generate_reports(
            args.db, audience=args.audience, langs=langs, out_dir=args.out_dir,
            as_of=parse_as_of(args.as_of),
        )
        print("wrote " + ", ".join(str(p) for p in paths))
    elif args.command == "refresh":
        log = refresh_chain(
            args.db, tags_csv=args.tags_csv, deviations_csv=args.deviations_csv,
            audience=args.audience, out_dir=args.out_dir, as_of=parse_as_of(args.as_of),
        )
        print("\n".join(log))
    return 0


if __name__ == "__main__":
    sys.exit(main())

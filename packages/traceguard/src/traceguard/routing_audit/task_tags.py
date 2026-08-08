"""task_type tagging for backfilled Claude Code traces (main thread focus).

Tagging unit — chosen from measured local data (2026-07-02: 84 main sessions,
996 human turns; sessions are too coarse at median 7 / max 68 turns, turns too
many to review by hand): an **idle-gap segment** of a session's human turns.
A new unit starts when the gap between consecutive human prompts exceeds
``--gap-minutes`` (default 60, yielding ~314 units locally — reviewable by
hand). ``unit_id = <session_id>#s<NN>``; the unit owns the half-open window
``[ts_start, next unit's ts_start)`` (last unit: until session end), and
traces — including subagent traces, which share the parent sessionId — join
by session_id + invoked_at window at report time. The contract ``traces``
table is never modified.

Heuristic v1 (no LLM calls): bilingual keyword tables over the unit's FIRST
human prompt, plus weak gitBranch prefix signals. Ties break by specificity
order (debug > implement > ops > research > decision > writing). Manual
corrections flow through the export/import CSV round-trip and always win
over later heuristic re-runs.

Privacy: prompt text is read in memory for classification only; the DB
stores no content. The export CSV carries a redacted, whitespace-collapsed
summary (≤100 chars, secret-looking tokens masked) purely so a human can
label rows — keep it out of version control like the DB itself.

CLI::

    python -m traceguard.routing_audit.task_tags heuristic [--write]
    python -m traceguard.routing_audit.task_tags export --csv units.csv
    python -m traceguard.routing_audit.task_tags import --csv units.csv
    python -m traceguard.routing_audit.task_tags report
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import uuid as uuid_mod
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.routing_audit.ingest_claude_code import (
    DEFAULT_SOURCE,
    _parse_ts,
    map_project,
)
from traceguard.routing_audit.models import RoutingAuditTaskTag, ensure_tables
from traceguard.store.models import Trace, make_engine

DEFAULT_DB = "sqlite:///traces_routing_audit.db"
DEFAULT_GAP_MINUTES = 60

TASK_TYPES = (
    "coding-implement",
    "coding-debug",
    "research-explore",
    "writing-doc",
    "decision-advisor",
    "ops-routine",
    "other",
)

# Bilingual keyword tables. ASCII terms match on word boundaries, CJK terms
# as substrings. Curated from the local prompt distribution — extend freely;
# manual CSV corrections always override.
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "coding-debug": (
        "报错", "修复", "排查", "不工作", "修一下", "修掉", "挂了", "崩溃", "异常",
        "失败", "为什么不", "红了", "回归",
        "bug", "fix", "crash", "error", "traceback", "debug", "broken", "failing",
        "regression", "flaky",
    ),
    "coding-implement": (
        "实现", "新增", "加一个", "加个", "创建", "开发", "重构", "接入", "集成",
        "迁移", "升级", "写一个", "写个", "支持", "改成", "改造", "模块", "写代码",
        "写测试", "本刀", "下一刀",
        "implement", "feature", "build", "refactor", "migrate", "module", "api",
        "cli", "endpoint", "schema",
    ),
    "research-explore": (
        "调研", "研究", "查一下", "搜索", "了解", "什么是", "是什么", "对比", "比较",
        "分析", "看看", "读一下", "评估", "调查", "梳理", "找找", "探查", "核实",
        "search", "research", "explore", "investigate", "compare", "analyze",
        "understand", "review paper",
    ),
    "writing-doc": (
        "文档", "文案", "报告", "翻译", "润色", "文章", "章节", "写一篇", "总结",
        "小说", "剧情", "角色", "大纲", "稿", "撰写", "案例",
        "readme", "draft", "blog", "document", "prose", "chapter", "case study",
        "changelog", "release note",
    ),
    "decision-advisor": (
        "该不该", "要不要", "怎么选", "建议", "意见", "决定", "值不值", "是否应该",
        "你觉得", "选哪个", "怎么办", "权衡", "取舍", "方向", "定位", "策略",
        "should i", "which one", "pros", "cons", "trade-off", "tradeoff", "advice",
    ),
    "ops-routine": (
        "部署", "发布", "依赖", "安装", "配置", "备份", "清理", "跑一下", "执行",
        "运行", "打包", "环境", "重跑", "升级到",
        "deploy", "release", "publish", "commit", "push", "merge", "install",
        "config", "cron", "rerun", "sync", "lint", "ci", "pipeline",
    ),
}
# Tie-break: more specific intent wins when hit counts are equal.
_PRIORITY = (
    "coding-debug",
    "coding-implement",
    "ops-routine",
    "research-explore",
    "decision-advisor",
    "writing-doc",
)

_BRANCH_SIGNALS = {
    "fix/": "coding-debug",
    "bugfix/": "coding-debug",
    "feat/": "coding-implement",
    "feature/": "coding-implement",
    "docs/": "writing-doc",
    "release/": "ops-routine",
    "chore/": "ops-routine",
}

_SECRET_PATTERNS = [
    re.compile(p)
    for p in (
        r"sk-[A-Za-z0-9_-]{8,}",
        r"(?:ghp|gho|ghu|ghs)_[A-Za-z0-9]{20,}",
        r"xox[a-z]-[A-Za-z0-9-]{10,}",
        r"AKIA[A-Z0-9]{12,}",
        r"eyJ[A-Za-z0-9_-]{20,}",
        r"Bearer\s+\S{16,}",
        r"\b[A-Fa-f0-9]{32,}\b",
        r"\b[A-Za-z0-9+/]{40,}={0,2}\b",
    )
]


@dataclass
class UnitRecord:
    unit_id: str
    session_id: str
    project: str
    ts_start: datetime
    ts_end: datetime | None  # start of the next unit; None = until session end
    n_turns: int
    first_prompt: str
    git_branch: str | None


@dataclass
class TagStats:
    units: int = 0
    inserted: int = 0
    updated: int = 0
    manual_kept: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    batch_id: str | None = None


def _batch_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"tag-{stamp}-{uuid_mod.uuid4().hex[:6]}"


def _word_hit(term: str, text: str) -> bool:
    if term.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    return term in text


def classify_prompt(text: str, git_branch: str | None = None) -> tuple[str, int]:
    """Keyword-vote classification of one prompt. Returns (task_type, hits)."""
    lowered = text.lower()
    scores = {t: 0 for t in _PRIORITY}
    for task_type, terms in _KEYWORDS.items():
        scores[task_type] = sum(1 for term in terms if _word_hit(term, lowered))
    if git_branch:
        branch = git_branch.lower()
        for prefix, task_type in _BRANCH_SIGNALS.items():
            if branch.startswith(prefix):
                scores[task_type] += 1
    best = max(_PRIORITY, key=lambda t: (scores[t], -_PRIORITY.index(t)))
    if scores[best] == 0:
        return "other", 0
    return best, scores[best]


def redact_summary(text: str, limit: int = 100) -> str:
    """Whitespace-collapsed, secret-masked, truncated label for the CSV."""
    flat = re.sub(r"\s+", " ", text).strip()
    for pattern in _SECRET_PATTERNS:
        flat = pattern.sub("[REDACTED]", flat)
    return flat[:limit]


def _human_prompt(rec: dict[str, Any]) -> str | None:
    """Extract the human prompt text from a user line, else None."""
    if rec.get("type") != "user" or "toolUseResult" in rec or rec.get("isMeta"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        has_tool_result = any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
        if texts and not has_tool_result:
            return "\n".join(texts)
    return None


def iter_session_units(
    source_root: Path, *, gap_minutes: int = DEFAULT_GAP_MINUTES
) -> Iterator[UnitRecord]:
    """Segment every main transcript's human turns by idle gap."""
    if not source_root.is_dir():
        return
    for project_dir in sorted(p for p in source_root.iterdir() if p.is_dir()):
        for path in sorted(project_dir.glob("*.jsonl")):
            turns: list[tuple[datetime, str]] = []
            session_id = path.stem
            cwd = None
            git_branch = None
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"user"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    if cwd is None and rec.get("cwd"):
                        cwd = rec["cwd"]
                    if git_branch is None and rec.get("gitBranch"):
                        git_branch = rec["gitBranch"]
                    if isinstance(rec.get("sessionId"), str):
                        session_id = rec["sessionId"]
                    prompt = _human_prompt(rec)
                    ts = _parse_ts(rec.get("timestamp"))
                    if prompt is not None and ts is not None:
                        turns.append((ts, prompt))
            if not turns:
                continue
            turns.sort(key=lambda t: t[0])
            project = map_project(cwd)
            segments: list[list[tuple[datetime, str]]] = [[turns[0]]]
            for prev, cur in zip(turns, turns[1:]):
                if (cur[0] - prev[0]).total_seconds() > gap_minutes * 60:
                    segments.append([cur])
                else:
                    segments[-1].append(cur)
            for idx, seg in enumerate(segments, start=1):
                next_start = segments[idx][0][0] if idx < len(segments) else None
                yield UnitRecord(
                    unit_id=f"{session_id}#s{idx:02d}",
                    session_id=session_id,
                    project=project,
                    ts_start=seg[0][0],
                    ts_end=next_start,
                    n_turns=len(seg),
                    first_prompt=seg[0][1],
                    git_branch=git_branch,
                )


def _db_exists(db_url: str | None) -> bool:
    """True if the target DB is already materialised.

    A dry-run must not create a database file as a side effect of previewing.
    For a SQLite URL that means checking the path before connecting, because
    connecting is what creates it. Anything else is assumed reachable.
    """
    url = db_url or DEFAULT_DB
    if not url.startswith("sqlite"):
        return True
    path = url.split("///", 1)[-1].split("?", 1)[0]
    if not path or path == ":memory:":
        return False
    return Path(path).exists()


def tag_heuristic(
    source: Path | str = DEFAULT_SOURCE,
    db_url: str | None = None,
    *,
    write: bool = False,
    gap_minutes: int = DEFAULT_GAP_MINUTES,
) -> TagStats:
    """Classify every unit; optionally upsert into routing_audit_task_tags.

    Existing ``source="manual"`` rows are never overwritten; existing
    heuristic rows are updated in place (keyword-table upgrades re-apply).
    """
    stats = TagStats()
    units: list[tuple[UnitRecord, str]] = []
    for unit in iter_session_units(Path(source).expanduser(), gap_minutes=gap_minutes):
        task_type, _ = classify_prompt(unit.first_prompt, unit.git_branch)
        units.append((unit, task_type))
        stats.by_type[task_type] = stats.by_type.get(task_type, 0) + 1
    stats.units = len(units)

    if not write and not _db_exists(db_url):
        # Nothing to compare against, and a dry-run must not materialise a
        # database just to say so. Every unit would be an insert.
        stats.inserted = len(units)
        return stats

    engine = make_engine(db_url)
    if write:
        ensure_tables(engine)
    stats.batch_id = _batch_id() if write else None
    # The session is opened in BOTH modes. A dry-run that returns before
    # touching the DB reports inserted/updated/manual_kept as a structural 0 —
    # exactly the counters a dry-run exists to show. Same blind spot as
    # routing_decisions.generate had; fixed the same way, and it commits
    # nothing unless write=True.
    with Session(engine) as sess:
        for unit, task_type in units:
            existing = sess.get(RoutingAuditTaskTag, unit.unit_id)
            if existing is None:
                stats.inserted += 1
                if write:
                    sess.add(
                        RoutingAuditTaskTag(
                            unit_id=unit.unit_id,
                            session_id=unit.session_id,
                            project=unit.project,
                            ts_start=unit.ts_start,
                            ts_end=unit.ts_end,
                            n_turns=unit.n_turns,
                            task_type=task_type,
                            source="heuristic",
                            batch_id=stats.batch_id,
                        )
                    )
            elif existing.source == "manual":
                stats.manual_kept += 1
            else:
                stats.updated += 1
                if write:
                    existing.task_type = task_type
                    existing.ts_end = unit.ts_end
                    existing.n_turns = unit.n_turns
                    existing.batch_id = stats.batch_id
        if write:
            sess.commit()
        else:
            sess.rollback()
    return stats


def export_csv(
    csv_path: Path | str,
    db_url: str | None = None,
    source: Path | str = DEFAULT_SOURCE,
    *,
    gap_minutes: int = DEFAULT_GAP_MINUTES,
) -> int:
    """Write tags to CSV for manual review. Returns the row count.

    Summaries are regenerated live from the source transcripts (never stored
    in the DB); units whose source file has since been rewritten get "".
    """
    summaries = {
        unit.unit_id: redact_summary(unit.first_prompt)
        for unit in iter_session_units(Path(source).expanduser(), gap_minutes=gap_minutes)
    }
    engine = make_engine(db_url)
    ensure_tables(engine)
    with Session(engine) as sess:
        tags = list(
            sess.scalars(
                select(RoutingAuditTaskTag).order_by(
                    RoutingAuditTaskTag.project, RoutingAuditTaskTag.ts_start
                )
            )
        )
        rows = [
            {
                "unit_id": t.unit_id,
                "project": t.project,
                "n_turns": t.n_turns,
                "ts_start": t.ts_start.isoformat(),
                "task_type": t.task_type,
                "source": t.source,
                "summary": summaries.get(t.unit_id, ""),
            }
            for t in tags
        ]
    with Path(csv_path).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["unit_id", "project", "n_turns", "ts_start", "task_type", "source", "summary"],
        )
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def import_csv(csv_path: Path | str, db_url: str | None = None) -> TagStats:
    """Re-import an edited CSV: every valid row becomes ``source="manual"``."""
    stats = TagStats()
    stats.batch_id = _batch_id()
    engine = make_engine(db_url)
    ensure_tables(engine)
    skipped_missing = 0
    skipped_bad_type = 0
    with Path(csv_path).open(encoding="utf-8", newline="") as fh, Session(engine) as sess:
        for row in csv.DictReader(fh):
            unit_id = (row.get("unit_id") or "").strip()
            task_type = (row.get("task_type") or "").strip()
            if task_type not in TASK_TYPES:
                skipped_bad_type += 1
                continue
            existing = sess.get(RoutingAuditTaskTag, unit_id)
            if existing is None:
                skipped_missing += 1
                continue
            existing.task_type = task_type
            existing.source = "manual"
            existing.batch_id = stats.batch_id
            stats.updated += 1
            stats.by_type[task_type] = stats.by_type.get(task_type, 0) + 1
        sess.commit()
    stats.units = stats.updated + skipped_missing + skipped_bad_type
    if skipped_missing:
        stats.by_type["(unit not in db)"] = skipped_missing
    if skipped_bad_type:
        stats.by_type["(bad task_type)"] = skipped_bad_type
    return stats


def _cache_fields(output_parsed: Any) -> tuple[int, int, int]:
    """(input_tokens, cache_read, cache_creation) from a trace's meta."""
    usage = (output_parsed or {}).get("usage") or {}
    return (
        int(usage.get("input_tokens") or 0),
        int(usage.get("cache_read_input_tokens") or 0),
        int(usage.get("cache_creation_input_tokens") or 0),
    )


@dataclass
class UnitIndex:
    """Maps a trace (session_id, invoked_at) → its tagging unit.

    Shared by the pivot, the decisions audit, and the counterfactual engine so
    all three attribute traces to units identically. Built from the persisted
    ``routing_audit_task_tags`` windows (see :func:`load_unit_index`).
    """

    # session_id -> sorted [(ts_start, ts_end, unit_id, task_type, project)]
    spans: dict[str, list[tuple[datetime, datetime | None, str, str, str]]]

    def lookup(
        self, session_id: str | None, invoked_at: datetime
    ) -> tuple[str, str, str] | None:
        """Return (unit_id, task_type, project) for a trace, or None if untagged."""
        spans = self.spans.get(session_id or "")
        if not spans:
            return None
        idx = bisect_right([s[0] for s in spans], invoked_at) - 1
        if idx < 0:
            return None
        ts_start, ts_end, unit_id, task_type, project = spans[idx]
        if ts_end is not None and invoked_at >= ts_end:
            return None
        return unit_id, task_type, project


def load_unit_index(engine: Any) -> UnitIndex:
    """Build a :class:`UnitIndex` from the persisted task_tags rows."""
    spans: dict[str, list[tuple[datetime, datetime | None, str, str, str]]] = defaultdict(list)
    with Session(engine) as sess:
        for t in sess.scalars(select(RoutingAuditTaskTag)):
            spans[t.session_id].append((t.ts_start, t.ts_end, t.unit_id, t.task_type, t.project))
    for lst in spans.values():
        lst.sort(key=lambda s: s[0])
    return UnitIndex(spans=dict(spans))


def format_pivot(db_url: str | None = None) -> str:
    """task_type × model pivot: traces, cost, and per-type cache-hit share.

    cache-hit share = Σcache_read / Σ(input + cache_read + cache_creation)
    over the task_type's traces — the sensitivity knob for counterfactual
    rerun costs (high share ⇒ rerunning on another model forfeits cache
    savings).
    """
    engine = make_engine(db_url)
    ensure_tables(engine)
    index = load_unit_index(engine)
    with Session(engine) as sess:
        traces = list(
            sess.execute(
                select(Trace.output_parsed, Trace.invoked_at, Trace.model_id, Trace.cost_usd)
            )
        )

    # (task_type, model) -> [traces, cost]; task_type -> token sums
    cells: dict[tuple[str, str], list[Any]] = defaultdict(lambda: [0, Decimal("0")])
    token_sums: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for output_parsed, invoked_at, model_id, cost_usd in traces:
        session_id = (output_parsed or {}).get("session_id")
        hit = index.lookup(session_id, invoked_at)
        task_type = hit[1] if hit is not None else "(untagged)"
        cell = cells[(task_type, model_id or "(api-error)")]
        cell[0] += 1
        cell[1] += cost_usd or Decimal("0")
        sums = token_sums[task_type]
        inp, cache_read, cache_creation = _cache_fields(output_parsed)
        sums[0] += inp
        sums[1] += cache_read
        sums[2] += cache_creation

    type_cost = defaultdict(lambda: Decimal("0"))
    for (task_type, _), (_, cost) in cells.items():
        type_cost[task_type] += cost

    header = f"{'task_type':<20} {'model':<28} {'traces':>7} {'cost_usd':>11} {'cache-hit':>10}"
    lines = [header, "-" * len(header)]
    for task_type in sorted(type_cost, key=lambda t: type_cost[t], reverse=True):
        inp, cache_read, cache_creation = token_sums[task_type]
        denom = inp + cache_read + cache_creation
        share = f"{cache_read / denom:>9.1%}" if denom else f"{'—':>9}"
        first = True
        for (tt, model), (n, cost) in sorted(
            cells.items(), key=lambda kv: kv[1][1], reverse=True
        ):
            if tt != task_type:
                continue
            lines.append(
                f"{task_type if first else '':<20} {model:<28} {n:>7} {cost:>11.4f} "
                f"{share if first else '':>10}"
            )
            first = False
        lines.append(
            f"{'':<20} {'subtotal':<28} "
            f"{sum(n for (tt, _), (n, _) in cells.items() if tt == task_type):>7} "
            f"{type_cost[task_type]:>11.4f} {'':>10}"
        )
    lines.append("-" * len(header))
    total_n = sum(n for n, _ in cells.values())
    total_cost = sum((c for _, c in cells.values()), Decimal("0"))
    lines.append(f"{'TOTAL':<20} {'':<28} {total_n:>7} {total_cost:>11.4f}")
    return "\n".join(lines)


def _format_tag_stats(stats: TagStats, *, wrote: bool) -> str:
    mode = f"WROTE batch={stats.batch_id}" if wrote else "DRY-RUN (no writes)"
    lines = [
        f"== routing_audit: task tagging — {mode} ==",
        f"units: {stats.units} | inserted: {stats.inserted} | updated: {stats.updated} "
        f"| manual kept: {stats.manual_kept}",
    ]
    for task_type, count in sorted(stats.by_type.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {task_type:<20} {count}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m traceguard.routing_audit.task_tags",
        description="task_type tagging over backfilled Claude Code traces.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", default=DEFAULT_DB)
        p.add_argument("--source", default=str(DEFAULT_SOURCE))
        p.add_argument("--gap-minutes", type=int, default=DEFAULT_GAP_MINUTES)

    p_heur = sub.add_parser("heuristic", help="classify units (dry-run unless --write)")
    add_common(p_heur)
    p_heur.add_argument("--write", action="store_true")

    p_export = sub.add_parser("export", help="export tags to CSV for manual review")
    add_common(p_export)
    p_export.add_argument("--csv", default="routing_audit_task_tags.csv")

    p_import = sub.add_parser("import", help="re-import edited CSV as manual tags")
    p_import.add_argument("--db", default=DEFAULT_DB)
    p_import.add_argument("--csv", required=True)

    p_report = sub.add_parser("report", help="task_type × model × cost pivot")
    p_report.add_argument("--db", default=DEFAULT_DB)

    args = parser.parse_args(argv)
    if args.command == "heuristic":
        stats = tag_heuristic(
            args.source, args.db, write=args.write, gap_minutes=args.gap_minutes
        )
        print(_format_tag_stats(stats, wrote=args.write))
        if not args.write:
            print("\n(dry-run — re-run with --write to persist)")
    elif args.command == "export":
        n = export_csv(args.csv, args.db, args.source, gap_minutes=args.gap_minutes)
        print(f"exported {n} units to {args.csv} (private summaries — keep out of git)")
    elif args.command == "import":
        stats = import_csv(args.csv, args.db)
        print(_format_tag_stats(stats, wrote=True))
    elif args.command == "report":
        print(format_pivot(args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())

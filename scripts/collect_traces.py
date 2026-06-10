#!/usr/bin/env python3
"""
market_intel → traceguard trace collector
读取 4 个 OpenClaw 实例的 cron runs JSONL，把新记录插入 traceguard 的 eval_traces 表。

用法:
  python3 collect_traces.py [--db /path/to/traces.db] [--dry-run] [--clear-demo]
"""

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────────────────────────

MARKET_INTEL_BASE = Path.home() / "market_intel"

PIPELINE_NAME = "market-intelligence"  # 与 pipeline.yaml 保持一致

STEP_MAP = {
    "01": {
        "linkedin-collection-001.jsonl": "step_01_collect",
    },
    "02": {
        "data-analysis-001.jsonl": "step_02_analyze",
    },
    "03": {
        "market-report-001.jsonl": "step_03_market_report",
    },
    "04": {
        "866551ac-56fd-412e-b4c0-b69f06414d3e.jsonl": "step_04_leads_report",  # leads-gen
        "672729c9-2fda-4879-848e-f7c41bfa25a8.jsonl": "step_04_leads_report",  # leads-deliver
        "leads-report-001.jsonl": "step_04_leads_report",
        "fca60a5e-6bf4-4ee5-a761-0689867a7618.jsonl": "step_04_leads_report",
    },
}

# ── 判断 pass/fail ─────────────────────────────────────────────────────────────

def evaluate_run(run: dict, step_name: str) -> tuple[str, float, list[str]]:
    """返回 (action, score, issues)"""
    issues = []

    # 1. 基本执行状态
    if run.get("status") != "ok":
        issues.append(f"execution_error: {run.get('error', 'unknown')[:120]}")

    # 2. Telegram 交付（step_03 / step_04 检查）
    if step_name in ("step_03_market_report", "step_04_leads_report"):
        if run.get("delivered") is False or run.get("deliveryStatus") == "not-delivered":
            issues.append("telegram_not_delivered")

    # 3. [REPORT_GENERATED] 信号
    summary = run.get("summary", "")
    if step_name in ("step_03_market_report", "step_04_leads_report"):
        if "[REPORT_GENERATED]" not in summary:
            issues.append("missing_REPORT_GENERATED_signal")

    # 4. 超时
    duration_ms = run.get("durationMs", 0)
    if duration_ms >= 890_000:
        issues.append(f"near_timeout: {duration_ms/1000:.0f}s")

    passed = len(issues) == 0
    score = max(0.0, round(1.0 - len(issues) * 0.25, 2))
    action = "pass" if passed else "fail"
    return action, score, issues


# ── 主采集逻辑 ─────────────────────────────────────────────────────────────────

def collect_runs() -> list[dict]:
    traces = []
    seen_ids = set()

    for inst, file_map in STEP_MAP.items():
        runs_dir = MARKET_INTEL_BASE / inst / "cron" / "runs"
        if not runs_dir.exists():
            continue

        for filename, step_name in file_map.items():
            fpath = runs_dir / filename
            if not fpath.exists():
                continue

            with open(fpath) as f:
                lines = f.readlines()

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    run = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if run.get("action") != "finished":
                    continue

                ts_ms = run.get("ts") or run.get("runAtMs")
                if not ts_ms:
                    continue

                # 稳定唯一 ID
                job_id = run.get("jobId", filename)
                uid = hashlib.sha1(f"{inst}:{job_id}:{ts_ms}".encode()).hexdigest()
                if uid in seen_ids:
                    continue
                seen_ids.add(uid)

                action, score, issues = evaluate_run(run, step_name)

                created_at = datetime.fromtimestamp(
                    ts_ms / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S")

                summary = run.get("summary", "")
                output_preview = summary[:300].strip() if summary else f"status={run.get('status')} dur={run.get('durationMs',0)//1000}s"

                traces.append({
                    "uid": uid,
                    "pipeline_name": PIPELINE_NAME,
                    "step_name": step_name,
                    "action": action,
                    "passed": 1 if action == "pass" else 0,
                    "score": score,
                    "issues": json.dumps(issues, ensure_ascii=False),
                    "attempt": 1,
                    "output_preview": output_preview,
                    "created_at": created_at,
                })

    return traces


# ── SQLite ────────────────────────────────────────────────────────────────────

def find_db() -> Path:
    script_dir = Path(__file__).parent
    candidates = [
        script_dir / "traces.db",
        script_dir / "guardian.db",
        script_dir / ".guardian" / "traces.db",
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def get_existing_uids(conn: sqlite3.Connection) -> set:
    """用 output_preview 末尾附加的 uid 做去重（因为 id 是自增）"""
    rows = conn.execute(
        "SELECT output_preview FROM eval_traces WHERE pipeline_name = ?",
        (PIPELINE_NAME,)
    ).fetchall()
    uids = set()
    for (preview,) in rows:
        if preview and preview.startswith("__uid:"):
            uids.add(preview[6:46])
    return uids


def insert_traces(conn: sqlite3.Connection, traces: list[dict], dry_run: bool) -> tuple[int, int]:
    existing = get_existing_uids(conn)
    inserted = skipped = 0
    for t in traces:
        if t["uid"] in existing:
            skipped += 1
            continue
        # 在 output_preview 前缀上 uid 供后续去重
        preview_with_uid = f"__uid:{t['uid']}__ {t['output_preview']}"
        if not dry_run:
            conn.execute(
                """INSERT INTO eval_traces
                   (pipeline_name, step_name, action, passed, score, issues, attempt, output_preview, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (t["pipeline_name"], t["step_name"], t["action"], t["passed"],
                 t["score"], t["issues"], t["attempt"], preview_with_uid, t["created_at"]),
            )
        inserted += 1
    if not dry_run:
        conn.commit()
    return inserted, skipped


# ── 摘要打印 ───────────────────────────────────────────────────────────────────

def print_summary(traces: list[dict]):
    from collections import defaultdict
    by_step = defaultdict(lambda: {"pass": 0, "fail": 0})
    for t in traces:
        by_step[t["step_name"]][t["action"]] += 1

    print("\n📊 采集摘要:")
    print(f"{'Step':<28} {'Pass':>5} {'Fail':>5} {'Total':>6} {'通过率':>7}")
    print("-" * 55)
    for step in sorted(by_step):
        p = by_step[step]["pass"]
        f = by_step[step]["fail"]
        total = p + f
        rate = f"{p/total*100:.0f}%" if total else "N/A"
        print(f"{step:<28} {p:>5} {f:>5} {total:>6} {rate:>7}")

    recent_fails = sorted(
        [t for t in traces if t["action"] == "fail"],
        key=lambda x: x["created_at"], reverse=True
    )
    if recent_fails:
        print(f"\n⚠️  最近失败（前5条）:")
        for t in recent_fails[:5]:
            issues = json.loads(t["issues"])
            print(f"  [{t['created_at'][:10]}] {t['step_name']} — {', '.join(issues)}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Collect market_intel traces into traceguard DB")
    parser.add_argument("--db", help="traceguard SQLite 路径（自动检测）")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写入")
    parser.add_argument("--clear-demo", action="store_true", help="清除 Demo 测试数据再导入")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else find_db()
    print(f"🗄️  数据库: {db_path}")

    traces = collect_runs()
    print(f"📥 采集到 {len(traces)} 条记录")

    if not traces:
        print("没有数据，退出。")
        return

    print_summary(traces)

    conn = sqlite3.connect(db_path)

    if args.clear_demo:
        deleted = conn.execute(
            "DELETE FROM eval_traces WHERE output_preview LIKE 'Demo output%'"
        ).rowcount
        conn.commit()
        print(f"\n🗑️  已清除 {deleted} 条 Demo 数据")

    inserted, skipped = insert_traces(conn, traces, args.dry_run)
    total = conn.execute("SELECT COUNT(*) FROM eval_traces").fetchone()[0]
    conn.close()

    if args.dry_run:
        print(f"\n[dry-run] 将插入 {inserted} 条，跳过重复 {skipped} 条")
    else:
        print(f"\n✅ 插入 {inserted} 条新记录，跳过重复 {skipped} 条")
        print(f"   数据库共 {total} 条 trace，刷新 dashboard 即可查看")


if __name__ == "__main__":
    main()

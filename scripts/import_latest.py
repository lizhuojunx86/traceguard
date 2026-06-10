#!/usr/bin/env python3
"""
Mac 侧一键导入：将最新的 real_traces_export.json 增量写入 traceguard 数据库。
在 ~/Desktop/APP/traceguard 目录下直接运行：python3 import_latest.py
"""
import json
import sqlite3
from pathlib import Path

base = Path(__file__).parent
db_path = base / "traces.db"
export_path = base / "real_traces_export.json"

if not export_path.exists():
    print("❌ real_traces_export.json 不存在，请先让 Cowork 任务运行一次")
    raise SystemExit(1)

traces = json.loads(export_path.read_text())
conn = sqlite3.connect(db_path)

existing_rows = conn.execute(
    "SELECT output_preview FROM eval_traces WHERE pipeline_name='market-intelligence'"
).fetchall()
existing_uids = {r[0][6:46] for r in existing_rows if r[0] and r[0].startswith('__uid:')}

inserted = 0
for t in traces:
    if t["uid"] in existing_uids:
        continue
    preview = f"__uid:{t['uid']}__ {t['output_preview']}"
    conn.execute(
        "INSERT INTO eval_traces (pipeline_name,step_name,action,passed,score,issues,attempt,output_preview,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (t["pipeline_name"], t["step_name"], t["action"], t["passed"],
         t["score"], t["issues"], t["attempt"], preview, t["created_at"])
    )
    inserted += 1

conn.commit()
total = conn.execute("SELECT COUNT(*) FROM eval_traces").fetchone()[0]
conn.close()
print(f"✅ 新增 {inserted} 条，数据库共 {total} 条 trace")

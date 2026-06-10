"""Import market_intel traces from JSON export into a fresh SQLite database."""
import json
import sqlite3
import os
import sys

src = os.path.join(os.path.dirname(__file__), "market_intel_export.json")
dst = os.path.join(os.path.dirname(__file__), "market_intel.db")

if not os.path.exists(src):
    print(f"ERROR: {src} not found", file=sys.stderr)
    sys.exit(1)

with open(src) as f:
    data = json.load(f)

conn = sqlite3.connect(dst)
conn.execute("""
    CREATE TABLE IF NOT EXISTS eval_traces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pipeline_name TEXT NOT NULL,
        step_name TEXT NOT NULL,
        action TEXT NOT NULL,
        passed BOOLEAN NOT NULL,
        score REAL NOT NULL,
        issues TEXT NOT NULL DEFAULT '[]',
        attempt INTEGER NOT NULL DEFAULT 1,
        output_preview TEXT,
        created_at DATETIME NOT NULL
    )
""")
conn.execute("DELETE FROM eval_traces")

for r in data:
    conn.execute("""
        INSERT INTO eval_traces
          (id, pipeline_name, step_name, action, passed, score, issues, attempt, output_preview, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (r['id'], r['pipeline_name'], r['step_name'], r['action'],
          r['passed'], r['score'], r['issues'], r['attempt'],
          r.get('output_preview'), r['created_at']))

conn.commit()
count = conn.execute("SELECT COUNT(*) FROM eval_traces").fetchone()[0]
print(f"Imported {count} traces into {dst}")

# Quick stats
rows = conn.execute("""
    SELECT pipeline_name, step_name, SUM(CASE WHEN passed=0 THEN 1 ELSE 0 END) as failed, COUNT(*) as total
    FROM eval_traces GROUP BY pipeline_name, step_name ORDER BY step_name
""").fetchall()
for r in rows:
    print(f"  {r[0]}/{r[1]}: {r[2]}/{r[3]} failures ({r[2]/r[3]*100:.0f}%)")
conn.close()

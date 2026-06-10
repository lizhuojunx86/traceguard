"""Standalone market-intel pipeline failure analyzer.

Usage:
  GUARDIAN_LLM_API_KEY=<key> .venv/bin/python analyze_pipeline.py [step_name]
  Default steps: step_04_leads_report and step_03_market_report
"""
import asyncio
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta

import httpx

DB_PATH = os.path.join(os.path.dirname(__file__), "market_intel.db")
API_KEY = os.environ.get("GUARDIAN_LLM_API_KEY", "")
API_BASE = "https://api.minimaxi.com/v1"
MODEL = "MiniMax-M2.7"
PIPELINE = "market-intelligence"
DAYS = 30  # look back 30 days to capture all demo data


# ── JSON robust parser ──────────────────────────────────────────────────────

def _fix_json(text: str) -> str:
    """Strip think blocks, extract JSON, fix unescaped control chars."""
    # Strip <think>...</think>
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL).strip()
    # Strip code fences
    if "```" in text:
        text = re.sub(r"```(?:json)?", "", text).strip()
    # Find outermost {}
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        text = text[s:e+1]
    # Fix literal newlines/tabs inside JSON strings
    result, in_str, i = [], False, 0
    while i < len(text):
        c = text[i]
        if c == "\\" and in_str:
            result.append(c); i += 1
            if i < len(text): result.append(text[i])
            i += 1; continue
        if c == '"':
            in_str = not in_str; result.append(c)
        elif in_str and c in "\n\r\t":
            result.append({"\\n": "\\n", "\r": "\\r", "\t": "\\t"}[c] if c != "\n" else "\\n")
        else:
            result.append(c)
        i += 1
    return "".join(result).strip()


def parse_llm_json(raw: str) -> dict | None:
    """Try multiple strategies to parse LLM JSON output."""
    candidates = [raw, _fix_json(raw)]
    for attempt in candidates:
        try:
            return json.loads(attempt)
        except json.JSONDecodeError as e:
            col = e.colno - 1
            lo = max(0, col - 60)
            hi = min(len(attempt), col + 60)
            print(f"  [parse debug] JSONDecodeError at char {col}: {e.msg}")
            print(f"  [parse debug] context: {repr(attempt[lo:hi])}")
            print(f"  [parse debug] char at error: {repr(attempt[col]) if col < len(attempt) else 'EOF'}")
        except Exception as e:
            print(f"  [parse debug] unexpected error: {e}")
    return None


# ── Trace loader ────────────────────────────────────────────────────────────

def load_traces(step_name: str) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    since = (datetime.now() - timedelta(days=DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute("""
        SELECT * FROM eval_traces
        WHERE pipeline_name=? AND step_name=? AND created_at>=?
        ORDER BY created_at DESC LIMIT 500
    """, (PIPELINE, step_name, since)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def analyze_failures(traces: list[dict]) -> dict:
    failed = [t for t in traces if not t["passed"]]
    issue_counter: Counter = Counter()
    for t in failed:
        try:
            issues = json.loads(t["issues"]) if t["issues"] else []
        except Exception:
            issues = []
        for iss in issues:
            issue_counter[str(iss)] += 1
    scores = [t["score"] for t in traces]
    return {
        "total": len(traces),
        "failed": len(failed),
        "rate": len(failed) / len(traces) if traces else 0,
        "avg_score": sum(scores) / len(scores) if scores else 0,
        "top_issues": dict(issue_counter.most_common(10)),
        "samples": [t.get("output_preview", "") for t in failed[:3] if t.get("output_preview")],
    }


# ── LLM call ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert AI pipeline quality analyst.
Analyze pipeline failure patterns and respond with ONLY a valid JSON object.
No prose outside the JSON. No markdown. No line breaks inside JSON string values.
Format:
{"root_causes":[{"cause":"...","evidence":"...","severity":"high|medium|low","frequency":"X%"}],"suggestions":[{"title":"...","type":"soul_md|cron_config|retry_hint","proposed":"...","impact":"..."}],"summary":"..."}
"""

def build_prompt(step: str, stats: dict) -> str:
    top = "\n".join(f'  - "{k}" ({v}x)' for k, v in list(stats["top_issues"].items())[:8])
    samples = "\n---\n".join(stats["samples"][:2])
    return f"""Pipeline: market-intelligence / Step: {step}

Statistics:
- Total: {stats['total']}, Failed: {stats['failed']} ({stats['rate']:.1%})
- Avg score: {stats['avg_score']:.3f}

Top issues:
{top}

Sample failed output preview:
{samples[:600] if samples else "(none)"}

Identify root causes and concrete fixes for the SOUL.md agent prompt or cron config."""


async def call_llm(prompt: str) -> str | None:
    if not API_KEY:
        print("ERROR: GUARDIAN_LLM_API_KEY not set", file=sys.stderr)
        return None
    async with httpx.AsyncClient(timeout=90.0) as client:
        try:
            resp = await client.post(
                f"{API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 1500,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"LLM error: {e}", file=sys.stderr)
            return None


# ── Report formatter ─────────────────────────────────────────────────────────

def print_report(step: str, stats: dict, data: dict | None) -> None:
    print(f"\n{'='*65}")
    print(f"  ANALYSIS: {step}")
    print(f"  Failures: {stats['failed']}/{stats['total']} ({stats['rate']:.1%})  avg_score={stats['avg_score']:.3f}")
    print(f"{'='*65}")

    if not data:
        print("  [LLM analysis unavailable — rule-based summary only]")
        print("\n  Top issues:")
        for iss, cnt in list(stats["top_issues"].items())[:5]:
            print(f"    - {iss} ({cnt}x)")
        return

    print(f"\n  Summary: {data.get('summary', '')}")
    print()

    for i, rc in enumerate(data.get("root_causes", []), 1):
        sev = rc.get("severity", "?").upper()
        print(f"  [{sev}] Root Cause {i}: {rc.get('cause', '')}")
        print(f"    Evidence : {rc.get('evidence', '')}")
        print(f"    Frequency: {rc.get('frequency', '')}")
        print()

    for i, sg in enumerate(data.get("suggestions", []), 1):
        print(f"  Suggestion {i} [{sg.get('type', '?')}]: {sg.get('title', '')}")
        print(f"    Proposed : {sg.get('proposed', '')}")
        print(f"    Impact   : {sg.get('impact', '')}")
        print()


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    steps = sys.argv[1:] if len(sys.argv) > 1 else [
        "step_04_leads_report",
        "step_03_market_report",
    ]

    for step in steps:
        print(f"\nLoading traces for {step}...")
        traces = load_traces(step)
        if not traces:
            print(f"  No traces found for {step} in last {DAYS} days")
            continue

        stats = analyze_failures(traces)
        print(f"  {stats['failed']}/{stats['total']} failures ({stats['rate']:.1%})")
        print(f"  Top issues: {', '.join(list(stats['top_issues'].keys())[:3])}")

        print("  Calling MiniMax M2.7 for analysis...")
        raw = await call_llm(build_prompt(step, stats))

        if raw:
            # Save raw to file for inspection
            with open("/tmp/llm_raw_last.txt", "w") as _f:
                _f.write(raw)
            fixed = _fix_json(raw)
            print(f"  [debug] raw len={len(raw)}, fixed len={len(fixed)}, fixed[:80]={repr(fixed[:80])}")
            data = parse_llm_json(raw)
            # Treat empty dict as failure too
            if not data or not isinstance(data, dict) or (not data.get("root_causes") and not data.get("suggestions")):
                print(f"  [Parse failed] data={repr(data)[:200]}")
                print(f"  [Parse failed] Raw (first 300 chars):\n  {repr(raw[:300])}")
            print_report(step, stats, data if (data and isinstance(data, dict)) else None)
        else:
            print_report(step, stats, None)


if __name__ == "__main__":
    asyncio.run(main())

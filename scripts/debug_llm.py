"""Debug: call MiniMax and show what's at the failing JSON position."""
import asyncio, os, httpx, json, re, sys

API_KEY = os.environ.get("GUARDIAN_LLM_API_KEY", "")
API_BASE = "https://api.minimaxi.com/v1"
MODEL = "MiniMax-M2.7"

PROMPT = """\
## Step: step_04_leads_report (Pipeline: market-intelligence)

## Statistics
- Total traces: 46, Failed: 27 (58.7%), Average score: 0.682
- Top Issues: "Report content not confirmed generated" (9x), \
"Report content may not have been generated" (8x), \
"Telegram delivery failed: bot not in channel" (6x), \
"Chinese character encoding issues in PDF" (5x), \
"Step execution timeout" (3x)

Analyze and respond with ONLY valid JSON (no markdown, no explanation outside the JSON):
{"root_causes": [{"cause": "...", "evidence": "...", "severity": "high|medium|low", "frequency": "X%"}], "summary": "..."}
"""

def fix_json_strings(text):
    result, in_string, i = [], False, 0
    while i < len(text):
        c = text[i]
        if c == "\\" and in_string:
            result.append(c); i += 1
            if i < len(text): result.append(text[i])
            i += 1; continue
        if c == '"': in_string = not in_string; result.append(c)
        elif in_string and c == "\n": result.append("\\n")
        elif in_string and c == "\r": result.append("\\r")
        elif in_string and c == "\t": result.append("\\t")
        else: result.append(c)
        i += 1
    return "".join(result)

async def main():
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "messages": [{"role": "user", "content": PROMPT}],
                  "temperature": 0.2, "max_tokens": 1024}
        )
        body = resp.json()
        raw = body["choices"][0]["message"]["content"]

    # Strip think block
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    cleaned = re.sub(r"<think>.*$", "", cleaned, flags=re.DOTALL).strip()

    # Extract JSON
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        print("NO JSON FOUND"); return
    candidate = cleaned[start:end+1]

    print(f"JSON length: {len(candidate)} chars")
    print(f"First 200: {repr(candidate[:200])}")
    print()

    # Try parse without fix
    try:
        json.loads(candidate)
        print("PARSED OK (no fix needed)")
        return
    except json.JSONDecodeError as e:
        print(f"Parse error without fix: {e}")
        col = e.colno - 1
        lo, hi = max(0, col-50), min(len(candidate), col+50)
        print(f"Context around error (chars {lo}-{hi}):")
        print(repr(candidate[lo:hi]))
        print(f"^ error at offset {col} in above: char={repr(candidate[col]) if col < len(candidate) else 'EOF'}")
        print()

    # Try parse with fix
    fixed = fix_json_strings(candidate)
    try:
        data = json.loads(fixed)
        print("PARSED OK after fix_json_strings!")
        print(json.dumps(data, indent=2, ensure_ascii=False)[:800])
    except json.JSONDecodeError as e:
        print(f"STILL FAILS after fix: {e}")
        col = e.colno - 1
        lo, hi = max(0, col-50), min(len(fixed), col+50)
        print(f"Context: {repr(fixed[lo:hi])}")

asyncio.run(main())

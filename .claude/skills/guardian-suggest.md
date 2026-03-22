---
name: guardian-suggest
description: Analyze pipeline step failure history and generate optimization suggestions. Identifies root causes and recommends prompt changes, config adjustments, or threshold tuning. Use when the user wants to improve pipeline quality, reduce failures, or optimize prompts.
---

# Guardian Suggest

Analyze failure patterns and generate actionable optimization suggestions.

## Gather from user

1. Pipeline config path
2. Step name to optimize
3. Database URL with historical traces (must have failure data)
4. (Optional) Lookback days (default: 14)

## Execute

```bash
cd /Users/lizhuojun/Desktop/APP/traceguard
uv run guardian suggest \
  --pipeline <config_path> \
  --step <step_name> \
  --db <db_url> \
  --days <days>
```

Add `--json-output` for structured JSON results.

## Interpret results

The output includes:
- **Failure pattern**: failure rate, top issues with counts
- **Root causes**: identified causes with severity (high/medium/low)
- **Suggestions**: each with Diagnosis, Root Cause Hypothesis, Recommended Change, and Expected Impact

Present each suggestion clearly. Emphasize that these are recommendations requiring human review — nothing is auto-applied. If the suggestions reference "prompt_change", explain what the upstream agent's prompt should be modified to include.

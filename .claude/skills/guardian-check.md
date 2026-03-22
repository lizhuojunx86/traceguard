---
name: guardian-check
description: Run Pipeline Guardian quality checks on a pipeline step output. Validates structure (JSON schema, required fields, length, language) and semantics (LLM-as-Judge). Use when the user wants to check, validate, or evaluate an AI pipeline step output.
---

# Guardian Check

Run quality validation on a pipeline step's output.

## Gather from user

1. Pipeline config path (look in `configs/examples/` if not specified)
2. Step name
3. Step output — either a file path or inline data
4. (Optional) Database URL (default: `sqlite:///traces.db`)

## Execute

If the user provides inline output data, write it to a temp file first:

```bash
echo '<output_data>' > /tmp/guardian_input.json
```

Then run:

```bash
cd /Users/lizhuojun/Desktop/APP/traceguard
uv run guardian check \
  --pipeline <config_path> \
  --step <step_name> \
  --input <output_file_or_/tmp/guardian_input.json> \
  --db sqlite:///traces.db
```

## Interpret results

The JSON output contains:
- **action**: `pass` (all good), `retry` (fixable issues), `abort` (critical failure), `alert` (notified), `passthrough` (logged only)
- **score**: 0.0 to 1.0 quality score
- **issues**: specific problems found (e.g. "Missing required field: data")
- **semantic**: LLM evaluation status ("evaluated", "skipped (no LLM available)")
- **retry_hint**: what the upstream agent should fix (if action=retry)

For `retry` or `abort`, explain each issue and suggest what the upstream agent prompt should change. For `pass`, confirm the output meets all quality criteria.

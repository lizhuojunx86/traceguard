# Pipeline Guardian

**Self-healing quality assurance for multi-agent LLM pipelines.**

Pipeline Guardian inserts lightweight checkpoints between pipeline steps, automatically validating outputs, scoring quality, and triggering corrective actions — turning blind agent chains into self-monitoring systems.

> Architecture details and design rationale: [Pipeline Guardian Specification](docs/Pipeline_Guardian_Specification.html)

---

## Table of Contents

- [Quick Start](#quick-start)
- [Installation](#installation)
- [Core Concepts](#core-concepts)
- [Usage Guide](#usage-guide)
  - [1. Write a Pipeline Config](#1-write-a-pipeline-config)
  - [2. Run a Guardian Check](#2-run-a-guardian-check)
  - [3. Interpret the Result](#3-interpret-the-result)
  - [4. View the Dashboard](#4-view-the-dashboard)
  - [5. Generate Optimization Suggestions](#5-generate-optimization-suggestions)
- [Configuration Reference](#configuration-reference)
  - [Structural Checks](#structural-checks)
  - [Semantic Evaluation](#semantic-evaluation)
  - [Actions](#actions)
- [CLI Reference](#cli-reference)
- [API Reference](#api-reference)
- [Telegram Alerts](#telegram-alerts)
- [Troubleshooting](#troubleshooting)
- [Running Tests](#running-tests)

---

## Quick Start

```bash
# Install
git clone https://github.com/lizhuojunx86/traceguard.git
cd traceguard
uv sync

# Run a check against the example config
echo '{"data":[{"symbol":"AAPL","price":185.5}],"timestamp":"2026-03-21T10:00:00Z","source":"api"}' > /tmp/output.json

uv run guardian check \
  --pipeline configs/examples/market_intel.yaml \
  --step step_01_collect \
  --input /tmp/output.json \
  --db sqlite:///traces.db \
  --serve

# Open the dashboard
open http://127.0.0.1:8000
```

---

## Installation

**Requirements:** Python 3.11+, [uv](https://docs.astral.sh/uv/)

```bash
git clone https://github.com/lizhuojunx86/traceguard.git
cd traceguard
uv sync
```

Verify the installation:

```bash
uv run guardian --help
```

Expected output:

```
Usage: guardian [OPTIONS] COMMAND [ARGS]...

  Pipeline Guardian — self-healing AI workflow system.

Options:
  -v, --verbose  Enable verbose logging.
  --help         Show this message and exit.

Commands:
  check    Run Guardian checks on a step's output.
  serve    Start the Guardian dashboard API server.
  suggest  Generate optimization suggestions for a pipeline step.
```

---

## Core Concepts

```
[Agent_01] → [Guardian_01] → [Agent_02] → [Guardian_02] → [Output]
                  │                            │
                  └──────── eval_store ─────────┘
                                │
                  Dashboard / Alerts / Suggestions
```

| Concept | Description |
|---------|-------------|
| **Pipeline** | A sequence of agent steps defined in a YAML config file |
| **Step** | A single agent execution that produces output (JSON or text) |
| **Guardian** | A checkpoint attached to a step that validates its output |
| **Structural Check** | Deterministic validation: JSON Schema, required fields, length, language |
| **Semantic Check** | LLM-as-Judge evaluation against human-defined criteria (score 1-5) |
| **Action** | What happens when a check fails: `pass`, `retry`, `abort`, `alert`, `passthrough` |
| **Eval Trace** | A record of every check result, stored in SQLite for analysis |
| **Drift Detection** | Automatic comparison of recent quality vs historical baseline |

For a complete architecture walkthrough, see [Pipeline Guardian Specification](docs/Pipeline_Guardian_Specification.html).

---

## Usage Guide

### 1. Write a Pipeline Config

Create a YAML file describing your pipeline steps and their guardian rules. Here is a minimal example:

```yaml
# my_pipeline.yaml
pipeline:
  name: "my-pipeline"
  trigger: "manual"

  steps:
    - name: "step_01_generate"
      container: "generator:latest"
      input_source: "trigger"

      guardian:
        structural:
          required_fields: ["result", "confidence"]
          min_length: 50
          max_length: 10000

        actions:
          on_structural_fail: "retry"
          max_retries: 2
          retry_hint: "Output must be valid JSON with 'result' and 'confidence' fields."
```

A full-featured example is available at `configs/examples/market_intel.yaml`.

### 2. Run a Guardian Check

Suppose your agent produced an output file `output.json`:

```bash
uv run guardian check \
  --pipeline my_pipeline.yaml \
  --step step_01_generate \
  --input output.json \
  --db sqlite:///traces.db
```

**What happens:**
1. Loads the pipeline config and finds `step_01_generate`
2. Loads `output.json` as the step's output
3. Runs all configured structural checks
4. If semantic evaluation is enabled, calls the LLM-as-Judge
5. Decides an action based on results and config
6. Writes an eval trace to the database (if `--db` is provided)
7. Prints the result as JSON to stdout

### 3. Interpret the Result

**Pass — all checks succeeded:**

```json
{
  "pipeline": "my-pipeline",
  "step": "step_01_generate",
  "action": "pass",
  "score": 1.0,
  "issues": [],
  "attempt": 1
}
```

Exit code: `0`

**Retry — checks failed, retries remaining:**

```json
{
  "pipeline": "my-pipeline",
  "step": "step_01_generate",
  "action": "retry",
  "score": 0.6,
  "issues": [
    "Missing required field: confidence"
  ],
  "attempt": 1,
  "retry_hint": "Output must be valid JSON with 'result' and 'confidence' fields."
}
```

Exit code: `0`. Your orchestrator should re-run the agent with the `retry_hint` appended to its prompt, then call `guardian check` again with `--attempt 2`.

**Abort — checks failed, retries exhausted or action is abort:**

```json
{
  "pipeline": "my-pipeline",
  "step": "step_01_generate",
  "action": "abort",
  "score": 0.4,
  "issues": [
    "Missing required field: result",
    "Output too short: 12 chars (minimum: 50)"
  ],
  "attempt": 2
}
```

Exit code: `2`. The pipeline should stop. The issues array tells you exactly what went wrong.

**Action summary:**

| Action | Meaning | Exit Code | Your Next Move |
|--------|---------|-----------|----------------|
| `pass` | All checks passed | 0 | Continue to next step |
| `retry` | Failed, but retries left | 0 | Re-run the agent with `retry_hint` |
| `abort` | Failed, no retries left | 2 | Stop the pipeline, investigate |
| `alert` | Failed, alert sent | 0 | Continue, but review the alert |
| `passthrough` | Failed, logged only | 0 | Continue (used for non-critical steps) |

### 4. View the Dashboard

**Option A — Auto-start with checks:**

```bash
uv run guardian check \
  --pipeline my_pipeline.yaml \
  --step step_01_generate \
  --input output.json \
  --db sqlite:///traces.db \
  --serve
```

The `--serve` flag starts the dashboard on port 8000 if it's not already running.

**Option B — Start independently:**

```bash
uv run guardian serve --db sqlite:///traces.db
```

Then open **http://127.0.0.1:8000** in your browser.

**Dashboard features:**
- Pipeline selector and time range filter
- Summary cards: total traces, pass rate, average score, step count
- Drift detection banner (green = stable, red = degrading)
- Score trend chart (daily averages)
- Action distribution pie chart
- Recent traces table with issues

The dashboard reads from the same SQLite database. Any new pipeline or step appears automatically after its first `guardian check --db` run.

### 5. Generate Optimization Suggestions

After accumulating enough traces (at least a few days), you can ask Guardian to analyze failure patterns and suggest improvements:

```bash
export GUARDIAN_LLM_API_KEY="your-api-key"

uv run guardian suggest \
  --pipeline my_pipeline.yaml \
  --step step_01_generate \
  --db sqlite:///traces.db \
  --days 14
```

**What happens (three-step pipeline):**

1. **Pattern extraction** — Aggregates recent failed traces: issue frequencies, score distribution, sample outputs
2. **Root cause analysis** — Sends the pattern to an LLM to identify underlying causes
3. **Suggestion generation** — Produces actionable changes (retry hints, config tweaks) with rationale

**Example output:**

```
Analyzing traces for my-pipeline/step_01_generate (last 14 days)...
Found 30/100 failures (30.0%). Top issues:
  - Missing required field: confidence (20x)
  - Output too short (10x)

Running root cause analysis...
Identified 2 root cause(s):
  [high] Agent not including confidence score in output
  [medium] Agent sometimes returns partial responses

Generating optimization suggestions...

============================================================
  OPTIMIZATION SUGGESTIONS
  Pipeline: my-pipeline
  Step: step_01_generate
============================================================

Root Cause Summary:
  Agent consistently omits the confidence field.

--- Suggestion 1: More specific retry hint [retry_hint] ---

  Current:
    - Output must be valid JSON with 'result' and 'confidence' fields.
  Proposed:
    + Your response MUST be a JSON object with exactly two keys:
    + "result" (string, your analysis) and "confidence" (number
    + between 0 and 1). Example: {"result": "...", "confidence": 0.85}

  Rationale: Current hint lacks a concrete example.
  Expected Impact: Reduce missing-field failures by ~60%

============================================================
  NOTE: These are suggestions only. Review before applying.
============================================================
```

**Important:** Suggestions are never auto-applied. You review them, decide which to adopt, and manually update your pipeline config.

Add `--json-output` for machine-readable JSON output.

---

## Configuration Reference

### Structural Checks

```yaml
guardian:
  structural:
    schema: "path/to/schema.json"          # JSON Schema file (optional)
    required_fields: ["field1", "field2"]   # Fields that must exist (optional)
    min_length: 100                         # Minimum output length in chars (optional)
    max_length: 50000                       # Maximum output length in chars (optional)
    language: "en"                          # Expected language: en, zh, ja, ko, ru, ar (optional)
```

All checks are optional. Only configured checks are executed.

| Check | Fails When | Common Cause |
|-------|-----------|--------------|
| `schema` | Output doesn't match JSON Schema | Agent returned wrong format |
| `required_fields` | Fields missing from JSON output | Agent omitted keys |
| `min_length` | Output shorter than threshold | Agent returned truncated/empty response |
| `max_length` | Output longer than threshold | Agent stuck in a loop |
| `language` | >50% of text is in unexpected script | Agent responded in wrong language |

### Semantic Evaluation

```yaml
guardian:
  semantic:
    enabled: true
    model: "gpt-4o-mini"                        # Any model name
    api_base: "https://api.openai.com/v1"        # Any OpenAI-compatible endpoint
    api_key_env: "GUARDIAN_LLM_API_KEY"          # Env var holding the API key
    criteria:
      - "Output contains actionable analysis"
      - "Conclusions are supported by data"
    min_score: 3                                  # Minimum acceptable score (1-5)
```

The LLM evaluates the output against each criterion and returns a score from 1 (unacceptable) to 5 (excellent). If the score is below `min_score`, the `on_semantic_low` action triggers.

**Score weighting:** When both structural and semantic checks run, the combined score uses a 40% structural + 60% semantic weighting.

### Actions

```yaml
guardian:
  actions:
    on_structural_fail: "retry"     # What to do when structural checks fail
    on_semantic_low: "alert"        # What to do when semantic score is low
    max_retries: 2                  # How many retries before escalating to abort
    retry_hint: "Fix the output..." # Message appended to agent prompt on retry
    alert_channel: "telegram"       # Where to send alerts (optional)
```

---

## CLI Reference

### `guardian check`

```
guardian check [OPTIONS]

  --pipeline PATH    Pipeline YAML config (required)
  --step TEXT        Step name to check (required)
  --input PATH       Step output file (required)
  --db TEXT          Database URL, e.g. sqlite:///traces.db
  --attempt INT      Attempt number, default 1
  --serve            Auto-start dashboard alongside check
  --serve-port INT   Dashboard port, default 8000
  -v, --verbose      Enable debug logging
```

### `guardian suggest`

```
guardian suggest [OPTIONS]

  --pipeline PATH    Pipeline YAML config (required)
  --step TEXT        Step name to optimize (required)
  --db TEXT          Database URL (required)
  --days INT         Lookback period, default 14
  --model TEXT       LLM model, default gpt-4o-mini
  --api-base TEXT    OpenAI-compatible API base URL
  --json-output      Output JSON instead of formatted text
```

Requires `GUARDIAN_LLM_API_KEY` environment variable.

### `guardian serve`

```
guardian serve [OPTIONS]

  --host TEXT    Bind host, default 127.0.0.1
  --port INT     Bind port, default 8000
  --db TEXT      Database URL, default sqlite:///traces.db
```

---

## API Reference

All endpoints are available when the dashboard server is running.

| Method | Path | Parameters | Description |
|--------|------|------------|-------------|
| GET | `/` | — | Dashboard UI |
| GET | `/api/health` | — | `{"status": "ok"}` |
| GET | `/api/pipelines` | — | List pipelines with trace counts |
| GET | `/api/traces` | `pipeline`, `step`, `days`, `limit` | Query trace history |
| GET | `/api/stats` | `pipeline` (required), `step` (required), `days` | Pass rate, avg score, action breakdown |
| GET | `/api/drift` | `pipeline` (required), `recent_days`, `baseline_days` | Drift detection per step |
| GET | `/docs` | — | Interactive Swagger documentation |

---

## Telegram Alerts

To receive alerts when checks fail:

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather) and note the token
2. Get your chat ID (send a message to the bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates`)
3. Set environment variables:

```bash
export GUARDIAN_TELEGRAM_BOT_TOKEN="123456:ABC-..."
export GUARDIAN_TELEGRAM_CHAT_ID="987654321"
```

4. Set `alert_channel: "telegram"` in your pipeline config

Alerts include the pipeline name, step name, action taken, score, and issue list.

---

## Troubleshooting

### "Step 'X' not found in pipeline 'Y'"

The `--step` value must exactly match a step name in your YAML config. Check spelling and use `--verbose` to see the loaded config.

### "Step 'X' has no guardian configuration"

The step exists but has no `guardian:` block. Add structural or semantic checks to the step config.

### Exit code 2 from `guardian check`

The check resulted in an `abort` action. Check the `issues` array in the JSON output to understand what failed. Common fixes:
- Loosen overly strict thresholds (`min_length`, `max_length`)
- Increase `max_retries`
- Improve the `retry_hint` to give the agent clearer instructions

### "Environment variable 'GUARDIAN_LLM_API_KEY' is not set"

The `suggest` command and semantic evaluation both require an LLM API key. Set it:

```bash
export GUARDIAN_LLM_API_KEY="sk-..."
```

### Dashboard shows no data

Ensure you passed `--db sqlite:///traces.db` to `guardian check` so traces are written. The dashboard reads from the same database — the `GUARDIAN_DB_URL` in `guardian serve` must point to the same file.

### Dashboard port already in use

Another process is using port 8000. Either stop it, or use a different port:

```bash
uv run guardian serve --port 9000
```

### Drift detection shows "Insufficient data"

Drift analysis needs at least 2 days of data spread across different calendar dates. Keep running checks over multiple days to build up enough trace history.

---

## Running Tests

```bash
uv run pytest               # Run all 180 tests
uv run pytest -v             # Verbose output
uv run pytest tests/test_structural.py  # Run a specific test file
```

---

## License

See [LICENSE](LICENSE) for details.

# Pipeline Guardian — Self-Healing AI Workflow System

> For the full process specification and architecture diagrams, see [docs/Pipeline_Guardian_Specification.html](docs/Pipeline_Guardian_Specification.html).

## Project Vision

Pipeline Guardian is a **generic** quality assurance and self-optimization system for multi-agent LLM pipelines. It works with ANY pipeline where multiple AI agents execute sequentially, with each step's output feeding the next step's input.

**Core principle**: Insert lightweight "Guardian" checkpoints between pipeline steps to validate, evaluate, score, and auto-correct outputs — transforming blind pipelines into self-healing systems.

## Architecture Overview

```
[Trigger] → [Agent_01] → [Guardian_01] → [Agent_02] → [Guardian_02] → ... → [Output]
                              ↑                            ↑
                         eval_store                   eval_store
                              ↓                            ↓
                        [Dashboard / Alerts / Optimization Engine]
```

## Key Design Principles

1. **Generic, not specific**: The system must work with ANY multi-agent pipeline, not just specific workflows. Pipeline definitions are loaded from configuration, not hardcoded.
2. **Non-invasive**: Guardian nodes are middleware — they do NOT modify the original agent containers or code. They intercept outputs between steps.
3. **Three-layer validation**: Every Guardian performs: (a) structural checks (schema, format, fields), (b) semantic evaluation (LLM-as-Judge), (c) decision & remediation (pass/retry/abort).
4. **Data-first**: Every check writes to eval_store. This trace data is the fuel for later optimization.
5. **Pluggable evaluators**: Users define per-step evaluation criteria via configuration files (JSON/YAML), not code changes.

## Project Structure

```
pipeline-guardian/
├── CLAUDE.md                        # This file
├── pyproject.toml                   # Python package config (uv)
├── guardian/                        # Main package (root-level, not src/)
│   ├── __init__.py
│   ├── cli.py                       # CLI entry point (check, suggest, serve)
│   ├── core/
│   │   ├── config.py                # Pydantic config models + YAML loader
│   │   ├── step.py                  # StepOutput abstraction
│   │   └── guardian_node.py         # Guardian checkpoint logic (sync + async)
│   ├── validators/
│   │   ├── structural.py            # JSON Schema, fields, length, language checks
│   │   └── semantic.py              # LLM-as-Judge evaluation (OpenAI-compatible)
│   ├── actions/
│   │   └── alert.py                 # Telegram Bot API alerts
│   ├── store/
│   │   ├── models.py                # SQLAlchemy EvalTrace model
│   │   ├── writer.py                # Write eval traces
│   │   └── reader.py                # Query traces, stats, daily aggregations
│   ├── optimizer/
│   │   ├── drift_detector.py        # Quality drift detection (baseline vs recent)
│   │   ├── root_cause.py            # LLM-powered root cause analysis
│   │   └── suggestion.py            # Optimization suggestion engine
│   └── api/
│       ├── server.py                # FastAPI dashboard API
│       └── dashboard.html           # Built-in single-page dashboard UI
├── configs/
│   ├── examples/
│   │   ├── market_intel.yaml        # Example pipeline config
│   │   └── schemas/                 # JSON Schema files for step outputs
│   └── grafana/
│       └── dashboard.json           # Grafana dashboard template
├── scripts/
│   └── seed_demo_data.py            # Generate demo traces for testing
├── tests/                           # pytest test suite (180 tests)
│   ├── test_config.py
│   ├── test_step.py
│   ├── test_structural.py
│   ├── test_semantic.py
│   ├── test_guardian_node.py
│   ├── test_store.py
│   ├── test_reader.py
│   ├── test_alert.py
│   ├── test_drift.py
│   ├── test_root_cause.py
│   ├── test_suggestion.py
│   ├── test_server.py
│   └── test_cli.py
└── docs/
    └── Pipeline_Guardian_Specification.html  # Full process specification
```

## CLI Commands

```bash
# Run guardian check on a step output
guardian check --pipeline config.yaml --step step_01 --input output.json --db sqlite:///traces.db

# Run check with auto-start dashboard
guardian check ... --db sqlite:///traces.db --serve

# Start dashboard API server standalone
guardian serve --db sqlite:///traces.db --port 8000

# Generate optimization suggestions (human-in-the-loop)
guardian suggest --pipeline config.yaml --step step_01 --db sqlite:///traces.db
```

## Pipeline Configuration Format (YAML)

```yaml
pipeline:
  name: "example-pipeline"
  description: "A sample multi-agent pipeline"
  trigger: "cron"  # or "webhook", "manual"

  steps:
    - name: "step_01_collect"
      container: "collector:latest"
      input_source: "trigger"

      guardian:
        structural:
          schema: "schemas/step_01_output.json"
          required_fields: ["data", "timestamp", "source"]
          max_length: 50000
          min_length: 100
          language: "en"

        semantic:
          enabled: true
          model: "gpt-4o-mini"
          api_base: "https://api.openai.com/v1"  # any OpenAI-compatible endpoint
          api_key_env: "GUARDIAN_LLM_API_KEY"
          criteria:
            - "Output contains structured market data"
            - "Data is from the correct date range"
          min_score: 3  # out of 5

        actions:
          on_structural_fail: "retry"   # retry | abort | alert | passthrough
          on_semantic_low: "alert"
          max_retries: 2
          retry_hint: "Ensure output is valid JSON with required fields."
          alert_channel: "telegram"
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Built-in dashboard UI |
| GET | `/api/health` | Health check |
| GET | `/api/pipelines` | List all pipelines with trace metadata |
| GET | `/api/traces?pipeline=X&step=Y&days=7` | Query historical traces |
| GET | `/api/stats?pipeline=X&step=Y` | Aggregated step statistics |
| GET | `/api/drift?pipeline=X` | Drift detection report |
| GET | `/docs` | Swagger API documentation |

## Tech Stack

- **Language**: Python 3.11+
- **Package manager**: uv
- **Database**: SQLite (MVP), TimescaleDB (production)
- **LLM calls**: httpx (OpenAI-compatible endpoints)
- **Config models**: Pydantic v2
- **Schema validation**: jsonschema
- **API**: FastAPI + uvicorn
- **Dashboard**: Single-page HTML + Chart.js
- **Alerts**: Telegram Bot API (via httpx async)
- **CLI**: Click
- **Testing**: pytest + pytest-asyncio (180 tests)

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GUARDIAN_LLM_API_KEY` | API key for LLM calls (semantic eval, root cause, suggestions) |
| `GUARDIAN_TELEGRAM_BOT_TOKEN` | Telegram bot token for alerts |
| `GUARDIAN_TELEGRAM_CHAT_ID` | Telegram chat ID for alerts |
| `GUARDIAN_DB_URL` | Database URL for the API server (default: `sqlite:///traces.db`) |

## Coding Standards

- Type hints everywhere
- Docstrings on all public functions
- Use dataclasses or Pydantic models for data structures
- Async where appropriate (LLM calls, API server)
- All configuration via YAML files, never hardcoded
- Log at INFO level for normal operations, DEBUG for troubleshooting
- Test coverage target: 80%+

## Implementation Status

| Phase | Feature | Status |
|-------|---------|--------|
| 1 | Structural validation, alerting, CLI, eval trace storage | Done |
| 2 | LLM-as-Judge semantic evaluation | Done |
| 3 | Drift detection, FastAPI dashboard, Grafana template | Done |
| 4 | Root cause analysis, optimization suggestions (human-in-the-loop) | Done |
| 5 | Auto-optimization loop | Planned |

## Important Notes

- This is a GENERIC system. Never reference specific pipeline names (market-intel, quant-research) in core code. Those belong only in example configs.
- Guardian nodes must be stateless — all state lives in eval_store.
- The system should work both as a standalone CLI tool AND as Docker middleware.
- Prioritize reliability over features. A guardian that crashes is worse than no guardian.
- The `suggest` command generates recommendations only — it never auto-applies changes.

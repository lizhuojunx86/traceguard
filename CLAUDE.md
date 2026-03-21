# Pipeline Guardian — Self-Healing AI Workflow System

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
├── CLAUDE.md                    # This file
├── README.md
├── pyproject.toml               # Python package config (use uv or poetry)
├── docker-compose.yml           # Guardian system containers
├── src/
│   └── guardian/
│       ├── __init__.py
│       ├── core/
│       │   ├── __init__.py
│       │   ├── pipeline.py      # Pipeline definition & loader
│       │   ├── step.py          # Step abstraction
│       │   ├── guardian_node.py # Guardian checkpoint logic
│       │   └── config.py        # Configuration management
│       ├── validators/
│       │   ├── __init__.py
│       │   ├── structural.py    # JSON schema / format validation
│       │   ├── semantic.py      # LLM-as-Judge evaluation
│       │   └── registry.py      # Validator plugin registry
│       ├── actions/
│       │   ├── __init__.py
│       │   ├── retry.py         # Retry with corrective hints
│       │   ├── alert.py         # Telegram / webhook alerts
│       │   └── passthrough.py   # Log and continue
│       ├── store/
│       │   ├── __init__.py
│       │   ├── models.py        # SQLAlchemy/dataclass models for eval_traces
│       │   ├── writer.py        # Write eval results
│       │   └── reader.py        # Query historical traces
│       ├── optimizer/           # Phase 4: optimization engine (future)
│       │   ├── __init__.py
│       │   ├── drift_detector.py
│       │   ├── root_cause.py
│       │   └── suggestion.py
│       └── api/
│           ├── __init__.py
│           └── server.py        # FastAPI endpoints for dashboard
├── configs/
│   └── examples/
│       ├── market_intel.yaml    # Example: market intelligence pipeline
│       └── quant_research.yaml  # Example: quantitative research pipeline
├── tests/
│   ├── __init__.py
│   ├── test_structural.py
│   ├── test_semantic.py
│   ├── test_guardian_node.py
│   └── test_pipeline.py
└── docs/
    ├── architecture.md
    └── configuration.md
```

## Pipeline Configuration Format (YAML)

```yaml
pipeline:
  name: "example-pipeline"
  description: "A sample multi-agent pipeline"
  trigger: "cron"  # or "webhook", "manual"
  
  steps:
    - name: "step_01_collect"
      container: "collector:latest"  # Docker image
      input_source: "trigger"        # or previous step name
      
      guardian:
        structural:
          schema: "schemas/step_01_output.json"  # JSON Schema file
          required_fields: ["data", "timestamp", "source"]
          max_length: 50000
          language: "en"  # expected language
        
        semantic:
          enabled: true
          model: "minimax-m2.7"  # or any LLM
          criteria:
            - "Output contains structured market data"
            - "Data is from the correct date range"
          min_score: 3  # out of 5
        
        actions:
          on_structural_fail: "retry"  # retry | abort | alert | passthrough
          on_semantic_low: "retry"
          max_retries: 2
          retry_hint: "Ensure output is valid JSON with required fields: {required_fields}"
          alert_channel: "telegram"
    
    - name: "step_02_analyze"
      container: "analyzer:latest"
      input_source: "step_01_collect"
      guardian:
        # ... similar config
```

## Tech Stack

- **Language**: Python 3.11+
- **Package manager**: uv (preferred) or poetry
- **Database**: SQLite for MVP, TimescaleDB for production
- **LLM calls**: httpx (direct API calls to OpenAI-compatible endpoints)
- **Schema validation**: jsonschema or pydantic
- **API**: FastAPI (for dashboard endpoints)
- **Alerts**: Telegram Bot API, webhooks
- **Container orchestration**: Docker Compose
- **Testing**: pytest

## Coding Standards

- Type hints everywhere
- Docstrings on all public functions
- Use dataclasses or Pydantic models for data structures
- Async where appropriate (LLM calls, API server)
- All configuration via YAML files, never hardcoded
- Log at INFO level for normal operations, DEBUG for troubleshooting
- Test coverage target: 80%+

## Current Phase: Phase 1 (MVP)

Focus: Structural validation + alerting for any configured pipeline.

**In scope:**
- Pipeline config loader (YAML → Python objects)
- Guardian node with structural validation (JSON schema, field checks, length, language)
- Eval trace storage (SQLite)
- Telegram alert on failure
- Auto-retry with corrective hints
- CLI to run guardian checks on a pipeline step's output

**Out of scope (future phases):**
- LLM-as-Judge semantic evaluation (Phase 2)
- Dashboard / Grafana integration (Phase 3)
- Drift detection and optimization suggestions (Phase 4)
- Auto-optimization loop (Phase 5)

## Important Notes

- This is a GENERIC system. Never reference specific pipeline names (market-intel, quant-research) in core code. Those belong only in example configs.
- Guardian nodes must be stateless — all state lives in eval_store.
- The system should work both as a standalone CLI tool AND as Docker middleware.
- Prioritize reliability over features. A guardian that crashes is worse than no guardian.

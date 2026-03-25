# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Pipeline Guardian is a **generic** quality assurance system for multi-agent LLM pipelines. It inserts stateless "Guardian" checkpoints between pipeline steps to validate, evaluate (LLM-as-Judge), score, and auto-correct outputs. All state lives in eval_store (SQLite/TimescaleDB).

## Common Commands

```bash
# Install dependencies
uv sync

# Run all tests (222 tests, ~0.5s)
uv run pytest

# Run a single test file
uv run pytest tests/test_structural.py

# Run a single test by name
uv run pytest tests/test_structural.py -k "test_required_fields"

# Verbose output
uv run pytest -v

# MCP tests require optional dep: uv sync --extra mcp
uv run pytest tests/test_mcp_server.py

# Run the CLI
uv run guardian --help
uv run guardian check --pipeline configs/examples/market_intel.yaml --step step_01_collect --input /tmp/output.json --db sqlite:///traces.db
uv run guardian suggest --pipeline configs/examples/market_intel.yaml --step step_01_collect --db sqlite:///traces.db
uv run guardian serve --db sqlite:///traces.db

# Seed demo data for dashboard testing
uv run python scripts/seed_demo_data.py
```

## Architecture

```
[Trigger] → [Agent] → [Guardian Node] → [Agent] → [Guardian Node] → [Output]
                            │                           │
                       eval_store                  eval_store
                            │                           │
                    [Dashboard / Alerts / Optimizer]
```

**Data flow through a Guardian check:**
1. `cli.py` loads YAML config via `core/config.py` (Pydantic models) and step output via `core/step.py`
2. `core/guardian_node.py` orchestrates: runs structural → (if pass) runs semantic → decides action
3. `validators/structural.py` — deterministic checks (schema, fields, length, language)
4. `validators/semantic.py` — async LLM-as-Judge via httpx to any OpenAI-compatible endpoint
5. `env.py` — probes runtime environment, discovers LLM endpoints (external API → local Ollama/LM Studio → DEGRADED mode), caches as process singleton
6. `store/writer.py` + `store/reader.py` — SQLAlchemy-based eval trace persistence
7. `optimizer/` — drift detection, LLM root cause analysis, suggestion generation (human-in-the-loop only, never auto-applies)
8. `api/server.py` — FastAPI dashboard with single-page HTML UI (`dashboard.html`)

**Score computation:** structural (0-1) weighted 40% + semantic (1-5 normalized to 0-1) weighted 60% when both present.

**Action resolution:** configured action (pass/retry/abort/alert/passthrough); retry escalates to abort when `attempt >= max_retries`.

**Graceful degradation:** `env.py` probes in order: external API (if key set and not sandboxed) → local LLM endpoints → DEGRADED mode (structural-only). `GUARDIAN_FORCE_EXTERNAL=1` bypasses sandbox detection.

## Key Design Constraints

- **Generic system** — never reference specific pipeline names (market-intel, quant-research) in core `guardian/` code. Those belong only in `configs/examples/`.
- **Guardian nodes are stateless** — all state lives in eval_store.
- **`suggest` is advisory only** — generates recommendations, never auto-applies changes.
- **Reliability over features** — a guardian that crashes is worse than no guardian.
- **All LLM calls use httpx** against OpenAI-compatible endpoints (not the openai SDK).

## Environment Variables

| Variable | Used By |
|----------|---------|
| `GUARDIAN_LLM_API_KEY` | Semantic eval, root cause, suggestions |
| `GUARDIAN_TELEGRAM_BOT_TOKEN` / `GUARDIAN_TELEGRAM_CHAT_ID` | Alert channel |
| `GUARDIAN_DB_URL` | API server default DB |
| `GUARDIAN_FORCE_EXTERNAL` | Set to `1` to bypass sandbox detection in env.py |

## MCP Server

Optional dependency (`uv sync --extra mcp`). Entry points: `guardian mcp` CLI command or `guardian-mcp` script. Exposes guardian tools via stdio transport for Claude Desktop/Cursor/VS Code.

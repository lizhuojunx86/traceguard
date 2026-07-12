# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repo hosts **two independent Python packages** (no shared imports, separate releases):

1. **`traceguard`** (`packages/traceguard/`) — the actively developed SDK: point-in-time correct LLM instrumentation (tracer, model/prompt registries, input normalizer, look-ahead-bias invariant validators). Contract: `TRACEGUARD_SPEC.md` (Chinese, authoritative) / `docs/SPEC.md` (English). Roadmap: `TRACEGUARD_ROADMAP.md`. **All new features land here.**
2. **`pipeline-guardian`** (import name `guardian`, repo root) — the original checkpoint QA system for multi-agent pipelines (structural checks, LLM-as-Judge, actions, dashboard). **Frozen: bugfixes only.** Its 4-symbol public API (`evaluate_async`, `StepOutput`, `GuardianConfig`, `GuardianDecision`) is pinned by downstream `huadian` (tag `v0.1.0-huadian-baseline`) — breaking it breaks their contract tests. Docs: `docs/pipeline-guardian.md`.

Downstream consumers pin git tags: huadian → `v0.1.0-huadian-baseline` (guardian), quant_alpha_v2 semdiff/chaingraph → `v0.2.0-phase0` (traceguard SDK). Both repos lock specific SHAs — never rewrite published tags.

## Common Commands

```bash
# ── traceguard SDK (packages/traceguard) ──
cd packages/traceguard
uv sync
uv run pytest                    # 181 tests (4 skip without contamination-hf extra)
uv run python ../../examples/quickstart/run_quickstart.py

# ── pipeline-guardian (repo root) ──
# Install dependencies
uv sync

# Run all tests (246 tests, ~1s)
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

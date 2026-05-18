# traceguard (Phase 0 MVP)

New SDK package — see `../../TRACEGUARD_SPEC.md` for the binding contract and
`../../TRACEGUARD_ROADMAP.md` for the Phase 0 scope.

Independent from the existing `guardian/` package at the repo root; no shared
imports. Existing huadian baseline (`pipeline-guardian v0.1.0-huadian-baseline`)
is not affected by this package.

## Install (dev)

```bash
cd packages/traceguard
uv sync
uv run pytest
```

## Install (downstream consumer)

```toml
[project]
dependencies = [
    "traceguard @ git+https://github.com/<owner>/traceguard.git@<tag>#subdirectory=packages/traceguard",
]
```

## What's in Phase 0

- `traceguard.sdk.normalizer` — canonical input hashing (SPEC §4.4)
- `traceguard.sdk.tracer` — @trace decorator + tracer.span() context manager (SPEC §4.1)
- `traceguard.sdk.wrappers.anthropic` — wrap_anthropic
- `traceguard.store.models` — traces + model_registry ORM (SPEC §3.1, §3.2)
- `traceguard.registry.models` — select_model (SPEC §4.2)
- `traceguard.registry.prompts` — load_prompt from YAML (SPEC §4.3, §3.3 backend = filesystem)
- `traceguard.validators.lookahead` — invariant validators (SPEC §4.5)

Not in Phase 0: drift checks, replay framework, CLI, Postgres/TimescaleDB,
OpenAI/Voyage wrappers. See ROADMAP §3.2 for the full deferred list.

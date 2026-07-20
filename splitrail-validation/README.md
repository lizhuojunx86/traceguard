# splitrail 3.5.9 vs 3.6.0 validation (issue #200 / PR #204)

Regression fixture validating splitrail's Claude Code rewrite-retention fix
against this repo's append-only `routing_audit` ground truth.

## Run

```bash
# 0. refresh ground truth first (full scan, no --since):
cd ../packages/traceguard
uv run python -m traceguard.routing_audit.ingest --write \
  --db "sqlite:///$PWD/../../traces_routing_audit.db"

# 1. run the A/B (downloads both release binaries, ~10 min on a large corpus):
cd ../../splitrail-validation
./run_ab_test.sh
```

Read `out/REPORT.md` (assertions + ground-truth tables) and
`out/REPLY_DRAFT.md` (GitHub comment draft).

## What it proves

| Phase | Claim |
|---|---|
| P1 | Cold start: 3.5.9 == 3.6.0 on identical frozen input ("3.6.0 higher" only materializes *across* drift events) |
| P2-P3 | After a simulated resume/compact rewrite: 3.5.9 drops by exactly the removed usage; 3.6.0 retains via its history store |
| P4 | 3.6.0 totals are byte-stable across restarts |
| P5 | Residual TG−splitrail gap decomposes into `deleted_file` (3.6.0 prunes these by design) + `vanished` pre-3.6.0 rewrites |

## Safety

- The real `~/.claude/projects` is never modified — the rewrite simulation runs
  on an APFS-cloned snapshot under `work/home/`.
- Every splitrail invocation runs with `HOME=work/home`: isolated config &
  history store, no `~/.splitrail.toml`, no cloud upload possible.
- The ground-truth DB is opened read-only (`mode=ro`).
- `work/` and `out/` are gitignored (they contain transcript copies / stats).

Protocol pre-validated end-to-end on a synthetic corpus (Linux arm64,
2026-07-20): all four assertions passed, with 3.5.9 dropping exactly the
removed 3 messages / 624 output tokens and 3.6.0 retaining totals.

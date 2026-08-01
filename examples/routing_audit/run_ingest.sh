#!/bin/bash
# Scheduled routing_audit ingest — driven by launchd (see the .plist alongside).
# Incremental by default (--since 2d): a 2-day mtime window re-scans anything
# resume/compact rewrote; the ingest-log dedupes. Run a full scan by hand
# periodically as the correctness backstop.
#
# Placeholders __REPO_DIR__ and __UV_BIN__ are filled in by the install steps
# in README.md — this file is committed as a template, never with real paths.
set -euo pipefail

REPO="__REPO_DIR__"
UV="__UV_BIN__"

cd "$REPO/packages/traceguard"

# Self-heal: Python 3.12's site module silently skips .pth files that carry
# the macOS hidden flag, which orphans the editable install and kills the
# scheduled run with ModuleNotFoundError (observed 2026-08-01 after a venv
# rebuild under an iCloud-synced tree). Clearing the flag is a no-op when
# it is already clear.
if command -v chflags >/dev/null 2>&1; then
  chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null || true
fi

exec "$UV" run python -m traceguard.routing_audit.ingest \
  --write \
  --since 2d \
  --db "sqlite:///$REPO/traces_routing_audit.db" \
  --log-file "$REPO/routing_audit_ingest.log" \
  --usage-report-history

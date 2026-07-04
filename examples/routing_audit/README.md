# Scheduled routing_audit ingest (macOS / launchd)

Runs `python -m traceguard.routing_audit.ingest --write` once a day so the
local Claude Code session history keeps flowing into `traces_routing_audit.db`.
The source is mutable (resume/compact rewrites session files), so a daily
incremental catch is the difference between a complete audit trail and a
lossy one.

Everything written stays local: the DB and the run log are gitignored, and
these two files are committed as **templates** with `__REPO_DIR__` /
`__UV_BIN__` placeholders — you fill them in at install time.

## What it does each run

- `--since 2d` — only re-scan files whose mtime is within the last 2 days
  (a full scan of ~1,600 files is unnecessary daily; the 2-day window covers
  same-day activity plus anything resume/compact rewrote). The ingest-log's
  unique `source_message_id` guarantees no duplicate traces.
- `--write --db sqlite:///$REPO/traces_routing_audit.db` — append new traces.
- `--log-file $REPO/routing_audit_ingest.log` — append one JSON line per run
  (`ts`, `written`, `new_cost_usd`, `already_ingested`, `error`, …).

Run a **full scan by hand** every so often as the correctness backstop:

```bash
cd packages/traceguard
uv run python -m traceguard.routing_audit.ingest --write \
  --db sqlite:///"$PWD/../../traces_routing_audit.db"
```

## Install

```bash
# From the repo root:
REPO="$(pwd)"
UV="$(command -v uv)"

# 1. Fill in the templates (writes copies into ~/Library/LaunchAgents and the
#    script stays in the repo but with real paths — it is gitignored via *.sh?
#    No: run_ingest.sh is committed as a template, so edit a LOCAL copy).
mkdir -p ~/.local/bin
sed -e "s|__REPO_DIR__|$REPO|g" -e "s|__UV_BIN__|$UV|g" \
  examples/routing_audit/run_ingest.sh > ~/.local/bin/traceguard_routing_ingest.sh
chmod +x ~/.local/bin/traceguard_routing_ingest.sh

# 2. Point the plist at the local script and install it.
sed -e "s|__REPO_DIR__/examples/routing_audit/run_ingest.sh|$HOME/.local/bin/traceguard_routing_ingest.sh|g" \
    -e "s|__REPO_DIR__|$REPO|g" \
  examples/routing_audit/com.traceguard.routing-audit.ingest.plist \
  > ~/Library/LaunchAgents/com.traceguard.routing-audit.ingest.plist

# 3. Load it (launchctl bootstrap is the modern form; `load` also works).
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.traceguard.routing-audit.ingest.plist

# Run once now to verify (optional):
launchctl kickstart -p gui/$(id -u)/com.traceguard.routing-audit.ingest
```

## Verify / inspect

```bash
launchctl print gui/$(id -u)/com.traceguard.routing-audit.ingest | grep -A2 'state ='
tail -n 5 routing_audit_ingest.log            # structured run trail
tail -n 20 routing_audit_launchd.err.log      # launchd stderr if a run failed
```

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.traceguard.routing-audit.ingest
rm ~/Library/LaunchAgents/com.traceguard.routing-audit.ingest.plist
rm ~/.local/bin/traceguard_routing_ingest.sh
```

## Notes

- launchd, not cron: it catches up at the next wake if the Mac was asleep at
  03:17, and it survives reboots without a crontab.
- The job needs your login session (the session files live under
  `~/.claude/projects`), so it's a **LaunchAgent** (per-user), not a
  system-wide LaunchDaemon.
- If `uv` isn't on launchd's minimal PATH, the wrapper calls it by absolute
  path (`__UV_BIN__`), so no PATH surgery is needed.

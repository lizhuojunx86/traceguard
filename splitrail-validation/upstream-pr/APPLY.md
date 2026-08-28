# How to turn this staging dir into the upstream PR

Mike invited the fixture PR in #200. Steps (run on your Mac):

```bash
# 1. Fork Piebald-AI/splitrail on GitHub (once, via web UI), then:
cd ~/dev   # or wherever
git clone https://github.com/lizhuojunx86/splitrail.git splitrail-fork
cd splitrail-fork
git remote add upstream https://github.com/Piebald-AI/splitrail.git
git fetch upstream && git checkout -b test/rewrite-retention-check upstream/main

# 2. Copy the staged files in:
cp -R /Users/lizhuojun/dev/traceguard/splitrail-validation/upstream-pr/scripts/rewrite-retention-check scripts/
chmod +x scripts/rewrite-retention-check/run_check.sh

# 3. Sanity-run. No Rust toolchain needed — reuse the 3.6.0 release binary
#    already downloaded by the earlier A/B run (equivalent to a local build):
SPLITRAIL_BIN=/Users/lizhuojun/dev/traceguard/splitrail-validation/work/bin/3.6.0/splitrail \
SPLITRAIL_BASELINE_BIN=/Users/lizhuojun/dev/traceguard/splitrail-validation/work/bin/3.5.9/splitrail \
  scripts/rewrite-retention-check/run_check.sh
# expect: PASS R1..R4, RESULT: ALL PASS
# (with cargo installed you'd instead do: cargo build --release && scripts/rewrite-retention-check/run_check.sh)

# 4. Commit + push + open PR:
git add scripts/rewrite-retention-check
git commit -m "test: add end-to-end rewrite-retention regression check (#200)"
git push -u origin test/rewrite-retention-check
# open PR at: https://github.com/Piebald-AI/splitrail/compare/main...lizhuojunx86:test/rewrite-retention-check
```

## Suggested PR title

```
test: add end-to-end rewrite-retention regression check (#200)
```

## Suggested PR body

As offered in #200 — a self-contained e2e guard for the #204 fix.

**What it does:** generates a synthetic Claude Code corpus (streaming-duplicate
lines, skip-cases) under an isolated `$HOME`, scans it with the built binary,
simulates a resume/compact rewrite (drops the last N assistant
message-groups), rescans, and asserts:

- R1 parse+dedup sanity (scan == generator manifest)
- R2 rewrite retention (post-rewrite scan == pre-rewrite scan) — **the #200 regression**
- R3 restart stability (3 scans byte-identical)
- R4 (optional, `SPLITRAIL_BASELINE_BIN=…3.5.9`) demonstrates the pre-fix drift

**Verified:** passes on 3.6.0; R2 fails on 3.5.9 as expected (that's the bug).
On my real ~50-day corpus the same protocol passed all four assertions
(details in #200). Touches nothing outside `scripts/rewrite-retention-check/`;
no real `~/.claude` data, no config, no upload path. Unix + python3 stdlib only.

Happy to adjust layout/conventions or wire it into CI if you want it there.

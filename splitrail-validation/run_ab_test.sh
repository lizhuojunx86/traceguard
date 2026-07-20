#!/usr/bin/env bash
# splitrail 3.5.9 vs 3.6.0 A/B regression — Claude Code JSONL rewrite retention.
#
# Validates the #204 fix (Piebald-AI/splitrail issue #200) against a frozen
# snapshot of the local ~/.claude/projects corpus, with an append-only
# traceguard routing_audit DB as ground truth.
#
# Phases
#   P1 cold-start parity   : fresh 3.5.9 == fresh 3.6.0 on identical frozen input
#   P2 simulated rewrite   : drop last N assistant message-groups from the
#                            largest main transcript IN THE SNAPSHOT COPY
#   P3 drift response      : 3.5.9 must drop by exactly the removed usage;
#                            3.6.0 must retain (history store)
#   P4 restart stability   : 3.6.0 output byte-identical across repeated runs
#   P5 report              : compare_totals.py -> out/REPORT.md + REPLY_DRAFT.md
#
# Safety
#   - Never touches the real ~/.claude/projects (snapshot via APFS clone).
#   - Runs every splitrail invocation with HOME=$FAKEHOME -> isolated config,
#     isolated 3.6.0 history store, no ~/.splitrail.toml, no cloud upload.
#   - Writes only inside $WORK and $OUT (gitignored).
#
# Usage:  ./run_ab_test.sh            # full run
#   env:  V_OLD=3.5.9 V_NEW=3.6.0 DROP_N=5 WORK=... SRC=...
#         SPLITRAIL_OLD_BIN / SPLITRAIL_NEW_BIN  (skip download, use given binaries)

set -euo pipefail

V_OLD="${V_OLD:-3.5.9}"
V_NEW="${V_NEW:-3.6.0}"
DROP_N="${DROP_N:-5}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${WORK:-$SCRIPT_DIR/work}"
OUT="${OUT:-$SCRIPT_DIR/out}"
SRC="${SRC:-$HOME/.claude/projects}"
FAKEHOME="$WORK/home"
SNAP="$FAKEHOME/.claude/projects"

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ -d "$SRC" ] || die "source tree not found: $SRC"
mkdir -p "$WORK" "$OUT"

# ---------- binaries ----------------------------------------------------------
fetch_bin() { # $1=version -> echoes binary path
  local v="$1" plat arch tarball dir bin
  case "$(uname -s)" in
    Darwin) plat="apple-darwin" ;;
    Linux)  plat="unknown-linux-gnu" ;;
    *) die "unsupported OS $(uname -s)" ;;
  esac
  case "$(uname -m)" in
    arm64|aarch64) arch="aarch64" ;;
    x86_64)        arch="x86_64" ;;
    *) die "unsupported arch $(uname -m)" ;;
  esac
  dir="$WORK/bin/$v"
  bin="$dir/splitrail"
  if [ ! -x "$bin" ]; then
    mkdir -p "$dir"
    tarball="$WORK/bin/splitrail-v$v-$arch-$plat.tar.gz"
    log "downloading splitrail v$v ($arch-$plat)" >&2
    curl -fSL --retry 3 -o "$tarball" \
      "https://github.com/Piebald-AI/splitrail/releases/download/v$v/splitrail-v$v-$arch-$plat.tar.gz" >&2
    tar xzf "$tarball" -C "$dir" --strip-components=1
    chmod +x "$bin"
  fi
  echo "$bin"
}

BIN_OLD="${SPLITRAIL_OLD_BIN:-$(fetch_bin "$V_OLD")}"
BIN_NEW="${SPLITRAIL_NEW_BIN:-$(fetch_bin "$V_NEW")}"
log "old: $("$BIN_OLD" --version)   new: $("$BIN_NEW" --version)"

# ---------- P0: frozen snapshot ------------------------------------------------
log "P0 snapshot $SRC -> $SNAP"
rm -rf "$FAKEHOME"
mkdir -p "$(dirname "$SNAP")"
# APFS copy-on-write clone when available (instant, ~zero extra disk); else rsync
if ! cp -cR "$SRC" "$SNAP" 2>/dev/null; then
  command -v rsync >/dev/null && rsync -a "$SRC/" "$SNAP/" || cp -R "$SRC" "$SNAP"
fi
log "snapshot: $(find "$SNAP" -name '*.jsonl' | wc -l | tr -d ' ') jsonl files"

run_stats() { # $1=bin $2=outfile
  HOME="$FAKEHOME" "$1" stats --pretty > "$2" 2>"$2.err" || die "stats failed, see $2.err"
}

# ---------- P1: cold-start parity ----------------------------------------------
log "P1 baseline scans (order: new first, so its history store predates the rewrite)"
run_stats "$BIN_NEW" "$OUT/base-new.json"
run_stats "$BIN_OLD" "$OUT/base-old.json"

# ---------- P2: simulated resume/compact rewrite -------------------------------
log "P2 rewrite: dropping last $DROP_N assistant message-groups (snapshot only)"
python3 "$SCRIPT_DIR/simulate_rewrite.py" "$SNAP" \
  --drop "$DROP_N" --manifest "$OUT/drift-manifest.json"

# ---------- P3: post-drift scans ------------------------------------------------
log "P3 post-drift scans"
run_stats "$BIN_OLD" "$OUT/post-old.json"
run_stats "$BIN_NEW" "$OUT/post-new.json"

# ---------- P4: restart stability ------------------------------------------------
log "P4 restart stability (3x $V_NEW)"
for i in 1 2 3; do run_stats "$BIN_NEW" "$OUT/stab-new-$i.json"; done

# ---------- P5: compare + report -------------------------------------------------
log "P5 compare + report"
python3 "$SCRIPT_DIR/compare_totals.py" \
  --out-dir "$OUT" \
  --v-old "$V_OLD" --v-new "$V_NEW" \
  --db "$SCRIPT_DIR/../traces_routing_audit.db" \
  --live-tree "$SRC" \
  --emit-reply

log "done. Read: $OUT/REPORT.md  and  $OUT/REPLY_DRAFT.md"

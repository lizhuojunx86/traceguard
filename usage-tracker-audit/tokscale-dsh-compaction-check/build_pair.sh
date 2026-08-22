#!/usr/bin/env bash
# Build the pre-fix and post-fix tokscale binaries for the #1162 A/B.
# Pre-fix  = 86126c2^  (the main commit PR #1162 branched from)
# Post-fix = 522027d   (PR #1162 head)
set -uo pipefail

REPO=/tmp/ts1162
OUT=/tmp/ab1162
mkdir -p "$OUT"
: > "$OUT/build.status"

cd "$REPO" || exit 1

echo "=== baseline 86126c2^ ===" >> "$OUT/build.log"
git checkout -q 86126c2^ || { echo "FAIL checkout base" >> "$OUT/build.status"; exit 1; }
git log --oneline -1 >> "$OUT/build.log"
cargo build --release -p tokscale-cli >> "$OUT/buildA.log" 2>&1 || { echo "FAIL buildA" >> "$OUT/build.status"; exit 1; }
cp target/release/tokscale "$OUT/tokscale-pre" || exit 1
echo "PRE_OK" >> "$OUT/build.status"

echo "=== pr head 522027d ===" >> "$OUT/build.log"
git checkout -q pr1162 || { echo "FAIL checkout pr" >> "$OUT/build.status"; exit 1; }
git log --oneline -1 >> "$OUT/build.log"
cargo build --release -p tokscale-cli >> "$OUT/buildB.log" 2>&1 || { echo "FAIL buildB" >> "$OUT/build.status"; exit 1; }
cp target/release/tokscale "$OUT/tokscale-post" || exit 1
echo "POST_OK" >> "$OUT/build.status"

"$OUT/tokscale-post" --help > "$OUT/help.txt" 2>&1
echo "DONE" >> "$OUT/build.status"

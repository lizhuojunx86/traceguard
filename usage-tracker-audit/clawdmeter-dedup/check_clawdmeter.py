#!/usr/bin/env python3
"""Run Clawdmeter's own transcript token paths over a synthetic corpus with a
known-exact manifest, and print measured vs ground truth.

Nothing here reimplements the tool. It imports `src/transcript.py` from a clone
of weltern/Clawdmeter and calls its functions directly. PySide6 is stubbed so
the module imports without a GUI toolkit; the token paths are stdlib-only.

Setup:
    git clone --depth 1 https://github.com/weltern/Clawdmeter.git /tmp/Clawdmeter
    python3 ../cct-dedup-check/gen_corpus.py --home /tmp/clawdcheck/fakehome --seed 20260805
    python3 check_clawdmeter.py --clawdmeter /tmp/Clawdmeter --home /tmp/clawdcheck/fakehome

Touches no real ~/.claude data.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import types
from pathlib import Path


def _install_pyside_stub() -> None:
    """Minimal PySide6.QtCore so transcript.py imports without Qt."""
    pkg = types.ModuleType("PySide6")
    qtcore = types.ModuleType("PySide6.QtCore")

    class QObject:
        def __init__(self, *a, **k):
            pass

    class QTimer:
        def __init__(self, *a, **k):
            pass

    def Signal(*a, **k):
        class _S:
            def connect(self, *a, **k):
                pass

            def emit(self, *a, **k):
                pass

        return _S()

    qtcore.QObject = QObject
    qtcore.QTimer = QTimer
    qtcore.Signal = Signal
    pkg.QtCore = qtcore
    sys.modules["PySide6"] = pkg
    sys.modules["PySide6.QtCore"] = qtcore


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clawdmeter", required=True, help="path to a Clawdmeter clone")
    ap.add_argument("--home", required=True, help="synthetic $HOME from gen_corpus.py")
    args = ap.parse_args()

    _install_pyside_stub()
    sys.path.insert(0, str(Path(args.clawdmeter) / "src"))
    import transcript as T  # noqa: E402

    home = Path(args.home)
    root = home / ".claude" / "projects"
    man = json.loads((home / "manifest.json").read_text())

    truth_all = man["all"]["correct"]["total_input_plus_output"]
    truth_main = man["main"]["correct"]["total_input_plus_output"]
    msgs = man["all"]["distinct_assistant_messages"]
    lines = man["all"]["assistant_jsonl_lines_with_usage"]

    print(f"corpus: {msgs} distinct assistant messages across {lines} records with usage\n")

    # 1. _file_token_events / account_window_tokens
    #    -> the token counts shown beside the 5h and 7d bars
    events = []
    for fp in root.rglob("*.jsonl"):
        events += T._file_token_events(fp)
    per_line = sum(w for _, w in events)
    print("1. _file_token_events (window token counts)")
    print(f"   events emitted        : {len(events):,}   (distinct messages: {msgs})")
    print(f"   reported input+output : {per_line:,}")
    print(f"   ground truth          : {truth_all:,}")
    print(f"   ratio                 : {per_line / truth_all:.3f}x\n")

    # 2. scan_events rows -> the Stats page (API value, value by model/project)
    rows, _acts, _files = T.scan_events(0.0, root=root)
    stats = sum(r[3] + r[4] for r in rows)
    print("2. scan_events rows (Stats page pricing)")
    print(f"   rows emitted          : {len(rows):,}   (distinct messages: {msgs})")
    print(f"   reported input+output : {stats:,}")
    print(f"   ground truth          : {truth_all:,}")
    print(f"   ratio                 : {stats / truth_all:.3f}x\n")

    # 3. _SessionTail.tokens.work -> the per-session total on the shelf
    mains = [p for p in root.rglob("*.jsonl") if "subagents" not in str(p)]
    tail = 0
    for p in mains:
        t = T._SessionTail(p)
        t.poll(time.time())
        tail += t.tokens.work
    print("3. _SessionTail per-session totals (main transcripts only)")
    print(f"   reported input+output : {tail:,}")
    print(f"   ground truth          : {truth_main:,}")
    print(f"   ratio                 : {tail / truth_main:.3f}x\n")

    ok = (
        per_line == truth_all
        and stats == truth_all
        and tail == truth_main
        and len(events) == msgs
        and len(rows) == msgs
    )
    print("RESULT:", "totals match the manifest" if ok else "totals do not match the manifest")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

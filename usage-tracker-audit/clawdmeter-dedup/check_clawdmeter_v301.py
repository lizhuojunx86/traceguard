#!/usr/bin/env python3
"""check_clawdmeter.py adapted to the v3.0.1 API.

v3.0.1 moved the duplicate collapse out of the per-file parser and into the
account-wide aggregator (#21): `_file_token_events` now returns uncollapsed
``(ts, work, record_key, message_key)`` events, and `account_window_tokens`
folds them per message under a per-bucket max across the whole scan. So site 1
is asserted through the public aggregator (with `now` chosen so the 7d window
covers the entire corpus) instead of summing raw per-file events. Sites 2 and 3
are unchanged from check_clawdmeter.py.

Verified 2026-08-09: red on 7dd0b7b via check_clawdmeter.py
(2.338x / 2.338x / 2.373x, exit 1); green on v3.0.1 via this script —
exact on all three sites, 540 rows for 540 distinct messages, exit 0.

Usage:
    python3 check_clawdmeter_v301.py <clawdmeter-clone> <fake-home>
"""

from __future__ import annotations

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
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    claw, home = sys.argv[1], Path(sys.argv[2])

    _install_pyside_stub()
    sys.path.insert(0, str(Path(claw) / "src"))
    import transcript as T  # noqa: E402

    root = home / ".claude" / "projects"
    man = json.loads((home / "manifest.json").read_text())
    truth_all = man["all"]["correct"]["total_input_plus_output"]
    truth_main = man["main"]["correct"]["total_input_plus_output"]
    msgs = man["all"]["distinct_assistant_messages"]

    # Corpus max timestamp -> a `now` that puts the whole corpus inside the
    # 7d window (the synthetic corpus spans ~2 days).
    mx = 0.0
    for fp in root.rglob("*.jsonl"):
        for line in fp.open(encoding="utf-8", errors="replace"):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = T.parse_iso_ts(ev.get("timestamp"))
            if ts:
                mx = max(mx, ts)
    now = mx + 60

    # 1. account_window_tokens — the aggregator the fix introduced
    _w5, w7 = T.account_window_tokens(now, root=root)
    print("1. account_window_tokens (7d window = whole corpus)")
    print(f"   reported input+output : {w7:,}")
    print(f"   ground truth          : {truth_all:,}")
    print(f"   ratio                 : {w7 / truth_all:.3f}x\n")

    # 2. scan_events rows -> the Stats page (unchanged tuple shape)
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

    ok = w7 == truth_all and stats == truth_all and tail == truth_main and len(rows) == msgs
    print("RESULT:", "totals match the manifest" if ok else "totals do not match the manifest")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

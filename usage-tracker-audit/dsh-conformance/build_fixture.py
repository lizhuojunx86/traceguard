#!/usr/bin/env python3
"""Emit the conformance fixture and its hand-computed expectations.

The fixture is committed, so this script exists to make it auditable rather
than to be run before every check. Re-running it must be a no-op; CI asserts
that.

What the fixture carries, and why each part is there:

  D-1  one assistant message written twice (chunk usage, then message usage
       with byte-identical numbers)
  D-2  a forked child whose leading events are a physical copy of the parent's
       completed prefix, bounded by seedLength
  D-3  a compaction/summary carrying provider-reported usage
  D-4  a retried step whose FAILED attempt reports real tokens -- the case no
       natural corpus I have seen produces, because failed attempts observed
       in the wild reported zeros
  cacheWriteTokens non-zero, which neither route in the measured corpus
       populated

Every number below is distinct and non-round, so a fold that lands on the
wrong total names its own mistake in the arithmetic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reference as ref  # noqa: E402

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixture"
SESSIONS = FIXTURE / "sessions"

BASE_TIME = 1_770_000_000_000


def usage(i, o, cr=0, cw=0):
    return {"inputTokens": i, "outputTokens": o,
            "cacheReadTokens": cr, "cacheWriteTokens": cw}


def ev(seq, type_, data):
    return {"type": type_, "seq": seq, "time": BASE_TIME + seq, "data": data}


# --- the four usage values, each used exactly where the comment says --------
U_DUAL = usage(100, 10, 1000, 5)      # 1115  D-1, written twice
U_FAILED = usage(200, 20, 2000, 0)    # 2220  D-4, the attempt that died
U_RETRY = usage(300, 30, 3000, 7)     # 3337  D-4, the attempt that survived
U_COMPACT = usage(400, 40, 4000, 0)   # 4440  D-3, the summarize call
U_CHILD = usage(50, 5, 500, 1)        # 556   the child's own work


def parent_events() -> list:
    return [
        ev(0, "user/message", {"turn": 0}),
        # D-1: same message, two records, identical usage
        ev(1, "assistant/chunk", {"turn": 0, "step": 0,
                                  "chunk": {"type": "usage", "usage": U_DUAL}}),
        ev(2, "assistant/message", {"turn": 0, "step": 0, "message": {}, "usage": U_DUAL}),
        # D-4: attempt one reports real tokens, then the transport dies
        ev(3, "assistant/chunk", {"turn": 0, "step": 1,
                                  "chunk": {"type": "usage", "usage": U_FAILED}}),
        ev(4, "assistant/chunk", {"turn": 0, "step": 1,
                                  "chunk": {"type": "finish",
                                            "reason": {"kind": "error",
                                                       "failure": {"code": "TRANSPORT"}}}}),
        ev(5, "llm/retry", {"retryId": "r-conformance-1", "provider": "conformance"}),
        ev(6, "llm/retry-started", {"retryId": "r-conformance-1", "retry": 1}),
        # attempt two, under the SAME (turn, step), then its dedup partner
        ev(7, "assistant/chunk", {"turn": 0, "step": 1,
                                  "chunk": {"type": "usage", "usage": U_RETRY}}),
        ev(8, "assistant/message", {"turn": 0, "step": 1, "message": {}, "usage": U_RETRY}),
        ev(9, "turn/end", {"turn": 0}),
        # D-3
        ev(10, "compaction/start", {"compactionId": "c1", "turn": None}),
        ev(11, "compaction/summary", {"compactionId": "c1", "summary": [],
                                      "shadowedRange": {"start": 0, "end": 9},
                                      "shadowedSeqs": list(range(10)),
                                      "shadowedTokenCount": 6672,
                                      "provider": "conformance",
                                      "model": "conformance-model",
                                      "usage": U_COMPACT}),
        ev(12, "user/message", {"turn": 0,
                                "surfaceOp": {"op": "replace", "start": 0, "end": 9}}),
        ev(13, "compaction/end", {"compactionId": "c1", "turn": None}),
    ]


def child_events(parent: list) -> list:
    # D-2: the child's log physically contains the parent's completed turn.
    # seq 0..9 are inherited; seedLength is the threshold, and the end-seed
    # marker sits inside the inherited region.
    seed = [dict(e) for e in parent[:10]]
    return [
        *seed,
        ev(10, "session/end-seed", {}),
        ev(11, "assistant/message", {"turn": 1, "step": 0, "message": {}, "usage": U_CHILD}),
        ev(12, "turn/end", {"turn": 1}),
    ]


def write_session(name: str, header: dict, events: list) -> None:
    d = SESSIONS / "--conformance--" / name
    d.mkdir(parents=True, exist_ok=True)
    lines = [header, *events]
    (d / "session.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in lines) + "\n",
        encoding="utf-8",
    )


def hand_computed() -> dict:
    """Ground truth by arithmetic, not by running the folds."""
    dual, failed, retry, compact, child = 1115, 2220, 3337, 4440, 556
    parent_own_requests = dual + failed + retry            # 6672
    parent_corrected = parent_own_requests + compact       # 11112
    parent_official = dual + retry                         # 4452, failed attempt replaced away
    parent_naive = dual * 2 + failed + retry * 2 + compact  # 15564

    child_corrected = child                                # 556
    child_official = parent_official + child               # 5008, seed counted again
    child_naive = dual * 2 + failed + retry * 2 + child     # 11680

    return {
        "corrected": parent_corrected + child_corrected,    # 11668
        "seed_aware": parent_official + child_corrected,    # 5008
        "official": parent_official + child_official,       # 9460
        "naive": parent_naive + child_naive,                # 27244
        "gap_compaction": compact,                          # 4440
        "gap_superseded": failed,                           # 2220
        "gap_inherited": parent_official,                   # 4452
    }


def main() -> int:
    parent = parent_events()
    child = child_events(parent)

    write_session(
        "session-conformance-parent",
        {"type": "session", "version": 0, "id": "conformance-parent", "cwd": "/conformance",
         "createdAt": BASE_TIME, "delegationDepth": 0},
        parent,
    )
    write_session(
        "session-conformance-child",
        {"type": "session", "version": 0, "id": "conformance-child", "cwd": "/conformance",
         "createdAt": BASE_TIME + 100, "parentSession": "conformance-parent",
         "seedLength": 11, "origin": "subagent", "delegationDepth": 1},
        child,
    )

    truth = hand_computed()
    agg = ref.aggregate(SESSIONS)

    mismatches = [k for k, want in truth.items() if ref.total(agg[k]) != want]
    if mismatches:
        for k in mismatches:
            print(f"  {k}: reference fold says {ref.total(agg[k]):,}, "
                  f"hand-computed says {truth[k]:,}", file=sys.stderr)
        print("\nThe fixture and the arithmetic disagree. One of them is wrong; "
              "do not commit until they agree.", file=sys.stderr)
        return 1

    expected = {
        "_comment": "Hand-computed, then reproduced by reference.py. Totals are the "
                    "sum over the four buckets; per-bucket figures are authoritative.",
        "buckets": list(ref.BUCKETS),
        "sessions": agg["sessions"],
        "folds": {k: agg[k] for k in ("naive", "official", "seed_aware", "corrected")},
        "gaps": {k: agg[k] for k in ("gap_compaction", "gap_superseded", "gap_inherited")},
        "totals": {k: ref.total(agg[k]) for k in truth},
    }
    (FIXTURE / "expected.json").write_text(
        json.dumps(expected, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("fixture written, and every total matches the hand-computed value:")
    for k in ("naive", "official", "seed_aware", "corrected"):
        print(f"  {k:<12}{ref.total(agg[k]):>10,}")
    print()
    for k in ("gap_inherited", "gap_superseded", "gap_compaction"):
        print(f"  {k:<16}{ref.total(agg[k]):>10,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

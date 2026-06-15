"""Example (STUB): self-contamination in a self-improving loop.

A self-improving / agentic loop writes its own freshly generated outputs back
into a memory store, then reads that memory on the next iteration as if it were
established fact. Over time the loop "learns" from itself: a claim the model
invented at step N becomes "evidence" at step N+1. In a point-in-time setting
this is a look-ahead leak — the memory now contains "facts" that were not
traceable to any source available before the simulated cutoff.

The fix is *evidence-gating*: only admit a memory write as fact if it is
traceable to evidence that existed before the cutoff. That helper lives in
`traceguard.loop` (see docs/loop-integration.md). This file is an illustrative
STUB sketching the failure and the gate.

Run (from the repo root)::

    uv run python examples/loop_self_contamination.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "traceguard" / "src"
if _PKG_SRC.is_dir() and str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))


def main() -> int:
    print(__doc__)
    print("-" * 72)
    print(
        "Without a gate:\n"
        "  step 1: model guesses 'Acme revenue = $1.2B' (no source)\n"
        "  step 2: that guess is in memory; model now cites it as fact\n"
        "  step 3: a downstream feature 'as of 2023' rests on a 2025 invention\n"
    )
    print(
        "With evidence-gating: a memory write is admitted only if its supporting\n"
        "evidence has a source timestamp <= the cutoff being simulated. The\n"
        "self-generated guess (no pre-cutoff source) is rejected at the gate.\n"
    )

    try:
        from traceguard.loop import EvidenceGate  # noqa: F401
    except ImportError:
        print(
            "traceguard.loop is not importable in this environment.\n"
            "See docs/loop-integration.md for the evidence-gating design.\n"
        )
        print("(stub) nothing to run — this sketch documents the failure mode.")
        return 0

    # Illustrative only — see docs/loop-integration.md for the real walkthrough.
    from datetime import datetime, timezone

    cutoff = datetime(2023, 1, 1, tzinfo=timezone.utc)
    gate = EvidenceGate(cutoff=cutoff)
    sourced = gate.admit(
        claim="Acme revenue = $1.2B",
        source_as_of=datetime(2022, 6, 30, tzinfo=timezone.utc),
    )
    invented = gate.admit(claim="Acme revenue = $1.2B", source_as_of=None)
    print(f"(illustrative) sourced-pre-cutoff admitted={sourced}")
    print(f"(illustrative) self-invented admitted={invented}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

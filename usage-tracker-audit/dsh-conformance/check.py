#!/usr/bin/env python3
"""DSH token-accounting conformance check. Stdlib only, no corpus, no account.

Point it at whatever your project uses to total tokens from a DeepSeek Harness
session log. It runs that against a committed synthetic fixture and, when the
number is wrong, says which invariant you missed rather than just that you
missed one.

    python3 check.py --self-test
    python3 check.py --cmd "node dist/fold.js"
    python3 check.py --cmd "cargo run --quiet -- --json"

Your command is invoked with the fixture's sessions root appended as its last
argument, and the same path in DSH_CONFORMANCE_ROOT. It must print one JSON
object to stdout carrying any of:

    uncachedInputTokens  outputTokens  cacheReadTokens  cacheWriteTokens

Absent keys are read as zero, so a tool that tracks only two buckets can still
be checked on those two with --buckets. The last JSON object printed wins, so
logging to stdout before the result is fine.

Exit status is 0 when every checked bucket matches, 1 otherwise.

Invariants: CONFORMANCE-DSH.md in lizhuojunx86/traceguard.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reference as ref  # noqa: E402

HERE = Path(__file__).resolve().parent
SESSIONS = HERE / "fixture" / "sessions"

# Each entry: label, how to build the total, what it means, which invariant.
DIAGNOSES = [
    ("naive",
     "You are summing every usage sighting. One assistant message is written "
     "twice, so this roughly doubles before anything else goes wrong.",
     "D-1"),
    ("official",
     "You are reporting exactly what the official tokenUsage projection "
     "reports, including its own gaps: compaction is uncounted, a superseded "
     "attempt is replaced rather than kept, and a forked child re-counts the "
     "prefix it inherited.",
     "D-2, D-3, D-4"),
    ("seed_aware",
     "The fork seed is handled, but compaction is uncounted and a superseded "
     "attempt is replaced rather than kept.",
     "D-3, D-4"),
]


def _fmt(d: dict) -> str:
    return "  ".join(f"{b.replace('Tokens', '')}={d[b]:,}" for b in ref.BUCKETS)


def _diff(got: dict, want: dict) -> dict:
    return {b: got[b] - want[b] for b in ref.BUCKETS}


def _nonzero(d: dict) -> bool:
    return any(d[b] for b in ref.BUCKETS)


def _eq(a: dict, b: dict, buckets) -> bool:
    return all(a[x] == b[x] for x in buckets)


def run_command(cmd: str, root: Path) -> dict:
    env = dict(os.environ, DSH_CONFORMANCE_ROOT=str(root))
    argv = shlex.split(cmd) + [str(root)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=300)
    except FileNotFoundError:
        raise SystemExit(f"could not run: {argv[0]} is not on PATH")
    except subprocess.TimeoutExpired:
        raise SystemExit("the command did not finish within 300s")
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"the command exited {proc.returncode}")

    objs = []
    for match in re.finditer(r"\{.*?\}", proc.stdout, re.DOTALL):
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and any(b in obj for b in ref.BUCKETS):
            objs.append(obj)
    if not objs:
        sys.stderr.write(proc.stdout)
        raise SystemExit(
            "no JSON object carrying any of "
            f"{', '.join(ref.BUCKETS)} was found on stdout")
    raw = objs[-1]
    return {b: int(raw.get(b) or 0) for b in ref.BUCKETS}


def report(got: dict, agg: dict, buckets) -> int:
    want = agg["corrected"]
    print()
    print("DSH token-accounting conformance")
    print("-" * 70)
    print(f"  reported   {_fmt(got)}")
    print(f"  expected   {_fmt(want)}")
    print()

    if _eq(got, want, buckets):
        print("  PASS -- every checked bucket matches the corrected fold.")
        print()
        print("  That covers D-1 (one message, one count), D-2 (an inherited fork")
        print("  prefix is not yours), D-3 (compaction is a billable call) and")
        print("  D-4 (a superseded attempt is a separate cost, not a duplicate).")
        print()
        return 0

    print("  FAIL")
    print()

    for key, why, invariant in DIAGNOSES:
        if _eq(got, agg[key], buckets):
            print(f"  Your total is exactly the `{key}` fold.")
            print(f"  {why}")
            print(f"  Invariant: {invariant}")
            print()
            _print_gaps(agg)
            return 1

    # Not one of the named folds. Decompose the residual against the three
    # known gap terms so the message still points somewhere.
    residual = _diff(got, want)
    print(f"  residual   {_fmt(residual)}")
    print()
    named = {
        "gap_compaction": ("compaction/summary.usage is not being folded", "D-3"),
        "gap_superseded": ("a superseded attempt's usage is being dropped", "D-4"),
        "gap_inherited": ("an inherited fork prefix is being counted", "D-2"),
    }
    explained = False
    for key, (why, invariant) in named.items():
        term = agg[key]
        if not _nonzero(term):
            continue
        if _eq(residual, {b: -term[b] for b in ref.BUCKETS}, buckets):
            print(f"  The residual is exactly the {key} term: {why}.")
            print(f"  Invariant: {invariant}")
            explained = True
        elif _eq(residual, term, buckets):
            print(f"  The residual is exactly +{key}: {why}, in the other direction.")
            print(f"  Invariant: {invariant}")
            explained = True
    if not explained:
        print("  The residual matches none of the three known gap terms alone.")
        print("  Either two of them are combining, or this is something the")
        print("  catalog does not describe yet. If it is the second, that is worth")
        print("  filing: CONFORMANCE-DSH.md takes counterexamples.")
    print()
    _print_gaps(agg)
    return 1


def _print_gaps(agg: dict) -> None:
    print("  What separates the folds on this fixture:")
    print(f"    inherited fork prefix   {ref.total(agg['gap_inherited']):>8,}   D-2")
    print(f"    superseded attempt      {ref.total(agg['gap_superseded']):>8,}   D-4")
    print(f"    compaction summarize    {ref.total(agg['gap_compaction']):>8,}   D-3")
    print()
    print("  Reference: https://github.com/lizhuojunx86/traceguard/blob/main/CONFORMANCE-DSH.md")
    print()


def self_test(agg: dict) -> int:
    expected_path = HERE / "fixture" / "expected.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    ok = True
    print()
    print("self-test -- reference folds vs the committed expectations")
    print("-" * 70)
    for group in ("folds", "gaps"):
        for key, want in expected[group].items():
            got = agg[key]
            match = all(got[b] == want[b] for b in ref.BUCKETS)
            ok &= match
            print(f"  {key:<18}{ref.total(got):>10,}   expected "
                  f"{sum(want.values()):>10,}   {'PASS' if match else 'FAIL'}")
    print()

    # The reconciliation D-5 asserts, stated as an identity over the three terms.
    lhs = _diff(agg["corrected"], agg["official"])
    rhs = {b: agg["gap_compaction"][b] + agg["gap_superseded"][b] - agg["gap_inherited"][b]
           for b in ref.BUCKETS}
    identity = all(lhs[b] == rhs[b] for b in ref.BUCKETS)
    ok &= identity
    print("  D-5 reconciliation, bucket for bucket:")
    print("    corrected - official == compaction + superseded - inherited")
    print(f"    {ref.total(lhs):>+8,}            ==    {ref.total(rhs):>+8,}"
          f"   {'PASS' if identity else 'FAIL'}")
    print()
    if ok:
        print("  The fixture reproduces every committed number, and the residual")
        print("  between the corrected fold and the official projection is fully")
        print("  accounted for. The suite may be pointed at an implementation.")
    else:
        print("  The reference disagrees with the committed fixture. Re-run")
        print("  build_fixture.py and read the diff before trusting anything here.")
    print()
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cmd", help="the command that totals tokens from a DSH log")
    ap.add_argument("--self-test", action="store_true",
                    help="check the reference folds against the committed fixture")
    ap.add_argument("--buckets", default=",".join(ref.BUCKETS),
                    help="comma-separated buckets to compare (default: all four)")
    ap.add_argument("--root", type=Path, default=SESSIONS,
                    help="fixture sessions root (default: the committed fixture)")
    args = ap.parse_args()

    buckets = [b.strip() for b in args.buckets.split(",") if b.strip()]
    unknown = [b for b in buckets if b not in ref.BUCKETS]
    if unknown:
        ap.error(f"unknown bucket(s): {', '.join(unknown)}")

    agg = ref.aggregate(args.root)
    if agg["sessions"] == 0:
        ap.error(f"no session.jsonl found under {args.root}")

    if args.self_test:
        return self_test(agg)
    if not args.cmd:
        ap.error("pass --cmd, or --self-test")
    return report(run_command(args.cmd, args.root), agg, buckets)


if __name__ == "__main__":
    raise SystemExit(main())

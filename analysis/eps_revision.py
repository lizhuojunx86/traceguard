"""Recompute the `epsActual` revision and decision-flip rates from the
published disclosure dataset.

Standard library only, no network, no vendor credentials. Everything it reads
is committed to this repo, so the two headline numbers in the TraceGuard and
tg-attest READMEs can be checked by anyone in one command:

    python analysis/eps_revision.py

Method and limitations: docs/eps-revision-methodology.md.

WHAT "DECISION FLIP" MEANS — the whole claim rests on this, so it is stated
once, here, and nowhere else:

    surprise_pct = (eps_actual - eps_estimated) / abs(eps_estimated) * 100
    threshold    = 2.0  when the first-seen analyst count is <= 1  ("V3" leg)
                   10.0 otherwise                                  ("V5" leg)
    tradeable    = surprise_pct > threshold          (long only, no short leg)
    FLIPPED      = tradeable(first_seen) != tradeable(final)

A flip is therefore a change in a binary entry decision, in either direction:
a trade the vendor's first-seen value said to take and its final value says to
skip, or the reverse. It is NOT a P&L estimate and NOT a claim about how much
money the flip is worth. The analyst count that picks the threshold is read
from the first-seen snapshot and used for BOTH legs — using the final count
would smuggle back in the look-ahead bias being measured.

This script does not recompute that rule: the per-record decision booleans are
in the CSV, because recomputing them would need the EPS values, which are FMP
proprietary and not redistributable. It does verify that the booleans are
mutually consistent (see `audit_rows`), which is the part a reader can check
without an FMP subscription.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
Z_95 = 1.959963984540054  # two-sided normal quantile at 95%


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval.

    Preferred over the normal approximation here because the flip rate is well
    under 20% and one dataset has a small per-bucket n, where the naive
    interval runs off the end of [0, 1].
    """
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    halfwidth = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - halfwidth), min(1.0, centre + halfwidth))


def load_rows(path: Path) -> list[dict]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def as_bool(value: str) -> bool | None:
    if value == "":
        return None
    return value == "true"


def audit_rows(rows: list[dict]) -> list[str]:
    """Internal-consistency checks a reader can run without vendor data.

    The keyed digests are what make this possible: a published `eps_differs`
    flag is only worth as much as the digest pair it has to agree with.
    """
    problems: list[str] = []
    for i, r in enumerate(rows, start=2):  # +1 header, +1 to 1-index
        differs = as_bool(r["eps_differs"])
        if differs != (r["first_seen_hash"] != r["final_hash"]):
            problems.append(f"line {i}: eps_differs disagrees with the digest pair")
        if differs is False and r["direction"] != "none":
            problems.append(f"line {i}: identical values but direction={r['direction']}")
        if differs is False and r["magnitude_bucket"] != "0":
            problems.append(f"line {i}: identical values but bucket={r['magnitude_bucket']}")
        if differs is True and r["magnitude_bucket"] == "0":
            problems.append(f"line {i}: differing values but bucket=0")
        t_first, t_final = as_bool(r["first_seen_tradeable"]), as_bool(r["final_tradeable"])
        flipped = as_bool(r["decision_flipped"])
        if t_first is not None and t_final is not None:
            if flipped != (t_first != t_final):
                problems.append(f"line {i}: decision_flipped disagrees with the tradeable pair")
        elif flipped:
            problems.append(f"line {i}: flipped with an undefined tradeable leg")
    return problems


def summarise(rows: list[dict]) -> dict:
    n = len(rows)
    differs = sum(1 for r in rows if r["eps_differs"] == "true")
    flipped = sum(1 for r in rows if r["decision_flipped"] == "true")
    sign_flipped = sum(1 for r in rows if r["surprise_sign_flipped"] == "true")
    buckets: dict[str, int] = {}
    for r in rows:
        buckets[r["magnitude_bucket"]] = buckets.get(r["magnitude_bucket"], 0) + 1
    directions: dict[str, int] = {}
    for r in rows:
        directions[r["direction"]] = directions.get(r["direction"], 0) + 1
    return {
        "n": n,
        "symbols": len({r["symbol"] for r in rows}),
        "first_seen_span": (
            min(r["first_seen_date"] for r in rows),
            max(r["first_seen_date"] for r in rows),
        ),
        "differs": differs,
        "differs_ci": wilson_interval(differs, n),
        "flipped": flipped,
        "flipped_ci": wilson_interval(flipped, n),
        "sign_flipped": sign_flipped,
        "buckets": buckets,
        "directions": directions,
        "stale": sum(1 for r in rows if r["first_seen_stale"] == "true"),
    }


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def report(dataset: dict, rows: list[dict]) -> None:
    s = summarise(rows)
    print(f"\n{'=' * 72}")
    print(f"{dataset['id']}  —  {dataset['role']}")
    print("=" * 72)
    print(f"  capture window     {dataset['capture_window']}")
    print(f"  first-seen source  {dataset['first_seen_source']}")
    print(f"  final reference    {dataset['final_reference']}")
    print(f"  N (records)        {s['n']}")
    print(f"  distinct symbols   {s['symbols']}")
    print(f"  first-seen dates   {s['first_seen_span'][0]} .. {s['first_seen_span'][1]}")

    lo, hi = s["differs_ci"]
    print(
        f"\n  epsActual differs first-seen vs final:  {s['differs']}/{s['n']} = "
        f"{pct(s['differs'] / s['n'])}   95% CI [{pct(lo)}, {pct(hi)}]"
    )
    lo, hi = s["flipped_ci"]
    print(
        f"  decision flipped:                       {s['flipped']}/{s['n']} = "
        f"{pct(s['flipped'] / s['n'])}   95% CI [{pct(lo)}, {pct(hi)}]"
    )
    print(
        f"  surprise sign flipped (context only):   {s['sign_flipped']}/{s['n']} = "
        f"{pct(s['sign_flipped'] / s['n'])}"
    )

    # Records where the capture demonstrably missed the print. Their "first
    # seen" may already be a revision, which drags both rates down, so the
    # subset is reported rather than silently kept or silently dropped.
    if s["stale"]:
        clean = [r for r in rows if r["first_seen_stale"] != "true"]
        c = summarise(clean)
        print(
            f"\n  excluding {s['stale']} records whose first capture was "
            f"more than a day after the print:"
        )
        lo, hi = c["differs_ci"]
        print(
            f"    differs   {c['differs']}/{c['n']} = {pct(c['differs'] / c['n'])}"
            f"   95% CI [{pct(lo)}, {pct(hi)}]"
        )
        lo, hi = c["flipped_ci"]
        print(
            f"    flipped   {c['flipped']}/{c['n']} = {pct(c['flipped'] / c['n'])}"
            f"   95% CI [{pct(lo)}, {pct(hi)}]"
        )

    print("\n  |delta eps| distribution")
    order = ["0", "(0,0.01)", "[0.01,0.05)", "[0.05,0.25)", "[0.25,1.00)", ">=1.00"]
    for b in order:
        cnt = s["buckets"].get(b, 0)
        print(f"    {b:<14} {cnt:>6}  {pct(cnt / s['n']):>7}")
    print("\n  revision direction")
    for d in ("none", "up", "down"):
        cnt = s["directions"].get(d, 0)
        print(f"    {d:<14} {cnt:>6}  {pct(cnt / s['n']):>7}")

    # The obvious objection to any threshold-crossing statistic is that the
    # thresholds were chosen after seeing the answer. They were not: 2.0/10.0
    # are the live router's, and most neighbours give a HIGHER flip rate.
    sens = dataset.get("threshold_sensitivity")
    if sens:
        print("\n  flip rate vs threshold choice")
        for s_row in sens:
            mark = "  <- production" if s_row["production"] else ""
            print(
                f"    V3={s_row['threshold_v3']:<5} V5={s_row['threshold_v5']:<5} "
                f"{s_row['flipped']:>5}  {pct(s_row['rate']):>7}{mark}"
            )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default=str(DATA_DIR))
    ap.add_argument("--json", action="store_true", help="emit machine-readable summary")
    args = ap.parse_args()

    data = Path(args.data)
    manifest = json.loads((data / "manifest.json").read_text())
    results = {}
    failures = 0

    for ds in manifest["datasets"]:
        path = data / ds["file"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != ds["sha256"]:
            print(f"!! {ds['file']}: sha256 does not match the manifest")
            failures += 1
        rows = load_rows(path)
        if len(rows) != ds["rows"]:
            print(f"!! {ds['file']}: {len(rows)} rows, manifest says {ds['rows']}")
            failures += 1
        problems = audit_rows(rows)
        if problems:
            failures += len(problems)
            for p in problems[:20]:
                print(f"!! {ds['file']}: {p}")
            if len(problems) > 20:
                print(f"!! {ds['file']}: ... and {len(problems) - 20} more")
        if not args.json:
            report(ds, rows)
        results[ds["id"]] = summarise(rows)

    if args.json:
        print(json.dumps(results, indent=2, default=list))
    else:
        print(f"\n{'=' * 72}")
        print(f"decision rule: {json.dumps(manifest['decision_rule'], indent=2)}")
        print(f"\nintegrity: {'OK' if failures == 0 else str(failures) + ' PROBLEM(S)'}")
        print("method and limitations: docs/eps-revision-methodology.md")

    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()

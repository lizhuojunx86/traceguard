"""Build the public, non-redistributing disclosure dataset behind the
41.4% / 15.3% `epsActual` revision claim.

Run by the data holder only. It reads private capture artifacts that are NOT in
this repo (and cannot be, see below) and emits the CSV + manifest under
``analysis/data/`` that anyone can then re-analyse with ``eps_revision.py``.

WHY THIS IS NOT A "RE-COLLECT" SCRIPT
-------------------------------------
A first-seen vendor value cannot be rebuilt after the fact. FMP serves one
value per (symbol, reportDate) — today's value. The value it served on the
morning of the print is gone the moment it is overwritten; there is no vintage
endpoint, and ``lastUpdated`` is a bulk-reprocess stamp, not a revision log.
That is the entire reason TraceGuard exists. So reproducibility here means
"recompute the statistic from the record captured at the time", not "re-run the
capture". Anyone with their own FMP entitlement can start their own capture and
reproduce the *method*; nobody, including us, can reproduce the *capture*.

WHY THE VALUES THEMSELVES ARE NOT PUBLISHED
-------------------------------------------
FMP Terms of Service (last updated 2023-08-01), §2.6.1(i): the customer shall
not "resell, sublicense, distribute or otherwise provide access to The Services,
or data or information contained in or derived from The Services, to any third
party". §2.2.2 separately forbids displaying FMP data on a public site or in a
software product without a specific agreement. So this dataset publishes, per
record: the symbol, the period, a keyed digest of the first-seen value, a keyed
digest of the final value, whether they differ, the direction, and a coarse
magnitude bucket. No EPS value, estimate, revenue or analyst count is released.

The digests are HMAC-SHA256 under a secret pepper, NOT bare SHA-256. Bare
hashing would be security theatre: EPS values live in a domain of a few tens of
thousands of two-decimal candidates, so a published-salt digest is brute-forced
in seconds and would amount to redistributing the values. The pepper stays with
the data holder; ``pepper_sha256`` in the manifest commits to it. Their role is
(a) to make ``eps_differs`` checkable rather than trusted — the flag must equal
``first_seen_hash != final_hash`` on every row, (b) to freeze the record set, so
the values behind a published row cannot be quietly restated later, and (c) to
let a specific auditor who holds their own FMP entitlement verify row by row,
after being handed the pepper alone.

That last mechanism — prove a claim about a record, disclose nothing else, and
let the holder reveal the key to exactly one auditor — is what the sibling
project tg-attest packages properly (Merkle inclusion proofs + an RFC 3161
timestamp instead of an ad-hoc pepper). This file is the hand-rolled version of
the same idea, which is a reasonable argument that the idea was needed.

PRIVATE INPUTS (paths are defaults; override with flags)
--------------------------------------------------------
  --episodes   quant_alpha_v2 var/vintage/revision_episodes.parquet
               Retrospective PIT series extracted from quant_trade's rotated
               PEAD autopilot logs (2026-02-03..2026-06-03), matched against an
               FMP-current snapshot pulled 2026-06-04. Source of the headline.
  --snapshots  ~/.local/share/qav2-vintage/forward_snapshots.jsonl
               Live forward poller, first snapshot written 2026-06-05. A
               genuinely point-in-time capture, but over a much shorter
               post-print horizon — see the methodology doc.

Usage (needs duckdb; run under the quant_alpha_v2 env that owns the artifacts):
    python analysis/build_disclosure.py --out analysis/data
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

# ── decision rule — the single definition of "flips a trading decision" ──────
#
# Mirrors the live PEAD router this study was run against. Kept here, in code,
# because the flip rate is meaningless without it.
#
#   surprise_pct = (eps_actual - eps_estimated) / |eps_estimated| * 100
#   threshold    = 2.0  if the analyst count is <= 1  (the "V3" leg)
#                  10.0 otherwise                     (the "V5" leg)
#   tradeable    = surprise_pct > threshold           (long-only)
#   flipped      = tradeable(first_seen) != tradeable(final)
#
# The analyst count is read ONCE, from the first-seen snapshot, and used for
# both legs. Using the final count would re-import the look-ahead bias the
# study is measuring: at decision time only the first-seen count exists.
V3_THRESHOLD = 2.0
V5_THRESHOLD = 10.0
V3_MAX_ANALYSTS = 1

# |eps| above this in a $50M-$10B universe is a vendor glitch, not a print
# (observed: TRS epsActual=2034.97 served for 96 consecutive polling cycles
# interleaved with the real 0.40). Excluded from both numerator and denominator.
PLAUSIBLE_ABS_EPS = 50.0

MAGNITUDE_BUCKETS = (
    (0.0, "0"),
    (0.01, "(0,0.01)"),
    (0.05, "[0.01,0.05)"),
    (0.25, "[0.05,0.25)"),
    (1.00, "[0.25,1.00)"),
    (float("inf"), ">=1.00"),
)

PEPPER_PATH = Path.home() / ".local/share/traceguard-eps-disclosure/pepper"

FIELDNAMES = [
    "dataset",
    "symbol",
    "period",
    "first_seen_date",
    "final_ref_date",
    "router",
    "first_seen_hash",
    "final_hash",
    "eps_differs",
    "direction",
    "magnitude_bucket",
    "first_seen_tradeable",
    "final_tradeable",
    "decision_flipped",
    "surprise_sign_flipped",
    "first_seen_stale",
]

# A "first-seen" value is only first-seen if the capture ran when the print
# landed. `first_seen_stale` marks records where it demonstrably did not: the
# capture is more than STALE_DAYS after the report date, so the value recorded
# as first-seen may already be a revision. Published per record so a reader can
# drop these and see what the numbers do.
STALE_DAYS = 1


# ── shared primitives ───────────────────────────────────────────────────────


def surprise_pct(actual: float | None, estimated: float | None) -> float | None:
    if actual is None or estimated is None or estimated == 0:
        return None
    return (actual - estimated) / abs(estimated) * 100.0


def tradeable_long(surprise: float | None, n_analysts: int | None) -> bool | None:
    """Long-only entry gate. `n_analysts` is the FIRST-SEEN count, always."""
    if surprise is None:
        return None
    thr = (
        V3_THRESHOLD if (n_analysts is not None and n_analysts <= V3_MAX_ANALYSTS) else V5_THRESHOLD
    )
    return surprise > thr


def plausible(x: float | None) -> bool:
    return x is not None and abs(x) <= PLAUSIBLE_ABS_EPS


def magnitude_bucket(delta: float) -> str:
    d = abs(delta)
    if d == 0:
        return "0"
    for edge, label in MAGNITUDE_BUCKETS[1:]:
        if d < edge:
            return label
    return ">=1.00"


def direction_of(delta: float) -> str:
    if delta == 0:
        return "none"
    return "up" if delta > 0 else "down"


def load_pepper(path: Path) -> bytes:
    """Persistent 32-byte pepper. Generated once; never enters the repo."""
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    pepper = secrets.token_bytes(32)
    path.write_text(pepper.hex() + "\n")
    os.chmod(path, 0o600)
    return pepper


def value_hash(pepper: bytes, dataset: str, symbol: str, value: float | None) -> str:
    """Keyed digest of one vendor value, scoped to (dataset, symbol).

    Scoping stops cross-symbol equality leaking ("these 40 tickers all printed
    the same EPS"), which the bare value digest would expose for free.
    """
    if value is None:
        return ""
    msg = f"{dataset}|{symbol}|{value:.6f}".encode()
    return hmac.new(pepper, msg, hashlib.sha256).hexdigest()[:16]


# Threshold pairs the flip rate is re-swept at, so a reader can see whether
# 2.0/10.0 was picked to flatter the number. Published as aggregate counts in
# the manifest — no per-record surprise value leaves the building.
SENSITIVITY_GRID = (
    (0.0, 0.0),
    (1.0, 5.0),
    (2.0, 5.0),
    (2.0, 10.0),  # production
    (2.0, 15.0),
    (2.0, 20.0),
    (3.0, 10.0),
    (5.0, 10.0),
    (5.0, 25.0),
)


def emit_row(
    dataset: str,
    pepper: bytes,
    *,
    symbol: str,
    period: str,
    first_seen_date: str,
    final_ref_date: str,
    n_analysts: int | None,
    first_a: float,
    first_e: float | None,
    final_a: float,
    final_e: float | None,
    capture_lag_days: int | None,
) -> tuple[dict, tuple[int | None, float | None, float | None]]:
    """Return (published row, the surprise pair kept only for the sweep)."""
    delta = round(final_a - first_a, 6)
    s_first = surprise_pct(first_a, first_e)
    s_final = surprise_pct(final_a, final_e)
    t_first = tradeable_long(s_first, n_analysts)
    t_final = tradeable_long(s_final, n_analysts)
    flipped = None if (t_first is None or t_final is None) else t_first != t_final
    sign_flipped = None if (s_first is None or s_final is None) else (s_first > 0) != (s_final > 0)
    router = "V3" if (n_analysts is not None and n_analysts <= V3_MAX_ANALYSTS) else "V5"
    row = {
        "dataset": dataset,
        "symbol": symbol,
        "period": period,
        "first_seen_date": first_seen_date,
        "final_ref_date": final_ref_date,
        "router": router,
        "first_seen_hash": value_hash(pepper, dataset, symbol, first_a),
        "final_hash": value_hash(pepper, dataset, symbol, final_a),
        "eps_differs": str(delta != 0).lower(),
        "direction": direction_of(delta),
        "magnitude_bucket": magnitude_bucket(delta),
        "first_seen_tradeable": "" if t_first is None else str(t_first).lower(),
        "final_tradeable": "" if t_final is None else str(t_final).lower(),
        # A record whose surprise is undefined on either leg (estimate missing
        # or zero) cannot flip a decision, so it counts in the denominator as
        # not-flipped. Kept explicit rather than dropped: dropping it would
        # inflate the rate.
        "decision_flipped": "false" if flipped is None else str(flipped).lower(),
        "surprise_sign_flipped": "" if sign_flipped is None else str(sign_flipped).lower(),
        "first_seen_stale": (
            "" if capture_lag_days is None else str(capture_lag_days > STALE_DAYS).lower()
        ),
    }
    return row, (n_analysts, s_first, s_final)


def sweep(aux: list[tuple[int | None, float | None, float | None]]) -> list[dict]:
    """Flip rate re-evaluated across SENSITIVITY_GRID."""
    out = []
    for v3, v5 in SENSITIVITY_GRID:
        flips = 0
        for n, s_first, s_final in aux:
            if s_first is None or s_final is None:
                continue
            thr = v3 if (n is not None and n <= V3_MAX_ANALYSTS) else v5
            if (s_first > thr) != (s_final > thr):
                flips += 1
        out.append(
            {
                "threshold_v3": v3,
                "threshold_v5": v5,
                "production": (v3, v5) == (V3_THRESHOLD, V5_THRESHOLD),
                "flipped": flips,
                "rate": round(flips / max(len(aux), 1), 4),
            }
        )
    return out


# ── dataset A: retrospective QT-PIT episodes (the headline) ─────────────────


def build_qt_pit(episodes: Path, pepper: bytes) -> tuple[list[dict], list[tuple]]:
    import duckdb

    con = duckdb.connect()
    con.execute(f"CREATE VIEW ep AS SELECT * FROM '{episodes}'")
    cols = [
        "ticker",
        "ep_start",
        "fmp_report_date",
        "num_max_pit",
        "pit_eps_a",
        "pit_eps_e",
        "fmp_eps_a",
        "fmp_eps_e",
        "match_day_offset",
    ]
    # The published cohort, verbatim from the original analysis: a
    # high-confidence report_date match, both endpoints plausible, and a
    # computable delta. Changing any of these three changes the denominator.
    rows = con.execute(
        f"SELECT {', '.join(cols)} FROM ep "
        "WHERE match_conf = 'high' AND both_plausible AND B_d_eps_a IS NOT NULL "
        "ORDER BY ticker, ep_start"
    ).fetchall()
    out, aux = [], []
    for r in rows:
        d = dict(zip(cols, r, strict=True))
        report_date = d["fmp_report_date"] or ""
        row, a = emit_row(
            "qt_pit_2026h1",
            pepper,
            symbol=d["ticker"],
            period=report_date[:7] if report_date else d["ep_start"][:7],
            first_seen_date=d["ep_start"],
            final_ref_date="2026-06-04",
            n_analysts=d["num_max_pit"],
            first_a=d["pit_eps_a"],
            first_e=d["pit_eps_e"],
            final_a=d["fmp_eps_a"],
            final_e=d["fmp_eps_e"],
            # match_day_offset = report_date - episode_start. Negative means the
            # print landed before the strategy first logged the ticker, so what
            # we call first-seen is that many days late.
            capture_lag_days=(
                None if d["match_day_offset"] is None else max(0, -d["match_day_offset"])
            ),
        )
        out.append(row)
        aux.append(a)
    return out, aux


# ── dataset B: live forward poller (independent, shorter horizon) ───────────


def build_forward_poll(snapshots: Path, pepper: bytes) -> tuple[list[dict], list[tuple]]:
    by_event: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for line in snapshots.read_text().splitlines():
        if not line.strip():
            continue
        s = json.loads(line)
        by_event[(s["symbol"], s["report_date"][:10])].append(s)

    out, aux = [], []
    for (symbol, report_date), snaps in sorted(by_event.items()):
        snaps.sort(key=lambda s: s["captured_at"])
        with_actual = [s for s in snaps if s["eps_actual"] is not None]
        if not with_actual:
            # Never printed inside the polling window (or a pre-announcement
            # row that stayed empty). Nothing to compare.
            continue
        first, final = with_actual[0], with_actual[-1]
        if not (plausible(first["eps_actual"]) and plausible(final["eps_actual"])):
            continue
        row, a = emit_row(
            "forward_poll_2026h2",
            pepper,
            symbol=symbol,
            period=report_date[:7],
            first_seen_date=first["captured_at"][:10],
            final_ref_date=final["captured_at"][:10],
            n_analysts=first["num_analysts_eps"],
            first_a=first["eps_actual"],
            first_e=first["eps_estimated"],
            final_a=final["eps_actual"],
            final_e=final["eps_estimated"],
            capture_lag_days=max(
                0,
                (
                    dt.date.fromisoformat(first["captured_at"][:10])
                    - dt.date.fromisoformat(report_date)
                ).days,
            ),
        )
        out.append(row)
        aux.append(a)
    return out, aux


# ── output ──────────────────────────────────────────────────────────────────


def write_csv(path: Path, rows: list[dict]) -> str:
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--episodes",
        default=str(Path.home() / "apps/quant_alpha_v2/var/vintage/revision_episodes.parquet"),
    )
    ap.add_argument(
        "--snapshots",
        default=str(Path.home() / ".local/share/qav2-vintage/forward_snapshots.jsonl"),
    )
    ap.add_argument("--out", default="analysis/data")
    ap.add_argument("--pepper", default=str(PEPPER_PATH))
    ap.add_argument("--built-on", default=None, help="ISO date recorded in the manifest")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pepper = load_pepper(Path(args.pepper).expanduser())

    qt, qt_aux = build_qt_pit(Path(args.episodes).expanduser(), pepper)
    fp, fp_aux = build_forward_poll(Path(args.snapshots).expanduser(), pepper)

    qt_path = out / "eps_revision_qt_pit_2026h1.csv"
    fp_path = out / "eps_revision_forward_poll_2026h2.csv"
    manifest = {
        "schema_version": 1,
        "built_on": args.built_on or dt.date.today().isoformat(),
        "pepper_sha256": hashlib.sha256(pepper).hexdigest(),
        "decision_rule": {
            "surprise_pct": "(eps_actual - eps_estimated) / abs(eps_estimated) * 100",
            "threshold_v3": V3_THRESHOLD,
            "threshold_v5": V5_THRESHOLD,
            "router": f"V3 when first-seen analyst count <= {V3_MAX_ANALYSTS}, else V5",
            "tradeable": "surprise_pct > threshold (long only)",
            "flipped": "tradeable(first_seen) != tradeable(final)",
            "analyst_count_source": "first-seen snapshot, used for both legs",
            "plausible_abs_eps": PLAUSIBLE_ABS_EPS,
        },
        "datasets": [
            {
                "id": "qt_pit_2026h1",
                "role": "headline",
                "file": qt_path.name,
                "rows": len(qt),
                "sha256": write_csv(qt_path, qt),
                "capture_window": "2026-02-03..2026-06-03",
                "final_reference": "FMP-current snapshot pulled 2026-06-04",
                "first_seen_source": (
                    "quant_trade PEAD autopilot rotated logs, ~11 min poll, "
                    "two accounts; first-day modal value per episode"
                ),
                "threshold_sensitivity": sweep(qt_aux),
            },
            {
                "id": "forward_poll_2026h2",
                "role": "independent replication, shorter horizon",
                "file": fp_path.name,
                "rows": len(fp),
                "sha256": write_csv(fp_path, fp),
                "capture_window": "2026-06-05..2026-08-05",
                "final_reference": "latest value seen inside the polling window",
                "first_seen_source": (
                    "vintage forward poller, hourly, value-tuple triggered, "
                    "FMP earnings calendar universe, [-7d, +14d] around report date"
                ),
                "threshold_sensitivity": sweep(fp_aux),
            },
        ],
        "not_published": [
            "eps_actual",
            "eps_estimated",
            "revenue_actual",
            "revenue_estimated",
            "num_analysts_eps",
            "grades_count",
            "lastUpdated",
        ],
        "license_note": (
            "Underlying values are FMP proprietary data; ToS 2.6.1(i) forbids "
            "distributing data 'contained in or derived from The Services'. "
            "Only keyed digests, booleans and coarse buckets are released."
        ),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"[build] {qt_path}  rows={len(qt)}")
    print(f"[build] {fp_path}  rows={len(fp)}")
    print(f"[build] {out / 'manifest.json'}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Compare three tokscale runs against the corpus manifest and drift manifest.

Inputs (all produced by run_check.sh):
    manifest.json        ground truth written by gen_corpus.py
    drift-manifest.json  expected per-field delta written by simulate_rewrite.py
    run1.json            tokscale --json, cold start on the intact corpus
    run2.json            tokscale --json, warm re-run, corpus unchanged
    run3.json            tokscale --json, after the in-place rewrite

Assertions (exact fields only — output / cacheRead / cacheWrite / messageCount;
``input`` is reported but not asserted: tokscale adds a ~4-chars/token estimate
for tool payloads on top of the API-reported input_tokens, a separate quirk):

    A  cold start matches the manifest        -> dedup + discovery are correct
    B  warm re-run identical to cold start    -> cache is stable when files are
    C  post-rewrite totals                    -> the verdict:
         equal to run1                        =  history frozen  (exit 0)
         equal to run1 minus expected delta   =  DRIFT CONFIRMED (exit 1)
         anything else                        =  unexpected      (exit 2)

Exit codes: 0 no drift (a fixed version passes), 1 drift confirmed, 2 unexpected.
"""
from __future__ import annotations

import json
import sys

EXACT = [  # (tokscale JSON field, manifest field, drift-manifest field)
    ("output", "output_tokens", "output_tokens"),
    ("cacheRead", "cache_read_input_tokens", "cache_read_input_tokens"),
    ("cacheWrite", "cache_creation_input_tokens", "cache_creation_input_tokens"),
]


def totals(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "messageCount": 0}
    for e in data["entries"]:
        if e.get("client") != "claude":
            continue
        for k in out:
            out[k] += int(e.get(k) or 0)
    return out


def main() -> int:
    manifest = json.load(open("manifest.json", encoding="utf-8"))
    drift = json.load(open("drift-manifest.json", encoding="utf-8"))
    run1, run2, run3 = totals("run1.json"), totals("run2.json"), totals("run3.json")

    correct = manifest["all"]["correct"]
    correct_msgs = manifest["all"]["distinct_assistant_messages"]
    delta = drift["expected_delta"]["last_line"]
    delta_msgs = drift["expected_delta"]["messages"]

    failures = []
    print(f"corpus : {correct_msgs} distinct assistant messages "
          f"({manifest['files']['main_transcripts']} main + "
          f"{manifest['files']['subagent_transcripts']} subagent transcripts)")
    print()

    # A — cold start vs manifest
    print("A. cold start vs ground truth (exact fields)")
    for tok_f, man_f, _ in EXACT:
        ok = run1[tok_f] == correct[man_f]
        print(f"   {tok_f:<12} tokscale={run1[tok_f]:>12,}  manifest={correct[man_f]:>12,}  "
              f"{'OK' if ok else 'MISMATCH'}")
        if not ok:
            failures.append(f"cold-start {tok_f}")
    ok = run1["messageCount"] == correct_msgs
    print(f"   {'messageCount':<12} tokscale={run1['messageCount']:>12,}  "
          f"manifest={correct_msgs:>12,}  {'OK' if ok else 'MISMATCH'}")
    if not ok:
        failures.append("cold-start messageCount")
    print(f"   input (informational): tokscale={run1['input']:,} vs manifest "
          f"{correct['input_tokens']:,} — excess is tokscale's tool-payload estimate")
    print()

    # B — warm re-run
    stable = run1 == run2
    print(f"B. warm re-run, corpus unchanged: {'identical' if stable else 'CHANGED'}")
    if not stable:
        failures.append("warm-rerun instability")
    print()

    # C — post-rewrite verdict
    print(f"C. after in-place rewrite (-{delta_msgs} messages, "
          f"{drift['removed_lines']} lines removed from "
          f"{drift['target_file'].rsplit('/', 1)[-1]})")
    frozen = drifted = True
    for tok_f, _, drift_f in EXACT:
        expected_if_drift = run1[tok_f] - delta[drift_f]
        print(f"   {tok_f:<12} before={run1[tok_f]:>12,}  after={run3[tok_f]:>12,}  "
              f"(frozen would be {run1[tok_f]:,}; drifted would be {expected_if_drift:,})")
        if run3[tok_f] != run1[tok_f]:
            frozen = False
        if run3[tok_f] != expected_if_drift:
            drifted = False
    if run3["messageCount"] != run1["messageCount"]:
        frozen = False
    if run3["messageCount"] != run1["messageCount"] - delta_msgs:
        drifted = False
    print(f"   {'messageCount':<12} before={run1['messageCount']:>12,}  "
          f"after={run3['messageCount']:>12,}")
    print()

    if failures:
        print(f"UNEXPECTED  baseline assertions failed: {', '.join(failures)}")
        return 2
    if frozen:
        print("PASS  totals unchanged after the rewrite: history is frozen.")
        return 0
    if drifted:
        print("FAIL  every exact field dropped by precisely the vanished messages'\n"
              "      usage: totals are recomputed from live files, so a resume/compact\n"
              "      rewrite silently rewrites history (and any submitted leaderboard\n"
              "      number drifts with it).")
        return 1
    print("UNEXPECTED  post-rewrite totals match neither the frozen nor the\n"
          "            drifted prediction — investigate before drawing conclusions.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

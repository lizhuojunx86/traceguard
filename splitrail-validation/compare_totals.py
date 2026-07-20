#!/usr/bin/env python3
"""Compare splitrail 3.5.9 vs 3.6.0 outputs against each other and against the
append-only traceguard routing_audit ground truth.

Inputs (produced by run_ab_test.sh in --out-dir):
    base-old.json / base-new.json          P1 frozen-snapshot scans
    drift-manifest.json                    P2 expected deltas
    post-old.json / post-new.json          P3 post-rewrite scans
    stab-new-1..3.json                     P4 stability runs

Assertions
    A  cold-start parity      base-old == base-new       (per-model msgs+tokens)
    B  3.5.9 drift response   base-old - post-old == manifest (last_line or
                              sum_lines convention; reports which matched)
    C  3.6.0 retention        post-new == base-new
    D  restart stability      stab-new-* byte-identical

Ground truth (needs --db, and --live-tree for the gap decomposition)
    TG = distinct assistant API messages in traces joined to
    routing_audit_ingest_log (append-only, survives rewrites & deletions).
    Gap classes vs the live tree:
      deleted_file   ingest-logged messages whose source file no longer exists
      vanished       file exists but message.id no longer present in it
Outputs: REPORT.md (+ REPLY_DRAFT.md with --emit-reply) in --out-dir.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

TOKEN_KEYS = ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens")


# ---------------------------------------------------------------- splitrail --
def cc_by_model(stats_file: Path) -> dict[str, dict]:
    """Aggregate splitrail Claude Code model_stats across days -> {model: {...}}."""
    data = json.loads(stats_file.read_text())
    out: dict[str, dict] = defaultdict(lambda: {k: 0 for k in ("messageCount", *TOKEN_KEYS)} | {"cost": 0.0})
    for a in data.get("analyzer_stats", []):
        if a.get("analyzer_name") != "Claude Code":
            continue
        for day in (a.get("daily_stats") or {}).values():
            for model, ms in (day.get("model_stats") or {}).items():
                slot = out[model]
                slot["messageCount"] += int(ms.get("messageCount") or 0)
                for k in TOKEN_KEYS:
                    slot[k] += int(ms.get(k) or 0)
                slot["cost"] += float(ms.get("cost") or 0.0)
    return dict(out)


def totals(by_model: dict[str, dict]) -> dict:
    t = {k: 0 for k in ("messageCount", *TOKEN_KEYS)} | {"cost": 0.0}
    for ms in by_model.values():
        for k in t:
            t[k] += ms.get(k, 0)
    return t


def diff_models(a: dict[str, dict], b: dict[str, dict]) -> dict[str, dict]:
    """a - b per model/field (ints only; cost kept informational)."""
    out: dict[str, dict] = {}
    for model in sorted(set(a) | set(b)):
        am = a.get(model, {})
        bm = b.get(model, {})
        d = {k: int(am.get(k, 0)) - int(bm.get(k, 0)) for k in ("messageCount", *TOKEN_KEYS)}
        if any(d.values()):
            out[model] = d
    return out


# ------------------------------------------------------------- ground truth --
def tg_by_model(db: Path) -> tuple[dict[str, dict], str]:
    """Per-model message counts / output tokens / usage split from traceguard DB."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cur = con.cursor()
    (max_ts,) = cur.execute("SELECT MAX(invoked_at) FROM traces").fetchone()
    rows = cur.execute(
        """SELECT t.model_id, t.tokens_out, t.output_parsed
             FROM traces t JOIN routing_audit_ingest_log l ON l.trace_id = t.trace_id
            WHERE t.model_id IS NOT NULL"""
    ).fetchall()
    out: dict[str, dict] = defaultdict(
        lambda: {"messageCount": 0, "outputTokens": 0, "inputTokens": 0,
                 "cacheCreationTokens": 0, "cacheReadTokens": 0})
    for model, tokens_out, parsed in rows:
        slot = out[model]
        slot["messageCount"] += 1
        slot["outputTokens"] += int(tokens_out or 0)
        try:
            usage = (json.loads(parsed) or {}).get("usage") or {}
        except (TypeError, json.JSONDecodeError):
            usage = {}
        slot["inputTokens"] += int(usage.get("input_tokens") or 0)
        slot["cacheCreationTokens"] += int(usage.get("cache_creation_input_tokens") or 0)
        slot["cacheReadTokens"] += int(usage.get("cache_read_input_tokens") or 0)
    con.close()
    return dict(out), str(max_ts)


def gap_decomposition(db: Path, live_tree: Path) -> dict:
    """Classify every ingest-logged message by where it lives now.

    source_file is stored relative to the projects root; classes:
      live_main      file exists, main transcript (<slug>/<session>.jsonl)
      live_subagent  file exists, subagent transcript (deeper paths)
      vanished       file exists but message.id no longer inside it
      deleted_file   file gone from the live tree
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cur = con.cursor()
    rows = cur.execute(
        """SELECT l.source_file, l.source_message_id, t.model_id, t.tokens_out
             FROM routing_audit_ingest_log l JOIN traces t ON t.trace_id = l.trace_id"""
    ).fetchall()
    con.close()

    by_file: dict[str, list] = defaultdict(list)
    for f, mid, model, tok in rows:
        if not f:
            continue
        by_file[f].append((mid, model, int(tok or 0)))

    live_ids_cache: dict[str, set] = {}

    def live_ids(path: Path) -> set:
        key = str(path)
        if key not in live_ids_cache:
            ids: set = set()
            try:
                with path.open(encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if '"assistant"' not in line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("type") == "assistant":
                            mid = (rec.get("message") or {}).get("id")
                            if mid:
                                ids.add(mid)
                            elif rec.get("uuid"):
                                ids.add(f"uuid:{rec['uuid']}")
            except OSError:
                pass
            live_ids_cache[key] = ids
        return live_ids_cache[key]

    classes = ("live_main", "live_subagent", "vanished", "deleted_file")
    stats: dict = {c: {"messages": 0, "outputTokens": 0, "files": 0} for c in classes}
    stats["checked_files"] = 0
    for f, entries in by_file.items():
        stats["checked_files"] += 1
        rel = Path(f)
        p = rel if rel.is_absolute() else live_tree / rel
        if not p.exists():
            stats["deleted_file"]["files"] += 1
            for _, _, tok in entries:
                stats["deleted_file"]["messages"] += 1
                stats["deleted_file"]["outputTokens"] += tok
            continue
        live_cls = "live_main" if len(rel.parts) == 2 else "live_subagent"
        ids = live_ids(p)
        counted = {live_cls: 0, "vanished": 0}
        for mid, _, tok in entries:
            cls = live_cls if mid in ids else "vanished"
            stats[cls]["messages"] += 1
            stats[cls]["outputTokens"] += tok
            counted[cls] += 1
        for cls, n in counted.items():
            if n:
                stats[cls]["files"] += 1
    return stats


# ------------------------------------------------------------------ report --
def fmt_table(header: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--v-old", default="3.5.9")
    ap.add_argument("--v-new", default="3.6.0")
    ap.add_argument("--db", type=Path)
    ap.add_argument("--live-tree", type=Path)
    ap.add_argument("--emit-reply", action="store_true")
    args = ap.parse_args()
    o = args.out_dir

    base_old = cc_by_model(o / "base-old.json")
    base_new = cc_by_model(o / "base-new.json")
    post_old = cc_by_model(o / "post-old.json")
    post_new = cc_by_model(o / "post-new.json")
    manifest = json.loads((o / "drift-manifest.json").read_text())

    results: list[tuple[str, bool, str]] = []

    # A — cold-start parity
    d = diff_models(base_old, base_new)
    results.append(("A cold-start parity (fresh scans identical)", not d,
                    "identical" if not d else f"diverged: {d}"))

    # B — 3.5.9 drops exactly the removed usage.
    # Conventions: one record per message.id with last-line usage ("last_line"),
    # or one record per JSONL line with per-line usage summed ("sum_lines").
    drop = diff_models(base_old, post_old)
    matched = None
    for conv, count_key in (("last_line", "messages"), ("sum_lines", "lines")):
        exp = {m: {"messageCount": v.get(count_key, v["messages"]),
                   "outputTokens": v[conv]["output_tokens"],
                   "inputTokens": v[conv]["input_tokens"],
                   "cacheCreationTokens": v[conv]["cache_creation_input_tokens"],
                   "cacheReadTokens": v[conv]["cache_read_input_tokens"]}
               for m, v in manifest["expected_delta_by_model"].items()}
        if {m: {k: d2[k] for k in exp.get(m, d2)} for m, d2 in drop.items()} == exp:
            matched = conv
            break
    results.append((f"B {args.v_old} drift response (drops removed usage)", matched is not None,
                    f"matches manifest ({matched} convention: "
                    f"{'1 record/message.id' if matched == 'last_line' else '1 record/line, usage summed'})"
                    if matched
                    else f"observed {drop} vs manifest {manifest['expected_delta_by_model']}"))

    # C — 3.6.0 retention
    d = diff_models(post_new, base_new)
    results.append((f"C {args.v_new} retention across rewrite", not d,
                    "totals unchanged" if not d else f"changed: {d}"))

    # D — restart stability
    stab = [(o / f"stab-new-{i}.json").read_bytes() for i in (1, 2, 3)]
    ok = all(s == stab[0] for s in stab)
    results.append((f"D {args.v_new} restart stability (3 runs)", ok,
                    "byte-identical" if ok else "outputs differ across runs"))

    # ground truth
    tg_section = ""
    gap_section = ""
    tg_note = ""
    gap = None
    if args.db and args.db.exists():
        tg, max_ts = tg_by_model(args.db)
        tg_note = f"DB max invoked_at: {max_ts} — run a full-scan ingest first if stale."
        rows = []
        for model in sorted(set(tg) | set(base_new)):
            t, s = tg.get(model, {}), base_new.get(model, {})
            rows.append([model, t.get("messageCount", 0), s.get("messageCount", 0),
                         t.get("messageCount", 0) - s.get("messageCount", 0),
                         t.get("outputTokens", 0), s.get("outputTokens", 0),
                         t.get("outputTokens", 0) - s.get("outputTokens", 0)])
        tt, ts_ = totals(tg), totals(base_new)
        rows.append(["**total**", tt["messageCount"], ts_["messageCount"],
                     tt["messageCount"] - ts_["messageCount"],
                     tt["outputTokens"], ts_["outputTokens"],
                     tt["outputTokens"] - ts_["outputTokens"]])
        tg_section = "\n## Ground truth vs splitrail " + args.v_new + " (frozen snapshot)\n\n" + \
            fmt_table(["model", "TG msgs", "SR msgs", "Δ msgs", "TG outTok", "SR outTok", "Δ outTok"], rows) + \
            f"\n\n> TG = append-only routing_audit ingest (message.id-keyed). {tg_note}\n"

        if args.live_tree and args.live_tree.exists():
            gap = gap_decomposition(args.db, args.live_tree)
            labels = {
                "live_main": "live_main (exists, main transcript — what splitrail scans)",
                "live_subagent": "live_subagent (exists, subagents/** transcript)",
                "vanished": "vanished (file exists, message.id gone)",
                "deleted_file": "deleted_file (session file gone)",
            }
            gap_section = "\n## Coverage decomposition of the append-only ground truth\n\n" + fmt_table(
                ["class", "files", "messages", "outputTokens"],
                [[labels[c], gap[c]["files"], gap[c]["messages"], gap[c]["outputTokens"]]
                 for c in ("live_main", "live_subagent", "vanished", "deleted_file")]) + \
                f"\n\n{gap['checked_files']} ingest-logged files checked against the live tree.\n"

    # ---------------- REPORT.md ----------------
    all_pass = all(ok for _, ok, _ in results)
    report = ["# splitrail {} vs {} — rewrite-retention regression".format(args.v_old, args.v_new), ""]
    report.append(fmt_table(["assertion", "result", "detail"],
                            [[name, "✅ PASS" if ok else "❌ FAIL", detail]
                             for name, ok, detail in results]))
    report.append("\n## Frozen-snapshot totals (both versions, pre-rewrite)\n")
    ts_ = totals(base_new)
    report.append(fmt_table(["metric", "value"],
                            [[k, f"{v:,.6f}" if k == "cost" else f"{v:,}"] for k, v in ts_.items()]))
    report.append(tg_section)
    report.append(gap_section)
    (o / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(("ALL PASS ✅ " if all_pass else "FAILURES ❌ ") + "-> " + str(o / "REPORT.md"))

    # possible extra findings against ground truth
    overcount_note = ""
    subagent_note = ""
    if args.db and args.db.exists():
        tg_msgs = totals(tg)["messageCount"]
        sr_msgs = totals(base_new)["messageCount"]
        if tg_msgs and sr_msgs > tg_msgs * 1.02:
            overcount_note = (
                f"The diff also surfaced a second effect worth a look: splitrail counts "
                f"{sr_msgs:,} Claude Code messages where message.id-level dedup gives "
                f"{tg_msgs:,}. Claude Code streams one API message as several JSONL lines "
                f"(same `message.id`, distinct line `uuid`s, partial usage snapshots that "
                f"grow toward the final line) — accounting per line double-counts those "
                f"partials. Happy to open a separate issue with per-message examples if useful."
            )
        if gap:
            lm, ls = gap["live_main"]["messages"], gap["live_subagent"]["messages"]
            if ls and lm and abs(sr_msgs - lm) <= 0.05 * lm and ls > 0.05 * (lm + ls):
                subagent_note = (
                    f"A separate coverage finding while diffing: splitrail's totals match the "
                    f"*main* transcripts almost exactly ({sr_msgs:,} counted vs {lm:,} "
                    f"message.id-deduped in `projects/<slug>/<session>.jsonl`), but Claude Code "
                    f"also writes subagent transcripts under "
                    f"`projects/<slug>/<sessionId>/subagents/**.jsonl`, which currently hold "
                    f"{ls:,} additional messages on my machine ({ls/(lm+ls):.0%} of live "
                    f"messages — Task/Explore/subagent-heavy workflows). If the analyzer's "
                    f"discovery only globs one level deep, that usage is invisible. I'll open a "
                    f"separate issue with the layout details — it dwarfs the rewrite drift in $ terms."
                )

    # ---------------- REPLY_DRAFT.md ----------------
    if args.emit_reply:
        b = manifest["expected_delta_by_model"]
        n_dropped = sum(v["messages"] for v in b.values())
        lines = [
            "Ran the comparison on my full local corpus (the #200 set, now grown — frozen "
            "snapshot of `~/.claude/projects`, both versions scanning identical bytes; every "
            "run under an isolated `$HOME`, so no state or config crosstalk).",
            "",
            "**Results**",
            "",
        ]
        for name, ok, detail in results:
            lines.append(f"- {'✅' if ok else '❌'} {name} — {detail}")
        lines += [
            "",
            f"Protocol: fresh scans of the frozen snapshot with both versions, then a simulated "
            f"resume/compact (removed the last {n_dropped} assistant message-groups from the largest "
            f"main transcript — same shape as the 5-message drift event in #200), then re-scans "
            f"and a 3x restart-stability check on {args.v_new}.",
            "",
            "One note on “3.6.0 should be higher”: on a cold start they're *equal* — the history "
            "store has nothing to restore yet. The divergence appears across drift events: after the "
            f"simulated rewrite, {args.v_old} dropped by exactly the removed usage while {args.v_new} "
            "held its totals. That's the regression the fixture pins down. Happy to PR the scripts "
            "(runner + rewrite simulator + assertions) if useful.",
            "",
            "Ground-truth deltas vs my append-only ingest log (message.id-keyed, predates both "
            "versions) are in the tables below, with every logged message classified by where it "
            "lives now: still in a main transcript, in a subagents/** transcript, vanished from a "
            "still-existing file, or in a deleted file (which 3.6.0 prunes by design).",
            "",
            "<!-- paste the two tables from REPORT.md here before posting -->",
            "",
            *( [subagent_note, ""] if subagent_note else [] ),
            *( [overcount_note, ""] if overcount_note else [] ),
            "The audit layer I mentioned is now public: TraceGuard `routing_audit` "
            "(https://github.com/lizhuojunx86/traceguard, v1.1.0 “audit evidence layer”) — an "
            "append-only, message.id-keyed ingest of Claude Code transcripts into a SQLite trace "
            "store, plus stated-vs-revealed routing analysis priced per decision. Stable totals are "
            "exactly what it needs underneath — nice to see splitrail land there.",
        ]
        (o / "REPLY_DRAFT.md").write_text("\n".join(lines), encoding="utf-8")
        print("reply draft -> " + str(o / "REPLY_DRAFT.md"))

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())

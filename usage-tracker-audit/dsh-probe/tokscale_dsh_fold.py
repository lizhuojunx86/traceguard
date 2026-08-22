#!/usr/bin/env python3
"""Line-for-line transcription of tokscale's DSH parser, folded over a corpus.

Source: junhoyeo/tokscale @ main, crates/tokscale-core/src/sessions/dsh.rs
        (`parse_dsh_file`, `tokens_from_usage`), read 2026-08-18.

The point of transcribing rather than running the binary is the same as it was
for deepseek-harness#1886: two implementations sharing no code landing on the
same number is checkable; a black-box difference is not. Run the real binary
afterwards to confirm the transcription, not instead of it.

What the transcription asserts about the shipped parser:

  * it matches only `session`, `request/header`, `user/message` and
    `assistant/message`; every other event type falls through `_ => {}`.
    `compaction/summary` is therefore never read.
  * it never reads `assistant/chunk`, so the double-write hazard (D-1) cannot
    reach it, and a usage chunk with no assembled message behind it -- the
    failed half of a retried step (D-4) -- is invisible to it.
  * `seedLength` is honoured (D-2), plus a cross-file dedup key built on
    `data.message.id`.

Usage:
    python3 tokscale_dsh_fold.py --root ~/dsh-probe/sessions
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

BUCKETS = ("input", "output", "cache_read", "cache_write", "reasoning")


def _int(obj, key):
    v = obj.get(key)
    return v if isinstance(v, int) else 0


def tokens_from_usage(usage: dict) -> dict:
    """dsh.rs::tokens_from_usage -- reasoning is carved out of output."""
    output = max(_int(usage, "outputTokens"), 0)
    reasoning = max(_int(usage, "reasoningTokens"), 0)
    return {
        "input": _int(usage, "inputTokens"),
        "output": max(output - reasoning, 0),
        "cache_read": _int(usage, "cacheReadTokens"),
        "cache_write": _int(usage, "cacheWriteTokens"),
        "reasoning": reasoning,
    }


def total(tokens: dict) -> int:
    return sum(tokens[b] for b in BUCKETS)


def parse_dsh_file(path: Path) -> list[dict]:
    """dsh.rs::parse_dsh_file, uncompressed branch only (compression: none)."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    session_id_from_path = path.parent.name or "unknown"
    session_id = None
    seed_length = 0
    fallback_provider = None
    fallback_model = None

    messages: list[dict] = []
    seen: set[str] = set()

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        event_type = value.get("type")

        if event_type == "session":
            session_id = value.get("id")
            sl = value.get("seedLength")
            seed_length = sl if isinstance(sl, int) and sl > 0 else 0

        elif event_type == "request/header":
            config = (value.get("data") or {}).get("header", {}).get("config", {})
            fallback_provider = config.get("provider")
            fallback_model = config.get("model")

        elif event_type == "assistant/message":
            seq = value.get("seq")
            if seed_length > 0 and isinstance(seq, int) and seq < seed_length:
                continue
            data = value.get("data") or {}
            usage = data.get("usage")
            if not isinstance(usage, dict):
                continue
            tokens = tokens_from_usage(usage)
            if total(tokens) == 0:
                continue
            timestamp = value.get("time")
            if not isinstance(timestamp, int) or timestamp <= 0:
                continue

            source = (data.get("message") or {}).get("source") or {}
            model_id = source.get("model") or fallback_model or "unknown"
            provider_id = source.get("provider") or fallback_provider or "unknown"
            sid = session_id or session_id_from_path

            mid = (data.get("message") or {}).get("id")
            mid = mid.strip() if isinstance(mid, str) else ""
            identity = f"msg:{mid}" if mid else f"sid:{sid}"
            dedup_key = (
                f"dsh:{identity}:{timestamp}:{provider_id}:{model_id}:"
                f"{tokens['input']}:{tokens['output']}:{tokens['cache_read']}:"
                f"{tokens['cache_write']}:{tokens['reasoning']}"
            )
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            messages.append({"dedup_key": dedup_key, "tokens": tokens, "session": sid})

    return messages


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    args = ap.parse_args()

    root = Path(os.path.expanduser(args.root))
    files = sorted(root.rglob("session.jsonl")) + sorted(root.rglob("session.jsonl.zstd"))
    if not files:
        print(f"no session logs under {root}")
        return 1

    per_file_total = {b: 0 for b in BUCKETS}
    per_file_count = 0
    global_seen: set[str] = set()
    global_total = {b: 0 for b in BUCKETS}
    global_count = 0

    print(f"tokscale dsh.rs transcription -- {len(files)} session log(s)\n")
    print(f"{'session':<26}{'messages':>10}{'total':>14}")
    print("-" * 50)

    for path in files:
        msgs = parse_dsh_file(path)
        sub = {b: 0 for b in BUCKETS}
        for m in msgs:
            for b in BUCKETS:
                sub[b] += m["tokens"][b]
                per_file_total[b] += m["tokens"][b]
            per_file_count += 1
            if m["dedup_key"] not in global_seen:
                global_seen.add(m["dedup_key"])
                for b in BUCKETS:
                    global_total[b] += m["tokens"][b]
                global_count += 1
        print(f"{path.parent.name[:24]:<26}{len(msgs):>10}{sum(sub.values()):>14,}")

    print("-" * 50)
    print(f"{'per-file fold':<26}{per_file_count:>10}{sum(per_file_total.values()):>14,}")
    print(f"{'+ cross-file dedup':<26}{global_count:>10}{sum(global_total.values()):>14,}")
    print("\nper bucket (cross-file deduped)")
    print("-" * 50)
    for b in BUCKETS:
        print(f"  {b:<24}{global_total[b]:>14,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

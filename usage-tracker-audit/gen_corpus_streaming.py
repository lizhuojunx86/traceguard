#!/usr/bin/env python3
"""Generate a synthetic Claude Code SUBAGENT corpus with streaming-partial usage.

Companion to gen_corpus.py (which writes the main-transcript shape: identical
usage on every line of a message). This generator writes the other shape that
real `subagents/**` transcripts carry, measured on a live corpus of 12,131
multi-line subagent groups with differing usage:

- input / cache_creation / cache_read identical on every line (98.3-99.9%)
- output_tokens monotone nondecreasing across lines (95.4%)
- first line's output near zero (<= 3 in 65.9% of groups)
- lines per group mostly 2-3 (histogram {2: 5868, 3: 4147, 4: 1288, ...})

Consequence: a dedup that keeps the FIRST record per (message.id, requestId)
freezes the near-zero partial and loses most output tokens (95.3% on the
corpus above); keeping the last (or per-field max) is correct. The manifest
records totals under both conventions so a fixture can assert the gap.

Usage:
    python3 gen_corpus_streaming.py --home /path/to/fake-home [--seed 20260731]

Creates:
    <home>/.claude/projects/<slug>/<sessionId>/subagents/agent-<id>.jsonl
    <home>/manifest.json

Stdlib only. No network. Touches nothing outside <home>.
"""

from __future__ import annotations

import argparse
import json
import os
import random
from datetime import datetime, timedelta, timezone

# Lines-per-message distribution from the measured differing-group histogram.
LINE_WEIGHTS = [(2, 5868), (3, 4147), (4, 1288), (5, 236), (6, 334), (7, 107), (8, 107)]

MODELS = [
    "claude-opus-4-8-20260514",
    "claude-sonnet-4-6-20260219",
    "claude-haiku-4-5-20251001",
]

PROJECTS = ["-Users-dev-alpha", "-Users-dev-beta"]


def _hex(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def _msg_id(rng: random.Random) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "msg_01" + "".join(rng.choice(alphabet) for _ in range(22))


class Totals:
    """Ground truth under both dedup conventions."""

    FIELDS = ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens")

    def __init__(self) -> None:
        self.messages = 0
        self.lines = 0
        self.last = {k: 0 for k in self.FIELDS}    # last-record-wins == correct
        self.first = {k: 0 for k in self.FIELDS}   # first-record-wins == the bug

    def add(self, per_line: list[dict]) -> None:
        self.messages += 1
        self.lines += len(per_line)
        for k in self.FIELDS:
            self.last[k] += per_line[-1][k]
            self.first[k] += per_line[0][k]


def _usage_sequence(rng: random.Random, n_lines: int) -> list[dict]:
    """n_lines usage snapshots: fixed input/cache, output grows to its final value."""
    inp = rng.randint(1, 40)
    cc = rng.randint(0, 30000)
    cr = rng.randint(0, 90000)
    final_out = rng.randint(50, 4000)
    # first snapshot near zero, then monotone growth to final_out
    outs = sorted(rng.randint(0, final_out) for _ in range(n_lines - 2))
    seq = [rng.randint(0, 3)] + outs + [final_out] if n_lines > 1 else [final_out]
    return [
        {"input_tokens": inp, "output_tokens": o,
         "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr}
        for o in seq
    ]


def write_subagent_transcript(
    path: str, rng: random.Random, session_id: str, agent_id: str,
    n_messages: int, totals: Totals, start: datetime,
) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    counts, weights = zip(*LINE_WEIGHTS)
    ts = start
    lines: list[str] = []

    for _ in range(n_messages):
        ts += timedelta(seconds=rng.randint(2, 40))
        lines.append(json.dumps({
            "type": "user", "uuid": _hex(rng, 32), "sessionId": session_id,
            "isSidechain": True,
            "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "message": {"role": "user", "content": "synthetic subagent prompt"},
        }))

        mid = _msg_id(rng)
        request_id = "req_01" + _hex(rng, 16)
        model = rng.choice(MODELS)
        k = rng.choices(counts, weights=weights, k=1)[0]
        seq = _usage_sequence(rng, k)
        totals.add(seq)

        for i, usage in enumerate(seq):
            ts += timedelta(milliseconds=rng.randint(40, 700))
            block = ({"type": "thinking", "thinking": "synthetic reasoning"} if i == 0 and k > 1
                     else {"type": "text", "text": "synthetic subagent text"})
            lines.append(json.dumps({
                "type": "assistant", "uuid": _hex(rng, 32), "sessionId": session_id,
                "requestId": request_id, "isSidechain": True, "agentId": agent_id,
                "timestamp": ts.isoformat().replace("+00:00", "Z"),
                "message": {"id": mid, "role": "assistant", "model": model,
                            "content": [block], "usage": usage},
            }))

    lines.insert(len(lines) // 2, "{not valid json")
    lines.append(json.dumps({"type": "summary", "summary": "synthetic", "leafUuid": _hex(rng, 32)}))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True, help="fake $HOME to populate")
    ap.add_argument("--seed", type=int, default=20260731)
    ap.add_argument("--sessions-per-project", type=int, default=3)
    ap.add_argument("--subagents-per-session", type=int, default=3)
    ap.add_argument("--messages-per-agent", type=int, default=20)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    projects_dir = os.path.join(args.home, ".claude", "projects")
    os.makedirs(projects_dir, exist_ok=True)

    totals = Totals()
    start = datetime(2026, 7, 1, 9, 0, 0, tzinfo=timezone.utc)
    n_files = 0

    for slug in PROJECTS:
        for _ in range(args.sessions_per_project):
            session_id = "-".join(
                [_hex(rng, 8), _hex(rng, 4), _hex(rng, 4), _hex(rng, 4), _hex(rng, 12)]
            )
            for _ in range(args.subagents_per_session):
                agent_id = "agent-" + _hex(rng, 17)
                write_subagent_transcript(
                    os.path.join(projects_dir, slug, session_id, "subagents", f"{agent_id}.jsonl"),
                    rng, session_id, agent_id, args.messages_per_agent, totals, start,
                )
                n_files += 1
            start += timedelta(hours=6)

    manifest = {
        "generator": "gen_corpus_streaming.py",
        "seed": args.seed,
        "files": {"subagent_transcripts": n_files},
        "distinct_assistant_messages": totals.messages,
        "assistant_jsonl_lines_with_usage": totals.lines,
        "correct_last_record_wins": totals.last,
        "if_first_record_wins": totals.first,
    }
    with open(os.path.join(args.home, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    lost = totals.last["output_tokens"] - totals.first["output_tokens"]
    pct = 100 * lost / max(totals.last["output_tokens"], 1)
    print(f"corpus: {n_files} subagent transcripts under {projects_dir}")
    print(f"  distinct assistant messages   : {totals.messages:,}")
    print(f"  assistant lines with usage    : {totals.lines:,}")
    print(f"  output (last-record-wins)     : {totals.last['output_tokens']:,}")
    print(f"  output (first-record-wins)    : {totals.first['output_tokens']:,}")
    print(f"  output lost under first-wins  : {lost:,} ({pct:.1f}%)")


if __name__ == "__main__":
    main()

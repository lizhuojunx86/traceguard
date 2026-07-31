#!/usr/bin/env python3
"""Generate a synthetic Claude Code corpus with a known-exact usage manifest.

The point of the corpus is one specific, verifiable property of real Claude Code
transcripts: **a single assistant message is written as several JSONL lines --
one per content block (thinking / text / tool_use) -- and every one of those
lines repeats the *same* `message.id` and a byte-identical `message.usage`
object.**

Measured on a real ~50-day corpus (78 main transcripts, 8,123 distinct assistant
message ids): 5,590 ids appear on more than one line, and for 5,590 of 5,590
(100.0%) every line carries identical usage. The per-id line-count histogram is
{1: 2533, 2: 1351, 3: 3553, 4: 415, 5: 58, 6: 138, 7: 24, 8: 33}; this generator
reproduces that shape.

Consequence: any tool that sums `message.usage` per *line* rather than per
*message id* reports each message's tokens once per content block. The manifest
written here records both numbers, so a checker can tell the two apart.

Usage:
    python3 gen_corpus.py --home /path/to/fake-home [--seed 20260725]

Creates:
    <home>/.claude/projects/<slug>/<sessionId>.jsonl                        (main)
    <home>/.claude/projects/<slug>/<sessionId>/subagents/agent-<id>.jsonl   (subagent)
    <home>/manifest.json                                                    (ground truth)

Stdlib only. No network. Touches nothing outside <home>.
"""

from __future__ import annotations

import argparse
import json
import os
import random
from datetime import datetime, timedelta, timezone

# Line-count-per-message distribution, taken from the real corpus histogram above.
BLOCK_WEIGHTS = [(1, 2533), (2, 1351), (3, 3553), (4, 415), (5, 58), (6, 138), (7, 24), (8, 33)]

MODELS = [
    "claude-opus-4-8-20260514",
    "claude-sonnet-4-6-20260219",
    "claude-haiku-4-5-20251001",
]

PROJECTS = ["-Users-dev-alpha", "-Users-dev-beta", "-Users-dev-gamma"]


def _hex(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def _msg_id(rng: random.Random) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "msg_01" + "".join(rng.choice(alphabet) for _ in range(22))


def _usage(rng: random.Random) -> dict:
    return {
        "input_tokens": rng.randint(1, 40),
        "output_tokens": rng.randint(20, 4000),
        "cache_creation_input_tokens": rng.randint(0, 30000),
        "cache_read_input_tokens": rng.randint(0, 90000),
    }


def _blocks(rng: random.Random, n: int) -> list[dict]:
    """n content blocks for one assistant message; each becomes its own JSONL line."""
    out: list[dict] = []
    for i in range(n):
        kind = "thinking" if i == 0 and n > 1 else ("tool_use" if i == n - 1 and n > 2 else "text")
        if kind == "thinking":
            out.append({"type": "thinking", "thinking": "synthetic reasoning block"})
        elif kind == "tool_use":
            out.append(
                {
                    "type": "tool_use",
                    "id": "toolu_01" + _hex(rng, 16),
                    "name": rng.choice(["Read", "Bash", "Grep"]),
                    "input": {"pattern": "synthetic"},
                }
            )
        else:
            out.append({"type": "text", "text": "synthetic assistant text"})
    return out


class Totals:
    """Ground truth accumulator."""

    def __init__(self) -> None:
        self.messages = 0            # distinct assistant message ids
        self.lines = 0               # assistant JSONL lines carrying usage
        self.inp = self.out = self.cc = self.cr = 0        # counted once per message id
        self.n_inp = self.n_out = self.n_cc = self.n_cr = 0  # counted once per line

    def add(self, usage: dict, n_lines: int) -> None:
        self.messages += 1
        self.lines += n_lines
        self.inp += usage["input_tokens"]
        self.out += usage["output_tokens"]
        self.cc += usage["cache_creation_input_tokens"]
        self.cr += usage["cache_read_input_tokens"]
        self.n_inp += usage["input_tokens"] * n_lines
        self.n_out += usage["output_tokens"] * n_lines
        self.n_cc += usage["cache_creation_input_tokens"] * n_lines
        self.n_cr += usage["cache_read_input_tokens"] * n_lines


def write_transcript(
    path: str,
    rng: random.Random,
    session_id: str,
    n_messages: int,
    totals: Totals,
    start: datetime,
    sidechain: bool,
) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    counts, weights = zip(*BLOCK_WEIGHTS)
    ts = start
    lines: list[str] = []

    for _ in range(n_messages):
        ts += timedelta(seconds=rng.randint(3, 90))
        # a user turn
        user = {
            "type": "user",
            "uuid": _hex(rng, 32),
            "sessionId": session_id,
            "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "message": {"role": "user", "content": "synthetic user turn"},
        }
        if sidechain:
            user["isSidechain"] = True
        lines.append(json.dumps(user))

        # one assistant message, emitted as k lines sharing id + usage
        mid = _msg_id(rng)
        usage = _usage(rng)
        model = rng.choice(MODELS)
        request_id = "req_01" + _hex(rng, 16)
        k = rng.choices(counts, weights=weights, k=1)[0]
        blocks = _blocks(rng, k)
        totals.add(usage, k)

        tool_use_id = None
        for block in blocks:
            ts += timedelta(milliseconds=rng.randint(50, 900))
            rec = {
                "type": "assistant",
                "uuid": _hex(rng, 32),
                "sessionId": session_id,
                "requestId": request_id,
                "timestamp": ts.isoformat().replace("+00:00", "Z"),
                "message": {
                    "id": mid,
                    "role": "assistant",
                    "model": model,
                    "content": [block],
                    "usage": dict(usage),  # identical on every line, as in real transcripts
                },
            }
            if sidechain:
                rec["isSidechain"] = True
                rec["agentId"] = "agent-" + _hex(rng, 17)
            lines.append(json.dumps(rec))
            if block["type"] == "tool_use":
                tool_use_id = block["id"]

        # a tool_result user turn, so the tool-correlation path is exercised
        if tool_use_id:
            ts += timedelta(seconds=1)
            lines.append(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": _hex(rng, 32),
                        "sessionId": session_id,
                        "timestamp": ts.isoformat().replace("+00:00", "Z"),
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_use_id,
                                    "content": "synthetic tool result",
                                }
                            ],
                        },
                    }
                )
            )

    # records a correct implementation must ignore: malformed line, and a
    # summary line with no usage
    lines.insert(len(lines) // 2, "{not valid json")
    lines.append(json.dumps({"type": "summary", "summary": "synthetic", "leafUuid": _hex(rng, 32)}))

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True, help="fake $HOME to populate")
    ap.add_argument("--seed", type=int, default=20260725)
    ap.add_argument("--sessions-per-project", type=int, default=3)
    ap.add_argument("--messages-per-session", type=int, default=40)
    ap.add_argument("--subagents-per-session", type=int, default=2)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    projects_dir = os.path.join(args.home, ".claude", "projects")
    os.makedirs(projects_dir, exist_ok=True)

    main_totals = Totals()
    sub_totals = Totals()
    start = datetime(2026, 7, 1, 9, 0, 0, tzinfo=timezone.utc)
    n_main = n_sub = 0

    for slug in PROJECTS:
        for _ in range(args.sessions_per_project):
            session_id = "-".join(
                [_hex(rng, 8), _hex(rng, 4), _hex(rng, 4), _hex(rng, 4), _hex(rng, 12)]
            )
            write_transcript(
                os.path.join(projects_dir, slug, f"{session_id}.jsonl"),
                rng, session_id, args.messages_per_session, main_totals, start, sidechain=False,
            )
            n_main += 1
            for _ in range(args.subagents_per_session):
                agent_id = "agent-" + _hex(rng, 17)
                write_transcript(
                    os.path.join(projects_dir, slug, session_id, "subagents", f"{agent_id}.jsonl"),
                    rng, session_id, max(args.messages_per_session // 4, 4), sub_totals, start,
                    sidechain=True,
                )
                n_sub += 1
            start += timedelta(hours=6)

    def block(t: Totals) -> dict:
        return {
            "distinct_assistant_messages": t.messages,
            "assistant_jsonl_lines_with_usage": t.lines,
            "correct": {
                "input_tokens": t.inp,
                "output_tokens": t.out,
                "cache_creation_input_tokens": t.cc,
                "cache_read_input_tokens": t.cr,
                "total_input_plus_output": t.inp + t.out,
            },
            "if_summed_per_line": {
                "input_tokens": t.n_inp,
                "output_tokens": t.n_out,
                "cache_creation_input_tokens": t.n_cc,
                "cache_read_input_tokens": t.n_cr,
                "total_input_plus_output": t.n_inp + t.n_out,
            },
        }

    combined = Totals()
    for t in (main_totals, sub_totals):
        combined.messages += t.messages
        combined.lines += t.lines
        combined.inp += t.inp; combined.out += t.out; combined.cc += t.cc; combined.cr += t.cr
        combined.n_inp += t.n_inp; combined.n_out += t.n_out
        combined.n_cc += t.n_cc; combined.n_cr += t.n_cr

    manifest = {
        "generator": "gen_corpus.py",
        "seed": args.seed,
        "files": {"main_transcripts": n_main, "subagent_transcripts": n_sub},
        "main": block(main_totals),
        "subagents": block(sub_totals),
        "all": block(combined),
    }
    with open(os.path.join(args.home, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    a = manifest["all"]
    print(f"corpus: {n_main} main + {n_sub} subagent transcripts under {projects_dir}")
    print(f"  distinct assistant messages : {a['distinct_assistant_messages']:,}")
    print(f"  assistant lines with usage  : {a['assistant_jsonl_lines_with_usage']:,}")
    print(f"  correct input+output tokens : {a['correct']['total_input_plus_output']:,}")
    print(f"  if summed per line          : {a['if_summed_per_line']['total_input_plus_output']:,}")


if __name__ == "__main__":
    main()

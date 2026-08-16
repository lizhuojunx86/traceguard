#!/usr/bin/env python3
"""How often does a real Claude Code `tool_result` carry explicit token counts?

The #1037 parser fix stops tokscale from char-estimating `tool_result` input
tokens. Correcting the *already cached* rows raises a design question: a
migration can either drop the stale tool_result rows outright, or re-derive
them by re-parsing the live transcript. Re-deriving is the safer of the two
only if a real transcript can carry explicit tool-result tokens — otherwise the
two produce the same result and the cheaper one is also correct.

This probe answers that empirically. It mirrors, on the corpus, exactly what
`explicit_tool_result_input_tokens` reads (`sessions/claudecode.rs:1217`):

    input_tokens | token_count | tokens | usage.input_tokens
    tool_output.{input_tokens, token_count, tokens, usage.input_tokens}

over exactly the values `claude_tool_result_values` collects (`:1154`), and
counts how many would yield a number.

It also reports id coverage. `extract_claude_tool_result_usage` (`:1122`) keys
its row on the first tool_result id it finds — `tool_use_id`, `id` or
`tool_result_id` (`:1201`) — and mints a *keyless* row when a record carries
none of them. A migration that filters cached rows by their `tool_result:`
dedup key would not reach a keyless row, so its count is the boundary of that
filter.

Read-only: opens transcripts, writes nothing.

Usage:
    ./explicit_tokens_probe.py --src ~/.claude/projects
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ID_FIELDS = ("tool_use_id", "id", "tool_result_id")


def tool_result_values(record: dict):
    """Every value `claude_tool_result_values` would collect from a record."""
    values = []
    if record.get("type") == "tool_result":
        values.append(record)
    if record.get("tool_result") is not None:
        values.append(record["tool_result"])
    message = record.get("message")
    if isinstance(message, dict) and message.get("tool_result") is not None:
        values.append(message["tool_result"])

    content = None
    if isinstance(message, dict) and message.get("content") is not None:
        content = message["content"]
    elif record.get("content") is not None:
        content = record["content"]
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                values.append(block)
    return values


def explicit_tokens(value) -> int | None:
    """What `explicit_tool_result_input_tokens` would return for this value."""
    if not isinstance(value, dict):
        return None

    candidates = [value.get("input_tokens"), value.get("token_count"), value.get("tokens")]
    usage = value.get("usage")
    if isinstance(usage, dict):
        candidates.append(usage.get("input_tokens"))
    tool_output = value.get("tool_output")
    if isinstance(tool_output, dict):
        candidates += [
            tool_output.get("input_tokens"),
            tool_output.get("token_count"),
            tool_output.get("tokens"),
        ]
        nested = tool_output.get("usage")
        if isinstance(nested, dict):
            candidates.append(nested.get("input_tokens"))

    for candidate in candidates:
        if isinstance(candidate, bool):
            continue
        if isinstance(candidate, (int, float)):
            return int(candidate)
        if isinstance(candidate, str):
            try:
                return int(candidate.strip())
            except ValueError:
                continue
    return None


def has_id(value) -> bool:
    if not isinstance(value, dict):
        return False
    return any(isinstance(value.get(field), str) and value[field] for field in ID_FIELDS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=Path("~/.claude/projects"))
    ap.add_argument("--show-shapes", type=int, default=5, help="how many key sets to print")
    args = ap.parse_args()

    src = args.src.expanduser()
    if not src.is_dir():
        print(f"source not a directory: {src}", file=sys.stderr)
        return 2

    n_files = 0
    n_with_results = 0
    n_records = 0
    n_values = 0
    n_explicit = 0
    n_keyless_values = 0
    n_keyless_records = 0
    n_bad_lines = 0
    shapes: Counter[str] = Counter()
    examples: list[str] = []

    for path in sorted(src.rglob("*.jsonl")):
        n_files += 1
        seen_here = False
        try:
            fh = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if "tool_result" not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    n_bad_lines += 1
                    continue
                if not isinstance(record, dict):
                    continue

                values = tool_result_values(record)
                if not values:
                    continue
                seen_here = True
                n_records += 1
                n_values += len(values)

                ids = [has_id(value) for value in values]
                n_keyless_values += ids.count(False)
                if not any(ids):
                    n_keyless_records += 1

                for value in values:
                    if isinstance(value, dict):
                        shapes[",".join(sorted(value.keys()))] += 1
                    tokens = explicit_tokens(value)
                    if tokens is not None:
                        n_explicit += 1
                        if len(examples) < 5:
                            examples.append(f"{path}: {json.dumps(value)[:200]}")
        if seen_here:
            n_with_results += 1

    print(f"corpus                          : {src}")
    print(f"transcripts scanned             : {n_files:,}")
    print(f"  containing tool_result        : {n_with_results:,}")
    print(f"malformed lines skipped         : {n_bad_lines:,}")
    print()
    print(f"records minting a tool_result row : {n_records:,}")
    print(f"tool_result values              : {n_values:,}")
    print(f"  yielding explicit tokens      : {n_explicit:,}")
    print(f"  lacking every id field        : {n_keyless_values:,}")
    print(f"records with no id anywhere     : {n_keyless_records:,}  (would mint a keyless row)")
    print()
    print("key sets seen on tool_result values:")
    for keys, count in shapes.most_common(args.show_shapes):
        print(f"  {count:>9,}  {{{keys}}}")

    if examples:
        print()
        print("values that would yield explicit tokens:")
        for example in examples:
            print(f"  {example}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

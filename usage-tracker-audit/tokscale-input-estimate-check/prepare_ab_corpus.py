#!/usr/bin/env python3
"""Build the A/B corpus for the tokscale `input` char-estimate measurement.

A = the real Claude Code transcripts, untouched.
B = the same transcripts with every `tool_result` block's content emptied.

Emptying the content zeroes `estimate_tokens_from_chars` (chars.div_ceil(4))
and changes nothing else about the records — same messages, same message ids,
same API-reported usage. Whatever `input` drops by between A and B is the
estimate's contribution.

Also computes the independent mechanism prediction: ceil(chars/4) summed over
unique (session, tool_use_id) pairs. If the A-B delta and this prediction agree
to within the tool's own cross-file dedup, the difference is the estimate and
not some unrelated side effect of the rewrite.

Usage:
    ./prepare_ab_corpus.py --src ~/.claude/projects --work /path/to/workdir
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from pathlib import Path


def iter_tool_results(record: dict):
    """Yield every tool_result block in a transcript record."""
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            yield block


def content_chars(block: dict) -> int:
    """Character count tokscale's fallback would see for this tool_result.

    The content is either a plain string or a list of blocks; for the list
    form the text payloads are what carry the characters.
    """
    content = block.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    total += len(text)
            elif isinstance(part, str):
                total += len(part)
        return total
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--work", type=Path, required=True)
    args = ap.parse_args()

    src = args.src.expanduser().resolve()
    work = args.work.expanduser().resolve()
    intact = work / "home-intact" / ".claude" / "projects"
    emptied = work / "home-emptied" / ".claude" / "projects"

    if not src.is_dir():
        print(f"source not a directory: {src}", file=sys.stderr)
        return 2

    print(f"==> copying intact corpus -> {intact}")
    intact.parent.mkdir(parents=True, exist_ok=True)
    if intact.exists():
        shutil.rmtree(intact)
    shutil.copytree(src, intact)

    print(f"==> writing emptied corpus -> {emptied}")
    if emptied.exists():
        shutil.rmtree(emptied)
    emptied.mkdir(parents=True)

    # (session, tool_use_id) -> chars, deduped the way the estimate would be
    # if the tool deduped it; both the deduped and raw sums are reported.
    unique: dict[tuple[str, str], int] = {}
    raw_chars = 0
    n_files = 0
    n_results = 0
    n_bad_lines = 0

    for path in sorted(intact.rglob("*.jsonl")):
        rel = path.relative_to(intact)
        out_path = emptied / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        session = path.stem
        n_files += 1

        with path.open(encoding="utf-8") as fh, out_path.open("w", encoding="utf-8") as out:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    out.write(line)
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    n_bad_lines += 1
                    out.write(line)  # malformed lines pass through untouched
                    continue

                touched = False
                for block in iter_tool_results(record):
                    n_results += 1
                    chars = content_chars(block)
                    raw_chars += chars
                    tool_use_id = block.get("tool_use_id")
                    if isinstance(tool_use_id, str):
                        unique[(session, tool_use_id)] = chars
                    block["content"] = ""
                    touched = True

                out.write(json.dumps(record, ensure_ascii=False) + "\n" if touched else line)

    dedup_estimate = sum(math.ceil(c / 4) for c in unique.values() if c)
    raw_estimate = math.ceil(raw_chars / 4)

    print()
    print(f"transcripts            : {n_files}")
    print(f"tool_result blocks     : {n_results}")
    print(f"unique (session, id)   : {len(unique)}")
    print(f"malformed lines        : {n_bad_lines}")
    print(f"predicted estimate     : {dedup_estimate:,} tokens (deduped by session+tool_use_id)")
    print(f"                         {raw_estimate:,} tokens (raw, no dedup)")
    print()
    print("Next: run tokscale against each fake HOME and diff the `input` field.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

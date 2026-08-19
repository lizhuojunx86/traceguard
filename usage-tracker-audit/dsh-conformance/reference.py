"""Reference folds for DeepSeek Harness session logs. Stdlib only.

Four folds and three gap terms. The folds are what an implementation might
plausibly do; the gap terms are what separates them, and naming each one is
what turns a failing total into a diagnosis.

  naive      every usage sighting summed, including compaction
  official   transcription of tokenUsageProjectionDefinition.apply
             (packages/llm/token-meter/src/usage-projection.ts at 47f9438)
  seed_aware official, but skipping events inherited through a fork seed
  corrected  seed_aware, plus an attempt boundary on a failed terminal chunk,
             plus compaction/summary.usage

The attempt boundary follows yha9806's 63688b0: a finish chunk whose
reason.kind is error or aborted clears the replacement slot when it names the
open sample's own (turn, step). Deliberately NOT generalised to `failure in
reason` -- AgentLoop only emits error | aborted today, and a third failure kind
still assembles an assistant message, so testing the field would double count.
See deepseek-ai/deepseek-harness#1886.

BUCKETS is the wire order for every total reported by this suite.
"""

from __future__ import annotations

import json
from pathlib import Path

BUCKETS = ("uncachedInputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")

# usage keys as DSH writes them, in BUCKETS order
_USAGE_KEYS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")

FAILED_FINISH_KINDS = ("error", "aborted")


def zero() -> dict:
    return {b: 0 for b in BUCKETS}


def buckets_from(u: dict) -> dict:
    return {b: int(u.get(k) or 0) for b, k in zip(BUCKETS, _USAGE_KEYS)}


def add(dst: dict, src: dict) -> dict:
    for b in BUCKETS:
        dst[b] += src[b]
    return dst


def sub(dst: dict, src: dict) -> dict:
    for b in BUCKETS:
        dst[b] -= src[b]
    return dst


def total(d: dict) -> int:
    return sum(d[b] for b in BUCKETS)


def load(path: Path) -> tuple[dict, list]:
    """Return (header, events). The first record is the session header."""
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # A torn trailing frame is a real condition on compressed logs.
            # Keep the decodable prefix rather than discarding the session.
            break
    if not records:
        return {}, []
    return records[0], records[1:]


def own_events(header: dict, events: list) -> list:
    """D-2. seedLength is a seq threshold, not a count of anything else.

    Never filter on origin: an ordinary user-created fork inherits a prefix
    too, and it carries no origin == 'subagent'.
    """
    seed_length = int(header.get("seedLength") or 0)
    return [e for e in events if int(e.get("seq", 0)) >= seed_length]


def usage_of(ev: dict) -> dict | None:
    """What a chunk or finalized message reports for its step, if anything."""
    t = ev.get("type")
    d = ev.get("data") or {}
    if t == "assistant/chunk":
        chunk = d.get("chunk") or {}
        return chunk.get("usage") if chunk.get("type") == "usage" else None
    if t == "assistant/message":
        return d.get("usage")
    return None


def _is_failed_finish(ev: dict) -> bool:
    if ev.get("type") != "assistant/chunk":
        return False
    chunk = (ev.get("data") or {}).get("chunk") or {}
    if chunk.get("type") != "finish":
        return False
    return (chunk.get("reason") or {}).get("kind") in FAILED_FINISH_KINDS


def compaction_usage(events: list) -> dict:
    """D-3. Provider-reported cost of each summarize call.

    Typed in packages/compaction/compaction/src/types.ts, written at
    compaction-basic/src/region.ts:447. usageOf() does not match it, so the
    official projection never folds it. `usage` is optional: absent stays
    absent rather than becoming a zero.
    """
    totals = zero()
    for ev in events:
        if ev.get("type") != "compaction/summary":
            continue
        u = (ev.get("data") or {}).get("usage")
        if u:
            add(totals, buckets_from(u))
    return totals


def fold_naive(events: list) -> dict:
    """See a usage, add it. Includes compaction, because a naive walker has no
    reason to skip it -- which is why naive is not uniformly higher."""
    totals = zero()
    for ev in events:
        u = usage_of(ev)
        if u is None and ev.get("type") == "compaction/summary":
            u = (ev.get("data") or {}).get("usage")
        if u is not None:
            add(totals, buckets_from(u))
    return totals


def fold_official(events: list) -> dict:
    """Transcription of the official projection. Single `last` slot; a repeated
    (turn, step) REPLACES rather than adding; an identical repeat is a no-op."""
    totals = zero()
    last = None
    for ev in events:
        u = usage_of(ev)
        if u is None:
            continue
        d = ev.get("data") or {}
        turn, step = d.get("turn"), d.get("step")
        b = buckets_from(u)
        previous = last[2] if (last and last[0] == turn and last[1] == step) else None
        if previous is not None and previous == b:
            continue
        if previous is not None:
            sub(totals, previous)
        add(totals, b)
        last = (turn, step, b)
    return totals


def fold_corrected(events: list) -> dict:
    """official + attempt boundary (D-4) + compaction (D-3). Feed it own
    events only (D-2) and it is the fold this suite calls correct."""
    totals = zero()
    last = None
    for ev in events:
        if _is_failed_finish(ev):
            d = ev.get("data") or {}
            if last and last[0] == d.get("turn") and last[1] == d.get("step"):
                last = None
            continue
        u = usage_of(ev)
        if u is None:
            continue
        d = ev.get("data") or {}
        turn, step = d.get("turn"), d.get("step")
        b = buckets_from(u)
        previous = last[2] if (last and last[0] == turn and last[1] == step) else None
        if previous is not None and previous == b:
            continue
        if previous is not None:
            sub(totals, previous)
        add(totals, b)
        last = (turn, step, b)
    add(totals, compaction_usage(events))
    return totals


def superseded_attempts(events: list) -> dict:
    """D-4's cost, isolated: usage the official fold subtracts back out when a
    later sample lands on the same (turn, step) after a failed attempt.

    Zero on a log with no retries, and zero on a log whose failed attempts
    reported nothing. That second case is why a corpus can pass D-5's narrow
    form by luck.

    The boundary is remembered as the (turn, step) it belongs to, never as a
    bare flag. A flag survives into unrelated steps: a step whose attempt died
    with no retry after it leaves the flag raised, and the next ordinary
    chunk-then-message pair anywhere in the log then looks like a superseded
    attempt. That bug read 2,244 tokens off a corpus whose failed attempts both
    reported zeros.
    """
    totals = zero()
    last = None       # (turn, step, buckets)
    boundary = None   # the (turn, step) whose attempt was closed by a failure
    for ev in events:
        if _is_failed_finish(ev):
            d = ev.get("data") or {}
            key = (d.get("turn"), d.get("step"))
            if last is not None and (last[0], last[1]) == key:
                boundary = key
            continue
        u = usage_of(ev)
        if u is None:
            continue
        d = ev.get("data") or {}
        key = (d.get("turn"), d.get("step"))
        b = buckets_from(u)
        same = last is not None and (last[0], last[1]) == key
        if same and boundary == key:
            # official replaces the dead attempt here; corrected keeps both.
            add(totals, last[2])
            boundary = None
        elif same and last[2] == b:
            continue
        if boundary is not None and boundary != key:
            boundary = None
        last = (key[0], key[1], b)
    return totals


def inherited_double_count(header: dict, events: list) -> dict:
    """D-2's cost, isolated: what the official fold counts inside the seed."""
    seed = [e for e in events if int(e.get("seq", 0)) < int(header.get("seedLength") or 0)]
    return fold_official(seed) if seed else zero()


def analyze(path: Path) -> dict:
    header, events = load(path)
    own = own_events(header, events)
    return {
        "path": str(path),
        "sessionId": header.get("id"),
        "parentSession": header.get("parentSession"),
        "seedLength": int(header.get("seedLength") or 0),
        "naive": fold_naive(events),
        "official": fold_official(events),
        "seed_aware": fold_official(own),
        "corrected": fold_corrected(own),
        "gap_compaction": compaction_usage(own),
        "gap_superseded": superseded_attempts(own),
        "gap_inherited": inherited_double_count(header, events),
    }


def discover(root: Path) -> list:
    root = Path(root)
    if root.is_file():
        return [root]
    return sorted(root.rglob("session.jsonl"))


def aggregate(root: Path) -> dict:
    keys = ("naive", "official", "seed_aware", "corrected",
            "gap_compaction", "gap_superseded", "gap_inherited")
    agg = {k: zero() for k in keys}
    reports = [analyze(p) for p in discover(root)]
    for r in reports:
        for k in keys:
            add(agg[k], r[k])
    agg["sessions"] = len(reports)
    return agg

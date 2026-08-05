# clawdmeter-dedup

End-to-end check for one question: does [Clawdmeter](https://github.com/weltern/Clawdmeter)
count each Claude Code assistant message **once**, or **once per content-block
record**?

Answer at commit `7dd0b7b` (v3.0.0, 2026-08-03): once per record. Running
Clawdmeter's own functions over a synthetic corpus whose exact totals are known,
every transcript-derived figure equals the per-record sum to the token.

```
./run_check.sh          # red on current main
```

Requires git and python3. No PySide6 install — the checker stubs `QtCore`, and
the token paths are stdlib-only. Builds a synthetic `$HOME`; touches no real
`~/.claude` data.

## Result

Corpus: 540 distinct assistant messages across 1,249 records carrying usage.

| call site | what it feeds | reported | truth | ratio |
|---|---|---|---|---|
| `_file_token_events` → `account_window_tokens` | token counts beside the 5h/7d bars | 2,592,168 | 1,108,697 | 2.34× |
| `scan_events` rows | Stats page: API value, value by model/project | 2,592,168 | 1,108,697 | 2.34× |
| `_SessionTail.tokens.work` | per-session total on the shelf (main transcripts) | 1,737,381 | 732,191 | 2.37× |

Both record-level paths emit 1,249 events for 540 messages, which is the
invariant that needs no corpus: the number of usage events should equal the
number of distinct `message.id`s.

Not affected: the percentage bars themselves (rate-limit headers) and the
overage figure (OAuth usage endpoint). Only the transcript-derived numbers
inflate.

## Why

Claude Code writes one assistant *message* as several JSONL *records* — one per
content block (thinking / text / tool_use) — and every record repeats the same
`message.id` with a byte-identical `usage` object. Summing per record multiplies
each message's usage by its block count.

`src/transcript.py` at `7dd0b7b` has no occurrence of `message.id`, `requestId`
or `isSidechain`. The comment above `add_usage` in `_consume_event` states the
reasoning: "Token usage rides EVERY assistant turn (text/thinking turns too, not
just tool calls)". Those are not separate turns.

Corpus shape, generator and the real ~50-day measurement behind it:
[`../cct-dedup-check`](../cct-dedup-check) (8,123 distinct ids, 68.8% on more
than one record, identical usage on 100% of those, ≈2.36×).

## Files

| file | what |
|---|---|
| `run_check.sh` | clone pinned commit → generate corpus → run the check |
| `check_clawdmeter.py` | imports `src/transcript.py`, calls the three paths, compares against the manifest |
| `ISSUE_DRAFT.md` | upstream issue text |
| `REDDIT_REPLY_DRAFT.md` | reply on the v3.0.0 announcement thread |

The corpus generator is shared with `cct-dedup-check` rather than copied.

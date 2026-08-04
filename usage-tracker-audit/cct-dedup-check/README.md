# cct-dedup-check

End-to-end check for one question: does
[claude-code-templates](https://github.com/davila7/claude-code-templates)'
analytics count each Claude Code assistant message **once**, or **once per
content-block line**?

Answer at commit `e54d3cd` (2026-07-24): once per line. On a synthetic corpus
whose exact totals are known, every reported field equals the per-line sum to
the token — 2.36× the ground truth for input+output.

Write-up: [The vendor documents this bug. A 30k-star repo shipped it
anyway.](https://dev.to/lizhuojunx86/the-vendor-documents-this-bug-a-30k-star-repo-shipped-it-anyway-27pb)
· Upstream fix: [PR #754](https://github.com/davila7/claude-code-templates/pull/754)

## Why the corpus looks the way it does

Claude Code writes one assistant *message* as several JSONL *lines* — one per
content block (thinking / text / tool_use). Every one of those lines repeats
the same `message.id` and a **byte-identical** `message.usage` object.

Measured on a real ~50-day corpus (78 main transcripts, 8,123 distinct
assistant message ids):

| property | value |
|---|---|
| ids appearing on exactly one line | 2,533 |
| ids appearing on more than one line | 5,590 (**68.8%**) |
| of those, ids where every line's `usage` is identical | 5,590 (**100.0%**) |
| lines-per-id histogram | `{1: 2533, 2: 1351, 3: 3553, 4: 415, 5: 58, 6: 138, 7: 24, 8: 33}` |
| assistant lines with usage ÷ distinct ids | **≈2.36×** |

(2,533 single-line ids + 5,590 multi-line ids = the 8,123 distinct ids above.
The histogram's visible buckets account for 8,105 of them; the remaining 18 sit
in a ≥9-line tail that is not printed.)

So summing per line multiplies each message's usage by its block count exactly.
`gen_corpus.py` reproduces that histogram, plus a `subagents/**` subtree, a
`tool_use` → `tool_result` pair, a malformed line and a usage-less summary
record.

## Run it

```bash
./run_check.sh              # clone pinned commit, install 2 deps, generate, run
./run_check.sh --keep       # keep the work dir for inspection
./run_check.sh --commit HEAD
```

Requires git, node ≥ 18, npm, python3 (stdlib only). Everything is written
under a temp work directory; **no real `~/.claude` data is read or touched**,
no config, no upload path.

Exit code `0` when reported totals equal the manifest, `1` when they do not —
so the same script doubles as a regression test after a fix.

## What the harness does and does not do

`run_check.js` requires `ConversationAnalyzer` straight out of an upstream
checkout and calls its real `loadConversations()`, which does its own recursive
file discovery, its own parsing and its own `calculateRealTokenUsage()`. The
harness supplies only the directory, a two-method `stateCalculator` stub (used
upstream purely for status labels), and the comparison. No accounting is
reimplemented here.

## Result at `e54d3cd`

```
transcripts discovered      : 27  (manifest: 9 main + 18 subagent)
distinct assistant messages : 540
assistant lines with usage  : 1,308
messagesWithUsage reported  : 1,308

field                 reported        correct    if summed per line   reported/correct
input_tokens            27,678         11,447                27,678             x2.418
output_tokens        2,632,674      1,115,321             2,632,674             x2.360
cache_creation      19,609,374      8,130,148            19,609,374             x2.412
cache_read          59,626,489     24,447,923            59,626,489             x2.439
total (in+out)       2,660,352      1,126,768             2,660,352             x2.361
```

The reported `messagesWithUsage` (1,308) is itself the line count rather than
the message count (540) — the same discrepancy, visible without any external
reference.

Upstream report: [PR #754](https://github.com/davila7/claude-code-templates/pull/754).

## Context

Part of a small audit series run from TraceGuard's `routing_audit` module — an
append-only, `message.id`-keyed ingest of Claude Code transcripts used as a
stable reference log. The same method has produced four shipped upstream
fixes — three in [splitrail](https://github.com/Piebald-AI/splitrail)
([#200](https://github.com/Piebald-AI/splitrail/issues/200),
[#207](https://github.com/Piebald-AI/splitrail/issues/207),
[#220](https://github.com/Piebald-AI/splitrail/issues/220)) and one in
[tokscale](https://github.com/junhoyeo/tokscale)
([#994](https://github.com/junhoyeo/tokscale/issues/994), shipped in
[v4.9.0](https://github.com/junhoyeo/tokscale/releases/tag/v4.9.0)); see
[`../../splitrail-validation/`](../../splitrail-validation/) and
[`../tokscale-drift-check/`](../tokscale-drift-check/).

# tokscale-drift-check

End-to-end check for one question: do [tokscale](https://github.com/junhoyeo/tokscale)'s
historical totals survive a Claude Code resume/compact rewrite, or are they
recomputed from whatever the live session files currently say?

Answer at npm `tokscale@4.7.0` (2026-07-31): recomputed. Remove 6 assistant
messages from one transcript in place — the observable effect of a real
resume/compact — and every reported field drops by precisely those messages'
usage. History drifts silently, and since tokscale feeds a public leaderboard
(`tokscale submit`), submitted numbers drift with it.

To be equally clear about what tokscale gets **right**: its per-message dedup
is correct. Cold start on the intact corpus matches the ground-truth manifest
token-exact on output / cacheRead / cacheWrite and message count — including
transcripts under `subagents/**` — so this is purely a persistence gap, not an
accounting bug. (Two siblings in this space already ship the fix shape:
splitrail's append-only SQLite history store, added in 3.6.0 for
[#200](https://github.com/Piebald-AI/splitrail/issues/200), and codeburn's
"never-lose" daily cache.)

## What the check does

1. `gen_corpus.py` (shared with [`../cct-dedup-check/`](../cct-dedup-check/))
   writes a synthetic Claude Code corpus — 9 main + 18 subagent transcripts,
   540 distinct assistant messages, streaming multi-line records with repeated
   `message.id` + identical usage, a malformed line, a usage-less summary
   record — plus a manifest with the exact per-field totals.
2. The published npm package (pinned version, no build step) runs three times
   against an isolated fake `$HOME`: cold, warm (cache exercise), and after
   `simulate_rewrite.py` (adapted from
   [`../../splitrail-validation/`](../../splitrail-validation/)) rewrites the
   largest main transcript in place, dropping the last 6 messages.
3. `compare_totals.py` asserts on the token-exact fields (output / cacheRead /
   cacheWrite / messageCount) and prints the verdict.

`input` is deliberately not asserted: tokscale adds a ~4-chars/token estimate
for tool payloads on top of the API-reported `input_tokens` (+6 tokens per
tool_use message on this corpus, +16% overall — a separate, minor quirk worth
its own footnote, not this check's subject).

## Run it

```bash
./run_check.sh                    # install pinned tokscale, generate, run 3x, compare
./run_check.sh --keep             # keep the work dir for inspection
./run_check.sh --version 4.7.0    # pin a different npm version
./run_check.sh --drop 12          # drop more messages in the rewrite
```

Requires node ≥ 18 + npm on PATH, python3 (stdlib only). Network for the npm
install and tokscale's LiteLLM pricing fetch. Everything is written under a
temp work directory; **no real `~/.claude` data is read or touched**, and
nothing is submitted anywhere.

Exit code `0` when totals survive the rewrite (history frozen — a fixed
version passes), `1` when they drift, `2` on any unexpected mismatch — so the
same script doubles as a regression test after a fix.

## Result at `tokscale@4.7.0`

```
A. cold start vs ground truth (exact fields)
   output       tokscale=   1,115,321  manifest=   1,115,321  OK
   cacheRead    tokscale=  24,447,923  manifest=  24,447,923  OK
   cacheWrite   tokscale=   8,130,148  manifest=   8,130,148  OK
   messageCount tokscale=         540  manifest=         540  OK

B. warm re-run, corpus unchanged: identical

C. after in-place rewrite (-6 messages, 18 lines)
   output       before=   1,115,321  after=   1,104,872  (drifted prediction:   1,104,872)
   cacheRead    before=  24,447,923  after=  24,192,633  (drifted prediction:  24,192,633)
   cacheWrite   before=   8,130,148  after=   8,052,730  (drifted prediction:   8,052,730)
   messageCount before=         540  after=         534

FAIL  every exact field dropped by precisely the vanished messages' usage.
```

The match to the drifted prediction is exact on every field, which pins the
cause to recompute-from-live-files rather than anything in parsing — the same
inference splitrail's fixture used.

## Context

Part of a small audit series run from TraceGuard's `routing_audit` module — an
append-only, `message.id`-keyed ingest of Claude Code transcripts used as a
stable reference log. Prior entries: two upstream fixes in
[splitrail](https://github.com/Piebald-AI/splitrail) (3.6.1), a merged e2e
regression fixture there ([#208](https://github.com/Piebald-AI/splitrail/pull/208)),
and the per-line double-count report against claude-code-templates
([`../cct-dedup-check/`](../cct-dedup-check/)).

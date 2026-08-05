<!--
Upstream: https://github.com/junhoyeo/tokscale/issues/new
Title: Claude: `input` adds a char estimate of tool_result content on top of API-reported input_tokens
Invited by junhoyeo in #1008 ("deserves its own issue rather than a rider here").
Before posting: re-measure against current main and update the sha + numbers if they moved.
Post as lizhuojunx86.
-->

# Claude: `input` adds a char estimate of tool_result content on top of API-reported input_tokens

## Summary

For Claude Code transcripts, `input` is not the API-reported number. Every
`tool_result` block without explicit token metadata gets
`estimate_tokens_from_chars` — `chars.div_ceil(4)`
(`sessions/claudecode.rs:1118-1126`, `:1206`) — and that estimate is added to
`input`. Claude Code never writes token metadata on tool_result blocks, so the
fallback fires on all of them.

The tool result is then sent back to the model as part of the next turn's
prompt, and that turn's API-reported `input_tokens` / `cache_creation_input_tokens`
/ `cache_read_input_tokens` already account for it. The same content is counted
twice: once by the API, once by the estimate.

This is the hazard your own suppression rule names. The bare-transcript branch
(`claudecode.rs:~458-463`) refuses the char estimate because it "would
double-count usage already tracked by the originating client's own parser."
Same double count, different route — here the originating parser is the API's
own usage field.

## Measured

1,618 transcripts, measured against merged `9ceae64` built from source, isolated
`HOME` per run. A/B: the same corpus with every `tool_result` content emptied,
which zeroes the estimate and changes nothing else.

| | intact | tool_result content emptied |
|---|---|---|
| `input` | 24,793,081 | 3,960,326 |
| `cost` | $7,331.39 | $7,215.86 |

So **20,832,755 of 24,793,081 reported input tokens (84%) are the estimate**,
across 40,068 unique tool results — a 6.26× inflation of the field.

**The cost impact is small: 1.6%** ($115.53). Input tokens are cheap next to
cache and output on this corpus, so the dollar figure barely moves. The token
figure is the misleading one, and it is the one a "tokens used" display shows.

Mechanism check, so the numbers aren't just a black-box difference: computing
`ceil(chars/4)` over unique `(session, tool_use_id)` externally predicts
24,968,754 against the 24,793,081 actually reported — 0.71% apart, the residual
being your cross-file dedup.

## Minimal repro

One assistant message, one `tool_use`, one `tool_result` whose content is 21
characters, `usage.input_tokens = 7`:

```
reported input = 13   (7 from the API + ceil(21/4) = 6)
```

## Possible directions

Yours to pick — both keep explicit tool-result token metadata honored, which is
the case the fallback was written for:

1. Extend the rule you already apply to bare transcripts: for Claude Code
   project transcripts the API reports the number, so the char fallback has
   nothing to add.
2. Keep the estimate but report it in its own field, so `input` stays
   API-reported and anything comparing against an API bill still matches.

## Scope

Claude lane only. I have not looked at whether the Kiro fallback this borrows
from has the same property in its own client, and this says nothing about it.

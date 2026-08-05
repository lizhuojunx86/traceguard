<!--
Follow-up comment for https://github.com/junhoyeo/tokscale/issues/1011
(open, zero replies since 2026-08-03). Post as lizhuojunx86.
Re-measured 2026-08-05 against fc7b26f built from source.
-->

Re-measured on current `main` (`fc7b26f`, which includes #1038) against a fresh
corpus. Still reproduces, and the share is higher than in the original report.

**A/B: same corpus, `tool_result` content emptied.** Emptying the content
zeroes `estimate_tokens_from_chars` and changes nothing else — same records,
same message ids, same API-reported usage. 1,504 transcripts, release binary
built from source, isolated `HOME` per run.

| | intact | tool_result emptied | delta |
|---|---|---|---|
| `input` | 22,778,313 | 2,817,117 | **19,961,196** |
| `output` | 35,532,363 | 35,532,363 | 0 |
| `cacheRead` | 6,955,517,271 | 6,955,517,271 | 0 |
| `cacheWrite` | 228,474,039 | 228,474,039 | 0 |
| `messageCount` | 32,045 | 32,045 | 0 |
| `cost` | $7,068.21 | $6,967.54 | $100.67 |

So **87.6% of reported input tokens are the estimate, an 8.09× inflation** of
the field. Every other field is identical across the pair, which is what says
the intervention isolated the estimate rather than perturbing something else.

**Mechanism check.** Computing `ceil(chars/4)` over unique
`(session, tool_use_id)` pairs externally predicts 19,963,236 against the
19,961,196 measured. 0.010% apart.

**Cost impact stays small: $100.67, or 1.42%.** Input is cheap next to cache
and output here, so the dollar figure barely moves. The token figure is the one
that misleads, and it is the one a "tokens used" display shows.

**Line numbers moved** since the original report: `estimate_tokens_from_chars`
is now at `sessions/claudecode.rs:1233`, reached from `:1151` in
`extract_tool_result_input_tokens`. The gate is still
`allow_char_estimate: !is_bare_transcript` at `:563`, so the fallback fires on
every Claude Code project transcript.

#1038 doesn't touch this one. It stops a `<synthetic>` placeholder from
inheriting attribution, which is a different path; a normal tool result still
gets its char estimate added on top of the API-reported number.

Reproduction harness (A/B builder + comparison, real transcripts required):
https://github.com/lizhuojunx86/traceguard/tree/main/usage-tracker-audit/tokscale-input-estimate-check

Happy to run the same A/B against a fix branch if that's useful.

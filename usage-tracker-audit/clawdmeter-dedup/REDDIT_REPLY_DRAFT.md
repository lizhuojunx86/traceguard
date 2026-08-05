# Draft — reply to weltern's Clawdmeter v3.0.0 post

Target: https://www.reddit.com/r/ClaudeAI/comments/1vfe9s9/ (crossposts:
r/ClaudeCode `1vfm1iy`, r/BuildWithClaude `1vfm15u` — reply on r/ClaudeAI only).

Post this **after** the GitHub issue exists. Status: draft, not posted.
Measured 2026-08-05 against Clawdmeter `7dd0b7b` (v3.0.0).

---

Nice work on the mascot. I audit token counters, so I pointed my harness at the
token path rather than the UI, and it reads about 2.34x high.

Claude Code writes one assistant message as several JSONL records, one per
content block (thinking / text / tool_use), and every one of those records
repeats the same `message.id` with a byte-identical `usage` object. Summing per
record counts each message once per block. On my real ~50-day corpus, 8,123
distinct assistant message ids, 68.8% of them land on more than one record, and
on every single one of those the repeated usage is byte-identical, so it
multiplies rather than rounds.

I ran your own functions over a synthetic corpus with a known-exact manifest
(540 distinct messages, 1,249 records carrying usage):

- `_file_token_events` — the counts beside the 5h/7d bars: 2,592,168 vs
  1,108,697 true, 2.34x, 1,249 events for 540 messages
- `scan_events` rows — what the Stats page prices: same 2,592,168, so "API value
  this month" and value-by-model/project inherit it
- `_SessionTail.tokens.work` — the per-session total on the shelf: 1,737,381 vs
  732,191 on the main transcripts, 2.37x

The percentage bars themselves are fine, since those come off the rate-limit
headers, and the overage figure comes from the OAuth endpoint. It's the
transcript-derived numbers that inflate.

The comment above `add_usage` in `_consume_event` has the reasoning in it:
"Token usage rides EVERY assistant turn (text/thinking turns too, not just tool
calls)". Those aren't separate turns, they're one message split across records.
Anthropic's Agent SDK cost-tracking guide says it in a warning box: always
deduplicate by ID.

Fix is a few lines at each of the three sites, collapse by `message.id` before
summing. The check that needs no corpus: the number of usage events should equal
the number of distinct message ids, not the number of records.

I opened an issue on the repo with the repro, since CONTRIBUTING asks for that
first. It's self-contained, runs in about a minute, builds a synthetic $HOME and
touches nothing in your real `~/.claude`.

---

## Notes for me

- No dev.to link in the body — keep it a GitHub/measurement conversation and
  avoid whatever filter ate 1vfb2i1.
- No issue number in the text, so nothing to edit after filing.
- Follow-up if he engages: offer the PR (three sites, namespaced key, one test).
- Fourth tracker in the series if it lands, and the first measured by driving
  the tool's own functions rather than a fixture of mine.

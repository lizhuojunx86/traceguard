# §7b draft — the number my own tool was not producing

Status: **draft prose, 2026-08-08, second pass.** The first pass had two
findings: the opus-5 pricing gap, and a cache-split defect said to be worth
$1,393.49. Measuring the second one returned $0.00 and showed the claim behind
it was backwards, so it is gone. What replaces it is 190 words about having
nearly published it, and the generalisable half of that belongs in §10 rather
than here — see the notes at the end.

## Re-verify before publishing

Checked against `traces_routing_audit.db` on 2026-08-08 at 13:13. Results:

| figure | claim | measured | verdict |
|---|---|---|---|
| opus-5 rows | 5,992 | **5,992**, all now priced | ✅ |
| opus-5 value | $1,213.85 | **$1,213.91** | ✅ 6c off, quote the measured one |
| 1h-multiplier undercount | $1,393.49 | **$0.00** — claim is backwards | ❌ **RETRACTED** |
| cache shape | "58,194 flat, 0 nested" | **34,234 nested, 0 flat** | ❌ **RETRACTED** |
| the $88.75 recompute batch | "the cache fix" | 3,907 of 3,918 rows were opus-5 | ⚠️ second-order opus-5 correction, unrelated |
| 318 `<synthetic>` messages | — | API-error records, 0 output tokens, never ingested | ✅ correctly excluded |
| store total | $12,588.23 | **$13,804.76** | ⚠️ restate everywhere |
| 24 rows, −69,714 / +6,977 | reconciliation counts | not reproducible | ❌ dropped from the docstring, do not quote |

**Settled, 2026-08-08.** `settle_cache_split.py` drove `compute_cost_usd`
across `~/.claude/projects` twice, with and without the flat cache keys:
34,234 distinct messages, 142,957,275 one-hour cache tokens, delta **$0.00**,
and **34,234 of 34,234 records carry the nested shape**. The docstring had it
backwards. The 2× branch always fired and there was no $1,393.49.

The $88.75 recompute batch was never the cache fix either — 3,907 of its 3,918
rows were opus-5, a second-order correction to the pricing that landed an hour
earlier. It reconciles: $1,127.77 from the first batch plus ~$86 from the
second is the $1,213.91 opus-5 total now in the store.

Corrections are committed (`a7f4f54`), so `pricing.py` no longer asserts any of
this. Both figures remain visible in the public history at `3804b6a`, which is
the right place for them.

**Not closeable.** `~/.claude/projects` is a rolling window and the store holds
rows from before it starts, whose transcripts are gone; `traces` keeps no
`usage` block, so they cannot be re-derived. If the flat shape ever existed it
existed there. Say "nothing surviving supports it", never "it was never true".

The Opus 5 price itself **is** verified: $5/MTok input, $25/MTok output, cache
write 1.25× / 2×, cache read 0.1×, fast mode 2×. Platform pricing page,
2026-08-08, cross-checked against the 2026-07-24 announcement.

---

## 7b. A number my own tool was not producing

Three posts of this series found other people's counters producing wrong
numbers. This one produced a missing one, in my own tool, for two weeks. I
found it while fact-checking this post rather than from an alert — and the
same fact-check killed the second finding I had lined up beside it.

### A model my price sheet had never heard of

`claude-opus-5` first appears in my store on 2026-07-25. By the time I went
looking it was 5,992 traces, 10.3% of the corpus, and `cost_usd` was NULL on
every single one.

That NULL is correct behaviour. `compute_cost_usd` returns None when a model
has no price entry, because the alternative is inventing a number. I would
write it the same way again.

The consequence is not correct. $1,213.85 of list-price spend left every total
as a zero rather than as an error. Nothing summed it, nothing flagged it, and
no report said "10% of your traces are unpriced." The model was also absent
from `routing_policy.yaml`'s tier list, so those traces could be scored neither
compliant nor deviant. Two weeks of a tenth of my usage sitting outside the
audit entirely, in the tool whose whole job is to notice that kind of thing.

It happened again while I was writing this. Between the first read for this
post and the last, the scheduled ingest ran and added 678 traces. The archive's
cost total moved by ninety-one cents.

| | first read | snapshot | delta |
|---|---|---|---|
| traces | 57,532 | 58,210 | +678 |
| priced cost | $12,587.32 | $12,588.23 | +$0.91 |
| opus-5 traces | 5,347 | 5,992 | +645 |

645 of the 678 were opus-5. The defect demonstrated itself, unprompted, inside
the window in which it was being written up.

### A second finding, which did not survive being checked

There was going to be a bigger one here. A comment in my own pricing module
said local transcripts carry a flat cache-split shape, that 58,194 of 58,210
rows had it and none had the nested one, that the 2× one-hour multiplier was
therefore a constant no code path could reach, and that the store was
$1,393.49 short because of it. Four numbers in a row. It reads like a
measurement. I wrote it, and a day later I was drafting it as the strongest
paragraph in this post.

So I measured it, the same way this series measures other people's tools:
drive their function instead of reimplementing it. Call `compute_cost_usd`
twice per message, once with the flat keys and once with them stripped, since
stripping them reproduces the old path exactly. Across 34,234 messages
carrying 142,957,275 one-hour cache tokens, the difference was **$0.00**. The
claim is backwards — 34,234 of 34,234 records carry the *nested* shape. The
branch always fired. There was no $1,393.49.

The comment had sat there for a day in a repository with 430 passing tests.
All 430 still pass. None of them assert anything about which shape production
sends, and the fixtures used the nested shape while the comment above them
said flat — a fixture agrees with whatever you build it from.

One limit I cannot close: `~/.claude/projects` is a rolling window, and the
rows in my store from before it starts have no transcripts left. If the flat
shape was ever written, it was written there. I cannot show the claim was
never true, only that nothing surviving supports it.

### What generalises

A tracker's dangerous state is not "wrong". It is **absent, and typed as
zero**. A wrong number invites an argument and someone eventually wins it. A
NULL that sums as zero invites nothing at all.

The opus-5 gap was catchable by a check needing no corpus and no fixture, which
is the same thing part 3 said about `messagesWithUsage`:

```sql
SELECT count(*) FROM traces WHERE tokens_out > 0 AND cost_usd IS NULL;
```

It would have fired on 2026-07-25. It did not exist. It does now.

I will not pretend the audit log caught this. It did not. What it gave me was
the ability to say how much and since when, once I was already looking — which
is the same thing it gave me about other people's tools in parts 1 through 3,
and exactly as much as it is worth.

---

## Notes for the surrounding sections

**§10 is where the lesson goes**, not §7. The incident is stated above in
about 190 words and does not need restating; §10 takes one bullet, phrased as
a claim about code rather than a confession:

- **A code comment is documentation, not measurement.** Part 3 argued that
  stacking more reading never becomes a measurement, and pointed that at
  vendor docs, stars and AI review. It applies inside your own repository
  too. Mine asserted a distribution over 58,210 rows that nobody had counted,
  and it was wrong in the direction that made the story better. Tests do not
  catch this: a fixture agrees with whatever you build it from.

**§9 keeps ONE new line, and drops two existing ones.** The outline already
carries eight counterweights; part 3 carried six. Adding a ninth turns
honesty into a tic and readers start skipping the section, which costs more
than the admission buys.

- Add: *A finding I had drafted for §7 did not survive being checked, and the
  check was one I only ran because it had to go in a post.*
- Drop: *§5a's 50% is arithmetic* (already killed on the page in §5a, so the
  counterweight is a repeat) and *22.6% is not a benchmark* (fold its one
  useful clause — a reader whose subagents are all pinned should get 0% —
  into §3, where it lands as a fact rather than as an apology).

**§3 and every total in the piece.** Do not quote a corpus-wide cost without
saying which models are priced. $12,588.23 was the pre-opus-5 figure; the
store now reads $13,804.76. The cache split does not move any total — that was
the retracted claim — so say nothing about the 1-hour rate.

**Two populations, and the piece must not mix them.** The store holds 58,210
traces back to 2026-05-30. `~/.claude/projects` today holds 34,234 distinct
messages. Any "% of the corpus" needs to name which. The gap between them is
the rolling window, which is §7a's subject arriving a second time.

**Timeline** gains: `Jul 24` Opus 5 announced · `Jul 25` first opus-5 trace,
unpriced · `Aug 8` price verified, PRICES and tier list updated, opus-5 rows
repriced ($1,213.91), flat-shape claim measured and retracted, NULL-cost check
added.

**Do not claim** (append to the outline's list): *do not attribute any money
to the flat cache-shape support.* It is insurance against a shape this corpus
does not contain. The measured value is $0.00.

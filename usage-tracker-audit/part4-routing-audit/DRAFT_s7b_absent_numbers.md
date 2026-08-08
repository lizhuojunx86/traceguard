# §7b draft — the numbers my own tool was not producing

Status: **draft prose, 2026-08-08.** Replaces the single-finding §7b in the
outline, which only had the opus-5 gap. The cache-split defect landed in the
working tree the same day and is the stronger of the two.

## Re-verify before publishing

Checked against `traces_routing_audit.db` on 2026-08-08 at 13:13. Results:

| figure | claim | measured | verdict |
|---|---|---|---|
| opus-5 rows | 5,992 | **5,992**, all now priced | ✅ |
| opus-5 value | $1,213.85 | **$1,213.91** | ✅ 6c off, quote the measured one |
| 1h-multiplier undercount | $1,393.49 | **$0.00** — claim is backwards | ❌ **RETRACTED** |
| cache shape | "58,194 flat, 0 nested" | **34,234 nested, 0 flat** | ❌ **RETRACTED** |
| the $88.75 recompute batch | "the cache fix" | 3,907 of 3,918 rows were opus-5 | ⚠️ second-order opus-5 correction, unrelated |
| store total | $12,588.23 | **$13,804.76** | ⚠️ pre-fix, restate everywhere |
| 58,194 flat / 0 nested | — | not checkable from the DB | ⚠️ see below |
| 24 rows, −69,714 / +6,977 | — | not checkable from the DB | ⚠️ see below |

**The $1,393.49 does not survive.** Two reprice batches ran on 2026-08-08:
`rp-…030632` priced 5,992 opus-5 rows at $1,127.77, and `rp-…041211`
recomputed 3,918 already-priced rows for a net **$88.75**. That second batch is
the cache-split correction as actually applied to the store, and it is 15.7×
smaller than the figure in the `pricing.py` docstring. (The two batches
reconcile with each other: $1,127.77 + ~$86 of opus-5 rows caught by the
recompute = the $1,213.91 opus-5 total now in the store.)

One of two things is true and the article cannot ship until it is known which:

1. **$1,393.49 was wrong** — an estimate taken before the reconciliation rule
   capped 1-hour tokens at the billable total, so it priced a premium on tokens
   that were never billable.
2. **$88.75 is not the whole fix** — `traces` stores only `tokens_in`,
   `tokens_out` and `cost_usd`, not the raw `usage` block, so a recompute over
   the store cannot see `cache_creation_1h` at all. If so the real correction
   needs a re-ingest from the source JSONL, and it has not happened yet.

The second reading also explains why the two shape figures above are
uncheckable from the DB: that evidence only exists in the transcripts. Settle
this with a re-ingest into a scratch DB and diff the totals. Until then the
whole "constant no code path could reach" section rests on one unverified
number, which is the exact failure this series is about.

The Opus 5 price itself **is** verified: $5/MTok input, $25/MTok output, cache
write 1.25× / 2×, cache read 0.1×, fast mode 2×. Platform pricing page,
2026-08-08, cross-checked against the 2026-07-24 announcement.

---

## 7b. Two numbers my own tool was not producing

Three posts of this series found other people's counters producing wrong
numbers. This one produced missing ones, in my own tool, and I found both
while fact-checking this post rather than from an alert.

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

### A constant no code path could reach — RETRACTED, see below

**This whole subsection is dead. Kept only so the retraction below has
something to point at. Do not publish anything between here and "What
generalises" — the replacement section follows it.**

The second one is worse, and it is worse in a way I like less.

Cache-creation tokens are billed at two rates: 1.25× the input price for a
5-minute TTL, 2× for an hour. My price table has had both multipliers since it
was written. The 2.0 was never wrong.

It was also never used. Not once, on any record, ever.

The splitter read only the shape the Messages API documents,
`cache_creation.ephemeral_5m_input_tokens` and its 1-hour sibling. Local Claude
Code JSONL writes a different shape: flat `cache_creation_5m` and
`cache_creation_1h` keys. Of the 58,210 rows in my store, 58,194 carry the flat
form and **zero** carry the nested one. So the "no split reported" fallback
fired on every record, every 1-hour cache write was billed at the 5-minute
rate, and the store came out **$1,393.49 low**.

Note the shape of that bug relative to the first one. The opus-5 gap was a
missing entry, and a missing entry at least leaves a NULL behind. This one
produced a number for every row. Each one looked fine. The total was wrong by
more than the entire opus-5 gap, and there was nothing anywhere to notice,
because a dead constant leaves no trace at all — it is not a value that is
absent, it is a branch that never runs.

Fixing it turned up a third thing I would rather not have found.
`cache_creation_input_tokens` is the billable total; the split describes how
that total is composed. On 24 of 58,194 rows they disagree, and they disagree
in both directions: one split under-reports its own total by 69,714 tokens,
another over-reports by 6,977. Trust the split and you silently drop 274,513
tokens off the bill. Trust the total alone and you throw away the 1-hour
premium. So the total wins on quantity and the split wins on composition: cap
the 1-hour figure at the total, give the rest to 5-minute. No token invented,
none dropped, and the 58,170 rows where the two agree are untouched.

### The number I nearly published

Everything in the retracted subsection above came from a comment I wrote in my
own pricing module. It said the local transcripts carry a flat cache-split
shape, that 58,194 of 58,210 rows had it and zero had the nested one, that the
2× one-hour multiplier was therefore a constant no code path could reach, and
that the store was $1,393.49 short as a result.

It reads like a measurement. It is four specific numbers in a row. I wrote it,
and a day later I was drafting it into this post as the strongest finding in
the section.

Then I measured it. The method is the one this series has used on four other
people's tools: drive the vendor's own function rather than reimplement it.
Call `compute_cost_usd` twice per message, once with the flat keys as written
and once with them stripped — stripping them reproduces the old code path
exactly, because the old code read only the nested shape and fell back to
"treat everything as 5-minute TTL" when it was absent. The difference between
the two totals is what the fix is worth, with no second implementation to
disagree with.

Across 34,234 distinct messages carrying 142,957,275 one-hour cache tokens:

| | |
|---|---|
| total, as written | $7,712.97 |
| total, pre-fix | $7,712.97 |
| **the fix is worth** | **$0.00** |

Zero. Not small — zero, to the cent. Because the shape claim is backwards:
**34,234 of 34,234 records carry the nested form and none carry the flat
one.** The nested branch always fired. The 2× multiplier was never
unreachable. There was no $1,393.49.

The honest limit, which cuts the other way: `~/.claude/projects` is a rolling
window and my store holds rows back to 2026-05-30 whose transcripts are gone.
If the flat shape was ever written it was written there, and `traces` keeps no
`usage` block, so those rows cannot be re-derived. I cannot prove the claim was
never true. I can only say nothing that survives supports it, which is a
weaker sentence than the one I nearly shipped and the only one I am entitled
to.

What made this catchable was not rigour. It was that the number had to go in a
post, and things that go in posts get checked. The comment had sat in the
module for a day, in a repository with 430 passing tests, none of which
assert anything about which shape production sends. The test fixtures used the
nested shape while the comment above them said production used flat, and every
test passed, because a fixture agrees with whatever you build it from.

I want to be plain about the shape of this, because it is the same shape as
part 3's. There I wrote that documentation describes hazards and measurement
finds instances, and that stacking more reading does not produce a
measurement. A code comment is documentation. Mine asserted a distribution over
58,210 rows that nobody had counted, and it was wrong in the direction that
made the story better.

### What generalises

A tracker's dangerous state is not "wrong". It is **absent, and typed as
zero**. A wrong number invites an argument and someone eventually wins it. A
NULL that sums as zero invites nothing at all, and a branch that never executes
invites less than that.

Both of these were catchable by a check that needs no corpus and no fixture,
which is the same thing part 3 said about `messagesWithUsage`:

```sql
SELECT count(*) FROM traces WHERE tokens_out > 0 AND cost_usd IS NULL;
```

would have fired on 2026-07-25. And for the second:

```sql
SELECT count(*) FROM traces WHERE json_extract(usage,'$.cache_creation_1h') > 0;
```

against a bill that has never once applied the 1-hour rate is the same
contradiction, one join away. Neither existed. Both do now.

I will not pretend the audit log caught these. It did not. What it gave me was
the ability to say how much and since when, once I was already looking — which
is the same thing it gave me about other people's tools in parts 1 through 3,
and exactly as much as it is worth.

---

## Notes for the surrounding sections

**§9 counterweights** needs two more lines:

- *The audit found its own blind spot two weeks late, by hand, not by alarm.*
  Already in the outline. Now applies twice.
- *One of the two was invisible to the method as well as to the alarm.* A dead
  branch produces no anomalous number, so no amount of reconciling totals
  against an append-only log would have surfaced it. It came out of reading the
  vendor's cache documentation against my own splitter. Reading is not
  measuring, and this is the case where reading won.

**§3 and every total in the piece** move once the fix is committed. Do not
quote a corpus-wide cost without saying which models are priced and whether
the 1-hour rate is being applied — the number at the 2026-08-08 snapshot,
$12,588.23, is pre-fix on both counts.

**Timeline** gains: `Jul 24` Opus 5 announced · `Jul 25` first opus-5 trace,
unpriced · `Aug 8` price verified, PRICES and tier list updated, cache split
fixed, both checks added.

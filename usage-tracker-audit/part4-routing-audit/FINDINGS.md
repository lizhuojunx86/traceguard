# Part 4 — data state

**Snapshot: 2026-08-08 01:31 UTC (09:31 HKT).** Every figure below is pinned to
that read. The database is live — see §7, which is not an aside.

Read against `report_en.md`, generated 2026-07-04 18:59 HKT.

DB: `traces_routing_audit.db`, opened read-only (`mode=ro`). Nothing here wrote
to it. Every figure carries the query that produced it, except where a line
explicitly says otherwise.

---

## 0. One hypothesis tested and refuted

**Claim tested:** `report_en.md` §3 was computed against a pre-v1
`routing_policy.yaml`, so 22.6% and $1,248.13 are stale.

**Refuted.** The `Effective: 2026-07-05` line inside the yaml is a forward-dated
label, not a write date.

```
git log --follow -- .../routing_policy.yaml
  6617b96  2026-07-04 16:04:14 +0800  routing_policy v1 — lizhuojun final review (2026-07-05)
  299ef30  2026-07-03 08:38:27 +0800  routing_audit policy-deviation audit + counterfactuals
```

The decisions batch `dec-20260704T105940Z` ran at 2026-07-04 **18:59:40 HKT**,
two hours fifty-five minutes after the v1 commit. v1's two new rules are visible
in the stored rows:

```sql
select expected_tier, count(*) from routing_decisions
 where component='main' and task_type='research-explore' group by 1;   -- frontier | 53
select expected_tier, count(*) from routing_decisions
 where component='general-purpose' group by 1;                          -- mid      | 23
```

Both are v1 values (`general-purpose` was `cheap` in the draft). And the DB
reproduced §3 to the digit: **425 decisions, 96 deviations, 22.6%, $1,248.13**,
with all seven per-task_type rows matching.

**§3 is publishable as written.**

> **As of 2026-08-08 13:15 this reproduction lives only in the frozen copy.**
> `generate --write` has since run (batch `dec-20260808T051350Z-de7773`) and the
> live table is now 784 / 161 / 20.5% / $1,945.2579. Use
> `traces_routing_audit.2026-07-04-audit.db` for anything that must reproduce
> §3, and read its README first — it preserves `routing_decisions`, not
> `traces.cost_usd`.

---

## 1. What `[PENDING: policy-review]` actually gates

Not §3.

```sql
select source, outcome, count(*), sum(deviation) from routing_decisions group by 1,2;
--  generated | unknown | 329 | 0
--  manual    | unknown |  96 | 96
select count(*) from routing_decisions where reason is not null and reason != '';  -- 96
```

All 96 deviations already carry `source='manual'` with `reason` populated — the
deviations CSV was imported. `outcome` is `unknown` on all 425 rows; nothing is
marked `adopted`. §6's class A / B / C filing and its per-prescription impact
numbers are the open item.

---

## 2. The corpus has more than doubled, and part of the growth landed *inside* the reported window

| | traces | priced cost |
|---|---|---|
| `report_en.md` states | 26,131 | $6,281.98 |
| DB at snapshot (through 2026-08-07 17:52 HKT) | **58,210** | **$12,588.23** |

"Priced" is load-bearing — see §3.

The daily launchd ingest never stopped. Evidence is the batch series in
`routing_audit_ingest_log`, not a log file:

```sql
select batch_id, count(*), max(ingested_at) from routing_audit_ingest_log
 group by 1 order by 3 desc limit 3;
--  cc-20260807T191705Z-1dc0ce | 678  | 2026-08-07 19:17:05
--  cc-20260806T191725Z-244e6b | 162  | 2026-08-06 19:17:25
--  cc-20260805T191714Z-2748fb | 1120 | 2026-08-05 19:17:16
```

The measurement worth publishing is not the growth. It is this:

```sql
select count(*), round(sum(t.cost_usd),2)
from traces t join routing_audit_ingest_log l on l.trace_id = t.trace_id
where t.invoked_at < :cut and l.ingested_at >= :cut;
--  cut = 2026-07-05 -> 3098 | 917.33
--  cut = 2026-07-06 -> 3658 | 1194.06
--  cut = 2026-07-10 -> 4796 | 1573.23
```

**3,098 traces dated before 2026-07-05 were first ingested after 2026-07-05,
carrying $917.33.** Most arrived in three batches: `cc-20260720T145607Z` (9,422
rows), `cc-20260730T055702Z` (5,593), `cc-20260801T012536Z` (2,879).

The mechanism — resume and compact rewriting session files in place, so a
session still open on 4 July grows records afterwards that carry their original
timestamps — is **not measured here.** It is the hypothesis `report_en.md` §1
records as Evidence 1, and it is consistent with this observation. What is
measured here is only the arrival lag.

The append-only ingest log is the only reason the lag can be stated as a number
at all. A store that overwrites on re-read shows a window that quietly changed
size, with nothing recording that it did.

---

## 3. NEW AND BROKEN — `claude-opus-5` is unpriced and untiered

First seen 2026-07-25 02:06:28, still arriving at the snapshot.

| | |
|---|---|
| traces | **5,992** (10.3% of corpus) |
| `cost_usd` | **NULL on every one** |
| entry in `pricing.py` `PRICES` | **absent** |
| entry in `routing_policy.yaml` `tiers` | **absent** |
| components | main 4,007 · workflow-subagent 1,985 |
| tokens | out 5,771,444 · cache_read 1,525,928,583 · cc-1h 22,969,204 · cc-5m 12,275,634 · in 36,215 |
| speed mix | 100% standard |

`compute_cost_usd` returns `None` when a model has no price entry — correct
behaviour, it never guesses. The consequence is not: these traces disappear from
every total silently, as NULL, rather than loudly as an error.

**Estimated list-price value of the missing slice: $1,213.85** — main $971.45,
workflow-subagent $242.40. Computed at $5/$25 per MTok with the tool's own cache
multipliers (0.1× read, 1.25× 5m, 2× 1h) and its fast-mode rule.

> ⚠ **The $5/$25 is from secondary sources, not verified.** Search results say
> Opus 5 is priced identically to Opus 4.8 ($5/$25, cache read $0.50, fast mode
> 2×, batch half, 1M context, no long-context premium). `pricing.py` holds itself
> to "verified against the platform pricing page" — this figure has not met that
> bar. **Verify before it enters the price sheet or the article.**

So the true corpus cost is roughly **$13,802** against a published $12,588. The
missing slice is **8.8% of the true total**, and it is invisible.

Also unpriced, and harmless: 596 traces with `model_id` NULL (zero tokens) and
2 opus-4-8 traces with no usage block. Total unpriced rows: 6,590.

**This is the same failure class this series documents in other people's
trackers, found in mine.** An unpriced model does not show up as an error. It
shows up as a zero.

---

## 3b. NEW — the time-aligned inheritance test, run over all 96 deviations

`report_en.md` §6 verified the lead prescription on 19 fable deviations and left
the rest "presumed". The same test now runs over all 96.

For each deviation, ask what the parent session's main thread ran **inside that
unit's own time window** (`[ts_start, ts_end)`, unbounded for a session's last
segment):

| verdict | n | cost |
|---|---|---|
| main ran **exclusively** the subagent's model | **95** | **$1,248.10** |
| main **never** on the subagent's model | 1 | $0.03 |
| main mixed / no main traces | 0 | $0.00 |

The single exception is a haiku on `general-purpose` — a deviation in the
*cheap* direction, worth three cents.

**Exclusivity alone is not the evidence.** Control 1: of 314 unit windows, 312
have a single-model main thread. Main-thread exclusivity is nearly automatic, so
"exclusive" is close to free. The evidence is the contrast with compliant
subagents:

| subagent decisions | model == parent main | model != parent main |
|---|---|---|
| **deviating** (96) | **95** — $1,248.10 | 1 — $0.03 |
| **compliant** (15) | 1 — $0.91 | **14** — $11.91 |

95 of 96 against 1 of 15. The 14 compliant non-matches are all `Explore` on
haiku-4.5.

**The single compliant match is worth its own sentence.** It is
`product-manager` on opus-4-8, $0.91. No rule in `routing_policy.yaml` covers
that component, so it fell through to `default_tier: frontier` and scored
compliant. **The policy's own default masked it.** A component with no rule
cannot deviate, which means coverage gaps in a stated policy read as compliance.

**What this does and does not establish.** Measured: deviating subagents ran
their parent's model and compliant ones did not. Not measured: that the
documented inheritance behaviour — a subagent whose definition omits `model`
takes the main thread's — is what produced these 95. The mechanism is documented
upstream; that it caused this particular set is inference. What the contrast
does rule out is deliberate per-subagent selection, which would not produce 95
exact matches and zero mixed windows.

### Query bug found and fixed during verification

The first run of this test reported 75 / 20 / 1 with twenty cases as "no main
traces in window". Wrong. `traces.invoked_at` is declared `DATETIME`, which
gives the temp-table column **NUMERIC affinity**; the open-ended upper bound
`'9999'` converted to the integer 9999, and in SQLite every INTEGER sorts before
every TEXT value, so `ia < 9999` was false for all twenty rows with a NULL
`ts_end`. Using `'9999-12-31'` — not convertible to a number, so it stays TEXT —
fixes it. Both runs summed to 96 rows and $1,248.13, so the totals looked right
in both. **A cross-foot that reconciles does not validate a classification.**

---

## 4. Directional check — does 22.6% hold on 2.2× the data?

The published 22.6% is at `(unit, component)` decision grain and needs
manually-corrected task tags, which the new traces do not have. But most of the
policy does not need tags at all.

Rule specificity in `routing_policy.yaml` resolves so that four components have a
fixed expected tier regardless of `task_type`. Confirmed empirically — each of
these returns exactly one row:

```sql
select distinct expected_tier from routing_decisions where component='main';
--  frontier          (also Explore->cheap, general-purpose->mid, workflow-subagent->mid)
```

So a **cost-grain, method-consistent** check is computable with zero tagging. It
answers "is the cross-tier share holding up", not "what is the new deviation
rate" — different grain, different question, **not comparable to 22.6%**.

| window | scored traces | cross-tier traces | scored cost | **cross-tier cost** |
|---|---|---|---|---|
| frozen, `< 2026-07-05` | 28,863 | 44.1% | $7,198.14 | **20.0%** |
| new, `>= 2026-07-05` | 22,718 | 20.4% | $5,388.85 | **12.3%** |
| full corpus | 51,581 | 33.7% | $12,586.99 | **16.7%** |

(The two windows sum to $12,586.99 within a cent; each is rounded independently.)

**Sensitivity — the unscorable slice matters.** 6,258 of the new window's traces
are unscorable: 5,992 opus-5 with no tier, plus 266 with a NULL `model_id`.
Classify opus-5 as frontier (it is an Opus at $5/$25) and price it:

```
new window scored cost   $5,388.85 -> $6,602.70
cross-tier cost          $660.75   -> $903.15
cross-tier share         12.3%     -> 13.7%
```

The direction survives: **20.0% -> 13.7%**, roughly a 30% relative reduction in
cross-tier cost share after the audit was published.

**Do not claim causation.** The windows differ in project mix, workflow shape and
available models. This is a before/after on non-comparable populations, offered
as "the number did not blow up", nothing more.

---

## 5. Not established — do opus-5 subagents reproduce the inheritance pattern?

The §3b test needs `routing_decisions` rows, and opus-5 has none — it has no
tier, so it is never scored. A cruder session-dominance proxy over the six
sessions with opus-5 on `workflow-subagent` (1,985 traces) gives 4 of 6, with
one mixed-main session and one clean counterexample (`7f82c643`: 142 subagent
traces on opus-5 while main ran fable-5 throughout).

**Do not put 4 of 6 in the article.** It is a different, weaker test than §3b's.
Once opus-5 has a tier, the real test can run and the answer will be worth
having. Until then, leave opus-5 out of that section.

---

## 6. Handover to the engineering layer

Three items, all one-liners, none of which need this document's reasoning:

1. Add `claude-opus-5` to `PRICES` in `pricing.py` — **after** verifying the
   rate against the platform pricing page.
2. Add `claude-opus-5` to `tiers.frontier` in `routing_policy.yaml` (policy
   change — operator's call, log it in the file's revision history as v2).
3. Make an unpriced model **loud.** Today it produces NULL. A count of traces
   with `tokens_out > 0` and `cost_usd IS NULL` belongs in the ingest summary —
   that check would have fired on 2026-07-25.

Item 3 is the generalisable one and belongs in the article: *the archive already
recorded everything needed to notice; nothing was watching the gap.*

---

## 6b. UPDATE 2026-08-08, after CC's fixes — three things changed, one is bigger than the original finding

**§3's diagnosis was wrong and CC corrected it.** The unpriced-model check
already existed, fired **10 times**, and had a passing test asserting its
counter. The defect was delivery: `append_run_log()` serialises only
`stats.warnings`, and `missing_price` never entered that list. The alarm was
wired to stdout, buried in a 2,571-line table dump. `routing_audit_ingest.log`
— the machine-readable channel — shows 18 runs with `warnings` empty every time.

That is a better story than "nobody built the alarm", and it must replace the
§3 framing in the article: **the check existed, fired, and was tested — and the
test stayed green because it asserted the counter, which was correct, while the
delivery path was broken.** CC's new tests assert delivery to the run log, not
the counter.

**opus-5 is now priced**, at the official $5/$25 (verified against
`anthropic.com/news/claude-opus-5`, released 2026-07-24). 5,992 traces repriced
to **$1,127.77**, batch `rp-20260808T030632Z-ec404e`, rollbackable. The
acceptance invariant returns 0 rows.

### And then reconciling that number against my own found a real bug — whose only victim was this audit

> **This section was wrong when first written and is corrected below.** The
> original version claimed the bug had understated the whole corpus by
> $1,393.49 across 27,130 traces, and the published $6,281.98 by ~$821. Both
> figures were wrong by more than an order of magnitude. The error and its
> cause are recorded at the end of this section rather than quietly deleted.

My hand estimate was $1,213.85; the tool produced $1,127.77. The $86.08 gap is
exactly `cache_creation_1h` priced at 1.25× instead of 2.0×.

`compute_cost_usd` reads the 5m/1h split only from a **nested**
`usage["cache_creation"]["ephemeral_*_input_tokens"]` structure. Every stored
usage block uses **flat** keys instead:

| usage blocks | count |
|---|---|
| nested `cache_creation` dict | **0** |
| flat `cache_creation_5m` / `cache_creation_1h` | 58,194 |
| neither | 16 |

So the fallback branch — "no split present, treat everything as 5-minute TTL at
1.25×" — fires on every single record, and
`ModelPrice.cache_write_1h_mult = Decimal("2.0")` is **a declared constant that
no production code path can reach.** The module docstring says the multipliers
are "0.1×/1.25×/2×"; the code contradicts its own documentation.

The multiplier is confirmed externally — the platform prompt-caching page says
"5-minute cache write tokens are 1.25 times / 1-hour cache write tokens are 2
times / Cache read tokens are 0.1 times." `Decimal("2.0")` was always right. It
was simply unreachable.

**But the blast radius is 3,918 rows and $88.75, not 27,130 rows and
$1,393.49.** Measured by the `--recompute` dry-run:

| model | rows changed | delta |
|---|---|---|
| claude-opus-5 | 3,907 | **+$86.13** |
| claude-opus-4-8 | 7 | +$0.93 |
| claude-fable-5 | 4 | +$1.69 |
| **total** | **3,918** | **+$88.75** |

Row-level comparison shows why. `stored` was already the 2× answer for
opus-4-8 and fable-5; only opus-5's `stored` matched the all-5m answer:

```
opus-4-8 : stored=2.687569  new(2x)=2.687569  old(all-5m)=1.686979
fable-5  : stored=0.548280  new(2x)=0.548280  old(all-5m)=0.381060
opus-5   : stored=4.720987  new(2x)=7.529523  old(all-5m)=4.720987
```

**There are two costing paths, and only one is broken.** The raw Claude Code
JSONL carries the nested `cache_creation` dict; ingest costs from the raw file,
where the nested form is present, and gets it right. `ingest_claude_code.py`
lines 331–332 then flatten it to `cache_creation_5m` / `cache_creation_1h`
before storing. So anything costed *at ingest* is correct, and only a
recompute *from the database* hits the fallback.

**The only recompute-from-database ever run was mine, one round earlier.** The
5,992 opus-5 rows I had CC backfill were the sole population the bug ever
touched. The audit created the defect it then detected.

Two consequences follow, both correcting the first draft of this section:

- `report_en.md`'s $6,281.98 is **not** understated by $821. The published
  window moved by **−$0.10** across 3 rows, downward, and for an unrelated
  reason (the split-vs-total edge case below).
- `routing_decisions` still reads **$1,248.1327** in the live table.
  $1,248.0292 is what regeneration *would* produce, not a value now stored —
  `routing_decisions.cost_usd` is a snapshot written at generate time and the
  reprice did not touch it. **Do not write "the deviation cost is now
  $1,248.0292" anywhere.**

### The edge case CC found that neither of us specified

24 rows have a 5m/1h split that disagrees with the total, in both directions
(`tot=86,009 / m5=16,295` under-reports by 69,714; `tot=4,566 / m5=11,543`
over-reports by 6,977). Costing off the split silently drops 274,513 tokens;
costing off the total silently drops the 1h premium. CC made the trade-off an
explicit rule — **the total is the authoritative billable quantity, the split
only describes composition** — implemented as `h1 = min(split_1h, total)`,
`m5 = total - h1`. The 58,170 consistent rows are unchanged to the cent.

### What I got wrong, and why

I derived the delta by recomputing every row from the **stored flat keys** and
differencing against a 1.25× baseline — which measures *what the buggy function
would produce*, and I reported it as *what the system had produced*. I never
checked how `cost_usd` was originally written. There were two paths and I had
read only one.

That is the same error the series is about, one level up: I read the code and
inferred the number instead of measuring it. The 7% discrepancy that started
this was real and worth chasing; the 15× overstatement of its scope was mine.

### The finding that survives the correction

The bug is inert today, but the condition that produced it is not: **cost is
computed from the nested form, and then the nested form is discarded.** The
database retains enough to *approximately* recompute a cost and not enough to
*exactly* recompute it, and nothing records that the stored representation is
lossy relative to the computation already performed on it.

That is precisely what `canonical_rule_version` exists for in pit-archive —
record which rule produced a derived value, because it cannot be reconstructed
afterwards. The same discipline was missing here, in the same operator's other
project, and it stayed invisible until something tried to recompute.

**`reprice_null_costs` could not fix this** — it filters
`.where(Trace.cost_usd.is_(None))` and hardcodes `old_cost_usd=None`. The tool
had a path for "was missing, now computable" and none for "was computed wrong."
`--recompute` now exists, batch `rp-20260808T041211Z-e92e55`, 3,918 rows, all
with real `old_cost_usd` (3918/3918, against 5992/1318 all-NULL in the two
older batches), rollbackable, and a re-run dry-run reports zero changes.

## 6c. The tagging pipeline stopped five weeks ago

```
task_tags   318 rows (314 manual, 4 heuristic), window 2026-05-30 -> 2026-07-03
traces      58,210 rows,                        window 2026-05-30 -> 2026-08-07
generate dry-run: skipped 31,142 untagged traces
```

`generate` skips untagged traces *before* tier lookup, so adding opus-5 to
`tiers.frontier` changes nothing on its own — none of the 23 sessions carrying
opus-5 traces has a tag. Same failure class as the price warning, different
stage: **a pipeline step stopped producing and nothing treats zero output as an
anomaly.**

Two consequences for this document:

- §4's directional check is **unaffected** — it was computed at trace grain
  directly from `traces` using the four components whose expected tier is fixed
  by the policy, deliberately bypassing `task_tags`. That choice now looks
  better than it did when it was made for convenience.
- §0's "the DB reproduces §3 to the digit" is **perishable**. `decision_id` is
  the primary key and `generate` upserts, so one `generate --write` destroys the
  reproduction. Freeze a copy before any regeneration.

---

## 7. The database moved while this document was being written

Not an aside. Between the first read and the snapshot, the scheduled ingest ran
(`cc-20260807T191705Z`, 2026-08-07 19:17 HKT) and added **678 traces**.

| | first read | snapshot | delta |
|---|---|---|---|
| traces | 57,532 | 58,210 | **+678** |
| priced cost | $12,587.32 | $12,588.23 | **+$0.91** |
| opus-5 traces | 5,347 | 5,992 | **+645** |
| NULL-model traces | 581 | 596 | +15 |

**A day of work added 678 traces and moved the archive's cost total by
ninety-one cents,** because 645 of the 678 were on a model the price sheet has
never heard of. §3 describes the defect; this table is the defect happening in
real time, unprompted, inside the window in which it was being written up.

Use it in the article. It is better than the static version of the same point.

`routing_decisions` did not move at that point (425 / 96 / $1,248.13) —
decisions are regenerated on demand, not on ingest. It has since been
regenerated; see §8.

---

## 8. Annotating a deviation exempts it from the policy

Found by CC while closing out the tag backfill. It is the most serious thing in
this document and the smallest in dollars.

After the backfill and regeneration:

| | before | after |
|---|---|---|
| decisions | 425 | 784 |
| deviations | 96 (22.6%) | 161 (20.5%) |
| deviation cost | $1,248.1327 | $1,945.2579 stored / $1,945.1544 printed |
| source generated / manual | 329 / 96 | 688 / 96 |
| task_tags | 318 (314 manual, 4 heuristic) | 602 (314 manual, **288 heuristic**) |

The rates are **not comparable** — the denominator changed and every added unit
carries an unreviewed heuristic tag.

### The defect

`generate` skips `source='manual'` rows whole, to protect the human-entered
`reason` and `outcome`. But `routing_decisions` mixes two kinds of column:

| kind | columns | must |
|---|---|---|
| human | `reason`, `outcome`, `source` | never be regenerated |
| derived | `cost_usd`, `expected_tier`, `expected_model`, `actual_model`, `actual_tier`, `deviation`, `n_traces` | track the traces and the policy |

Protecting the row froze both. The 96 manual rows still carry their
2026-07-04 batch id and no path can refresh them.

The $0.10 cost drift is the visible symptom and the least of it. **`deviation`
and `expected_tier` are frozen on those rows too**, which means:

> Every one of the audit's 96 deviations is now permanently exempt from the
> routing policy. A future policy revision cannot reach them, and nothing will
> say so.

96 of the current 161 deviations — 60% — sit in that island. The rows a human
cared enough about to annotate are exactly the rows that stopped responding.
**The act of recording why something deviated freezes the judgment that it
deviated.**

Chain closes: stored $1,945.2579 − printed $1,945.1544 = $0.1034, and the
manual rows' own drift is $1,248.1327 − $1,248.0292 = $0.1035.

> **Correction, 2026-08-08.** This section originally said `$1,248.0292` was
> "not pending, it is **void** — it assumes a refresh that cannot occur."
> Wrong. The refresh could not occur *given the defect*; splitting the columns
> made it occur, and $1,248.0292 is now the live stored value. I described the
> current state of the system as a property of the system. **That is the second
> time in this document** — §6b's $1,393.49 did the same thing, assuming every
> stored cost had come from a path that had in fact never run. Both errors have
> one shape: a contingent fact stated as a necessary one.

### The shape of the fix already exists in the operator's other project

This is the same problem `pit.ingestion_log_raw` solves: determinations are
appended, never updated; a correction supersedes with a version bump; a view
resolves the current verdict; and the obvious name (`pit.ingestion_log`) is the
safe one. Human input attaches to the *identity* of a determination, not as a
column on the derived row.

`routing_decisions` took the opposite approach — upsert on a primary key, with
human and derived data in the same row — and arrived at a frozen island. The
`routing_decisions_rebuild_log.jsonl` CC wrote is the `rebuild_log` half of the
pattern; the append-only half is missing.

### Attribution of the change, measured row by row

CC captured the 425-row state before writing, so this is diffed, not reasoned:

- **tag backfill**: 359 entirely new decisions, 65 of them deviations,
  $697.1252. This is the whole increase.
- **opus-5 into `frontier`**: made 60 previously unclassifiable decisions
  scorable (3 deviations). Verdict flips on pre-existing rows: **0** — every
  opus-5 unit was untagged before, so all its decisions are new. Adding the
  tier changed no existing judgment.
- **cost recompute on carried-forward rows**: **$0.0000**, because the only
  affected rows were manual ones, which do not update. That is the defect
  above, showing up as a zero.

### Resolved 2026-08-08 — and the fix needed a third column class

`generate` now refreshes derived columns on manual rows and leaves
`reason`/`outcome`/`source` alone. Measured after the change: **0 deviation
flips**, 0 human columns modified, 96/96 `batch_id` refreshed, deviation cost
$1,248.1327 → $1,248.0292, and `stored == printed` at $1,945.1544.

Zero flips is not "nothing happened" — what was removed is the exemption. The
next policy revision reaches these rows.

**The two-class split I specified was wrong.** CC used three: `decision_id` and
`created_at` are neither human-written nor derived. Classing `created_at` as
derived would rewrite it on every rebuild and destroy the only record of when a
decision first appeared. The evidence is visible right now — after two
regenerations, the manual rows still carry `created_at` = 2026-07-03 00:36:51,
while `generated` spans 2026-07-03 → 2026-08-08.

That is `first_seen_at` under another name, and it is the same rule pit-archive
enforces: a derived record's identity includes when it was first observed, and
that timestamp is not derivable from anything. A partition test asserts the
three sets exactly cover the table's columns, so a new column forces its author
to choose.

### Tag provenance now travels with the number

`tag provenance: heuristic 280 (47%), manual 310 (53%) — heuristic tags are
unreviewed; task_type may be wrong`. Nearly half the tags are uncorrected, and
any figure computed across them has to say so.

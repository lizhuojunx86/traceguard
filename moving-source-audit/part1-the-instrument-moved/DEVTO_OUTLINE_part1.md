# Series B, Part 1 — outline + argument skeleton

Status: **outline, 2026-09-04.** Not a draft.
All figures pinned to the 2026-09-03 end-of-day state of the daily retention
series (`pit.retention_floor_daily`, 38 rows, two sources). Re-verify before
drafting; the series grows by two rows a day.
Series: Measuring a moving source. Working notes: `docs/measuring-a-moving-source.md`
(notes 1–4 written 2026-08-16, notes 5–9 added 2026-09-04).

Red line, unchanged since 2026-08-08: **the source is not named.** Not the
operator, not the sector, not the country. "A public data source with a
rolling retention window" is all the reader gets, and every number is
arithmetically faithful to the real series.

---

## §0 先读这段（中文）

**一、这篇只讲一条线：留存探针。** 从周探针的混叠（note 2）、日探针、截断读数（note 3）、
停机允差（note 4），到今天新添的两个：分子分母不同基准（note 8）、探针时刻压在源自己的
修剪抖动带上（note 9）。记录层那三个案例（把失败算成观测、把漏抓判成假期、不转的轮转）
留给 Part 2，别塞进来。

**二、本篇的硬货是一个算术。** 15 个探针间隔里 floor 走了 16 个日历日。多出来那一天
就是全部读数里唯一的 "2"。六个 0 恰好落在六个非工作日上。一行减法把"batch 信号"
变回"探针在修剪前后各站了一次"。

**三、自曝三次。** 同一类失败——仪器替假设制造证据——在同一个探针上出现了三回：
截断读数、周日顺序、今天的分母。第三次是在前两次都"修好"并写了回归测试之后。
这个事实比任何一条修复都值钱，标题从这里出。

**四、note 4 的忏悔要保留。** "每个周一都会误报"是我自己在讲仪器背叛的笔记里
写下的一个没核实的机制。这篇不能再犯：所有机制性断言旁边必须有那个读数。

**五、发布时机受一件事牵制：探针时刻要挪。** 挪之后的两周序列是这篇最好的收尾图
（挪前 0/1/2 混杂，挪后应当是干净的 1-per-calendar-day）。没有那张图也能发，
但有了它读者不用信我。

---

## Title candidates

1. **Three times my instrument manufactured the evidence I was testing for**
2. My probe reported a batch deletion. The source deleted one day a day.
3. 15 probes, 16 days, one wrong hour
4. I built a probe to tell smooth from batch. Every time it was wrong, it said batch.

Recommend **1.** Same register as the Series A titles: a count, a fact, no
adjective doing the work, and the second half is the finding. Candidate 4 is the
best sentence in the piece and belongs in the body where it is earned. Candidate
3 is the subtitle.

Tags: `datascience, monitoring, opensource, python`

---

## The thesis

Series A asked whether tools counted correctly. This series asks the question
one layer up: when the value is fine, what else moves?

Here the thing that moved was the instrument. A source keeps roughly the last
60 days online. Whether it trims **smoothly** (one day a day) or in **batches**
(nothing, then several at once) decides how re-fetch effort should be spent, so
a probe was built to measure the floor daily. Over three weeks the probe
produced exactly one batch-shaped reading, and one batch alarm, and both were
the probe.

The finding, and it is the piece:

> **Every reading that supported the hypothesis under test was produced by a
> defect in the instrument, and each defect was found only after the previous
> one had been fixed and locked with a test.**

Three defects, three mechanisms, one shape: a limit, an ordering, or a
denominator that lived in the observer got recorded as a fact about the source.
The fourth section is the arithmetic that unmasked the last of them, and it
needs no code: count the calendar days the floor moved, count the intervals you
measured over, and if the first exceeds the second by one, you have one double
reading, not a batch.

---

## Structure

### 1. Open, ~150 words

The question in two sentences: rolling window, smooth or batch, why it matters
(re-fetch priority under a request budget). Then the result before the method:
**19 daily readings per source, one "2", one alarm, both artefacts.** No
apology for the length of the road; state that this is the road.

### 2. A probe that could not tell the hypotheses apart, ~250 words (note 2)

Weekly probe, seven days, floor moved seven days — under both hypotheses.
The two readings are the same string. Aliasing, plainly named: anything with
period P, sampled at P, looks constant.

The test, verbatim from the notes: *write down the reading each hypothesis
would produce; if the two strings match, the probe is decoration.*

Then the second-order trap, one paragraph: anchoring the daily probe on the
*modelled* floor bakes the smooth hypothesis into the measurement. Anchor on the
last **measured** floor. This paragraph sets up §5, where the same word —
"measured" — is exactly what goes wrong.

### 3. A truncated reading must not look like a complete one, ~300 words (note 3)

Budget of four probes per source. The capped row writes **no** distance —
not zero, not the partial distance, the key is absent — and the anchor advances
only on a measured reading, so a real eleven-day jump records as
`[NULL, NULL, NULL, 11]` and never as `[4, 4, 3]`.

Include the replay-as-proof paragraph and the direction asymmetry (budget
exhaustion is benign in one search direction and alarming in the other). Keep
it to what the piece needs: this is defect zero — the one that was designed
out *before* it happened — and it matters because it establishes the class the
next three belong to.

### 4. Two ways the observer's own state leaked into the series, ~350 words (notes 4 and 8)

**4a. The Sunday.** Two jobs both advanced the same "last measured floor"
registry. The weekly one ran twenty minutes before the daily one, so every
Sunday the daily series recorded a 0, and a run of zeros followed by a jump is
the batch signature. Fixed by moving the *weekly* job, because the daily series
is the one that has to stay clean. One Sunday was contaminated before the fix
and is excluded from every read-out, by name, forever.

**4b. The denominator.** The alert compares `moved` against an allowance: how
far smooth trimming could have moved the floor in the time since the last
reading. `moved` is measured from the last **measured** floor. The allowance
was computed from the last **run**. On a day when the previous run had failed
to pin the floor, the numerator spanned two days and the denominator one:

```
day 1   floor measured at D
day 2   probe failed walking up -- no floor pinned, registry stays at D
day 3   floor measured at D+2 business days
alert:  "2 business days in one step against an allowance of 1 over 1 day"
```

Two business days over two calendar days. Smooth. The alarm said BATCH.

Then say what these two have in common, because it is the piece's spine: in
both, the instrument compared a quantity about the world to a quantity about
**itself** — its own scheduling, its own gaps — and wrote the difference into
the world's column. The Series A version of this sentence was "absent is worse
than wrong"; this one is **"the observer's clock is not the source's clock."**

### 5. The arithmetic, ~400 words — the core (note 9)

Now the clean rows, after excluding the Sunday and the denominator alarm.
Fourteen daily readings for one source:

```
0 × 6    1 × 7    2 × 1
```

One "2": two dates gone in one day, both business days, the day before measured
present. That is the textbook batch signature, and it survived every fix above.

Then the subtraction. Over those fifteen probe intervals the floor advanced
**sixteen calendar days.** Sixteen trims in fifteen intervals means exactly one
interval saw two trims. There is only one candidate: the "2".

Then the zeros. Six of them. The span the floor crossed contains exactly six
non-business days (weekends and public holidays). A rolling *calendar-day* cut
that crosses a weekend removes zero *business* days; the business-day floor does
not move. Six non-business days, six zeros, each on the right date.

So the whole series is one model: **a 60-calendar-day window, trimmed once a
day, at a time of day close to the probe's own hour.** The probe ran at the
same minute every morning; the trim ran at a nearby minute that jitters. On the
one morning the trim ran *before* the probe instead of after, the probe saw two
days' trimming at once. The second source, probed the same way, shows the same
double one day later and the same early/late flip on different mornings — the
two sources trim at different moments, both near the probe hour.

Land it: **the only batch-shaped reading in the record is the probe standing
on the wrong side of a trim, once.** The hypothesis the whole apparatus was
built to test is not supported by a single reading that requires it.

Honest limit, same breath: the trim hour is *inferred* from one early reading
per source, not measured. The defensible sentence is "consistent with smooth
calendar-day trimming; no reading requires batching." Do not write "proven
smooth."

### 6. What it changes, ~200 words

- The re-fetch weighting built on the batch hypothesis was buying nothing;
  under smooth trimming every date's last day online is deterministic, and one
  look inside the final 48 hours replaces ten. Request volume roughly halves.
  The direction the footprint should always move.
- The probe hour was the worst one available — inside the trim's own jitter
  band. It stays where it is until the read-out is done (moving it *before*
  would put an artificial step into the series being read), then moves once,
  with the date of the step written next to the schedule.
- The alert's denominator now reads the same registry the numerator does.

If the post-move series exists by publication, show it: two weeks of clean
1-per-calendar-day next to the 0/1/2 mix. That figure is the whole article in
one picture and it costs two weeks of waiting.

### 7. Counterweights I owe you, ~200 words

- **Three of the three batch-shaped signals were mine.** That is a statement
  about this instrument, not about rolling windows in general; a source that
  trims in batches exists somewhere and this method would find it.
- **The trim hour is inferred, not measured.** One early reading per source.
- **The series is short.** 19 rows per source; three of them are gaps caused by
  the observer's own network.
- **Note 4's mechanism was wrong when first written** ("fires every Monday").
  It is kept in the notes as written, with the correction, because a piece
  about instruments narrating confidently should not delete its own instance.
- **Smooth is not proven.** "No reading requires batch" is the claim.

### 8. What I'd generalize

- **Write both hypotheses' readings down before you build the probe.** If the
  strings match, you are about to collect decoration.
- **A limit in the observer must be incapable of being read as a fact about
  the world.** Absent key, not zero; registry advances on measurement only.
- **Numerator and denominator must share a clock.** If `moved` is measured from
  the last measurement, the allowance is measured from the last measurement.
- **Never sample at the source's own cadence, and never at its own phase.**
  Weekly sampling aliased the period; a 07:20 probe aliased the trim's hour.
  Same failure, two scales.
- **Do the subtraction before you believe the shape.** Calendar days moved
  minus intervals measured. It takes ten seconds and it dissolved three weeks
  of "batch".

### 9. The layer underneath, ~80 words

The recording layer never modelled anything: raw rows, absent keys where
nothing was measured, alerts carrying their own inputs. That is the only reason
§5 could be done afterwards on the series and not on the alert history. Close on
that sentence, not on a product. There is no CTA in this piece.

---

## Evidence checklist

| # | asset | source | status |
|---|---|---|---|
| 1 | weekly readings identical under both hypotheses (7 in 7, two sources) | weekly probe log, 08-08 → 08-15 | **verified 2026-08-16** |
| 2 | capped run records `[NULL, NULL, NULL, 11]` | `tests/test_watchdog.py::TestCappedReadingNeverLooksLikeACompletedOne`, real replay | **verified 2026-08-16**, regression-locked |
| 3 | Sunday contamination: daily 0 with the step in the weekly series | 2026-08-23 rows, both series | **verified 2026-08-25** |
| 4 | denominator alarm: 2 business days / 2 calendar days flagged against allowance of 1 | daily probe log 2026-09-03; `probe_daily_floor` uses `last_daily_probe` for elapsed | **verified 2026-09-03** (code read + log) |
| 5 | clean rows: `0×6, 1×7, 2×1` (source A), `0×5, 1×8, 2×0` (source B) | `pit.retention_floor_daily`, 08-17..08-31 / 08-30 minus 08-23 | **verified 2026-09-04** |
| 6 | 16 calendar days in 15 intervals (A); 15 in 14 (B) | floor dates on 08-16 and 08-31 / 08-30 | **verified 2026-09-04** |
| 7 | six zeros on six non-business days (A) | calendar of the span the floor crossed | **verified 2026-09-04** |
| 8 | source B early/late flips on 08-26, 08-29, 08-30 vs A's on 08-26 | same series | **verified 2026-09-04** |
| 9 | post-move clean series | does not exist yet | ⚠ **wait for it or ship without** |

---

## Do not claim

- **Do not name the source**, its operator, its sector or its jurisdiction.
  Calendar facts (weekends, "public holidays") are fine; holiday names are not.
- **Do not claim the trim hour is measured.** One early reading per source.
- **Do not claim smooth is proven.** "No reading requires batch."
- **Do not claim all rolling sources trim smoothly.** One source, one shape.
- **Do not present the request-volume halving as achieved.** It is the
  consequence of a decision taken on 2026-09-06; say so.
- **Do not re-tell the "every Monday" mechanism as if it were true.** It is in
  the piece only as the author's own error.
- **Do not mention the network outage's cause.** "Gaps caused by the observer's
  own connectivity" is all that is needed; the rest is the operator's business.

---

## Open decisions before drafting

1. **Anonymised real numbers vs a neutral reproducible source.** The 2026-08-08
   plan said Series B examples should come from a site a reader can hit.
   Options: (a) ship with the anonymised series — the arithmetic is checkable
   as arithmetic, not as a fetch; (b) re-run the two-hypotheses probe against a
   public rolling window anyone can query (a wiki's recent-changes retention is
   one candidate; **its trim behaviour has not been measured and must not be
   asserted**) and use that as the worked example, keeping the real series as
   the story. **Recommend (a) for Part 1**, (b) as a follow-up if readers ask.
2. **Wait for the post-move series?** Two weeks. **Recommend wait**: the
   before/after figure is worth more than the fortnight, and the 2026-09-06
   decisions have to land first anyway.
3. **Part 2 scope.** Notes 5, 6, 7 — the recording layer. Decide after Part 1
   ships; do not let Part 1 grow to cover them.

## Publish timing

Blocked on the 2026-09-06 sitting (the probe-hour move is a decision taken
there) and then on two weeks of post-move readings. Target: **week of
2026-09-21.** Run `/verify-claims` before publishing, per the repo rule; every
number above has a row in the checklist that says where it came from.

# Measuring a moving source — nine notes

Working notes, not a finished piece. Series B material.

`eps-revision-methodology.md` in this directory documents the case where a
vendor's **value** moves underneath you and the earlier value is unrecoverable.
These notes are about the failures one layer up: the cases where the value is
fine and the **measurement apparatus** is what quietly betrays you.

All nine were found in live systems (notes 1–4 on 2026-08-16, notes 5–9 on 2026-09-04). The worked numbers are constructed to be
arithmetically faithful without identifying any particular source.

---

## Note 1 — The document your definition rests on is also a moving source

You build a taxonomy on top of a published code table: numeric codes in the data,
a PDF from the regulator that says what each code means. You classify, you count,
you publish a rate.

Now ask where the code table is. It is a URL. You have never held its bytes.

Two things follow, and the second is worse than the first. If a new edition is
published, your existing counts silently change meaning — codes get added, split,
or reworded, and the same query returns the same number for a different thing.
And you cannot say **which edition produced any past figure**, because you never
recorded it. The number looks reproducible: the query still runs, it still
returns something. But the thing it returns is no longer the thing you reported.

The general rule:

> **Anything that turns bytes into meaning is part of the record.**

Code tables, field dictionaries, unit conventions, the vendor's "methodology"
page, the schema doc, the enum you copied into a comment. Each is a document with
an edition, and each can be revised without notice or changelog. Data provenance
without *definition* provenance is half a chain.

The test:

> For every derived figure you would publish, can you name the version of every
> document that gives its inputs meaning, and produce those bytes on demand?

If not, the figure is reproducible by coincidence.

The fix is almost always trivial — one request, once, stored beside the data with
its edition date taken **from inside the document**, not from the URL or the
filename. A date in a URL is the publisher's filing convention. A date in the
document body is the document's own claim about itself, and it is the one that
survives a reorganised website.

---

## Note 2 — A probe that cannot tell the two hypotheses apart

A source keeps a rolling retention window: roughly the last N days are available,
older material is gone. You want to know **how** it trims, because your re-fetch
priority depends on it:

- **Smooth** — one day drops off per day. A date with 50 days of runway is safe
  for 50 days.
- **Batch** — nothing for a while, then several days vanish at once. Runway tells
  you much less, so dates near the floor deserve disproportionate attention.

Reasonable plan: probe the floor weekly, accumulate readings, decide later.

Seven days pass. The floor has moved seven days.

```
smooth trimming, measured weekly  ->  moved 7 days
batch  trimming, measured weekly  ->  moved 7 days
```

The readings are **identical**, and they stay identical for as many weeks as you
care to collect. The probe buys confidence about the *rate*, which nobody was
asking about, and nothing at all about the *shape*, which is the only thing the
decision depends on.

The failure is not that weekly is "too slow". It is that the sampling interval
landed on the hypothesised period, at which point the two hypotheses become
aliases of one another. Anything that changes with period P, sampled at period P,
looks constant. This is ordinary aliasing, and it is easy to walk into because a
weekly probe *feels* conservative — light footprint, steady series, no drama. Six
months of it is six months of data that cannot answer the question.

The test, and it takes a minute before you write the probe:

> Write down the reading each hypothesis would produce. If the two strings are
> the same, the probe is decoration.

The fix here was to probe daily instead of weekly. Cheap. What was expensive was
the pile of weekly readings collected first, which felt like evidence.

### The second-order version

Having moved to daily, there is a subtler way to bake in the answer. It is
tempting to anchor each day's search on where the floor *should* be — extrapolate
from a constant window length, probe around that. But extrapolating from a
constant window length **is** the smooth-decay assumption. Anchor there and every
reading will politely agree with the hypothesis you were trying to test.

Anchor on the last **measured** floor, never the modelled one. A measurement
whose starting point is derived from the hypothesis is not a measurement.

---

## Note 3 — A truncated reading must not have the shape of a complete one

Same probe. To bound the request budget you cap the search: walk outward at most
four steps, and if the target is still not found, stop and resume tomorrow from
where you got to. Sensible.

The trap is in what gets written. If a capped search records `moved = 4` in the
same column and the same row shape as a completed search, then one real jump of
eleven has been recorded as **4, then 4, then 3** — which is precisely the smooth
signature you were trying to rule out. The cap does not merely lose precision:

> **Truncation is a property of the observation, not of the world.** A limit that
> leaves no trace in the data becomes, downstream, a fact about the subject.

### The fix is structural, not a flag

The obvious remedy is to add an `is_truncated` column and trust every future
reader to check it. The better remedy, and the one that held up here, makes the
incomplete row **incapable** of being read as complete:

- the capped row does not write the distance field **at all** — not zero, not the
  partial distance, the key is simply absent — alongside an explicit
  `measured = false`;
- the anchor registry advances **only on a measured reading**, so across a whole
  capped run the "previous" value stays pinned where it was. The final measured
  row's `previous → observed` span therefore *is* the complete step.

Collapsing a run of capped rows back into the single step it really was needs no
special-casing at read time. It falls out. Prefer this shape wherever you can get
it: a marker protects you only while every reader remembers to look, and the
usual failure of marker-based schemes is that in six months nobody does.

### And prove it by replay, not by reading

Reading the code establishes what it looks like. It does not establish what it
does. The honest check was to replay a real eleven-day jump through four
consecutive daily runs and print the series:

```
[NULL, NULL, NULL, 11]     what it records
[4, 4, 3]                  the failure being ruled out
```

That replay then stays as a regression lock. A check worth running once is worth
running forever, and it costs nothing after the first time.

### One asymmetry worth checking in your own system

Budget exhaustion is harmless when it can only happen in the *benign* direction —
you ran out of requests while confirming things are fine — and dangerous when it
happens in the *alarming* one — you ran out while measuring how bad it is. The
same cap, in two search directions, has opposite consequences. Know which one you
have, and do not carry a rule written for one direction over to the other.

---

## Note 4 — Your alert threshold has to account for your own downtime

The daily probe should say something when the floor moves more than it ought to.
The obvious rule: alert when `moved >= 2`, since smooth trimming can only ever
produce 1.

That rule fires by arithmetic alone whenever **you** skip a reading. The subject
moved at its ordinary rate; your measurement window was simply two days wide
instead of one. Miss a day and you get a 2. Let a laptop sleep for a week and you
get a 4. Nothing happened, and the alert lands on the same channel as the real
watchdog — which is how a watchdog gets muted.

> A threshold expressed per *observation* silently assumes your observations are
> evenly spaced. They are not. You sleep, you deploy, you crash, the power goes
> out. Express thresholds in units of the world's time, not of your sample index.

Concretely: replace the constant with an allowance computed from the time
actually elapsed since the previous successful reading — the largest movement the
benign hypothesis could account for over that interval. In steady state it equals
the old constant. It widens exactly when your own series has a gap, which is
exactly when the constant was wrong.

### Where the assumption is allowed to live

Note 2 says not to let the smooth hypothesis contaminate the measurement. This
note computes an allowance *from* the smooth hypothesis. Both are right, because
they touch different layers:

> The alerting layer is allowed to model. The recording layer is not.

Alerting is a decision about attention and can be re-derived any time from the
raw series. Recording is evidence. So the allowance and the elapsed interval are
written into the alert's own payload — a rule whose inputs are not stored cannot
be audited afterwards — while the measured columns stay untouched, and the
eventual analysis runs on the raw series and never on the alert history. Worth an
assertion of its own: the alerting code must not modify the rows it judges.

### A confession, which is the point of the note

The first draft of this note claimed the false alarm would fire *every Monday* —
weekends contribute no trading day, so Monday shows a 2. Plausible, mechanical,
wrong. The probe runs seven days a week, so the allowance is uniformly 1 and the
observed distribution over a month of real floors was `{1: 22}`. Mondays are
fine. The recurring false alarm was never about the calendar; it was about gaps
in the observer's own uptime.

The fix survived the correction unchanged, which is luck rather than method. What
is not luck is the shape of the error: **a mechanism narrated confidently is not
a measurement.** A note about instruments betraying you, betrayed by its author
asserting an unchecked number in exactly the register the note warns about.

---

---

## Note 5 — A look that saw nothing is not an observation

*(added 2026-09-04)*

A provenance view counts, per document, how many times it was looked at on its
publication day. Two or more looks, and the document is promoted to the tier
that asserts "did not change between the first look and the last". The view
filtered on *who* looked (the scheduled poller, not a backfill) and never on
*what the look returned*.

Then the network went bad. Failed requests and not-yet-published 404s were
rows like any other, so a document the archive **did not hold at all** — three
polls, three failures — came out in the top tier with `same_day_polls = 3`.
Five documents in that tier had never been seen on their day.

> **An interval assertion is only as wide as the observations that actually saw
> the thing.** A request that returned nothing widens nothing.

The test: for every row in your best tier, does at least one contributing
observation carry the document's own bytes? If the answer can be "no", the tier
is asserting something about attempts, not about the document. Filter on the
outcome, not the mode.

---

## Note 6 — Absence bracketed by presence is not a holiday

*(added 2026-09-04)*

A classifier had a rung that read: a date with content on both sides of it,
inside the retained window, cannot be an expiry — expiry eats a contiguous
prefix — so it must be a day the source did not publish for. Verdict:
`no_activity`.

Bracketing does rule out expiry. It does not rule out **a missed fetch**. One
scheduled slot failed to run; the two earlier polls were before publication;
the document was never captured. The rung filed it as a holiday, the gap report
said zero, and a business day on which a sibling source published a 155-row report
sat under `no_activity` for a week.

Thirteen other dates on the same rung were real holidays. The rung had been
right by coincidence, which is the most dangerous way to be right.

> **A rule that turns "we did not get it" into a positive fact about the world
> needs positive evidence for the fact.** Here it was one query away: if
> another source holds content for that date, the day traded, and the absence
> is a gap.

Same family, one more: the verdict keyed on the *latest* observation, so a
later transport failure displaced an earlier positive one. A holiday that had
been positively observed as a sentinel page became `fetch_failed` because a
re-fetch weeks later hit a dropped connection. Positive observations should be
sticky; a failed re-fetch cannot un-observe them.

---

## Note 7 — A rotation that does not rotate

*(added 2026-09-04)*

Re-fetch design: the oldest ten days of the window every day, everything newer
on a ten-day cycle — one slice per day, whole span every ten days. The slice
was chosen as `today − n` for `n` in `offset, offset+10, …` with
`offset = today mod 10`.

`n` and `offset` advance together. Their difference is constant. The selected
dates are exactly those whose ordinal is divisible by ten — **the same dates,
every day, for ten days** — and the other ninety percent are never touched until
they age into the daily zone. Measured from the request log: three dates
re-fetched twelve days running; every other date in the zone, zero times.

The description in the project's own documentation said "swept on a 10-day
cycle" for six weeks. It described the intent. Nothing had ever measured the
behaviour, because nothing was wrong with any individual fetch.

> **Coverage is a property of the schedule over time, and it is only ever
> tested by simulating time.** Ten consecutive days, count hits per date,
> assert each is one. The test fails against the old code in the first second.

---

## Note 8 — The numerator and the allowance must share a clock

*(added 2026-09-04)*

Note 4 replaced the constant threshold with an allowance: how far smooth
trimming could have moved the floor since the previous reading. The reading
itself, `moved`, is measured from the last **measured** floor — the registry
that, per note 3, advances only on a measurement.

The allowance was computed from the last **run**.

Those are the same date until a run fails to pin the floor. Then:

```
day 1   floor measured at D
day 2   probe fails walking up; registry stays at D; row written, unmeasured
day 3   floor measured at D + 2 business days
alarm   "2 business days in one step against an allowance of 1 over 1 day"
```

Two business days over two calendar days is smooth. The alarm said BATCH. Third
appearance of the same class — capped walks, then the Sunday ordering, now this
— each found after the previous one was fixed and locked with a test.

> **If the reading is anchored on the last measurement, the allowance is
> anchored on the last measurement.** Anything else compares a quantity about
> the source to a quantity about the observer's own uptime and writes the
> difference into the source's column.

---

## Note 9 — Sampling on the source's own phase

*(added 2026-09-04)*

After notes 3, 4 and 8, the clean daily series for one source read
`0 × 6, 1 × 7, 2 × 1`. One two-step: two dates gone in one day, both business
days, the previous day measured present. Textbook batch. It survived every fix.

Now the subtraction. Across those fifteen probe intervals the floor advanced
**sixteen calendar days.** Sixteen trims in fifteen intervals is one interval
that saw two trims, and there is exactly one candidate. The six zeros fall on
the six non-business days the cut crossed — a calendar-day cut crossing a weekend
removes no business day, so the business-day floor holds still. Six for six.

One model fits every reading of both sources: a fixed calendar-day window,
trimmed once a day, at a time of day **near the probe's own hour.** On the one
morning the trim ran before the probe instead of after, the probe saw two days
at once. The second source shows the same flip on different mornings.

> **Never sample at the source's own cadence, and never at its own phase.**
> Note 2 aliased the period (weekly probe, weekly hypothesis). This aliased the
> phase (a 07:20 probe against a ~07:20 trim). Same failure, two scales.

The honest statement is "consistent with smooth trimming; no reading requires
batch" — the trim hour is inferred from one early reading per source, not
measured. The fix is to move the probe well clear of the trim hour, once, after
the read-out, with the date of the step written beside the schedule. And the
test that would have caught this on day one costs ten seconds:

> Calendar days the floor moved, minus intervals you measured over. If the
> answer is one, you have one double reading, not a batch.

---

## What ties them together

Each is a case where the instrument, not the source, is the thing that moved —
and in all nine the failure is silent by construction. No error is raised. The
query runs, the probe returns, the series fills up. What you get is a number that
is wrong in a way that looks exactly like a number that is right.

Which is the vendor-revision problem from `eps-revision-methodology.md`, one
level up. There, the recorded value was overwritten. Here the recorded value is
fine, and its *meaning*, its *sampling*, its *completeness* or its *baseline* was
overwritten instead.

The discipline that catches all of them is the same one: never infer success from
the absence of an error.

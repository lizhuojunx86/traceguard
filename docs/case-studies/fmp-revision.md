<!--
EDITOR'S NOTE — RECONCILIATION STATUS (2026-06-16)
Numbers reconciled against the quant_alpha_v2 vintage harness
(scripts/vintage/ + var/vintage/revision_episodes.parquet):
  - 41.4% (eps differs, 896/2163) and 15.3% (decision flip, 332/2163):
    VERIFIED — bit-reproduced from revision_episodes.parquet.
  - four-month window (2026-02-03 -> 2026-06-02): VERIFIED (script constant).
  - ~73% returns / ~82% Sharpe retention: from the 2026-06-05 backtest run;
    documented but never persisted to a file, and the FINAL leg drifts as FMP
    keeps revising — carry the as-of caveat (the ratio is more stable than the
    levels).
  - snapshot count re-anchored to ~1,400 first-day (as of 2026-06-05); the live
    harness has since grown past 3,000.
The arXiv citations near the foot are verified (2512.23847 / 2601.13770 /
2602.17234; MIN-K% PROB = 2310.16789). Do NOT redistribute raw FMP data
(ToS / copyright); this file contains
no vendor data, only the method and aggregate findings.
-->

# The `epsActual` That Wasn't: Measuring Look-Ahead Bias from Data Revisions in an LLM Earnings Backtest

> A TraceGuard case study on **harness / pipeline leakage** — the kind of
> look-ahead bias that rides in through ordinary code, not through the model
> weights. See [../POSITIONING.md](../POSITIONING.md) for the two-kinds-of-
> look-ahead framing.

**TL;DR.** We were backtesting an LLM-driven earnings signal against a field
called `epsActual` — the kind of field everyone treats as ground truth. It
isn't. About **41.4%** of those "actual" values were *different* from what the
vendor had first reported, and about **15.3%** of cases differed enough to
flip a tradeable decision. When we re-ran the backtest using only the values
that actually existed at each decision date, the strategy kept ~**73%** of its
returns and ~**82%** of its Sharpe (as of the 2026-06-05 run). The rest was
look-ahead bias — and it had entered through a field whose name promised it was
final.

*(41.4% / 15.3% and the four-month window are reconciled against the harness;
the ~73% / ~82% retention pair is as of the 2026-06-05 run and the FINAL leg
drifts — see the editor's note above.)*

---

## The setup

The signal is a post-earnings drift play: at each earnings print, an LLM scores
the release and we take a position. To backtest it, you replay history — for
every past print, you reconstruct what the model would have decided and check
what happened next.

The reconstruction needs one obviously-trustworthy input: what the earnings
number actually *was*. Our data vendor exposes exactly that, in a field named
`epsActual`. "Actual." Final. Settled. You query it for a print from two years
ago and you get a number back. What could go wrong?

## The invisible killer

Vendor "actuals" are not frozen at print time. They get backfilled, corrected,
and restated — sometimes the day after, sometimes months later. Restatements,
late filings, vendor parsing fixes, standardization passes: all of them quietly
rewrite history. **The value you query today for a 2023 print is not, in
general, the value that was available the day after that print.**

This is textbook look-ahead bias, and it is especially dangerous here because it
doesn't *look* like leakage. Nobody intentionally fed the model future data. The
leakage rode in on a field everyone trusts — and "actual" is about the most
trustworthy-sounding name a field can have. A backtest built on today's
`epsActual` is quietly asking the model to react to numbers that, on the
decision date, did not yet exist.

## How we measured it honestly

You can't detect this from a single snapshot of the database — by definition the
revision has already overwritten the original. So we built a **forward-polling
harness**: poll the vendor on a schedule, snapshot every value we care about,
and watch for changes over time. It had accumulated ~**1,400 snapshots** in the
first day of polling (as of 2026-06-05; the live harness has grown well past
that since).

The one methodological decision that mattered most:

> **Detect revisions by the value itself, not by the vendor's `lastUpdated`
> timestamp.**

The `lastUpdated` field is unreliable — it doesn't reliably fire on silent
backfills, and trusting it would have hidden exactly the revisions we were
hunting. So change detection keys on the **value-tuple**: if any field in the
record we track changes between two snapshots, that's a revision, regardless of
what the metadata claims.

```python
# Illustrative. Revision = the tracked value-tuple changed between snapshots,
# NOT "the vendor bumped lastUpdated".
def is_revision(prev_snapshot, curr_snapshot, tracked_fields):
    prev = tuple(prev_snapshot[f] for f in tracked_fields)
    curr = tuple(curr_snapshot[f] for f in tracked_fields)
    return prev != curr
```

To quantify the *trading* impact, we compared two backtests over a four-month
point-in-time window: a **naive** one using today's revised `epsActual`, and an
**as-of** one using only each value as first seen on (or before) the decision
date.

## What we found

The first two are bit-reproduced from the harness; the retention pair is as of
the 2026-06-05 run (see the editor's note for status).

- **41.4%** of `epsActual` values (896/2163) differed between first-seen and final.
- **15.3%** of cases (332/2163) differed enough to flip a tradeable decision — a
  sign change or a threshold crossing in the signal.
- Over the four-month point-in-time window, the as-of backtest retained
  **~73%** of the naive backtest's returns and **~82%** of its Sharpe (as of the
  2026-06-05 run; the FINAL leg drifts as FMP keeps revising, so treat the ratio
  as more stable than the levels).
- Read inversely: roughly a quarter of the headline returns, and a fifth of the
  Sharpe, were look-ahead artifacts.

The encouraging half: most of the strategy survives honest data. The sobering
half: a naive backtest overstated it by a wide margin, and a meaningful fraction
of "winning" trades were decided on numbers that did not exist at decision time.
A 15% decision-flip rate is not noise you can wave away.

## Why this is structural, not a one-off

The natural reaction is "okay, we'll be careful with that field." That doesn't
hold. The risk is reintroduced by every new feature, every new vendor, every
rerun, every teammate who reaches for "the actual value." Carefulness is a
property of a person on a good day; **as-of correctness has to be a property of
the pipeline.**

So we treat the question "*could this value have been known at the decision time
we're simulating?*" as an invariant the code enforces and CI checks — not a
thing we hope a reviewer notices. A vendor "actual" is **time-versioned
reference data**: it only becomes valid at the instant we first observed it. Use
it to decide *before* that instant and you are using a value from the future.
That is exactly TraceGuard's invariant 3 (`validate_reference_timing`), which
requires `valid_from <= feature_as_of`:

```python
# The check that turns a silent inflation into a loud failure.
from traceguard.validators.lookahead import validate_reference_timing

# The eps "actual" is time-versioned reference data: valid_from is when this
# specific value first existed (first-seen in our snapshots), feature_as_of is
# the decision moment we are simulating.
validate_reference_timing(
    valid_from=eps_first_seen,    # when this value actually existed
    feature_as_of=decision_date,  # the moment we're simulating
    kind="vendor_eps_actual",
)  # raises InvariantViolation([invariant 3]) if eps_first_seen > decision_date
```

When a value is used before its availability timestamp, the run fails loudly
rather than silently inflating a Sharpe ratio. (TraceGuard also ships invariant 1
`validate_feature_as_of` for as-of monotonicity across upstream traces, and
invariant 2 `validate_model_timing` for the model itself — see
[../SPEC.md](../SPEC.md) §5.)

It's worth being precise about scope. There are **two** kinds of look-ahead bias
in LLM pipelines:

1. **Training contamination** — the model itself was pre-trained on the future
   you're predicting, so it "recalls" rather than reasons. That's a separate,
   active research problem (membership-inference tests, point-in-time LLMs,
   claim-level temporal verification), and it needs different tooling.
2. **Harness / pipeline leakage** — your code uses a value, prompt, or model that
   didn't exist at the simulated time. *This case study is entirely about this
   kind*, and it's the kind a pipeline can be made to refuse structurally.

Both matter. They are not the same problem, and conflating them is how teams
"fix" one and ship the other.

## A checklist you can apply today

- Treat every `actual` / `final` / `reported` vendor field as a **moving target**
  until you've proven otherwise with your own snapshots.
- Detect revisions by **value**, not by the vendor's update timestamp.
- Backtest on **as-of (first-seen)** data, and explicitly measure the gap against
  revised data. That gap is your look-ahead tax — quantify it instead of assuming
  it's zero.
- Encode "known at decision time?" as a **CI invariant**, so the failure mode is
  a red test, not a flattering backtest.

## Limitations

One vendor, one field, a four-month window. The exact percentages are
dataset-specific and should not be read as universal constants — your numbers
will differ. And again: this addresses harness leakage only, not whether the
model itself has seen the future.

---

*Tooling: the validators and point-in-time instrumentation described here are
part of [traceguard](https://github.com/lizhuojunx86/traceguard), an open-source
library for point-in-time-correct LLM instrumentation.*

*Research context on the training-contamination side: "A Test of Lookahead Bias
in LLM Forecasts" (arXiv 2512.23847), "Look-Ahead-Bench" (arXiv 2601.13770),
"All Leaks Count, Some Count More / TimeSPEC" (arXiv 2602.17234), and MIN-K% PROB
("Detecting Pretraining Data from Large Language Models", Shi et al., arXiv 2310.16789).*

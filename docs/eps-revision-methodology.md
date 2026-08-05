# How the 41.4% / 15.3% numbers were produced

Two numbers appear in the TraceGuard and tg-attest READMEs:

- **41.4%** of `epsActual` values differed between the value the vendor served first and the value it serves now.
- **15.3%** differed enough to flip a long-entry decision.

Both come from a 2,163-record dataset published in [`analysis/data/`](../analysis/data/), and you can recompute them in one command:

```console
$ python analysis/eps_revision.py
```

No dependencies, no network, no vendor account. This document says where the records came from, what "flip" means exactly, and what the numbers do not support.

## The one thing that cannot be reproduced

A first-seen vendor value is not recoverable after the fact. Financial Modeling Prep serves one value per `(symbol, reportDate)` — today's. The value it served the morning of the print is overwritten in place. There is no vintage endpoint. `lastUpdated` does not help: we tested it and it is a bulk-reprocess stamp that jumps for every recent row on every wave and misses value edits between waves, so it cannot be used as a revision log or even as a change trigger.

So there is no script here that re-collects the data and lands on 41.4% again. There cannot be, from anyone. That gap is the reason this project exists.

What is reproducible is the arithmetic on top of the capture. What a third party can do independently is start their own capture and check whether the method gives them a similar answer over their own window. The capture code is described below and its behaviour is fully specified, so that is a real option, not a rhetorical one.

## What was actually captured

Two separate captures exist. They are not the same dataset, they do not cover the same period, and they do not give the same answer. Both are published.

| | **A — `qt_pit_2026h1`** (headline) | **B — `forward_poll_2026h2`** |
|---|---|---|
| Role | source of 41.4% / 15.3% | independent replication, different design |
| How first-seen was captured | scraped back out of a live PEAD strategy's own runtime logs, which print the EPS the strategy saw at that instant | purpose-built poller, writes a snapshot only when the decision-relevant value tuple changes |
| Capture window | 2026-02-03 .. 2026-06-03 (121 days) | 2026-06-05 .. 2026-08-05 (62 days) |
| Poll cadence | ~11 min, two brokerage accounts | hourly |
| Universe | the strategy's own screen: US small/mid cap, roughly $50M–$10B; 1,258 tickers appeared | FMP earnings calendar, US-listed, report date in [-7d, +14d]; 537 names off-season rising to ~6,900 in August; 9,582 symbols total |
| Raw volume | 18,206 (account, day, ticker) rows over 101 log days | 35,952 snapshot lines over 17,743 (symbol, report date) pairs |
| Records analysed | **2,163** (1,240 tickers) | **5,850** (4,780 symbols) |
| Fields | epsActual, epsEstimated, surprise%, SUE, market cap, ownership, industry, numAnalystsEps, grades_count, timestamp | epsActual, epsEstimated, numAnalystsEps, grades_count (trigger tuple); lastUpdated, revenueActual, revenueEstimated, captured_at (metadata) |
| "Final" value means | an FMP `/stable/earnings` snapshot pulled 2026-06-04, up to 4 months after the print | the last value seen inside the polling window, at most 7 days after the print |
| Coverage gaps | 20 of 121 calendar days have no log; 18 are weekends or market holidays, 2 are unexplained weekday gaps (2026-02-18, 2026-05-11) | one hard outage, 2026-06-25 22:46 UTC .. 2026-07-04 15:21 UTC, 8.7 days |
| Result | **41.4% differ, 15.3% flip** | **18.6% differ, 4.6% flip** |

Dataset A is where the headline numbers come from and always was. Dataset B was built later, is the better-designed capture, and produces materially smaller numbers. Section [Why the two datasets disagree](#why-the-two-datasets-disagree) explains why, and it is not a contradiction, but anyone quoting 41.4% should know 18.6% exists.

### From raw capture to analysed records

**Dataset A.** The strategy's log lines were segmented into episodes: a maximal run of consecutive daily appearances for one ticker, broken by a gap of more than 7 calendar days. That yields 2,282 episodes. The first-seen value for an episode is the **modal** EPS across that first day's polling cycles (median 49 observations per ticker-day, up to 630), not the literally-first one, because the vendor intermittently serves a wrong-units value for a minority of cycles inside a single day (`TRS epsActual=2034.97` appeared in 96 cycles interleaved with the real 0.40). Each episode is then matched to an FMP-current earnings event for the same ticker whose report date is nearest within [start − 7d, start + 60d]. From 2,282 episodes, 119 are dropped: 93 where more than one candidate event fell in the window or the nearest was over 30 days away, 16 with no match at all, 10 where the first-seen value was implausible (|EPS| > 50 in a small/mid-cap universe is a vendor glitch, not a print). That leaves **2,163**.

**Dataset B.** Snapshots are grouped by `(symbol, report date)`. First-seen is the earliest snapshot carrying a non-null `epsActual`, final is the latest. 11,883 pairs never carried an `epsActual` at all — calendar entries whose print landed outside the polling window — and 10 were vendor glitches. That leaves **5,850**.

## What "flips a trading decision" means

This is the number most likely to be attacked, so it is pinned in code, in [`analysis/build_disclosure.py`](../analysis/build_disclosure.py), and restated in the manifest:

```
surprise_pct = (eps_actual - eps_estimated) / abs(eps_estimated) * 100

threshold    = 2.0   if the first-seen analyst count is <= 1     ("V3" leg)
               10.0  otherwise                                   ("V5" leg)

tradeable    = surprise_pct > threshold          # long only, no short leg
FLIPPED      = tradeable(first_seen) != tradeable(final)
```

Five things this does and does not say:

1. **It is a binary entry decision, counted in both directions.** A flip is either a trade the first-seen value said to take and the final value says to skip, or a trade the first-seen value said to skip and the final value says to take. 253 of dataset A's 332 flips are the second kind, 79 the first. Both are look-ahead bias; a backtest reading the final value takes trades the live system could not have taken, and skips ones it would have.

2. **The thresholds are the live strategy's, not chosen here.** 2.0 and 10.0 are what the PEAD router actually ran with. The `flip rate vs threshold choice` block in the script output re-sweeps the rate across nine threshold pairs. The production pair gives 15.3%; the sweep spans 11.1% to 19.4%, and most neighbouring settings give a **higher** rate than the one quoted. Setting both thresholds to zero — flip means the surprise changed sign — gives 19.4%.

3. **The analyst count that picks the threshold is read from the first-seen snapshot and used for both legs.** The count is itself revised. Using the final count on the final leg would re-import the look-ahead bias the study is measuring.

4. **It is not a P&L claim.** Nothing here says a flipped decision costs money, or how much. A separate point-in-time backtest on the same window found roughly 73% of returns and 82% of Sharpe surviving the switch from final values to as-of values, but that run was never persisted and is not reproducible, so it is not part of this dataset and should not be quoted as if it were.

5. **Records with an undefined surprise count as not-flipped.** Two records in dataset A have a missing or zero estimate on one leg, so `surprise_pct` is undefined and no decision exists. They stay in the denominator. Dropping them would nudge the rate up by rounding noise; keeping them is the conservative choice.

## What is published, and what is withheld

FMP's Terms of Service, §2.6.1(i), forbid a customer to "resell, sublicense, distribute or otherwise provide access to The Services, or data or information contained in or derived from The Services, to any third party". §2.2.2 separately forbids displaying FMP data in a public software product without a specific agreement. So the values do not ship.

Each published record carries:

| Column | Content |
|---|---|
| `symbol`, `period`, `first_seen_date`, `final_ref_date` | which company, which quarter, when each leg was observed |
| `first_seen_hash`, `final_hash` | keyed digest of each value |
| `eps_differs`, `direction`, `magnitude_bucket` | whether they differ, which way, and how far, bucketed |
| `router`, `first_seen_tradeable`, `final_tradeable`, `decision_flipped` | which threshold applied and what each leg decided |
| `surprise_sign_flipped` | context |
| `first_seen_stale` | whether the capture demonstrably missed the print |

No EPS value, estimate, revenue figure, analyst count or vendor timestamp is released. From these columns you can compute 41.4% and 15.3%; you cannot reconstruct a single number FMP sells.

### Why the digests are keyed

The digests are HMAC-SHA256 under a 32-byte secret pepper, truncated to 16 hex characters. Plain SHA-256 would be theatre. EPS values live in a domain of a few tens of thousands of two-decimal candidates, so a published-salt digest is brute-forced in seconds, and publishing one would amount to redistributing the values under a thin disguise. The pepper stays with the data holder; `manifest.json` publishes `pepper_sha256`, which commits to it without revealing it.

The digests do three jobs:

- **They make `eps_differs` checkable instead of trusted.** On every row the flag must equal `first_seen_hash != final_hash`. `analysis/eps_revision.py` verifies this on all 8,013 rows and exits non-zero if any row disagrees. The same check catches a direction or bucket that contradicts the pair.
- **They freeze the record set.** The values behind a published row cannot be quietly restated later without the digest changing, and the CSV's own SHA-256 is in the manifest.
- **They allow selective verification.** Someone with their own FMP entitlement can be handed the pepper alone and re-derive every digest from their own data, row by row, without anything else being disclosed to anyone.

That last mechanism is the whole idea behind [tg-attest](https://github.com/lizhuojunx86/tg-attest): commit to a record, prove one claim about it, disclose nothing else, and let the holder open exactly one record to exactly one auditor. It is a slightly funny coincidence that publishing the evidence for TraceGuard's founding claim required hand-rolling a worse version of the sibling library's core feature. tg-attest does it properly, with Merkle inclusion proofs and an RFC 3161 timestamp instead of an ad-hoc pepper and a README.

## Results

Full output comes from `python analysis/eps_revision.py`. Intervals are Wilson score intervals at 95%, which behave better than the normal approximation at these rates.

**Dataset A — `qt_pit_2026h1`**, N = 2,163, 1,240 tickers, first-seen 2026-02-03 .. 2026-06-03:

| | count | rate | 95% CI |
|---|---|---|---|
| `epsActual` differs | 896 | **41.4%** | 39.4% – 43.5% |
| decision flipped | 332 | **15.3%** | 13.9% – 16.9% |
| surprise sign flipped | 419 | 19.4% | |

Of the 896 differences, 650 revised up and 246 down. The magnitudes are not rounding: 109 records moved by at least $1.00 of EPS and 260 by $0.25 to $1.00, against 20 that moved by less than a cent.

**Dataset B — `forward_poll_2026h2`**, N = 5,850, 4,780 symbols, first-seen 2026-06-05 .. 2026-08-05:

| | count | rate | 95% CI |
|---|---|---|---|
| `epsActual` differs | 1,087 | **18.6%** | 17.6% – 19.6% |
| decision flipped | 268 | **4.6%** | 4.1% – 5.1% |

### Why the two datasets disagree

Three differences, in descending order of how much they explain:

**Settle horizon.** Dataset A compares the print-day value against a snapshot taken up to four months later. Dataset B compares it against the last value seen *inside the polling window*, which the poller's `--lookback-days 7` setting caps at seven days after the print. Measured across dataset B, the median record is observed for 1 day after the print, the 75th percentile is 2 days, the maximum is 7, and nothing at all is observed 14 days out. Dataset B therefore measures "revised within a week", which is a strict subset of what dataset A measures. It is a lower bound on the same quantity, not a competing estimate of it.

That setting was a design error, not a considered trade-off. `--lookback-days` gates the calendar universe as well as the snapshot filter, so a symbol dropped out of the polling universe seven days after reporting and was never polled again: a revision landing on day 8 was unobservable *in principle*, and no amount of running the poller longer would have produced one. It has been raised to 90 days, in the launchd job and in the script's own default — see [`analysis/CHANGELOG.md`](../analysis/CHANGELOG.md) for the change and its measured cost. The fix is forward-only. The published dataset B was captured under the 7-day setting and nothing recovers observations that were never made, so the cap is permanent for these 5,850 records and the limitation below stands as written.

**Universe.** Dataset A is a small/mid-cap screen, roughly $50M to $10B. Dataset B is the entire US earnings calendar, most of whose weight is larger, better-covered names. Vendor data on a $200M company with two analysts is worse than on Apple, in exactly the direction that widens the gap.

**Capture design.** Dataset A's first-seen is the modal value across a day of 11-minute polls, which is robust but coarse; dataset B's is a genuine event-triggered snapshot. Dataset B is the better instrument. It has simply not been running long enough, over a wide enough post-print horizon, to answer the question dataset A answers.

The honest summary: 41.4% and 15.3% describe *revision to settled value, in a small-cap universe, over four months of 2026*. They are not a general property of FMP, and dataset B is published here so that nobody has to take my word for that.

## Known biases and limitations

This section is deliberately the longest one. Every item below is a real reason the two headline numbers could be wrong, or right for the wrong reasons.

**1. One vendor, one field, one window.** Everything here is FMP's `epsActual` between February and August 2026. Nothing was measured on Refinitiv, Bloomberg, Polygon, S&P, or on revenue, guidance, shares outstanding, or any other field. A vendor that revises less would produce a smaller number and the method would not notice anything wrong. Treat 41.4% as a measurement of one feed, not as an industry statistic.

**2. The universe was selected by a trading strategy, not sampled.** Dataset A's 1,240 tickers are whatever the PEAD screen surfaced: US small and mid caps, roughly $50M to $10B, filtered on having a recent earnings event and enough liquidity to trade. That is a double selection. It is small-cap-weighted, where vendor data quality is worst, and it conditions on the exact event type where revisions cluster. Dataset B, on a broad calendar universe, gives less than half the difference rate. Some of that gap is universe, and the split between "universe" and "horizon" has not been separated. It could be done — pull today's settled values for dataset B's 4,780 symbols and rerun the comparison at a four-month horizon — and it has not been done here. Raising the poller's lookback to 90 days closes the horizon gap for records captured from now on, but it cannot answer the question for the window already captured; that still needs the settled-value re-pull. Tracked as an open item in [`analysis/CHANGELOG.md`](../analysis/CHANGELOG.md).

**3. "First seen" in dataset A is a proxy, and a lossy one.** It is not a snapshot written when the print landed. It is the modal value on the first day the strategy happened to log that ticker, which depends on the strategy running, the ticker passing the screen, and the log rotating. Where that first log day falls more than a day after the report date, the value recorded as first-seen may already be a revision, which biases both rates **down**. 144 of the 2,163 records are flagged `first_seen_stale` for this. Excluding them raises the numbers to 43.1% and 16.2%. The lower figures are reported as the headline.

**4. The modal-value choice discards real churn.** 984 of 2,282 episodes (43.1%) showed more than one distinct `epsActual` within a single day. Taking the daily mode collapses that to one number. It is the right call for suppressing wrong-units glitches, and it means the study is blind to intraday revision entirely. A system that reads the feed once at 09:25 could see a value that this method never records.

**5. The report-date match in dataset A is heuristic.** Episodes are joined to FMP events by nearest report date within [−7d, +60d]. Vendors shift report dates, so 93 episodes had more than one candidate or a nearest match over 30 days away, and 16 had none. All 109 were dropped as ambiguous rather than resolved. If ambiguity correlates with revision — and it plausibly does, since a shifting report date is itself a symptom of a record being reworked — dropping them biases the rates **down** by an unknown amount.

**6. The glitch filter is a judgment call.** Records with |EPS| > 50 are excluded as vendor garbage, 10 in dataset A and 10 in dataset B. The threshold suits a $50M–$10B universe and would be wrong for a universe containing NVR or Seaboard. These records are arguably the most severe data failures in the sample, and excluding them makes the numbers smaller, not larger.

**7. Dataset B has an 8.7-day hole.** The poller died on 2026-06-25 and nothing noticed until 2026-07-04, because the launchd job was writing block-buffered stdout to a file, so the log looked days behind rather than dead. Anything that reported inside that window has no genuine first-seen value; 240 analysed records fall in it. More broadly, 909 of dataset B's 5,850 records are flagged `first_seen_stale`. Excluding all of them moves dataset B to 20.0% and 5.0%.

**8. "Final" is a timestamp, not a truth.** Dataset A's final leg is a snapshot pulled 2026-06-04 and dataset B's is whatever was current when polling stopped. Neither is settled in any absolute sense. If revisions keep arriving after those dates, both studies understate the total.

**9. The flip rate is specific to one strategy's thresholds.** 2.0 and 10.0 belong to the PEAD router this was run against. The published sweep spans 11.1% to 19.4% across nine reasonable pairs, which bounds the sensitivity but does not remove it. A strategy with a 50% surprise threshold would see a much lower flip rate, and a continuous position sizer would see no flips at all because it has no threshold to cross. "15.3% of decisions flip" is a statement about threshold-crossing entry rules, and most quantitative strategies are threshold-crossing entry rules, but not all of them are.

**10. Nothing here is causal, and nothing here is P&L.** The study measures disagreement between two observations of the same field. It does not establish that acting on the first-seen value would have made or lost money, and it does not control for anything.

**11. Dataset A's capture was retrospective.** The values were recovered by parsing log lines a strategy emitted for operational reasons, not written by an instrument designed to record vintages. It works because those lines happen to carry the EPS the strategy saw, and it is exactly the kind of accident that cannot be relied on twice. Dataset B exists because of this. It is the design that should have been running from the start, and the two-month lag between the two is the honest cost of having learned that late.

**12. One person, one machine, one API key.** No independent party has replicated either capture. The `analysis/build_disclosure.py` inputs live on a single laptop. The digests and the manifest hashes make the published dataset tamper-evident going forward; they say nothing about whether the capture that produced it was sound. The only real fix is someone else running their own poller, which is why the poller's design is specified above in enough detail to rebuild.

## Verifying this yourself

**Without an FMP account,** you can check that the published dataset is internally consistent and that the two rates follow from it:

```console
$ python analysis/eps_revision.py
```

The script verifies each CSV against the SHA-256 in `manifest.json`, checks every row's `eps_differs` flag against its digest pair and every `decision_flipped` against its tradeable pair, then prints the rates with confidence intervals, the magnitude distribution and the threshold sweep. It exits non-zero if anything disagrees.

**With your own FMP entitlement,** the rebuild path is `analysis/build_disclosure.py`, which documents the two private inputs it reads and the exact cohort filters it applies. To verify the digests row by row, ask me for the pepper — the custody arrangement, and what it does and does not disclose, is set out in [`analysis/README.md`](../analysis/README.md#custody-of-the-pepper). To verify the underlying claim rather than this dataset, run your own poller: hourly, snapshot on a change to `(epsActual, epsEstimated, reportDate, numAnalystsEps, grades_count)`, and set the post-print lookback to at least 90 days. Seven, which is what produced dataset B, is not enough to see the revisions that matter, and that is the single thing worth copying from this write-up rather than repeating.

## Files

- [`analysis/eps_revision.py`](../analysis/eps_revision.py) — recomputes the numbers, stdlib only
- [`analysis/CHANGELOG.md`](../analysis/CHANGELOG.md) — changes to the dataset and to the capture config behind it
- [`analysis/README.md`](../analysis/README.md) — how to run it, and custody of the pepper
- [`analysis/build_disclosure.py`](../analysis/build_disclosure.py) — builds the published dataset from the private captures
- [`analysis/data/manifest.json`](../analysis/data/manifest.json) — window, N, SHA-256 per file, decision rule, threshold sweep, pepper commitment
- [`analysis/data/eps_revision_qt_pit_2026h1.csv`](../analysis/data/eps_revision_qt_pit_2026h1.csv) — 2,163 records
- [`analysis/data/eps_revision_forward_poll_2026h2.csv`](../analysis/data/eps_revision_forward_poll_2026h2.csv) — 5,850 records
- [`docs/case-studies/fmp-revision.md`](case-studies/fmp-revision.md) — the narrative version

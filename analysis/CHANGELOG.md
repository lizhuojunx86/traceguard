# Changelog — the `epsActual` revision disclosure

Changes to the published dataset, to the capture configuration behind it, and to
the claims it supports. Separate from
[`packages/traceguard/CHANGELOG.md`](../packages/traceguard/CHANGELOG.md), which
versions the SDK and has nothing to do with this evidence.

A published dataset cannot be silently restated: the CSV SHA-256s and the
keyed digests in [`data/manifest.json`](data/manifest.json) freeze it. Any
rebuild that changes a number has to appear here, with the reason.

## [Unreleased]

### Security

- **The pepper was rotated and the whole dataset rebuilt under the new key**
  before any of this was published. The original key left the machine, which
  ends its usefulness: anyone holding it can enumerate the two-decimal EPS
  domain against the published digests and recover the vendor values, so every
  digest built under it is now equivalent to publishing the data.

  New commitment, in [`data/manifest.json`](data/manifest.json):
  `c78370af63a965302a17b536d43657d9a64217e0d046d47cdb4cd91f8bca3ae0`. The
  previous commitment `071e0f55…ebab79` is dead and should be treated as
  disclosed wherever it appears.

  Only `first_seen_hash` and `final_hash` changed. Verified by diffing the
  rebuilt CSVs against the previous ones cell by cell: **zero** non-digest cells
  differ, row counts hold at 2,163 and 5,850, and all four published rates —
  41.4%, 15.3%, 18.6%, 4.6% — are unchanged. The rebuild reads a frozen slice of
  the forward snapshot log (the first 35,952 lines, matching the raw volume
  recorded in the method doc) rather than the live file, which has since grown;
  rebuilding against the live file would have silently moved dataset B's numbers
  under cover of a key rotation. Reproducibility was confirmed first by
  rebuilding under the *old* key and getting both CSVs back byte for byte.

  Rotation was free here because nothing had been pushed. After publication it
  would not be: the digests are the published artifact, so rotating means
  reissuing the dataset and invalidating every verification anyone had done.

### Fixed

- **The forward capture's post-print observation horizon was 7 days, and that
  was a structural blind spot rather than a documented limitation.** The
  poller's `--lookback-days` gates both the earnings-calendar universe and the
  per-ticker snapshot filter, so a symbol left the polling universe seven days
  after it reported and was never looked at again. No revision arriving later
  than a week after the print could be observed *in principle*, and running the
  poller longer would never have surfaced one. Raised to **90 days** in
  `quant_alpha_v2` — `infra/launchd/com.qav2.vintage.forward-poll.plist` and the
  `--lookback-days` default in `scripts/vintage/forward_poll.py`, so a manual
  `--once` run does not silently reintroduce it.

  This is why dataset B reports 18.6% / 4.6% against dataset A's 41.4% / 15.3%.
  The two numbers were never in conflict; B was measuring "revised within a
  week" and A was measuring "revised within four months". B was a lower bound on
  A's quantity and was being read as an independent estimate of it, including by
  me.

  Measured cost, taken from the existing snapshot log rather than estimated: the
  polled universe grows 1.3× in season (6,905 → 9,582 symbols at 2026-08-05) and
  about 2.2× off-season. Earnings cluster, so a wider window mostly overlaps
  what was already being polled. Value-tuple dedup means the additional symbols
  write nothing on days when nothing changes.

  **This fix is forward-only.** `data/eps_revision_forward_poll_2026h2.csv` was
  captured under the 7-day setting and its horizon cap is permanent — no rebuild
  recovers observations that were never made. The shipped numbers are unchanged
  and the limitation stays documented in
  [`docs/eps-revision-methodology.md`](../docs/eps-revision-methodology.md).
  Records captured from now on are not subject to it; a future dataset C will
  be, and should be labelled with the horizon it was captured under.

### Added

- `analysis/README.md` gains a **Custody of the pepper** section: where the HMAC
  key lives, how to check it still matches the manifest commitment, how it is
  handed to an auditor, and what is permanently lost if the single copy is lost.
  The custody arrangement is one laptop with no backup, which is stated rather
  than left to be discovered.
- ToS containment tests in `tests/test_eps_revision.py`. The previous check
  asserted that no forbidden column *name* appeared in the CSV header, which a
  vendor value in a column called `symbol` passes. The published shape is now
  asserted positively — an exact column allowlist, closed vocabularies per
  categorical column, fixed-width opaque digests, no cell anywhere that parses
  as a number, and no numeric leaf in the manifest outside an allowlist of
  analysis parameters. Verified by mutation: four smuggling attempts (value
  glued into `symbol`, an added float column, an added manifest float, a value
  pasted into manifest prose) each fail at least one test.
- Tests that execute the pepper self-check command **out of**
  `analysis/README.md` and assert it reproduces `pepper_sha256`, following the
  pattern in tg-attest's `tests/test_readme_repro.py`. A command copied into a
  test goes stale the first time the document is edited. Three of these need the
  key and therefore skip in CI with the reason stated rather than passing
  silently; two run everywhere, because the trap they guard — hashing the key
  file's raw bytes instead of its decoded value — is visible in the command text
  without the key. A negative control pins that the raw-file digest is
  well-formed and wrong, which is what makes the mistake hard to spot.

## [2026-08-05] — first publication

- `data/eps_revision_qt_pit_2026h1.csv` (2,163 records) and
  `data/eps_revision_forward_poll_2026h2.csv` (5,850 records), with
  `data/manifest.json` committing to both by SHA-256.
- No vendor value published; per record, a keyed digest of each leg, whether
  they differ, direction, coarse magnitude bucket, and the decision booleans.
- `eps_revision.py` recomputes 41.4% / 15.3% from the committed data with the
  standard library alone, and exits non-zero on any internal inconsistency.

## Open, not fixed

- **The universe/horizon split in dataset B is still unmeasured.** Dataset A is
  a small/mid-cap strategy screen; dataset B is the whole US earnings calendar.
  Both differ from A, and how much of the 41.4% → 18.6% gap is horizon and how
  much is universe has not been separated. Raising the lookback fixes this
  prospectively but answers nothing about the already-captured window; that
  needs a settled-value re-pull for dataset B's 4,780 symbols, which costs FMP
  quota and has not been run.

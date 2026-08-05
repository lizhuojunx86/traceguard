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

- **Pepper rotated, dataset rebuilt under the new key.**

  **What happened.** While checking the key on disk against the manifest
  commitment, I pasted the pepper itself into a chat window. Not a digest of it,
  the key. It has to be treated as public from that point on. A public pepper is
  worth nothing: EPS values live in a domain of a few tens of thousands of
  two-decimal candidates, so anyone holding the key can walk that domain against
  the published digests and read the vendor values straight out. Every digest
  built under that key became equivalent to publishing the data.

  The check that caused it was also wrong, which is the part worth keeping. I
  ran `shasum -a 256` on the key file, got a digest that did not match
  `pepper_sha256`, and reached for the key to work out why. The mismatch was the
  method, not the key: `shasum` digests the file's 65 bytes, 64 ASCII hex
  characters plus a newline, where the manifest commits to the 32 bytes those
  characters decode to. The key had been correct the whole time. See the
  `Added` entry below for the tests that now execute the documented command
  instead of trusting it.

  **Timing.** Nothing had been pushed. No remote, no release, and nobody had
  verified anything against the old digests, so rotating cost one rebuild and a
  commit amend. That stops being true at publication. The digests are the
  artifact, so rotating afterwards means reissuing the dataset and invalidating
  every verification anyone had already run.

  **New commitment**, in [`data/manifest.json`](data/manifest.json):
  `c78370af63a965302a17b536d43657d9a64217e0d046d47cdb4cd91f8bca3ae0`

  The previous commitment `071e0f5522cf6d066bbe796110472f6ce329eab6da07d4e3b78f015622ebab79`
  is dead. Treat it as disclosed wherever it still appears. The old key was
  overwritten in place and not backed up; nothing needs it now, and keeping a
  compromised key is only a liability.

  **What the rebuild changed: the digests, and nothing else.** The rebuilt CSVs
  were diffed against the previous ones cell by cell. Only `first_seen_hash` and
  `final_hash` differ. Non-digest cells that changed: zero. Row counts hold at
  2,163 and 5,850. All four published rates are unchanged at 41.4%, 15.3%,
  18.6% and 4.6%, and `eps_revision.py` reports `integrity: OK`.

  Two things made that checkable rather than asserted. The rebuild reads a
  frozen slice of the forward snapshot log, its first 35,952 lines, matching the
  raw volume recorded in the method doc, rather than the live file, which has
  since grown to 36,271 lines as the poller keeps running. Building against the
  live file would have moved dataset B's numbers under cover of a key rotation,
  which is exactly the kind of change this changelog exists to prevent. And the
  pipeline was first re-run under the *old* key, returning both CSVs byte for
  byte, which is what proves the only variable in the real rebuild was the key.

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

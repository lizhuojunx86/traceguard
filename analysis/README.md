# analysis/

Published evidence for the `epsActual` revision claim quoted in this repo's
README, in [`docs/case-studies/fmp-revision.md`](../docs/case-studies/fmp-revision.md),
and in [tg-attest](https://github.com/lizhuojunx86/tg-attest).

```console
$ python analysis/eps_revision.py
```

Standard library only. It recomputes both headline rates from the committed
dataset, prints 95% confidence intervals and a threshold sweep, verifies every
row against its digest pair, and exits non-zero if anything disagrees.

| File | What it is |
|---|---|
| [`eps_revision.py`](eps_revision.py) | recomputes the numbers; contains the definitive statement of what "decision flip" means |
| [`build_disclosure.py`](build_disclosure.py) | builds the dataset from the private captures; run by the data holder, needs duckdb and the source artifacts |
| [`data/manifest.json`](data/manifest.json) | window, N, SHA-256 per file, decision rule, threshold sweep, pepper commitment |
| [`data/eps_revision_qt_pit_2026h1.csv`](data/eps_revision_qt_pit_2026h1.csv) | 2,163 records, Feb–Jun 2026 — the source of 41.4% / 15.3% |
| [`data/eps_revision_forward_poll_2026h2.csv`](data/eps_revision_forward_poll_2026h2.csv) | 5,850 records, Jun–Aug 2026 — a second capture giving 18.6% / 4.6% |
| [`CHANGELOG.md`](CHANGELOG.md) | changes to the dataset and to the capture config behind it |

No vendor value is published. Each record carries a keyed digest of the
first-seen and final values, whether they differ, the direction and a coarse
magnitude bucket. Enough to recompute the rates, not enough to reconstruct the
data.

Method, decision-flip definition, and the limitations that matter:
[`docs/eps-revision-methodology.md`](../docs/eps-revision-methodology.md).

## Custody of the pepper

The digests are HMAC-SHA256 under a 32-byte secret **pepper**. It is not in this
repo and must never be. `manifest.json` publishes only `pepper_sha256`, a
commitment to it.

**Where it lives.** `~/.local/share/traceguard-eps-disclosure/pepper`, mode
`0600`, 64 hex characters plus a newline. Created by `build_disclosure.py` when
the file is absent. Verify the key on disk still matches what the manifest
committed to:

```console
$ python3 -c "import hashlib,pathlib; print(hashlib.sha256(bytes.fromhex(pathlib.Path.home().joinpath('.local/share/traceguard-eps-disclosure/pepper').read_text().strip())).hexdigest())"
c78370af63a965302a17b536d43657d9a64217e0d046d47cdb4cd91f8bca3ae0
```

If that digest disagrees with `pepper_sha256` in
[`data/manifest.json`](data/manifest.json), the wrong key is on disk and no row
in the published CSVs can be reproduced with it.

**Do not check it with `shasum` on the file.** `shasum -a 256 pepper` digests
the file's 65 raw bytes — 64 ASCII hex characters plus the trailing newline —
whereas the commitment is over the 32 bytes those characters *decode to*. The
two never agree, and the failure is the dangerous kind: it produces a
plausible 64-character hex string that simply is not the committed value, so it
reads as "the key on disk is wrong" when the key is fine. The command above
hex-decodes first (`bytes.fromhex`), which is why it is written that way and
why `tests/test_eps_revision.py` executes it out of this file rather than
trusting it by eye. Hashing a *representation* instead of the *value* it stands
for has now cost this project three separate bugs; they are written up together,
with a checklist for new hash points, in
[tg-attest's fail-open audit](https://github.com/lizhuojunx86/tg-attest/blob/main/docs/fail-open-audit.md#hashing-the-representation-instead-of-the-value).

**Why it cannot be published.** An EPS value is one of a few tens of thousands
of plausible two-decimal candidates. With the key in hand, the entire domain is
enumerable in seconds, so a published pepper turns all 8,013 digests back into
the vendor values they stand for. That is straightforward redistribution under
FMP ToS §2.6.1(i), and it is the whole reason the digests are keyed rather than
bare SHA-256.

**How it reaches an auditor.** One named auditor at a time, out of band —
Signal, an encrypted mail attachment, or read aloud — never in a repo, an
issue, a pull request, a CI secret, or anything indexed. The auditor needs
their own FMP entitlement for it to be worth anything: the pepper alone
discloses nothing, it only lets someone who *already holds the values* confirm
that the published digests are digests of those values. Handing it over is
therefore not a disclosure of FMP data, which is precisely the property that
makes the scheme usable. There is no revocation. Once given, it is given, and
every past digest is open to that holder forever.

**If it is lost.** Backups: none. There is one copy, on one laptop, in a
directory that is not synced anywhere.

Losing it does not corrupt the published numbers — 41.4% and 15.3% are
recomputed from the boolean columns, and `eps_revision.py` keeps passing,
because its internal check only tests `first_seen_hash != final_hash` for
consistency with `eps_differs`. What dies is the link between those digests and
reality. Nobody, including me, could ever again demonstrate that a given digest
corresponds to a given vendor value. `eps_differs` reverts from *checkable* to
*trusted*, the freeze on the record set becomes unverifiable, and selective
row-by-row verification by a third party becomes impossible forever. The
dataset degrades into exactly the sort of unauditable assertion this project
exists to argue against. The private capture artifacts cannot rescue it either:
regenerating a pepper produces different digests, so the published CSVs and
their manifest SHA-256s would no longer match anything.

That is a single point of failure with no redundancy, and it is stated here
rather than left implicit. Anyone relying on these digests for more than
internal consistency should assume the custody arrangement is one laptop.

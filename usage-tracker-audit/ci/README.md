# Conformance checks as CI

Drop-in GitHub Actions workflows that run a tracker's conformance harness
(see [`CONFORMANCE.md`](../../CONFORMANCE.md)) against the tracker's own
repository on every push. Each harness builds a synthetic corpus with a
by-construction manifest, imports the tracker's own code, and exits 0/1 — so
the workflow is nothing but checkout + one script.

For maintainers: copy the matching `.yml` into your repo's
`.github/workflows/`, done. The harness pins nothing about your internals —
it calls the same entry points a user's data reaches — so it fails only when
a counted total stops matching the manifest.

| workflow | for | runs | wall time |
|---|---|---|---|
| `clawdmeter.yml` | weltern/Clawdmeter | `clawdmeter-dedup/run_check.sh --commit $GITHUB_SHA` | ~1 min |
| `cct.yml` | davila7/claude-code-templates | `cct-dedup-check/run_check.sh --commit $GITHUB_SHA` | ~2 min |
| `tokscale.yml` | junhoyeo/tokscale | `tokscale-drift-check/run_check.sh --bin <built binary>` | build + ~1 min |

Notes.

The harness clones the tracker at `$GITHUB_SHA`, so the check covers pushes
and same-repo pull requests. A PR from a fork has no such sha on the base
repository yet; gate on `push` (as these do) or check out the PR head and
pass `--work` a local clone.

`tokscale.yml` builds the release binary first and passes `--bin` — that is
how an unreleased branch gets checked, and it is the same flag the cache
migration's regression gate uses
([tokscale #1011](https://github.com/junhoyeo/tokscale/issues/1011)).

A green run asserts the invariants the harness exercises (the table in
`CONFORMANCE.md` says which), not all eleven. It is a regression gate for the
defect class that already shipped once, which is the class most likely to
ship again.

If you maintain a tracker that is not here and want a harness: the corpus
generator (`gen_corpus_streaming.py`, `cct-dedup-check/gen_corpus.py`) and
the vendor-import pattern (`clawdmeter-dedup/check_clawdmeter_v301.py` stubs
Qt in twelve lines rather than reimplementing the parser) are the two
reusable parts. Open an issue on this repo with a pointer to your token path.

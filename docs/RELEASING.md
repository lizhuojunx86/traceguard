# Releasing the traceguard SDK

The published package is `packages/traceguard` (PyPI name `traceguard`).
The root `pipeline-guardian` package is frozen and never published.

## One-time setup

1. Account at <https://pypi.org> with 2FA enabled.
2. A trusted publisher on the `traceguard` PyPI project (Manage project →
   Publishing → Add a new publisher): owner `lizhuojunx86`, repository
   `traceguard`, workflow `publish.yml`, environment `pypi`. These are the
   values `publish.yml`'s header comment lists and the publisher that 1.6.0's
   provenance names; it is what lets step 5 upload with no stored token.
3. Only for the fallback below: a project-scoped API token (scope:
   `traceguard`), stored somewhere safe.

## Release checklist

1. Bump the version in **both** places (they must match):
   - `packages/traceguard/pyproject.toml` → `version`
   - `packages/traceguard/src/traceguard/__init__.py` → `__version__`
2. Run the test suite: `cd packages/traceguard && uv sync --extra openai && uv run pytest`
   (keep the extra: a bare `uv sync` uninstalls the openai SDK that
   `scripts/routing_probe_daily.sh` runs with; the tests themselves pass
   without it)
3. Commit on a **release branch** and open a PR — releases go through a PR, not
   a direct push to `main` (see note below):

   ```bash
   git switch -c release/X.Y.Z
   git commit -am "chore(release): bump traceguard SDK to X.Y.Z"
   git push -u origin release/X.Y.Z
   gh pr create --base main --title "release/X.Y.Z: <summary>" --body "…"
   ```

4. After the PR is reviewed, merge it (a merge commit, matching the repo's
   release history), then tag the merged `main` and push the tag:

   ```bash
   gh pr merge --merge            # don't auto-merge without an explicit OK
   git switch main && git pull    # fast-forward to the merge commit
   git tag vX.Y.Z                 # the tag points at the merge commit on main
   git push origin vX.Y.Z
   ```

5. **Pushing the tag publishes.** `.github/workflows/publish.yml` triggers on
   `v*` and does the whole upload through PyPI Trusted Publishing (OIDC) — no
   token is typed, and none needs to exist on the machine. Watch it rather than
   doing anything:

   ```bash
   gh run list --workflow publish.yml --limit 1     # take the run id
   gh run watch <id>
   ```

   Three jobs, all of which must go green:

   - **build** — refuses the release unless the tag, `pyproject.toml`'s
     `version` and `__version__` all agree; runs the suite; rejects `* [0-9].py`
     sync duplicates; and checks the built artifacts are exactly two, named
     `traceguard-X.Y.Z-*`, with no `pipeline_guardian`.
   - **publish** — uploads under the `pypi` environment.
   - **verify** — installs the version back from PyPI on 3.12 and round-trips
     `__version__`.

   If **publish** sits in `waiting`, the `pypi` environment wants a manual
   approval: approve it on GitHub. Do not re-run via `workflow_dispatch` and do
   not fall back to a manual upload — a `workflow_dispatch` run has a different
   ref and the version check will not mean what it means here.

   If any job goes red **after the tag is pushed**: do not delete the tag, do
   not re-tag, do not upload by hand. The version number is already spent
   whether or not the upload happened (see the immutability note below). Read
   the log, fix forward on a new patch version.

6. Verify independently — not just by reading the workflow's own `verify` job.
   In an environment with no traceguard in it:

   ```bash
   uv run --no-project --python 3.12 --with "traceguard[anchors]==X.Y.Z" \
     python -c "import traceguard, traceguard.sources, traceguard.audit; print(traceguard.__version__)"
   uv run --no-project --python 3.12 --with "traceguard[anchors]==X.Y.Z" python -m traceguard.audit --help
   uv run --no-project --python 3.12 --with "traceguard[anchors]==X.Y.Z" python -m traceguard.sources --help
   ```

   The `--help` runs are not decoration: 1.6.0 shipped after a release-shaped
   branch in which an unescaped `%` in an argparse help string meant a CLI could
   not print its own help, and on 3.14 could not build its parser at all. That
   is invisible to `import`.

7. **Reproducibility check** — rebuild the tagged tree locally and compare
   digests with what PyPI serves. hatchling builds reproducibly by default
   (every timestamp pinned to 2020-02-02, file modes and owners normalized), so
   the tagged tree built with the same hatchling agrees with PyPI byte for byte.
   Build from an extracted copy of the tag, not from the working tree. `N` is
   the release PR's number, and the first line must print the same SHA twice:

   ```bash
   git rev-parse 'vX.Y.Z^{commit}'; gh pr view N --json mergeCommit -q .mergeCommit.oid
   tmp=$(mktemp -d)
   git -C "$(git rev-parse --show-toplevel)" archive vX.Y.Z | tar -x -C "$tmp"
   (cd "$tmp/packages/traceguard" && env -u SOURCE_DATE_EPOCH uv build && shasum -a 256 dist/*)
   curl -s https://pypi.org/simple/traceguard/ | grep -oE 'traceguard-X.Y.Z(-py3-none-any.whl|.tar.gz)#sha256=[0-9a-f]*'
   ```

   1.6.0: wheel `5ceb0a43…53f8`, sdist `2856c88a…de2b`, local == PyPI — in bash
   and zsh, from the repo root and from `packages/traceguard/`.

   Each part of that block removes a cause of false mismatches, all measured on
   the 1.6.0 tree:

   - **The extracted copy.** A build in the working tree packs untracked files
     under `packages/traceguard/` into the sdist — and into the wheel as well if
     they sit under `src/traceguard/` — unless the repo-root `.gitignore`
     excludes them. `git status` cannot vouch for the tree either: hatchling
     honours only that `.gitignore`, so a file git ignores through
     `.git/info/exclude` still lands in the sdist, and an uncommitted edit to the
     `.gitignore` changes the sdist too, because the file ships inside it. Both
     left `git status --short packages/traceguard` empty.
   - **`git -C "$(git rev-parse --show-toplevel)"`.** A plain `git archive` run
     from `packages/traceguard/` — where step 2 leaves the shell — exports only
     that directory.
   - **`env -u SOURCE_DATE_EPOCH`.** When the variable is set, hatchling uses it
     for every timestamp: `SOURCE_DATE_EPOCH=1700000000` changed both digests.

   What the block cannot control is **the hatchling version**. `build-system`
   requires only `hatchling>=1.27`, the version that ran is written into the
   wheel (`Generator:` in `*.dist-info/WHEEL`), and the default
   `Metadata-Version` moves between releases (2.4 in 1.27.0, 2.5 in 1.32.0), so
   building 1.6.0 with 1.27.0 instead of 1.32.0 changed both digests. On a
   mismatch, compare the two `Generator:` lines and rebuild in the same shell
   with the hatchling version from PyPI's `Generator:` line pinned as `A.B.C`:

   ```bash
   url=$(curl -s https://pypi.org/simple/traceguard/ | grep -oE 'https://files.pythonhosted.org/[^"#]*/traceguard-X.Y.Z-py3-none-any.whl') \
     && curl -fsSL "$url" -o "$tmp/pypi.whl" \
     && unzip -p "$tmp/pypi.whl" '*.dist-info/WHEEL' | grep Generator
   unzip -p "$tmp"/packages/traceguard/dist/*.whl '*.dist-info/WHEEL' | grep Generator
   (cd "$tmp/packages/traceguard" && rm -rf dist \
     && env -u SOURCE_DATE_EPOCH uv build --build-constraints <(echo "hatchling==A.B.C") \
     && shasum -a 256 dist/*)
   ```

   A mismatch that survives the pinned rebuild is the one worth investigating.

8. **Provenance attestations** — `publish.yml` uploads through Trusted
   Publishing, and `pypa/gh-action-pypi-publish` generates an attestation for
   each file on the way in. Every file from 1.1.1 on has a provenance file on
   PyPI, linked from the simple index by a `data-provenance` attribute; 0.2.0
   through 1.1.0, released before `publish.yml` existed, have none. List a
   release's links, then verify each file against this repository:

   ```bash
   curl -s https://pypi.org/simple/traceguard/ | grep -oE 'data-provenance="https://pypi.org/integrity/traceguard/X.Y.Z/[^"]*"'
   uvx --from pypi-attestations==0.0.30 pypi-attestations verify pypi --repository https://github.com/lizhuojunx86/traceguard pypi:traceguard-X.Y.Z-py3-none-any.whl
   uvx --from pypi-attestations==0.0.30 pypi-attestations verify pypi --repository https://github.com/lizhuojunx86/traceguard pypi:traceguard-X.Y.Z.tar.gz
   ```

   For 1.6.0 both verifications print `OK:` and exit 0; naming another
   repository fails with exit 1, and so does a version with no provenance
   ("Provenance for file … was not found"). Each provenance URL returns a
   provenance object whose `attestation_bundles[].publisher` names the
   repository, `publish.yml` and the `pypi` environment. The format is PEP 740,
   now maintained as PyPA's
   [Index hosted attestations](https://packaging.python.org/en/latest/specifications/index-hosted-attestations/)
   spec.

   For an evidence tool, the provenance of its own artifacts is part of the
   claim, so keep the upload on Trusted Publishing. As of 2026-09, PyPI accepts
   attestations only from Trusted Publishing uploads: give the publish step a
   long-lived API token (`password:`) and the action skips the attestations with
   a warning while the release still goes green. The fallback below ships
   without provenance for the same reason.

9. Create the GitHub release: `gh release create vX.Y.Z --title "vX.Y.Z" --notes-file <the CHANGELOG section>`

### Fallback: publishing by hand

Only if `publish.yml` cannot run at all (the workflow is broken, or Trusted
Publishing is misconfigured) — and only for a version number that has never
been uploaded:

```bash
cd packages/traceguard
uv build                       # MUST show traceguard-X.Y.Z, not pipeline_guardian
read -s "PYPI_TOKEN?paste the project-scoped PyPI token, then Enter: "
uv publish --token "$PYPI_TOKEN" \
  dist/traceguard-X.Y.Z-py3-none-any.whl dist/traceguard-X.Y.Z.tar.gz
curl -s https://pypi.org/simple/traceguard/ | grep X.Y.Z
```

Publish the **explicit** version files so old artifacts left in `dist/` are not
re-uploaded. This path skips every check the `build` job does, so run
`uv run pytest` and eyeball `dist/` yourself first.

> **Why a PR, not `git push origin main`?** Releases land through
> `release/X.Y.Z` branches merged via PR (e.g. #5, #6, #7); a direct push to the
> default branch is blocked by policy. Tag the **merge commit** after the PR
> lands, never before. The PyPI publish is irreversible and needs an explicit,
> per-release go-ahead.

## Versioning rules

SemVer per `docs/SPEC.md` §6: breaking a MUST field, an SDK signature, the
normalize algorithm, or an invariant definition = major. New methods/fields/
invariants = minor. Bugfixes = patch.

PyPI versions are immutable: a published version number can never be reused,
even after deletion. That is why the checks live in the `build` job, before the
upload, and why a mistyped tag has no way back — double-check step 4's tag
against both version files before pushing it.

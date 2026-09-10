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
   token is typed, and none needs to exist on the machine. Nothing stands
   between the push and the upload: the `pypi` environment has no required
   reviewers (as of 2026-09), and on 1.6.0 `publish` started three seconds after
   `build` finished. The tag push is the point of no return, so the go-ahead has
   to come before step 4. Watch the run, taking `<id>` from the first command:

   ```bash
   gh run list --workflow publish.yml --branch vX.Y.Z --limit 1
   gh run watch <id> --exit-status
   ```

   Three jobs, all of which must go green:

   - **build** — refuses the release unless the tag, `pyproject.toml`'s
     `version` and `__version__` all agree; runs the suite; rejects `* [0-9].py`
     sync duplicates; and checks that `dist/` holds exactly
     `traceguard-X.Y.Z-py3-none-any.whl` and `traceguard-X.Y.Z.tar.gz`, with no
     `pipeline_guardian`.
   - **publish** — uploads under the `pypi` environment.
   - **verify** — installs the version back from PyPI on 3.12 and round-trips
     `__version__`.

   Never start `publish.yml` by hand (`workflow_dispatch`). A dispatched run
   builds the ref it was started on — the default branch unless `--ref` is
   given — and checks the typed version only against that ref's own files, so a
   dispatch from `main` after `main` has moved on without a new bump publishes
   `main`'s tip under the tag's version. To retry, re-run the tag's own run.

   If a job goes red, read its log before anything else, and never delete or
   move the tag: downstream repos pin tags, so a pushed tag stays where it is
   whether or not anything was uploaded. What comes next depends on the job:

   - **build** — nothing was uploaded. For a transient failure, re-run the
     failed jobs of the same run (`gh run rerun <id> --failed`); for a defect in
     the tagged code, fix it and release a new patch version.
   - **publish** — check the index first:
     `curl -s https://pypi.org/simple/traceguard/ | grep -oE 'traceguard-X.Y.Z(-py3-none-any.whl|.tar.gz)#sha256=[0-9a-f]*'`
     (the verify job gives the index up to about 150 seconds to catch up). Both
     files listed: the upload happened; go on to step 6. Only one: stop and read
     the log again. Neither listed: nothing was uploaded. If the log reports the
     token exchange failing with "This generally indicates a trusted publisher
     configuration error", correct the publisher on PyPI (one-time setup, item
     2) and re-run the whole run (`gh run rerun <id>`), which keeps provenance;
     otherwise handle it like a red build.
   - **verify** — the release is already on PyPI. This happened on 1.1.1 and
     1.3.0, and neither needed a new version: re-run the job
     (`gh run rerun <id> --failed`) or do step 6 by hand.

   None of these is a reason for the manual upload below.

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

A deliberate decision, never a reflex to a red run (step 5): only when
`publish.yml` cannot publish this release at all and the release cannot wait —
GitHub Actions itself being unavailable, say — and only for a version whose
files are not on the index. A broken `publish.yml` inside the tag is a defect
in the tagged code (a new patch version), and a misconfigured trusted publisher
is corrected on PyPI and re-run (step 5); neither is a reason to come here. The
path is kept on purpose as a break-glass option, and it costs two things: the
only checks are the ones you run below, and the release ships without
provenance (step 8), permanently for that version.

Build the tagged tree, not the working tree (step 7 says why), and check it —
the build job's version agreement, the suite and the artifact names — before
anything leaves the machine:

```bash
tmp=$(mktemp -d)
git -C "$(git rev-parse --show-toplevel)" archive vX.Y.Z | tar -x -C "$tmp"
cd "$tmp/packages/traceguard" && grep -m1 '^version = "X.Y.Z"' pyproject.toml \
  && grep -m1 '^__version__ = "X.Y.Z"' src/traceguard/__init__.py \
  && uv run pytest -q \
  && env -u SOURCE_DATE_EPOCH uv build \
  && ls dist/
```

Both `grep` lines must print, the suite must pass, and `dist/` must hold exactly
`traceguard-X.Y.Z-py3-none-any.whl` and `traceguard-X.Y.Z.tar.gz`. Only then,
from the same directory, paste the block below whole. The parentheses make the
shell read all of it before `read` asks for the token. An empty line — a stray
one from the paste, say — only repeats the prompt, and Ctrl-D stops without
uploading. The token reaches `uv` through the environment rather than on its
command line, and it exists only inside that subshell, so nothing is left in
the shell afterwards, even when the upload is interrupted:

```bash
(
  PYPI_TOKEN=
  while [ -z "$PYPI_TOKEN" ]; do
    printf 'project-scoped PyPI token: '; read -rs PYPI_TOKEN || exit 1; echo
  done
  UV_PUBLISH_TOKEN="$PYPI_TOKEN" uv publish \
    dist/traceguard-X.Y.Z-py3-none-any.whl dist/traceguard-X.Y.Z.tar.gz
  curl -s https://pypi.org/simple/traceguard/ | grep -oE 'traceguard-X.Y.Z(-py3-none-any.whl|.tar.gz)#sha256=[0-9a-f]*'
)
```

Naming the two files explicitly means nothing else in `dist/` can ride along.

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

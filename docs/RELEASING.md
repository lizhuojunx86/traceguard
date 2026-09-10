# Releasing the traceguard SDK

The published package is `packages/traceguard` (PyPI name `traceguard`).
The root `pipeline-guardian` package is frozen and never published.

## One-time setup

1. Account at <https://pypi.org> with 2FA enabled.
2. A project-scoped API token (scope: `traceguard`), stored somewhere safe.
   The very first publish requires an account-scoped token (the project
   doesn't exist yet); replace it with a project-scoped one afterwards.

## Release checklist

1. Bump the version in **both** places (they must match):
   - `packages/traceguard/pyproject.toml` → `version`
   - `packages/traceguard/src/traceguard/__init__.py` → `__version__`
2. Run the test suite: `cd packages/traceguard && uv sync && uv run pytest`
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

7. Create the GitHub release: `gh release create vX.Y.Z --title "vX.Y.Z" --notes-file <the CHANGELOG section>`

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

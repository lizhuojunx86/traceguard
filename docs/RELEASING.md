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

5. Build and publish — publish the **explicit** version files so old artifacts
   left in `dist/` aren't re-uploaded:

   ```bash
   cd packages/traceguard
   uv build                       # MUST show traceguard-X.Y.Z, not pipeline_guardian
   read -s "PYPI_TOKEN?paste the project-scoped PyPI token, then Enter: "
   uv publish --token "$PYPI_TOKEN" \
     dist/traceguard-X.Y.Z-py3-none-any.whl dist/traceguard-X.Y.Z.tar.gz
   ```

6. Verify the upload (the `simple` index refreshes faster than the JSON API):

   ```bash
   curl -s https://pypi.org/simple/traceguard/ | grep X.Y.Z
   uv run --no-project --with "traceguard==X.Y.Z" python -c "import traceguard; print(traceguard.__version__)"
   ```

7. Create the GitHub release: `gh release create vX.Y.Z --title "vX.Y.Z" --notes "..."`

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
even after deletion. Double-check the build output before `uv publish`.

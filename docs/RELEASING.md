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
3. Commit, tag, and push:

   ```bash
   git commit -am "chore(release): bump traceguard SDK to X.Y.Z"
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```

4. Build and publish:

   ```bash
   cd packages/traceguard
   rm -rf dist
   uv build                       # MUST show traceguard-X.Y.Z, not pipeline_guardian
   read -s "PYPI_TOKEN?paste PyPI token, then Enter: "
   uv publish --token "$PYPI_TOKEN"
   ```

5. Verify the upload:

   ```bash
   uv run --no-project --with "traceguard==X.Y.Z" python -c "import traceguard; print(traceguard.__version__)"
   ```

6. Create the GitHub release: `gh release create vX.Y.Z --title "vX.Y.Z" --notes "..."`

## Versioning rules

SemVer per `docs/SPEC.md` §6: breaking a MUST field, an SDK signature, the
normalize algorithm, or an invariant definition = major. New methods/fields/
invariants = minor. Bugfixes = patch.

PyPI versions are immutable: a published version number can never be reused,
even after deletion. Double-check the build output before `uv publish`.

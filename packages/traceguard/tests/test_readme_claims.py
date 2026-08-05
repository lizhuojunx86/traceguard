"""Guard the README's factual claims against the code they describe.

The README sat five releases behind the implementation (invariant 4 was
advertised as "planned (Phase 2)" for four minor versions after it shipped in
0.8.0). For a tool whose entire pitch is "your claims should match what was
actually true", that is not a cosmetic defect.

These tests assert *relationships*, never frozen constants: each expectation is
recomputed from the current code, so a legitimate change (a new validator, a
version bump, a symbol added to the public surface) fails here only when the
README was not updated alongside it.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

import traceguard
from traceguard.validators import lookahead

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"
CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"


@pytest.fixture(scope="module")
def readme() -> str:
    if not README.is_file():
        pytest.skip(f"README not reachable from the package tree: {README}")
    return README.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Return the body of a `## heading` section, up to the next `##`."""
    match = re.search(
        rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"README has no '## {heading}' section"
    return match.group(1)


def test_invariant_table_lists_every_validator(readme: str) -> None:
    """The four-invariant table must name exactly the validators that exist.

    Catches both directions: a validator shipped but still advertised as
    planned, and a validator removed/renamed while the table still claims it.
    """
    implemented = {
        name
        for name, obj in vars(lookahead).items()
        if not name.startswith("_")
        and inspect.isfunction(obj)
        and obj.__module__ == lookahead.__name__
    }

    # Read the Validator column only — the Invariant column is full of field
    # names (`feature_as_of`, `valid_from`, …) that are not validators.
    advertised: set[str] = set()
    for line in _section(readme, "The four invariants").splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 5 or not cells[1].isdigit():
            continue  # header, separator, or prose
        advertised.update(re.findall(r"`(\w+)`", cells[3]))

    assert advertised == implemented, (
        "README invariant table is out of sync with "
        "traceguard.validators.lookahead.\n"
        f"  only in README: {sorted(advertised - implemented)}\n"
        f"  only in code:   {sorted(implemented - advertised)}"
    )


def test_public_surface_size_claim_matches(readme: str) -> None:
    """Any "N-symbol public surface" claim must equal len(__all__).

    Scoped to "public surface" on purpose: pipeline-guardian's separate
    "4-symbol public API" is a different, frozen package and not this count.
    """
    claims = re.findall(r"(\d+)-symbol public surface", readme)
    if not claims:
        pytest.skip("README makes no numeric claim about the public surface")

    actual = len(traceguard.__all__)
    for claimed in claims:
        assert int(claimed) == actual, (
            f"README advertises a {claimed}-symbol public surface, "
            f"but traceguard.__all__ holds {actual}"
        )


def test_readme_mentions_the_current_minor_series() -> None:
    """The README's version narrative must reach the current MAJOR.MINOR.

    Patch releases are exempt — 1.1.1 satisfies a README that discusses 1.1.
    A new minor with user-visible capability is expected to land in the README
    in the same change.
    """
    if not README.is_file() or not CHANGELOG.is_file():
        pytest.skip("README or CHANGELOG not reachable from the package tree")

    versions = re.findall(r"^## \[(\d+\.\d+)\.\d+\]", CHANGELOG.read_text("utf-8"), re.MULTILINE)
    assert versions, "CHANGELOG has no parseable release headings"
    latest_series = versions[0]

    assert latest_series in README.read_text("utf-8"), (
        f"CHANGELOG's newest release series is {latest_series}, "
        "but the README never mentions it — the version narrative has drifted"
    )

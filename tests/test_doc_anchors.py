"""Every `](...#anchor)` link in the shipped docs must point at a real heading.

A broken anchor does not 404. GitHub serves the file with HTTP 200 and silently
scrolls to the top, so link checkers that only look at status codes report the
link as healthy. The reader clicks, lands somewhere plausible, and concludes the
document is disorganised rather than that the link is wrong.

That is the same shape as the other defects this project keeps hitting: a
success signal that is not a correctness signal. See the representation-vs-value
section of tg-attest's fail-open audit, and `test_eps_revision.py`'s reason for
executing the pepper self-check instead of reading it.

Scope is intra-repo anchors, checked offline against the files on disk. Anchors
that point into ANOTHER repository resolve against that repository's default
branch, which this test suite does not control and which can break without a
commit here. Those are verified at publish time rather than build time; see the
cross-repo check in the release checklist.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def github_slug(heading: str) -> str:
    """Reproduce github-slugger, which is what GitHub renders anchors with.

    Lowercase, strip HTML tags, drop everything that is not a word character,
    space or hyphen (this is what removes backticks, punctuation and emphasis
    markers), then spaces to hyphens.
    """
    s = heading.strip().lower()
    s = re.sub(r"<[!/a-z][^>]*>", "", s)
    s = re.sub(r"[^\w\- ]", "", s, flags=re.UNICODE)
    return s.replace(" ", "-")


def heading_anchors(markdown: str) -> set[str]:
    """Anchor set GitHub would generate for one document.

    Fenced blocks are skipped: a `#` inside a shell sample is a comment, not a
    heading, and counting it would invent anchors that do not exist. Repeated
    headings get github-slugger's `-1`, `-2` suffixes.
    """
    found: list[str] = []
    seen: dict[str, int] = {}
    fence: str | None = None
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            token = stripped[:3]
            fence = None if fence == token else (fence or token)
            continue
        if fence is not None:
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if not m:
            continue
        slug = github_slug(m.group(2))
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        found.append(slug if n == 0 else f"{slug}-{n}")
    return set(found)


# Directories that hold copies of documents rather than documents. `git
# ls-files` already excludes them; the walk fallback has to do it by name.
_WALK_SKIP = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "build",
    "dist",
    "splitrail-validation",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "__pycache__",
    "htmlcov",
}


def tracked_markdown() -> list[Path]:
    """Only files git actually ships. Vendored working copies are not docs.

    Falls back to a filesystem walk when git is unavailable: run from an
    unpacked sdist there is no `.git`, and `git ls-files` exits 128.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True
        )
        paths = [ROOT / line for line in out.stdout.splitlines() if line]
        if paths:
            return paths
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    return [
        p for p in sorted(ROOT.rglob("*.md")) if not _WALK_SKIP & set(p.relative_to(ROOT).parts)
    ]


# `](target#anchor)` — target may be empty, meaning "this same file".
ANCHOR_LINK = re.compile(r"\]\(([^)\s]*)#([^)\s]+)\)")


def _intra_repo_anchor_links():
    for md in tracked_markdown():
        text = md.read_text(encoding="utf-8", errors="replace")
        for m in ANCHOR_LINK.finditer(text):
            target, anchor = m.group(1), m.group(2)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            yield md, target, anchor


def test_every_intra_repo_anchor_points_at_a_real_heading():
    problems = []
    checked = 0
    for md, target, anchor in _intra_repo_anchor_links():
        rel = md.relative_to(ROOT)
        dest = md if target == "" else (md.parent / target)
        if not dest.exists():
            problems.append(f"{rel}: link target does not exist: {target}")
            continue
        checked += 1
        anchors = heading_anchors(dest.read_text(encoding="utf-8", errors="replace"))
        if anchor.lower() not in anchors:
            near = sorted(a for a in anchors if a[:6] == anchor.lower()[:6])
            problems.append(
                f"{rel}: '#{anchor}' is not a heading in "
                f"{dest.relative_to(ROOT)}" + (f" (did you mean {near}?)" if near else "")
            )
    assert problems == [], "\n".join(problems)
    assert checked > 0, "no anchored links found — the extractor is broken"


def test_the_checker_rejects_an_anchor_that_does_not_exist():
    """Negative control.

    Without this, the test above passes just as happily when `heading_anchors`
    returns everything, or when the extractor silently matches nothing.
    """
    doc = "# Real Heading\n\ntext\n\n## Another One\n"
    anchors = heading_anchors(doc)
    assert "real-heading" in anchors
    assert "another-one" in anchors
    assert "no-such-heading" not in anchors

    # And end to end: a fabricated anchor against a document that ships.
    target = ROOT / "analysis" / "README.md"
    assert "custody-of-the-pepper" in heading_anchors(target.read_text(encoding="utf-8"))
    assert "custody-of-the-peppr" not in heading_anchors(target.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Custody of the pepper", "custody-of-the-pepper"),
        ("## Why the two datasets disagree", "-why-the-two-datasets-disagree"),
        ("`code` and **bold**", "code-and-bold"),
        (
            "Hashing the representation, instead of the value",
            "hashing-the-representation-instead-of-the-value",
        ),
        ("Verify it yourself, in about 30 seconds", "verify-it-yourself-in-about-30-seconds"),
    ],
)
def test_slug_matches_github(heading, expected):
    """Pins the slug rules that make the rest of this file meaningful."""
    assert github_slug(heading) == expected

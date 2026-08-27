"""Catch file-sync damage to the installed environment, by name.

The repo already refuses to commit or package sync conflict copies
(``.gitignore`` ``* 2.*``, pyproject ``exclude``, conftest
``collect_ignore_glob``). All three protect the source tree and the wheel.
None of them looks at the environment the code is installed into — which is
where it actually bit on 2026-08-26: three copies of the editable-install path
file,

    _editable_impl_traceguard.pth
    _editable_impl_traceguard 2.pth
    _editable_impl_traceguard 3.pth

and ``import traceguard`` failing from anywhere but ``PYTHONPATH=src``. The
symptom was a bare ``ModuleNotFoundError`` that named neither file sync nor the
editable install, and it cost several rounds to trace.

These tests do not fix sync; they make its damage announce itself.
"""
from __future__ import annotations

import site
import sys
from pathlib import Path

import pytest

#: A sync conflict copy is "<name> <digit>.<ext>".
_CONFLICT_GLOB = "* [0-9].*"


def _site_packages_dirs() -> list[Path]:
    dirs = [Path(p) for p in site.getsitepackages()]
    user = getattr(site, "getusersitepackages", None)
    if callable(user):
        try:
            dirs.append(Path(user()))
        except Exception:  # noqa: BLE001 - absent on some layouts
            pass
    return [d for d in dirs if d.is_dir()]


def test_no_conflict_copies_of_path_files_in_site_packages() -> None:
    """Duplicated .pth files break the editable install silently."""
    offenders = [
        p
        for d in _site_packages_dirs()
        for p in d.glob(_CONFLICT_GLOB)
        if p.suffix == ".pth"
    ]
    assert not offenders, (
        "file-sync conflict copies of .pth files found in site-packages:\n  "
        + "\n  ".join(str(p) for p in offenders)
        + "\n\nThese can stop the editable install from being picked up, which "
        "surfaces as `ModuleNotFoundError: No module named 'traceguard'` even "
        "after a successful `uv sync`. Delete them and reinstall:\n"
        "  uv sync --reinstall-package traceguard\n"
        "Consider moving the repo out of an iCloud/Dropbox-synced directory."
    )


def test_the_package_is_importable_the_normal_way() -> None:
    """Guards the failure mode itself, not just one of its causes.

    Passing means ``import traceguard`` resolved without PYTHONPATH help —
    the tests themselves set ``pythonpath = ["src"]``, so this asserts on where
    the module was actually found rather than on the import succeeding.
    """
    import traceguard

    location = Path(traceguard.__file__).resolve()
    src_dir = Path(__file__).resolve().parent.parent / "src"
    on_path = any(
        Path(p).resolve() in (src_dir, *_site_packages_dirs())
        for p in sys.path
        if p
    )
    assert location.is_file(), traceguard.__file__
    assert on_path, (
        "traceguard resolved from an unexpected location; the environment may "
        f"be half-installed. Module at: {location}"
    )


def test_no_conflict_copies_in_the_package_source() -> None:
    """A stray '<name> 2.py' in src/ is a module that shadows nothing and
    imports badly; 0.8.1 shipped some into a release before this was caught."""
    src = Path(__file__).resolve().parent.parent / "src"
    offenders = sorted(str(p.relative_to(src)) for p in src.rglob(_CONFLICT_GLOB))
    assert not offenders, (
        "file-sync conflict copies inside the package source:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("pattern", ["* 2.py", "* 3.py", "* 4.py"])
def test_conftest_ignores_every_conflict_generation(pattern: str) -> None:
    """.gitignore learned to cover ' 3.' and ' 4.'; conftest had not."""
    # Load the sibling conftest.py by path: ``tests`` is not a package (no
    # __init__.py) and is not on sys.path under ``uv run pytest``, so a
    # ``from tests import conftest`` only works when the cwd happens to be
    # importable (``python -m pytest``) — CI ran it the other way and was red.
    import importlib.util

    location = Path(__file__).with_name("conftest.py")
    spec = importlib.util.spec_from_file_location("_traceguard_tests_conftest", location)
    assert spec is not None and spec.loader is not None
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)

    assert pattern in conftest.collect_ignore_glob

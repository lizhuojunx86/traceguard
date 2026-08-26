"""Shared pytest fixtures for traceguard test suite."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine

from traceguard.store.models import Base, make_engine

# iCloud/file-sync duplicates new files as "<name> 2.py"; those stale copies
# would be collected as tests. Ignore them (mirrors the build-time
# `exclude = ["* 2.py"]` in pyproject).
#
# " 3." and " 4." are included because sync produces them once a conflict
# repeats — .gitignore already learned this ("extension-by-extension lists kept
# missing new cases") and this list had not caught up.
collect_ignore_glob = ["* 2.py", "* 3.py", "* 4.py"]


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine, schema fresh per test."""
    eng = make_engine("sqlite:///:memory:", create_all=True)
    yield eng
    Base.metadata.drop_all(eng)

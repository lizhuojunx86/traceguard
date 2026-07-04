"""Shared pytest fixtures for traceguard test suite."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine

from traceguard.store.models import Base, make_engine

# iCloud/file-sync duplicates new files as "<name> 2.py"; those stale copies
# would be collected as tests. Ignore them (mirrors the build-time
# `exclude = ["* 2.py"]` in pyproject).
collect_ignore_glob = ["* 2.py"]


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine, schema fresh per test."""
    eng = make_engine("sqlite:///:memory:", create_all=True)
    yield eng
    Base.metadata.drop_all(eng)

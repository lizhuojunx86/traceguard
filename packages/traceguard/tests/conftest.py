"""Shared pytest fixtures for traceguard test suite."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine

from traceguard.store.models import Base, make_engine


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine, schema fresh per test."""
    eng = make_engine("sqlite:///:memory:", create_all=True)
    yield eng
    Base.metadata.drop_all(eng)

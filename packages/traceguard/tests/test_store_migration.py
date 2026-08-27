"""SPEC v1.1 columns on databases created before them: added on open, additive only.

Once ``agent_id`` / ``session_id`` are mapped on ``Trace``, every ORM SELECT
names them — a 1.0-era database would fail on READ, not just on write. So
``make_engine`` brings the ``traces`` table up to date on every open, with a
plain ``ALTER TABLE ... ADD COLUMN`` (nullable) plus the index, and nothing
else. These tests build the legacy schema by dropping the two columns from a
fresh one, so the "before" state is exactly what 1.x created.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from traceguard.store.models import (
    TRACE_COLUMNS_ADDED_SINCE_1_0,
    Trace,
    ensure_trace_columns,
    make_engine,
)

pytestmark = pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 35),
    reason="building the legacy schema needs ALTER TABLE DROP COLUMN (SQLite >= 3.35)",
)


def _legacy_db(path: Path) -> str:
    """A database exactly as pre-v1.1 traceguard left it: one trace, no new columns."""
    url = f"sqlite:///{path}"
    engine = make_engine(url)  # full current schema ...
    with Session(engine) as sess:
        sess.add(
            Trace(
                project="legacy",
                component="c",
                operation="llm_complete",
                input_hash="h" * 64,
                parse_status="success",
                invoked_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            )
        )
        sess.commit()
    engine.dispose()
    con = sqlite3.connect(path)  # ... minus the v1.1 columns and their indexes
    for name in TRACE_COLUMNS_ADDED_SINCE_1_0:
        con.execute(f"DROP INDEX IF EXISTS ix_traces_{name}")
        con.execute(f"ALTER TABLE traces DROP COLUMN {name}")
    con.commit()
    con.close()
    return url


def _columns(engine) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns("traces")}


def _indexes(engine) -> set[str]:
    return {ix["name"] for ix in inspect(engine).get_indexes("traces")}


def test_legacy_schema_really_lacks_the_columns(tmp_path: Path) -> None:
    url = _legacy_db(tmp_path / "legacy.db")
    raw = create_engine(url)  # not make_engine: no migration on this open
    assert not (set(TRACE_COLUMNS_ADDED_SINCE_1_0) & _columns(raw))
    raw.dispose()


def test_make_engine_adds_columns_and_indexes_on_open(tmp_path: Path) -> None:
    url = _legacy_db(tmp_path / "legacy.db")
    engine = make_engine(url)
    assert set(TRACE_COLUMNS_ADDED_SINCE_1_0) <= _columns(engine)
    assert {f"ix_traces_{n}" for n in TRACE_COLUMNS_ADDED_SINCE_1_0} <= _indexes(engine)

    with Session(engine) as sess:  # the pre-existing row reads back with NULLs
        old = sess.scalars(select(Trace).where(Trace.project == "legacy")).one()
        assert old.agent_id is None and old.session_id is None
        sess.add(  # and new rows can carry the identity dimensions
            Trace(
                project="new",
                component="c",
                operation="llm_complete",
                input_hash="h" * 64,
                parse_status="success",
                invoked_at=datetime.now(timezone.utc),
                agent_id="agent-7",
                session_id="run-42",
            )
        )
        sess.commit()
    with Session(engine) as sess:
        new = sess.scalars(select(Trace).where(Trace.project == "new")).one()
        assert (new.agent_id, new.session_id) == ("agent-7", "run-42")


def test_migration_is_idempotent(tmp_path: Path) -> None:
    url = _legacy_db(tmp_path / "legacy.db")
    first = create_engine(url)
    assert set(ensure_trace_columns(first)) == set(TRACE_COLUMNS_ADDED_SINCE_1_0)
    assert ensure_trace_columns(first) == []  # second call: nothing left to add
    first.dispose()
    again = make_engine(url)  # a later open changes nothing either
    assert ensure_trace_columns(again) == []
    assert set(TRACE_COLUMNS_ADDED_SINCE_1_0) <= _columns(again)


def test_migration_runs_even_without_create_all(tmp_path: Path) -> None:
    """create_all=False callers still get a readable traces table."""
    url = _legacy_db(tmp_path / "legacy.db")
    engine = make_engine(url, create_all=False)
    assert set(TRACE_COLUMNS_ADDED_SINCE_1_0) <= _columns(engine)
    with Session(engine) as sess:
        assert sess.scalars(select(Trace)).one().project == "legacy"


def test_no_traces_table_is_a_noop() -> None:
    engine = create_engine("sqlite:///:memory:")
    assert ensure_trace_columns(engine) == []
    assert "traces" not in inspect(engine).get_table_names()


def test_fresh_schema_needs_no_migration(engine) -> None:
    # The shared in-memory fixture goes through make_engine: create_all built
    # the columns, so the migration path had nothing to do.
    assert ensure_trace_columns(engine) == []
    assert set(TRACE_COLUMNS_ADDED_SINCE_1_0) <= _columns(engine)


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions"
)
def test_read_only_legacy_db_fails_loudly_with_the_manual_statement(tmp_path: Path) -> None:
    """A DB the process cannot ALTER is unreadable by the ORM anyway; say so."""
    path = tmp_path / "legacy.db"
    url = _legacy_db(path)
    path.chmod(0o444)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            make_engine(url, create_all=False)
        message = str(excinfo.value)
        assert "ALTER TABLE traces ADD COLUMN agent_id" in message
        assert "SPEC v1.1" in message
    finally:
        path.chmod(0o644)

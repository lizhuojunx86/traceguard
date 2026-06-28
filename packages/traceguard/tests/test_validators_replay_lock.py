"""Invariant 4 read-side validator + the sanctioned replay-set write-path.

assert_replay_set_locked is meaningless without a way to produce a locked set,
so these test both together: the write-path (create/add/lock and the convenience
builder) and the validator's pass/fail directions, including the un-migrated-DB
trap surfacing as a clear InvariantViolation.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.registry.replay import (
    add_replay_item,
    build_locked_replay_set,
    create_replay_set,
    lock_replay_set,
)
from traceguard.store.models import ReplaySet, ReplaySetItem, ReplaySetLockedError, make_engine
from traceguard.validators.lookahead import InvariantViolation, assert_replay_set_locked


def test_write_path_create_add_lock(engine):
    create_replay_set("demo/x/golden", project="demo", component="x", engine=engine)
    i0 = add_replay_item("demo/x/golden", input_payload={"q": "a"}, engine=engine)
    i1 = add_replay_item(
        "demo/x/golden", input_payload={"q": "b"}, expected_output={"y": 1}, engine=engine
    )
    assert (i0, i1) == (0, 1)  # auto-assigned indices

    # Not locked yet -> invariant 4 fails.
    with pytest.raises(InvariantViolation) as exc:
        assert_replay_set_locked("demo/x/golden", engine=engine)
    assert exc.value.invariant == 4

    lock_replay_set("demo/x/golden", engine=engine)
    assert_replay_set_locked("demo/x/golden", engine=engine)  # now passes

    with Session(engine) as sess:
        rs = sess.get(ReplaySet, "demo/x/golden")
        assert rs.is_locked is True
        assert rs.item_count == 2


def test_lock_is_idempotent(engine):
    create_replay_set("s", project="p", component="c", engine=engine)
    add_replay_item("s", input_payload={}, engine=engine)
    lock_replay_set("s", engine=engine)
    lock_replay_set("s", engine=engine)  # no-op, must not raise
    assert_replay_set_locked("s", engine=engine)


def test_cannot_add_after_lock_via_write_path(engine):
    create_replay_set("s", project="p", component="c", engine=engine)
    add_replay_item("s", input_payload={"q": 1}, engine=engine)
    lock_replay_set("s", engine=engine)
    with pytest.raises(ReplaySetLockedError):
        add_replay_item("s", input_payload={"q": 2}, engine=engine)


def test_build_locked_replay_set(engine):
    build_locked_replay_set(
        "demo/x/v1",
        project="demo",
        component="x",
        items=[
            {"input_payload": {"q": "a"}},
            {"input_payload": {"q": "b"}, "expected_output": {"y": 2}},
        ],
        engine=engine,
    )
    assert_replay_set_locked("demo/x/v1", engine=engine)
    with Session(engine) as sess:
        items = sess.scalars(
            select(ReplaySetItem).order_by(ReplaySetItem.item_index)
        ).all()
        assert [it.input_payload for it in items] == [{"q": "a"}, {"q": "b"}]
        assert items[1].expected_output == {"y": 2}
        assert sess.get(ReplaySet, "demo/x/v1").item_count == 2


def test_build_rejects_item_without_payload(engine):
    with pytest.raises(ValueError, match="input_payload"):
        build_locked_replay_set(
            "s", project="p", component="c", items=[{"expected_output": 1}], engine=engine
        )


def test_create_rejects_duplicate(engine):
    create_replay_set("s", project="p", component="c", engine=engine)
    with pytest.raises(ValueError, match="already exists"):
        create_replay_set("s", project="p", component="c", engine=engine)


def test_assert_unknown_set_raises(engine):
    with pytest.raises(InvariantViolation, match="not registered"):
        assert_replay_set_locked("nope", engine=engine)


def test_assert_on_unmigrated_db_gives_clear_error():
    """The migration trap: a DB without the replay_sets table must surface a
    clear invariant-4 error, not a raw OperationalError."""
    bare = make_engine("sqlite:///:memory:", create_all=False)
    with pytest.raises(InvariantViolation) as exc:
        assert_replay_set_locked("anything", engine=bare)
    assert exc.value.invariant == 4
    assert "replay_sets table is missing" in str(exc.value)

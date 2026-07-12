"""replay_sets / replay_set_items ORM + physical lock rejection (SPEC §3.4/§5.4).

Invariant 4: once a replay set is locked it is physically immutable — the
guarantee that A/B results from different periods stay comparable. These tests
assert the rejection happens at the ORM flush layer, not by convention.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.store.models import ReplaySet, ReplaySetItem, ReplaySetLockedError


def _make_set(sess, set_id="demo/extractor/golden", *, n_items=2, locked=False):
    rs = ReplaySet(replay_set_id=set_id, project="demo", component="extractor")
    sess.add(rs)
    for i in range(n_items):
        sess.add(
            ReplaySetItem(
                replay_set_id=set_id, item_index=i, input_payload={"q": f"item-{i}"}
            )
        )
    sess.commit()
    if locked:
        rs.is_locked = True
        rs.item_count = n_items
        sess.commit()
    return rs


def test_create_unlocked_set_with_items(engine):
    with Session(engine) as sess:
        _make_set(sess, n_items=3)
        items = sess.scalars(select(ReplaySetItem)).all()
        assert len(items) == 3
        rs = sess.get(ReplaySet, "demo/extractor/golden")
        assert rs.is_locked is False
        assert rs.items[0].input_payload == {"q": "item-0"}


def test_lock_transition_allowed(engine):
    """The one-way False -> True lock must succeed."""
    with Session(engine) as sess:
        rs = _make_set(sess)
        rs.is_locked = True
        rs.item_count = 2
        sess.commit()  # must not raise
        assert sess.get(ReplaySet, "demo/extractor/golden").is_locked is True


def test_cannot_add_item_to_locked_set(engine):
    with Session(engine) as sess:
        _make_set(sess, locked=True)
        sess.add(
            ReplaySetItem(
                replay_set_id="demo/extractor/golden", item_index=99,
                input_payload={"q": "sneaky"},
            )
        )
        with pytest.raises(ReplaySetLockedError):
            sess.commit()


def test_cannot_modify_item_of_locked_set(engine):
    with Session(engine) as sess:
        _make_set(sess, locked=True)
    with Session(engine) as sess:
        item = sess.scalars(
            select(ReplaySetItem).where(ReplaySetItem.item_index == 0)
        ).one()
        item.input_payload = {"q": "tampered"}
        with pytest.raises(ReplaySetLockedError):
            sess.commit()


def test_cannot_delete_item_of_locked_set(engine):
    with Session(engine) as sess:
        _make_set(sess, locked=True)
    with Session(engine) as sess:
        item = sess.scalars(
            select(ReplaySetItem).where(ReplaySetItem.item_index == 0)
        ).one()
        sess.delete(item)
        with pytest.raises(ReplaySetLockedError):
            sess.commit()


def test_cannot_unlock_a_locked_set(engine):
    with Session(engine) as sess:
        _make_set(sess, locked=True)
    with Session(engine) as sess:
        rs = sess.get(ReplaySet, "demo/extractor/golden")
        rs.is_locked = False
        with pytest.raises(ReplaySetLockedError):
            sess.commit()


def test_cannot_mutate_locked_set_fields(engine):
    with Session(engine) as sess:
        _make_set(sess, locked=True)
    with Session(engine) as sess:
        rs = sess.get(ReplaySet, "demo/extractor/golden")
        rs.component = "renamed"
        with pytest.raises(ReplaySetLockedError):
            sess.commit()


def test_cannot_delete_locked_set(engine):
    with Session(engine) as sess:
        _make_set(sess, locked=True)
    with Session(engine) as sess:
        rs = sess.get(ReplaySet, "demo/extractor/golden")
        sess.delete(rs)
        with pytest.raises(ReplaySetLockedError):
            sess.commit()


def test_unlocked_set_is_fully_mutable(engine):
    """Negative control: before locking, all writes succeed."""
    with Session(engine) as sess:
        _make_set(sess, n_items=1)  # unlocked
    with Session(engine) as sess:
        rs = sess.get(ReplaySet, "demo/extractor/golden")
        rs.component = "still-editable"
        sess.add(
            ReplaySetItem(
                replay_set_id=rs.replay_set_id, item_index=5,
                input_payload={"q": "added"},
            )
        )
        sess.commit()  # must not raise
        items = sess.scalars(select(ReplaySetItem)).all()
        assert len(items) == 2


def test_duplicate_item_index_rejected(engine):
    """(replay_set_id, item_index) is unique within a set."""
    from sqlalchemy.exc import IntegrityError

    with Session(engine) as sess:
        rs = ReplaySet(replay_set_id="s", project="p", component="c")
        sess.add(rs)
        sess.add(ReplaySetItem(replay_set_id="s", item_index=0, input_payload={}))
        sess.add(ReplaySetItem(replay_set_id="s", item_index=0, input_payload={}))
        with pytest.raises(IntegrityError):
            sess.commit()

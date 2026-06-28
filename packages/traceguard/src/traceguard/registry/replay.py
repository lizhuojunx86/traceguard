"""Replay-set write-path: the sanctioned way to create and lock a replay set.

A replay set is curated unlocked, then locked once; locking is one-way and the
store layer physically rejects any later mutation (``ReplaySetLockedError``,
invariant 4 / SPEC §3.4/§5.4). ``assert_replay_set_locked`` (the read-side
invariant-4 validator) is meaningless without a way to produce a locked set —
that is what this module provides.

Note: a set cannot be locked in the *same* transaction that inserts its items
(the store inserts the parent before the children, so a locked parent would
reject its own items). ``lock_replay_set`` is therefore a separate commit, and
``build_locked_replay_set`` commits the items first, then the lock.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard.store.models import ReplaySet, ReplaySetItem, make_engine


def create_replay_set(
    replay_set_id: str,
    *,
    project: str,
    component: str,
    engine: Engine | None = None,
) -> None:
    """Create a new, unlocked replay set. Rejects a duplicate id (insert-only)."""
    eng = engine if engine is not None else make_engine()
    with Session(eng) as sess:
        if sess.get(ReplaySet, replay_set_id) is not None:
            raise ValueError(
                f"replay_set {replay_set_id!r} already exists; pick a new id "
                "instead of mutating an existing set"
            )
        sess.add(
            ReplaySet(
                replay_set_id=replay_set_id,
                project=project,
                component=component,
                is_locked=False,
                item_count=0,
            )
        )
        sess.commit()


def add_replay_item(
    replay_set_id: str,
    *,
    input_payload: Any,
    expected_output: Any = None,
    item_index: int | None = None,
    engine: Engine | None = None,
) -> int:
    """Append an item to an unlocked set; returns the assigned ``item_index``.

    ``item_index`` auto-assigns to the next free position when omitted. Raises
    ``ReplaySetLockedError`` (from the store layer) if the set is locked.
    """
    eng = engine if engine is not None else make_engine()
    with Session(eng) as sess:
        if sess.get(ReplaySet, replay_set_id) is None:
            raise ValueError(f"replay_set {replay_set_id!r} does not exist")
        if item_index is None:
            current = sess.scalar(
                select(func.count())
                .select_from(ReplaySetItem)
                .where(ReplaySetItem.replay_set_id == replay_set_id)
            )
            item_index = int(current or 0)
        sess.add(
            ReplaySetItem(
                replay_set_id=replay_set_id,
                item_index=item_index,
                input_payload=input_payload,
                expected_output=expected_output,
            )
        )
        sess.commit()
    return item_index


def lock_replay_set(replay_set_id: str, *, engine: Engine | None = None) -> None:
    """Lock a replay set (one-way). Idempotent: a no-op if already locked.

    Stamps ``item_count`` with the current number of items at lock time.
    """
    eng = engine if engine is not None else make_engine()
    with Session(eng) as sess:
        rs = sess.get(ReplaySet, replay_set_id)
        if rs is None:
            raise ValueError(f"replay_set {replay_set_id!r} does not exist")
        if rs.is_locked:
            return  # idempotent — avoid issuing an UPDATE the store would reject
        count = sess.scalar(
            select(func.count())
            .select_from(ReplaySetItem)
            .where(ReplaySetItem.replay_set_id == replay_set_id)
        )
        rs.item_count = int(count or 0)
        rs.is_locked = True
        sess.commit()


def build_locked_replay_set(
    replay_set_id: str,
    *,
    project: str,
    component: str,
    items: Iterable[Mapping[str, Any]],
    engine: Engine | None = None,
) -> None:
    """Convenience: create + populate + lock a replay set.

    Each item is a mapping with ``input_payload`` (required) and optional
    ``expected_output``; ``item_index`` is assigned by position. Items are
    committed first, then the set is locked in a second transaction (a locked
    parent cannot accept its own children in one flush).
    """
    eng = engine if engine is not None else make_engine()
    with Session(eng) as sess:
        if sess.get(ReplaySet, replay_set_id) is not None:
            raise ValueError(
                f"replay_set {replay_set_id!r} already exists; pick a new id"
            )
        sess.add(
            ReplaySet(
                replay_set_id=replay_set_id,
                project=project,
                component=component,
                is_locked=False,
                item_count=0,
            )
        )
        for idx, item in enumerate(items):
            if "input_payload" not in item:
                raise ValueError(f"item {idx} is missing required 'input_payload'")
            sess.add(
                ReplaySetItem(
                    replay_set_id=replay_set_id,
                    item_index=idx,
                    input_payload=item["input_payload"],
                    expected_output=item.get("expected_output"),
                )
            )
        sess.commit()
    lock_replay_set(replay_set_id, engine=eng)

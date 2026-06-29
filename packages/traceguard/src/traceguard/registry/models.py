"""Model registry queries (SPEC §4.2)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, overload

from sqlalchemy import or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard.store.models import ModelRegistryEntry, make_engine


class NoEligibleModelError(LookupError):
    """No model satisfies the look-ahead constraints (SPEC §4.2 strict mode)."""


def register_model(
    model_id: str,
    *,
    model_family: str,
    capability_class: str,
    released_at: datetime,
    available_to_us_at: datetime,
    deprecated_at: datetime | None = None,
    engine: Engine | None = None,
    if_exists: str = "error",
) -> None:
    """Insert a new model registry entry.

    Per SPEC §3.2, ``model_id`` is the stable primary key and entries are
    insert-only — re-registering the same id with different metadata is
    rejected. To "upgrade" a model, register a new ``model_id``.

    ``if_exists`` controls what happens when ``model_id`` is already registered:
    ``"error"`` (default) raises — the entry is never modified; ``"ignore"``
    leaves the existing entry untouched and returns, so a setup step that runs a
    fixed set of ``register_model`` calls is idempotent across re-runs (it does
    not update the existing row — to change metadata, use a new ``model_id``).
    """
    if if_exists not in ("error", "ignore"):
        raise ValueError(f"if_exists must be 'error' or 'ignore', got {if_exists!r}")
    if released_at > available_to_us_at:
        raise ValueError(
            "released_at MUST be <= available_to_us_at "
            f"(got released_at={released_at!r}, available_to_us_at={available_to_us_at!r})"
        )
    eng = engine if engine is not None else make_engine()
    with Session(eng) as sess:
        existing = sess.get(ModelRegistryEntry, model_id)
        if existing is not None:
            if if_exists == "ignore":
                return
            raise ValueError(
                f"model_id {model_id!r} already registered; "
                "register a new model_id instead of modifying existing entries"
            )
        sess.add(
            ModelRegistryEntry(
                model_id=model_id,
                model_family=model_family,
                capability_class=capability_class,
                released_at=released_at,
                available_to_us_at=available_to_us_at,
                deprecated_at=deprecated_at,
            )
        )
        sess.commit()


@overload
def select_model(
    capability_class: str,
    *,
    available_at: datetime,
    strict: Literal[True],
    engine: Engine | None = None,
) -> str: ...


@overload
def select_model(
    capability_class: str,
    *,
    available_at: datetime,
    strict: Literal[False],
    engine: Engine | None = None,
) -> tuple[str, bool]: ...


def select_model(
    capability_class: str,
    *,
    available_at: datetime,
    strict: bool,
    engine: Engine | None = None,
) -> str | tuple[str, bool]:
    """Pick a model_id for ``capability_class`` at the ``available_at`` instant.

    ``strict`` is keyword-only and has no default (SPEC §4.2). Callers MUST
    explicitly state which mode they want — this is the friction that
    prevents accidental look-ahead.

    strict=True
        Returns the most recently available model whose
        ``available_to_us_at <= available_at`` and which is not deprecated as
        of ``available_at``. Raises ``NoEligibleModelError`` if none.

    strict=False
        Returns ``(model_id, is_anachronistic)`` for the latest currently
        active model in the capability class regardless of timing.
        ``is_anachronistic`` is True iff that model was not yet available at
        ``available_at``.
    """
    eng = engine if engine is not None else make_engine()
    with Session(eng) as sess:
        if strict:
            stmt = (
                select(ModelRegistryEntry)
                .where(ModelRegistryEntry.capability_class == capability_class)
                .where(ModelRegistryEntry.available_to_us_at <= available_at)
                .where(
                    or_(
                        ModelRegistryEntry.deprecated_at.is_(None),
                        ModelRegistryEntry.deprecated_at > available_at,
                    )
                )
                .order_by(ModelRegistryEntry.available_to_us_at.desc())
            )
            entry = sess.scalars(stmt).first()
            if entry is None:
                raise NoEligibleModelError(
                    f"no model registered for capability_class={capability_class!r} "
                    f"available at {available_at.isoformat()} (strict mode)"
                )
            return entry.model_id

        now = datetime.now(timezone.utc)
        stmt = (
            select(ModelRegistryEntry)
            .where(ModelRegistryEntry.capability_class == capability_class)
            .where(
                or_(
                    ModelRegistryEntry.deprecated_at.is_(None),
                    ModelRegistryEntry.deprecated_at > now,
                )
            )
            .order_by(ModelRegistryEntry.available_to_us_at.desc())
        )
        entry = sess.scalars(stmt).first()
        if entry is None:
            raise NoEligibleModelError(
                f"no active model registered for capability_class={capability_class!r}"
            )
        is_anachronistic = entry.available_to_us_at > available_at
        return entry.model_id, is_anachronistic

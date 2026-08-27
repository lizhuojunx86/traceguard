"""SQLAlchemy ORM for traces and model_registry tables.

Fields follow TRACEGUARD_SPEC.md §3.1 and §3.2 MUST lists. Implementation
adds nullable fields freely (per SPEC §3.5) but does not rename/remove MUST
fields.

SQLite is the Phase 0 default. The engine factory accepts any SQLAlchemy URL
so the same code works against Postgres in later phases.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UTCDateTime(TypeDecorator):
    """DateTime that requires tz-aware input and round-trips as UTC.

    SQLite stores datetimes as strings without tz info; this wrapper
    re-attaches UTC on read so comparisons against tz-aware values work.
    Postgres preserves tz natively — this layer is a no-op there.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime not allowed; pass a tz-aware datetime "
                "(e.g. datetime.now(timezone.utc))"
            )
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Trace(Base):
    __tablename__ = "traces"

    trace_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Identity (SPEC §3.1 MUST)
    project: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    component: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)

    # Linking
    parent_trace_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("traces.trace_id"), nullable=True
    )
    correlation_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)

    # Identity dimensions (SPEC §3.1, v1.1): who ran this call, and in which
    # session. Indexed aggregation keys for correlating several executors after
    # the fact. By contract they take no part in input_hash or invariants 1-4;
    # they are also outside the audit algo v1 hash envelope (see
    # audit/canonical.py TRACE_CONTENT_FIELDS) — append-only under the guard,
    # but not attested by the chain until algo v2.
    agent_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)

    # Input
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    input_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Model / Prompt
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_template_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    prompt_template_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Output
    output_parsed: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    parse_status: Mapped[str] = mapped_column(String(16), nullable=False)

    # Performance
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)

    # Time
    feature_as_of: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, index=True
    )
    invoked_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )

    # Error
    error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ModelRegistryEntry(Base):
    __tablename__ = "model_registry"

    model_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_family: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_class: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    released_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    available_to_us_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, index=True
    )
    deprecated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ReplaySet(Base):
    """A curated, lockable set of inputs for regression / A/B (SPEC §3.4).

    ``is_locked = TRUE`` makes the set physically immutable: invariant 4
    (SPEC §5.4) is enforced at the ORM flush layer by the event listeners below,
    not merely by convention. Locking is one-way — a locked set cannot be
    unlocked, mutated, or deleted; create a new set instead.
    """

    __tablename__ = "replay_sets"

    # str PK (matches SPEC §4.5 assert_replay_set_locked(replay_set_id: str)).
    replay_set_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    project: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    component: Mapped[str] = mapped_column(String(128), nullable=False)
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    curated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )

    items: Mapped[list[ReplaySetItem]] = relationship(
        back_populates="replay_set", cascade="all, delete-orphan"
    )


class ReplaySetItem(Base):
    __tablename__ = "replay_set_items"
    __table_args__ = (
        UniqueConstraint("replay_set_id", "item_index", name="uq_replay_set_item_index"),
    )

    item_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    replay_set_id: Mapped[str] = mapped_column(
        String(256), ForeignKey("replay_sets.replay_set_id"), nullable=False, index=True
    )
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    input_payload: Mapped[Any] = mapped_column(JSON, nullable=False)
    expected_output: Mapped[Any | None] = mapped_column(JSON, nullable=True)

    replay_set: Mapped[ReplaySet] = relationship(back_populates="items")


class ReplaySetLockedError(Exception):
    """Raised when a write is attempted against a locked replay set (invariant 4).

    The physical guarantee behind SPEC §3.4/§5.4: once ``is_locked = TRUE`` no
    item may be added/modified/deleted and the set itself may not be mutated or
    deleted, so A/B results from different periods stay comparable.
    """


def _set_is_locked(connection, replay_set_id: str | None) -> bool:
    """The DB-persisted lock state of a set, read on the flush ``connection``.

    Mapper ``before_*`` events fire before the row's own UPDATE/DELETE is
    emitted, so this SELECT still sees the pre-flush (committed) value within the
    transaction — robust regardless of ORM expiry/history quirks. This lets the
    one-way False -> True lock transition through (persisted value is still
    False) while rejecting every write once the row is committed as locked.
    """
    if replay_set_id is None:
        return False
    locked = connection.execute(
        select(ReplaySet.is_locked).where(ReplaySet.replay_set_id == replay_set_id)
    ).scalar()
    return bool(locked)


@event.listens_for(ReplaySetItem, "before_insert")
@event.listens_for(ReplaySetItem, "before_update")
@event.listens_for(ReplaySetItem, "before_delete")
def _block_item_write_if_locked(mapper, connection, target: ReplaySetItem) -> None:
    if _set_is_locked(connection, target.replay_set_id):
        raise ReplaySetLockedError(
            f"replay_set {target.replay_set_id!r} is locked; items cannot be "
            "added, modified, or deleted (SPEC §3.4/§5.4 invariant 4)."
        )


@event.listens_for(ReplaySet, "before_update")
def _block_locked_set_update(mapper, connection, target: ReplaySet) -> None:
    # Reject every mutation of an already-locked set, including any unlock. The
    # False -> True lock is allowed because the persisted value is still False.
    if _set_is_locked(connection, target.replay_set_id):
        raise ReplaySetLockedError(
            f"replay_set {target.replay_set_id!r} is locked and immutable; it "
            "cannot be modified or unlocked (SPEC §3.4/§5.4 invariant 4). "
            "Create a new replay set instead."
        )


@event.listens_for(ReplaySet, "before_delete")
def _block_locked_set_delete(mapper, connection, target: ReplaySet) -> None:
    if _set_is_locked(connection, target.replay_set_id):
        raise ReplaySetLockedError(
            f"replay_set {target.replay_set_id!r} is locked and cannot be deleted "
            "(SPEC §3.4/§5.4 invariant 4)."
        )


DEFAULT_DB_URL = "sqlite:///traceguard.db"

# Columns added to the contract ``traces`` table after 1.0 (SPEC v1.1).
# Databases created before then lack them, and ``create_all`` never ALTERs an
# existing table — so :func:`ensure_trace_columns` adds them on open. Once a
# column is mapped on :class:`Trace`, every ORM SELECT names it: a legacy DB
# would fail on READ (including ``python -m traceguard.audit verify``), not
# merely on write, which is why this runs on every :func:`make_engine`.
TRACE_COLUMNS_ADDED_SINCE_1_0: tuple[str, ...] = ("agent_id", "session_id")


def _trace_column_names(engine: Engine) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns("traces")}


def ensure_trace_columns(engine: Engine) -> list[str]:
    """Add the SPEC v1.1 ``traces`` columns to an existing table (idempotent).

    Additive only: nullable columns plus their indexes, via
    ``ALTER TABLE traces ADD COLUMN`` (the one ALTER every dialect this
    package targets supports). A no-op when ``traces`` does not exist yet
    (``create_all`` builds it complete) or already has every column. Returns
    the names of the columns it added, so callers can log the migration.

    Concurrency: two processes opening the same legacy DB at once both see
    the column missing; the loser's ALTER fails with "duplicate column". That
    is re-checked by inspection and treated as success. A failure that leaves
    the column missing (read-only file, insufficient privilege) is raised with
    the manual statement in the message — the ORM could not read the table
    anyway, so a loud error beats a puzzling one at first SELECT.

    This is deliberately NOT a migration framework (the audit and routing_audit
    tables keep their manual-ALTER posture); it covers exactly the contract
    columns listed in :data:`TRACE_COLUMNS_ADDED_SINCE_1_0`.
    """
    if "traces" not in inspect(engine).get_table_names():
        return []
    table = Trace.__table__
    added: list[str] = []
    for name in TRACE_COLUMNS_ADDED_SINCE_1_0:
        if name not in _trace_column_names(engine):
            column = table.c[name]
            ddl_type = column.type.compile(dialect=engine.dialect)
            statement = f"ALTER TABLE traces ADD COLUMN {name} {ddl_type}"
            try:
                with engine.begin() as conn:
                    conn.execute(text(statement))
            except (OperationalError, ProgrammingError) as exc:
                if name not in _trace_column_names(engine):
                    raise RuntimeError(
                        f"traces table lacks column {name!r} (added in SPEC v1.1) and it "
                        f"could not be added automatically ({exc.__class__.__name__}: "
                        f"{exc.orig if exc.orig is not None else exc}). Run manually: "
                        f"{statement}"
                    ) from exc
                # A concurrent opener added it between our inspect and ALTER.
            else:
                added.append(name)
        # ``index=True`` on the mapped column registers Index ix_traces_<col> on
        # the Table; create_all builds it for new DBs, this builds it for
        # migrated ones. Reuse THAT Index object — constructing a new
        # ``Index(name, column)`` would attach a duplicate to the shared Table
        # metadata and break every later create_all in the process. checkfirst
        # makes it idempotent; a concurrent creator's collision is re-checked
        # the same way as the column above.
        index_name = f"ix_traces_{name}"
        index = next((ix for ix in table.indexes if ix.name == index_name), None)
        if index is None:  # pragma: no cover - index=True guarantees it exists
            continue
        try:
            index.create(bind=engine, checkfirst=True)
        except (OperationalError, ProgrammingError):
            existing = {ix["name"] for ix in inspect(engine).get_indexes("traces")}
            if index_name not in existing:
                raise
    return added


def make_engine(url: str | None = None, *, create_all: bool = True) -> Engine:
    """Build a SQLAlchemy engine and optionally create the schema.

    Resolution order for the URL: explicit arg → TRACEGUARD_DB_URL env →
    DEFAULT_DB_URL (sqlite:///traceguard.db in the current working directory).

    An existing ``traces`` table created before SPEC v1.1 is brought up to
    date on open (:func:`ensure_trace_columns`: additive nullable columns
    only) regardless of ``create_all``, because the ORM cannot read the table
    without them.
    """
    resolved = url or os.environ.get("TRACEGUARD_DB_URL") or DEFAULT_DB_URL
    engine = create_engine(resolved, future=True)
    if create_all:
        Base.metadata.create_all(engine)
    ensure_trace_columns(engine)
    return engine

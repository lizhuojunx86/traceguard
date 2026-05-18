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
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    create_engine,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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


DEFAULT_DB_URL = "sqlite:///traceguard.db"


def make_engine(url: str | None = None, *, create_all: bool = True) -> Engine:
    """Build a SQLAlchemy engine and optionally create the schema.

    Resolution order for the URL: explicit arg → TRACEGUARD_DB_URL env →
    DEFAULT_DB_URL (sqlite:///traceguard.db in the current working directory).
    """
    resolved = url or os.environ.get("TRACEGUARD_DB_URL") or DEFAULT_DB_URL
    engine = create_engine(resolved, future=True)
    if create_all:
        Base.metadata.create_all(engine)
    return engine

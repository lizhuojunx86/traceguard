"""Contract-external table for the routing_audit ingest (idempotency + rollback).

``routing_audit_ingest_log`` maps every ingested source record (one Claude
Code API message) to the ``traces`` row it produced. It lives on its own
``DeclarativeBase`` so importing this module never mutates the core
``traceguard.store.models.Base`` metadata — ``make_engine(create_all=True)``
in unrelated code keeps creating exactly the contract tables. Callers create
this table explicitly via :func:`ensure_tables`.

``trace_id`` is a plain integer (no ForeignKey) on purpose: the contract
``traces`` table is not touched, and cross-metadata FKs would force the two
schemas to be created together.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Integer, String
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from traceguard.store.models import UTCDateTime


class RoutingAuditBase(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RoutingAuditIngestLog(RoutingAuditBase):
    __tablename__ = "routing_audit_ingest_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # One row per ingested source record; batch_id groups a single CLI run so
    # it can be rolled back as a unit.
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Global idempotency key: the Claude API message id (``msg_...``) when the
    # record has one, else ``uuid:<line uuid>``. Unique across ALL batches —
    # re-running ingest never duplicates a trace, even when Claude Code
    # resume/compact copied the same message into a second session file.
    source_message_id: Mapped[str] = mapped_column(
        String(256), nullable=False, unique=True, index=True
    )
    source_session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Path relative to the ingest --source root (provenance/debugging only).
    source_file: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Subagent transcript id (``agent-<id>.jsonl``); None for main transcripts.
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    trace_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


def ensure_tables(engine: Engine) -> None:
    """Create the routing_audit tables if missing (idempotent, additive-only)."""
    RoutingAuditBase.metadata.create_all(engine)

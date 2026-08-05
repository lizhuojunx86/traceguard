"""Contract-external tables for the opt-in audit evidence layer.

Like :mod:`traceguard.routing_audit.models`, these live on their own
``DeclarativeBase`` so importing this module never mutates the core
``traceguard.store.models.Base`` metadata — ``make_engine(create_all=True)``
in unrelated code keeps creating exactly the contract tables. Callers create
these tables explicitly via :func:`ensure_audit_tables` (or implicitly via
:func:`traceguard.audit.enable`).

``trace_id`` columns are plain integers (no ForeignKey) on purpose: the
contract ``traces`` table is not touched, and cross-metadata FKs would force
the two schemas to be created together.

Evidence posture (see docs/audit.md for the full honest-layering table):
``audit_chain_entries`` is the tamper-EVIDENT hash chain — it makes silent
modification of covered rows *detectable* via :func:`traceguard.audit.verify_chain`;
it does not *prevent* anything, and without an externally stored anchor a
full-chain rewrite or tail truncation is undetectable (the chain is not a MAC).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Integer, String, Text, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from traceguard.store.models import UTCDateTime


class AuditBase(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditSettings(AuditBase):
    """Single-row (id=1) switch persisted in the audited DB itself.

    ``enabled`` gates both chaining and the guard; ``append_only`` additionally
    gates the ORM-layer append-only guard (chain-only mode when False). The
    flag lives in the DB (not just in-process) so every process that
    :func:`~traceguard.audit.attach`-es the engine sees one consistent state.
    """

    __tablename__ = "audit_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    append_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    algo_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    genesis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


class AuditChainEntry(AuditBase):
    """One link of the hash chain. Pure evidence — never legally mutated.

    Chain order is ``seq`` (explicit autoincrement; NEVER trace_id order, and
    never assumed contiguous — Postgres sequences skip on rollback).
    ``sqlite_autoincrement`` keeps SQLite from reusing rowids of deleted tail
    entries, preserving a weak forensic signal after truncation.

    ``prev_hash`` is NOT NULL + UNIQUE: linearity is enforced by this
    constraint, not by transaction isolation (under pysqlite legacy mode the
    head read is not transactional) — a concurrent head race becomes a
    constraint error that the writer retries, never a fork. NULLs would be
    mutually distinct in SQL and permit multiple genesis rows.

    ``cost_at_event`` snapshots ``cost_usd`` at write/event time INTO the hash
    preimage — ``cost_usd`` itself is excluded from the trace content hash
    (legal in-place reprice path), so this field is what binds cost history
    into the evidence envelope.

    ``canon_status='failed'`` marks an entry whose trace content could not be
    canonicalized (NaN/Inf floats, dict keys that are not JSON-representable
    or that collide after JSON key coercion …); the hash then covers a
    deterministic error marker built from ``canon_error`` instead of the row
    content, keeping the chain linear (fail-open). Such a row's content is
    NOT attested. (Lone surrogates are NOT failures — ``ensure_ascii=True``
    escapes them deterministically and the content is attested normally.)
    """

    __tablename__ = "audit_chain_entries"
    __table_args__ = {"sqlite_autoincrement": True}

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 'write' | 'backfill' | 'cost_event' | 'deletion'
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False)
    trace_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # str(Decimal) snapshot of cost_usd at write/event time; hashed.
    cost_at_event: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Free-text hashed payload for 'deletion' entries (reason).
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    canon_status: Mapped[str] = mapped_column(String(8), nullable=False, default="ok")
    canon_error: Mapped[str | None] = mapped_column(String(128), nullable=True)

    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    algo_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AuditCostEvent(AuditBase):
    """A recorded, legal cost_usd write — the event ledger for the one field
    the trace content hash deliberately excludes.

    Naming: deliberately ``cost_events`` (not "reconciliation") — SPEC §3.1
    places billed-cost reconciliation out of spec scope, and the existing
    reprice tool self-describes as a *deferred first write* of list price.
    ``event_type``: 'deferred_first_write' (reprice backfill) | 'rollback'
    (reprice rollback) | 'correction' (any other legal fix).

    Every row is mirrored by an ``entry_type='cost_event'`` chain entry written
    in the same transaction, so recorded corrections sit INSIDE the
    tamper-evident envelope. v1 has no signing key: a forged event can be
    appended by anyone with write access; the chain only guarantees that once
    written it cannot be silently altered or removed.
    """

    __tablename__ = "audit_cost_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    old_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


_CREATE_ATTEMPTS = 4


def _audit_tables_present(engine: Engine) -> bool:
    """True when every audit table exists, whoever created it."""
    existing = set(inspect(engine).get_table_names())
    return all(name in existing for name in AuditBase.metadata.tables)


def ensure_audit_tables(engine: Engine) -> None:
    """Create the audit tables if missing (idempotent, additive-only).

    Same KNOWN-DEBT posture as routing_audit: ``create_all`` never ALTERs an
    existing table; adding a column later needs a one-off manual
    ``ALTER TABLE`` on long-lived DBs. Do NOT add a migration framework.

    Concurrency: ``create_all``'s exists-check races a concurrent creator
    (TOCTOU) — e.g. two processes calling ``enable()`` at once. The loser sees
    "table already exists".

    A single retry is not enough, because ``create_all`` walks several tables
    and the walk is not atomic. Two racers can lose to each other on different
    tables in turn: A creates t1 while B fails on t1, B retries and starts t2
    just as A gets there, and the second collision escapes an except-block that
    only wraps one retry. That is rare enough to pass locally and still surface
    on a slower CI runner.

    So retry in a loop, and treat "every table is now present" as success no
    matter which racer created which table — that is the postcondition this
    function actually promises.
    """
    for attempt in range(_CREATE_ATTEMPTS):
        try:
            AuditBase.metadata.create_all(engine)
            return
        except (OperationalError, ProgrammingError):
            if _audit_tables_present(engine):
                return
            if attempt == _CREATE_ATTEMPTS - 1:
                raise

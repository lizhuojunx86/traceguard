"""Contract-external table for the opt-in source-snapshot extension.

Like :mod:`traceguard.audit.models` and :mod:`traceguard.routing_audit.models`,
this lives on its own ``DeclarativeBase`` so importing it never mutates the
core ``traceguard.store.models.Base`` metadata — ``make_engine(create_all=True)``
in unrelated code keeps creating exactly the contract tables. Callers create
this table explicitly via :func:`ensure_source_tables` (or
:func:`traceguard.sources.enable`).

``trace_id`` is a plain indexed integer, NOT a ``ForeignKey``: the contract
``traces`` table is not touched, and a cross-metadata FK would force the two
schemas to be created together (SQLite does not enforce FKs by default
anyway). The reference is honoured by the WRITE PATH instead — a snapshot row
is inserted in the same transaction as its trace, with the trace's just-issued
primary key (see :mod:`traceguard.sources.record`).

Evidence posture (docs/sources.md has the full honest-layering table): a row
here records **what the host handed to** ``record_source`` at ``retrieved_at``
and how those bytes relate in time to ``feature_as_of``. It does not, and
cannot, establish that the host actually fetched them from ``source_uri``, nor
that ``published_at`` is true — that timestamp is what the source *claims*.
The table is also entirely outside the audit algo v1 hash envelope: snapshot
rows are not attested by the chain.

**No retrieved content is stored** — digests and metadata only. That is the
contract intent of this extension, not a configurable mode; archiving the
bytes is the consumer's own business, joined back on ``content_hash``.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Integer, String, Text, inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from traceguard.store.models import UTCDateTime


class SourcesBase(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: Allowed ``source_kind`` values (SPEC v1.2 §6.6). ``other`` is the escape
#: hatch — deliberately present so an unforeseen retrieval shape is recorded
#: honestly rather than mislabelled as one of the five known kinds.
SOURCE_KINDS: frozenset[str] = frozenset(
    {"http", "mcp", "file", "db", "vendor_api", "other"}
)


class SourceSnapshotRow(SourcesBase):
    """One retrieval, as digests + metadata. Never the content itself.

    ``content_hash`` is ``sha256`` of the bytes as received, with no
    normalization (SPEC v1.2 revision D6). ``normalized_hash`` is the optional
    second digest for CDN / template noise; it MUST be accompanied by
    ``normalizer_id`` (``<name>@<version>``), because two normalized hashes
    from unnamed, unversioned normalizers are not comparable — which makes the
    hash worse than useless, since it looks comparable.

    ``retrieved_at`` is physical time (when the host got the bytes);
    ``published_at`` is what the SOURCE claims about itself and is the
    ``valid_from`` invariant 3 compares against. They are different kinds of
    fact and are never conflated.
    """

    __tablename__ = "source_snapshots"

    snapshot_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Logical FK -> traces.trace_id; see the module docstring for why it is not DDL.
    trace_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    source_uri: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)

    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_encoding: Mapped[str | None] = mapped_column(String(32), nullable=True)
    normalized_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    normalizer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    retrieved_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    effective_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    source_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    mcp_server_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    cache_status: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Invariant-3 judgement AS OF THE RECORDING CALL, and the mode that call
    # declared. Both are persisted rather than recomputed at read time: the
    # verdict a row carries should be the one that applied when it was written
    # (same reasoning as routing_detail's recorded `requested_is_alias`), and
    # `strict` lets a later reader tell "was not refused" apart from "was never
    # checked".
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    strict: Mapped[bool] = mapped_column(Boolean, nullable=False)

    recorded_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )


_CREATE_ATTEMPTS = 4


def _tables_present(bind: Engine | Connection) -> bool:
    existing = set(inspect(bind).get_table_names())
    return all(name in existing for name in SourcesBase.metadata.tables)


def ensure_source_tables(engine: Engine) -> None:
    """Create ``source_snapshots`` if missing (idempotent, additive-only).

    Same posture and the same concurrency retry loop as
    :func:`traceguard.audit.models.ensure_audit_tables` — ``create_all``'s
    exists-check races a concurrent creator, and a single retry is not enough
    because the walk over several tables is not atomic. "Every table is now
    present" is the postcondition, whoever created it.

    Deliberately NOT a migration framework: adding a column later needs a
    one-off manual ``ALTER TABLE`` on long-lived DBs, exactly like the audit
    and routing_audit tables.
    """
    for attempt in range(_CREATE_ATTEMPTS):
        try:
            SourcesBase.metadata.create_all(engine)
            return
        except (OperationalError, ProgrammingError):
            if _tables_present(engine):
                return
            if attempt == _CREATE_ATTEMPTS - 1:
                raise


def source_tables_exist(bind: Engine | Connection) -> bool:
    """Whether ``source_snapshots`` exists behind ``bind``.

    Accepts an ``Engine`` or an already-open ``Connection``. Passing the
    connection matters when a transaction is in flight: opening a SECOND
    connection to the same SQLite database returns it to the pool afterwards,
    and the pool resets it with a ROLLBACK — which on a shared-connection
    (``:memory:``, SingletonThreadPool) engine rolls back the caller's
    uncommitted work. The tracer's failure path hit exactly that and silently
    destroyed the trace it was trying to protect, so it now passes
    ``session.connection()``.
    """
    try:
        return _tables_present(bind)
    except Exception:  # noqa: BLE001 - unreachable DB == not enabled
        return False

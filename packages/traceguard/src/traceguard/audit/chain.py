"""Hash-chain write path + activation for the audit evidence layer.

Activation model (deliberate, fail-open by design):

- Importing :mod:`traceguard.audit` has ZERO side effects — no listeners are
  registered. Mapper/session events in SQLAlchemy are process-global, so
  import-time registration would tax (and, worse, could break) engines that
  never opted in.
- :func:`enable` (first time) / :func:`attach` (other processes) lazily
  register the listeners ONCE and add the engine to a process-level
  ``WeakSet``. Every listener's first gate is a cheap identity check against
  that set — engines that never attached see zero behavior change, and code
  that never imports this module pays nothing at all.
- Attached engines then consult the DB-persisted ``audit_settings`` flag on
  the flush connection, so :func:`disable` in one process is honored by every
  attached process.

Failure semantics mirror SPEC §4.1: the chain hook must never break the host
write. All audit statements run inside a SAVEPOINT (on SQLite a failed
statement merely aborts itself, but on PostgreSQL it would poison the whole
host transaction — the savepoint is what makes fail-open real on both).
Head races (UNIQUE ``prev_hash``) and lock contention are retried a bounded
number of times — silently dropping entries on mild contention would
manufacture evidence gaps. After retries: default = WARNING + coverage gap;
strict mode (``enable(strict=True)`` / ``TRACEGUARD_AUDIT_STRICT=1``,
mirroring ``strict_persistence``) re-raises and aborts the host transaction.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any
from weakref import WeakSet

from sqlalchemy import event, insert, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from traceguard.audit.canonical import (
    ALGO_VERSION,
    COST_EVENT_CONTENT_FIELDS,
    GENESIS_PREV_HASH,
    CanonicalizationError,
    canon_error_content,
    compute_row_hash,
    entry_payload,
    trace_content,
)
from traceguard.audit.models import (
    AuditChainEntry,
    AuditCostEvent,
    AuditSettings,
    ensure_audit_tables,
)
from traceguard.store.models import Trace

_log = logging.getLogger("traceguard.audit")

_MAX_APPEND_ATTEMPTS = 3
_RETRY_BACKOFF_S = 0.05
_CANON_ERROR_MAX = 128

VALID_COST_EVENT_TYPES = frozenset({"deferred_first_write", "rollback", "correction"})

_attached_engines: WeakSet[Engine] = WeakSet()
_unreadable_warned: WeakSet[Engine] = WeakSet()
_listeners_registered = False


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


_strict = _env_truthy("TRACEGUARD_AUDIT_STRICT")


class AuditNotEnabledError(RuntimeError):
    """An explicit audit API was called against a DB where audit is not enabled."""


class AuditChainError(RuntimeError):
    """A chain entry could not be appended (after bounded retries)."""


class AppendOnlyViolationError(RuntimeError):
    """An ORM write would mutate/delete audit-covered data (see guard module).

    Defined here (not in guard.py) so both the row guard and the chain's
    unconditional evidence-table blockers share one exception without an
    import cycle.
    """


def set_strict(value: bool) -> None:
    """Process-level fail-closed switch for CHAIN failures (not the guard —
    the guard raising on a blocked write is its feature, not a failure)."""
    global _strict
    _strict = bool(value)


def is_strict() -> bool:
    return _strict


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _engine_of(connection: Connection) -> Engine:
    return connection.engine


def is_attached(engine: Engine) -> bool:
    return engine in _attached_engines


def read_settings(connection: Connection) -> tuple[bool, bool] | None:
    """(enabled, append_only) as persisted; ``None`` when UNREADABLE.

    Runs inside its own SAVEPOINT so a missing table / failed SELECT cannot
    poison the host transaction (PostgreSQL). Callers decide what unreadable
    means: the guard fails open (treated as disabled); the chain hook fails
    open by default but raises in strict mode — silently reading "disabled"
    there would let strict chaining stop without a trace.
    """
    try:
        nested = connection.begin_nested()
    except Exception:  # noqa: BLE001 - no savepoint support → unreadable
        return None
    try:
        row = connection.execute(
            select(AuditSettings.enabled, AuditSettings.append_only).where(
                AuditSettings.id == 1
            )
        ).first()
        nested.commit()
    except Exception:  # noqa: BLE001 - unreadable; caller picks the failure mode
        nested.rollback()
        return None
    if row is None:
        return (False, False)
    return (bool(row.enabled), bool(row.append_only))


def is_enabled(engine: Engine) -> bool:
    """Whether audit is enabled in the DB behind ``engine`` (False if the
    audit tables don't exist). Cheap pre-flight for explicit integrations
    (e.g. ``reprice --audit``)."""
    try:
        with engine.connect() as connection:
            settings = read_settings(connection)
    except Exception:  # noqa: BLE001 - unreachable DB == not enabled
        return False
    return bool(settings and settings[0])


def _warn_unreadable_once(connection: Connection) -> None:
    engine = _engine_of(connection)
    if engine in _unreadable_warned:
        return
    _unreadable_warned.add(engine)
    try:
        _log.warning(
            "audit settings are unreadable on attached engine %s; treating audit "
            "as disabled (fail-open) — chaining has silently stopped on this engine",
            engine,
        )
    except Exception:  # noqa: BLE001
        pass


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _hash_entry(
    prev_hash: str,
    *,
    entry_type: str,
    trace_id: int | None,
    event_id: int | None,
    cost_at_event: str | None,
    note: str | None,
    created_at: datetime,
    content: Any,
) -> tuple[str, str | None, str]:
    """(canon_status, canon_error, row_hash) — canon failure falls back to the
    deterministic error-marker payload instead of raising (fail-open)."""
    try:
        payload = entry_payload(
            entry_type=entry_type,
            trace_id=trace_id,
            event_id=event_id,
            cost_at_event=cost_at_event,
            note=note,
            canon_status="ok",
            canon_error=None,
            created_at=created_at,
            content=content,
        )
        return "ok", None, compute_row_hash(prev_hash, payload)
    except CanonicalizationError as exc:
        canon_error = _truncate(f"{type(exc).__name__}: {exc}", _CANON_ERROR_MAX)
        payload = entry_payload(
            entry_type=entry_type,
            trace_id=trace_id,
            event_id=event_id,
            cost_at_event=cost_at_event,
            note=note,
            canon_status="failed",
            canon_error=canon_error,
            created_at=created_at,
            content=canon_error_content(canon_error),
        )
        return "failed", canon_error, compute_row_hash(prev_hash, payload)


def _append_entry(
    connection: Connection,
    *,
    entry_type: str,
    trace_id: int | None,
    event_id: int | None = None,
    cost_at_event: str | None = None,
    note: str | None = None,
    content: Any = None,
) -> None:
    """Append one chain entry on ``connection`` (same transaction as the host
    write when called from the flush hook). Raises :class:`AuditChainError`
    after bounded retries — the CALLER decides fail-open vs strict.

    Linearity relies on the UNIQUE(prev_hash) constraint, not on isolation:
    under pysqlite legacy mode the head read is not even transactional. A
    concurrent writer that read the same head hits the constraint and retries
    against the new head. Lock contention (SQLite "database is locked")
    retries the same way.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_APPEND_ATTEMPTS):
        if attempt:
            time.sleep(_RETRY_BACKOFF_S * attempt)
        nested = connection.begin_nested()
        try:
            head = connection.execute(
                select(AuditChainEntry.row_hash)
                .order_by(AuditChainEntry.seq.desc())
                .limit(1)
            ).first()
            prev_hash = head.row_hash if head is not None else GENESIS_PREV_HASH
            created_at = _utcnow()
            canon_status, canon_error, row_hash = _hash_entry(
                prev_hash,
                entry_type=entry_type,
                trace_id=trace_id,
                event_id=event_id,
                cost_at_event=cost_at_event,
                note=note,
                created_at=created_at,
                content=content,
            )
            connection.execute(
                insert(AuditChainEntry).values(
                    entry_type=entry_type,
                    trace_id=trace_id,
                    event_id=event_id,
                    cost_at_event=cost_at_event,
                    note=note,
                    canon_status=canon_status,
                    canon_error=canon_error,
                    prev_hash=prev_hash,
                    row_hash=row_hash,
                    algo_version=ALGO_VERSION,
                    created_at=created_at,
                )
            )
            nested.commit()
            if canon_status == "failed":
                _log.warning(
                    "audit chain entry %s for trace_id=%s written with "
                    "canon_status='failed' (%s); its content is NOT attested",
                    entry_type,
                    trace_id,
                    canon_error,
                )
            return
        except IntegrityError as exc:
            # Head race: a concurrent writer claimed our prev_hash (UNIQUE).
            nested.rollback()
            last_exc = exc
        except OperationalError as exc:
            nested.rollback()
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise  # structural (missing table, …) — retrying cannot help
            last_exc = exc
        except Exception:
            nested.rollback()
            raise
    raise AuditChainError(
        f"could not append audit chain entry ({entry_type}, trace_id={trace_id}) "
        f"after {_MAX_APPEND_ATTEMPTS} attempts"
    ) from last_exc


def _cost_snapshot(value: Any) -> str | None:
    return None if value is None else str(value)


def _append_trace_entry(connection: Connection, trace: Trace, entry_type: str) -> None:
    _append_entry(
        connection,
        entry_type=entry_type,
        trace_id=trace.trace_id,
        cost_at_event=_cost_snapshot(trace.cost_usd),
        content=trace_content(trace),
    )


def _chain_after_insert(mapper: Any, connection: Connection, target: Trace) -> None:
    """Mapper hook: chain every ORM-inserted trace on attached+enabled engines.

    Covers the tracer/session.add path only — Core/bulk INSERTs bypass mapper
    events entirely and surface later as coverage gaps in verify_chain.
    Must never raise in non-strict mode (SPEC §4.1: the audit layer's own
    failure must not break the host write).
    """
    if _engine_of(connection) not in _attached_engines:
        return
    try:
        settings = read_settings(connection)
        if settings is None:
            # Unreadable settings on an ATTACHED engine is anomalous (attach
            # ensures the tables). Fail-open reads as disabled; in strict mode
            # silently-stopped chaining is exactly what must not happen.
            if _strict:
                raise AuditChainError(
                    "audit settings unreadable on an attached engine (strict mode)"
                )
            _warn_unreadable_once(connection)
            return
        if not settings[0]:
            return
        _append_trace_entry(connection, target, "write")
    except Exception:  # noqa: BLE001 - fail-open unless strict
        if _strict:
            try:
                # The host flush is about to abort — but a fail-open caller
                # above us (a non-strict tracer) may swallow the exception, so
                # leave an audit-layer ERROR as the durable signal either way.
                _log.error(
                    "audit chain append failed in STRICT mode; aborting the host "
                    "transaction (a fail-open caller such as a non-strict tracer "
                    "may swallow this — see docs/audit.md 'Failure semantics')",
                    exc_info=True,
                )
            except Exception:  # noqa: BLE001
                pass
            raise
        try:
            _log.warning(
                "audit chain append failed for trace insert; the host write is "
                "unaffected and this trace will show as a coverage gap "
                "(set TRACEGUARD_AUDIT_STRICT=1 / enable(strict=True) to fail closed)",
                exc_info=True,
            )
        except Exception:  # noqa: BLE001 - even the recovery log must not escape
            pass


def _block_evidence_mutation(mapper: Any, connection: Connection, target: Any) -> None:
    """Unconditional ORM-layer blocker for the evidence tables themselves."""
    raise AppendOnlyViolationError(
        f"{type(target).__name__} rows are audit evidence and are never legally "
        "mutated or deleted via the ORM"
    )


def _register_listeners() -> None:
    global _listeners_registered
    if _listeners_registered:
        return
    event.listen(Trace, "after_insert", _chain_after_insert)
    for cls in (AuditChainEntry, AuditCostEvent):
        event.listen(cls, "before_update", _block_evidence_mutation)
        event.listen(cls, "before_delete", _block_evidence_mutation)
    # Function-level import: guard.py imports this module at its top level.
    from traceguard.audit.guard import register_guard_listeners

    register_guard_listeners()
    _listeners_registered = True


def attach(engine: Engine) -> None:
    """Opt this process's ``engine`` into the audit layer.

    Ensures the audit tables exist (so the flush-time settings SELECT can
    never fail structurally), registers the process-global listeners once,
    and marks the engine attached. Whether anything actually happens per
    flush is then governed by the DB-persisted ``audit_settings`` flag.
    """
    ensure_audit_tables(engine)
    _register_listeners()
    _attached_engines.add(engine)


def detach(engine: Engine) -> None:
    """Process-level opt-out (does not touch the DB flag)."""
    _attached_engines.discard(engine)


def enable(
    engine: Engine,
    *,
    append_only: bool = True,
    backfill: bool = True,
    strict: bool = False,
) -> int:
    """Turn the audit evidence layer on for the DB behind ``engine``.

    Idempotent. ``append_only=False`` runs chain-only mode (no ORM guard).
    ``backfill=True`` chains every not-yet-chained existing trace as
    ``entry_type='backfill'`` — those entries attest DB state AT ENABLE TIME,
    not at original write time. Returns the number of rows backfilled.

    ``strict=True`` flips the process-level fail-closed switch for chain
    failures (equivalent to ``TRACEGUARD_AUDIT_STRICT=1``). IMPORTANT for
    tracer users: the tracer's own persistence is fail-open by default, so a
    strict chain failure raised inside its flush is swallowed there — losing
    the trace AND its evidence with only log output. Strict chain semantics
    reach the caller only when the tracer is also strict
    (``strict_persistence=True`` / ``TRACEGUARD_STRICT_PERSISTENCE=1``);
    :func:`enable` warns when the module-level tracer disagrees.
    """
    ensure_audit_tables(engine)
    if strict:
        set_strict(True)
        _warn_if_tracer_not_strict()
    with Session(engine) as sess:
        try:
            row = sess.get(AuditSettings, 1)
            if row is None:
                sess.add(
                    AuditSettings(
                        id=1,
                        enabled=True,
                        append_only=append_only,
                        algo_version=ALGO_VERSION,
                        genesis_hash=GENESIS_PREV_HASH,
                        enabled_at=_utcnow(),
                    )
                )
            sess.commit()
        except IntegrityError:
            # Two processes raced the FIRST enable; the loser updates instead.
            sess.rollback()
        row = sess.get(AuditSettings, 1)
        row.enabled = True
        row.append_only = append_only
        row.enabled_at = _utcnow()
        sess.commit()
    attach(engine)
    return backfill_traces(engine) if backfill else 0


def _warn_if_tracer_not_strict() -> None:
    """Strict chain + fail-open tracer silently loses trace AND evidence."""
    try:
        from traceguard.sdk.tracer import tracer as module_tracer

        if not module_tracer.strict_persistence:
            _log.warning(
                "audit strict mode is on but the tracer is fail-open "
                "(strict_persistence=False): a chain failure inside a tracer "
                "flush will be swallowed there, losing the trace and its "
                "evidence with only log output. Set strict_persistence=True / "
                "TRACEGUARD_STRICT_PERSISTENCE=1 for end-to-end fail-closed."
            )
    except Exception:  # noqa: BLE001 - advisory only
        pass


def disable(engine: Engine) -> None:
    """Flip the DB flag off: the guard lifts and new writes stop being chained.

    The existing chain stays verifiable — modifying an already-chained row
    while disabled is still detected by verify_chain (hash_mismatch); rows
    INSERTED while disabled surface only as coverage gaps. This is the honest
    escape hatch, not a security boundary.
    """
    ensure_audit_tables(engine)
    with Session(engine) as sess:
        row = sess.get(AuditSettings, 1)
        if row is not None:
            row.enabled = False
            sess.commit()


def backfill_traces(engine: Engine, *, chunk_size: int = 500) -> int:
    """Chain every trace that has no write/backfill entry yet, trace_id ASC.

    Used by :func:`enable` and safe to re-run (e.g. after a disable window, to
    at least attest current state of the uncovered rows). Chunked commits.
    """
    total = 0
    while True:
        with Session(engine) as sess:
            chained = (
                select(AuditChainEntry.trace_id)
                .where(AuditChainEntry.entry_type.in_(("write", "backfill")))
                .where(AuditChainEntry.trace_id.is_not(None))
            )
            missing = list(
                sess.scalars(
                    select(Trace)
                    .where(Trace.trace_id.not_in(chained))
                    .order_by(Trace.trace_id.asc())
                    .limit(chunk_size)
                )
            )
            if not missing:
                return total
            connection = sess.connection()
            for tr in missing:
                _append_trace_entry(connection, tr, "backfill")
            sess.commit()
            total += len(missing)


def record_cost_event(
    engine: Engine,
    *,
    trace_id: int,
    event_type: str,
    old_value: Any,
    new_value: Any,
    reason: str | None = None,
    batch_id: str | None = None,
) -> int:
    """Record one legal cost_usd write as chained evidence. Returns event_id.

    Explicit API, not a hot path: raises :class:`AuditNotEnabledError` when
    audit is off (a silently dropped correction record would defeat the point)
    and propagates chain failures.
    """
    if event_type not in VALID_COST_EVENT_TYPES:
        raise ValueError(
            f"event_type must be one of {sorted(VALID_COST_EVENT_TYPES)}, got {event_type!r}"
        )
    old_s = _cost_snapshot(old_value)
    new_s = _cost_snapshot(new_value)
    with engine.begin() as connection:
        settings = read_settings(connection)
        if settings is None or not settings[0]:
            raise AuditNotEnabledError(
                "audit is not enabled on this DB; call traceguard.audit.enable(engine) first"
            )
        occurred_at = _utcnow()
        result = connection.execute(
            insert(AuditCostEvent).values(
                trace_id=trace_id,
                event_type=event_type,
                old_value=old_s,
                new_value=new_s,
                reason=reason,
                batch_id=batch_id,
                occurred_at=occurred_at,
            )
        )
        event_id = result.inserted_primary_key[0]
        content = dict(
            zip(
                COST_EVENT_CONTENT_FIELDS,
                (event_id, trace_id, event_type, old_s, new_s, reason, batch_id, occurred_at),
            )
        )
        _append_entry(
            connection,
            entry_type="cost_event",
            trace_id=trace_id,
            event_id=event_id,
            cost_at_event=new_s,
            content=content,
        )
        return event_id


def record_deletion(engine: Engine, *, trace_id: int, reason: str) -> None:
    """Chain a tombstone for a legal deletion of ``trace_id``.

    verify_chain downgrades a missing trace WITH a tombstone from BREAK
    (``missing_trace``) to WARN (``deleted_with_record``). The tombstone
    records intent — it does not perform, authorize, or verify the deletion.
    """
    with engine.begin() as connection:
        settings = read_settings(connection)
        if settings is None or not settings[0]:
            raise AuditNotEnabledError(
                "audit is not enabled on this DB; call traceguard.audit.enable(engine) first"
            )
        _append_entry(
            connection,
            entry_type="deletion",
            trace_id=trace_id,
            note=reason,
            content=None,
        )

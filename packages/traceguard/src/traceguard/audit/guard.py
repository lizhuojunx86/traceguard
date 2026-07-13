"""ORM-layer append-only guard for the traces table (anti-footgun tier).

Honest scope — this guard prevents MISTAKES, it is not an integrity boundary:

- Covered: ORM unit-of-work writes (attribute set + flush, ``session.delete``)
  and ``session.execute(update(Trace)...)`` / ``delete(Trace)`` via
  ``do_orm_execute`` — the latter is the most likely accidental path (it is
  exactly how this repo's own reprice backfill is written; reprice stays
  allowed because it only sets ``cost_usd``).
- Bypassed: engine-level Core SQL, ``exec_driver_sql``, raw sqlite3/psql,
  editing the DB file, any process that never attached, the legacy bulk APIs
  (``Session.bulk_update_mappings`` / ``bulk_save_objects``, which skip both
  mapper events and ``do_orm_execute``), and dialect upserts
  (``insert(...).on_conflict_do_update``, which are Inserts to the event
  system). Tampering through any of these is the hash chain's job to DETECT,
  not this guard's to prevent.

The guard raising :class:`AppendOnlyViolationError` on a blocked write is its
feature — only guard INFRASTRUCTURE failures fail open (unreadable settings
reads as disabled).

Known fail-closed false positive (documented, accepted): setting a covered
field of an EXPIRED instance to its current value emits a real UPDATE whose
attribute history has no old value, so it is indistinguishable from a change
and gets blocked.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import Delete, Update, event, inspect
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from traceguard.audit.chain import (
    AppendOnlyViolationError,
    _attached_engines,
    read_settings,
)
from traceguard.store.models import Trace

__all__ = ["AppendOnlyViolationError", "register_guard_listeners"]

# The one field with a legal in-place UPDATE path (reprice backfill/rollback,
# SPEC §3.1). Everything else on traces is hash-covered and append-only.
UPDATE_ALLOWED_FIELDS = frozenset({"cost_usd"})

_registered = False


def _guard_active(connection: Connection) -> bool:
    if connection.engine not in _attached_engines:
        return False
    settings = read_settings(connection)
    if settings is None:  # guard infrastructure failure → fail open
        return False
    enabled, append_only = settings
    return enabled and append_only


def _changed_columns(target: Trace) -> set[str]:
    state = inspect(target)
    return {
        attr.key
        for attr in state.mapper.column_attrs
        if state.attrs[attr.key].history.has_changes()
    }


def _guard_before_update(mapper: Any, connection: Connection, target: Trace) -> None:
    if not _guard_active(connection):
        return
    blocked = _changed_columns(target) - UPDATE_ALLOWED_FIELDS
    if blocked:
        raise AppendOnlyViolationError(
            f"traces row {target.trace_id} is append-only under the audit layer; "
            f"refusing ORM UPDATE of hash-covered field(s) {sorted(blocked)}. "
            "Only cost_usd has a legal in-place write path (record it via "
            "traceguard.audit.record_cost_event); for anything else, write a "
            "new trace. Disable with traceguard.audit.disable(engine)."
        )


def _guard_before_delete(mapper: Any, connection: Connection, target: Trace) -> None:
    if not _guard_active(connection):
        return
    raise AppendOnlyViolationError(
        f"traces row {target.trace_id} is append-only under the audit layer; "
        "refusing ORM DELETE. Record a tombstone via "
        "traceguard.audit.record_deletion and delete outside the guard if the "
        "deletion is genuinely intended."
    )


_TRACE_PK_KEYS = frozenset(c.key for c in Trace.__table__.primary_key)


def _dml_assigned_keys(statement: Update, parameters: Any) -> set[str]:
    """Best-effort set of column keys an UPDATE assigns.

    ``update(Trace).values(cost_usd=...)`` puts coerced Column keys in
    ``statement._values``; the bulk-by-PK form passes dicts via parameters,
    where the PK entries are row SELECTORS (a PK can't be reassigned that
    way), so PK keys from parameters don't count as assignments. Keys from
    WHERE-clause bindparams fed via parameters can still be conflated —
    that direction is a fail-closed false positive, never a bypass.
    An empty result means "could not determine" and is treated conservatively
    by the caller.
    """
    keys: set[str] = set()
    values = getattr(statement, "_values", None) or {}
    for k in values:
        keys.add(getattr(k, "key", None) or str(k))
    params = parameters or []
    if isinstance(params, dict):
        params = [params]
    param_keys: set[str] = set()
    for p in params:
        if isinstance(p, dict):
            for k in p:
                param_keys.add(getattr(k, "key", None) or str(k))
    keys |= param_keys - _TRACE_PK_KEYS
    return keys


def _guard_orm_execute(execute_state: Any) -> None:
    """Session-level gate for bulk DML against traces.

    Fires for every ``session.execute``; cheap type/table checks come first so
    unrelated statements (including everything on non-attached engines) pay a
    couple of isinstance checks at most.
    """
    statement = execute_state.statement
    if not isinstance(statement, (Update, Delete)):
        return
    # ORM-entity statements carry an ANNOTATED clone of the table, so identity
    # against Trace.__table__ fails; the bind_mapper is the ORM-level truth.
    bind_mapper = getattr(execute_state, "bind_mapper", None)
    if bind_mapper is not None:
        if bind_mapper.class_ is not Trace:
            return
    else:
        table = getattr(statement, "table", None)
        if table is None or table.name != Trace.__table__.name:
            return
    try:
        # Resolve the bind FOR THIS MAPPER — a multi-bind session's default
        # bind can differ from the engine this statement actually hits.
        bind = execute_state.session.get_bind(mapper=bind_mapper or Trace)
        engine = getattr(bind, "engine", bind)
    except Exception:  # noqa: BLE001 - unbindable session → not ours to guard
        return
    if engine not in _attached_engines:
        return
    try:
        # The settings live on the engine THIS statement hits — a bare
        # session.connection() would consult the session's default bind.
        connection = execute_state.session.connection(
            bind_arguments={"mapper": bind_mapper or inspect(Trace)}
        )
        settings = read_settings(connection)
    except Exception:  # noqa: BLE001 - guard infrastructure failure → fail open
        return
    if settings is None:
        return
    enabled, append_only = settings
    if not (enabled and append_only):
        return
    if isinstance(statement, Delete):
        raise AppendOnlyViolationError(
            "traces is append-only under the audit layer; refusing bulk ORM "
            "DELETE. Record tombstones via traceguard.audit.record_deletion "
            "and delete outside the guard if genuinely intended."
        )
    assigned = _dml_assigned_keys(statement, execute_state.parameters)
    if not assigned or assigned - UPDATE_ALLOWED_FIELDS:
        # Unknown assignments are blocked conservatively: the only sanctioned
        # bulk write is the reprice-style `.values(cost_usd=...)` form.
        raise AppendOnlyViolationError(
            "traces is append-only under the audit layer; refusing bulk ORM "
            f"UPDATE of field(s) {sorted(assigned) or '<undetermined>'} "
            "(only cost_usd-only updates pass — record them via "
            "traceguard.audit.record_cost_event)."
        )


def register_guard_listeners() -> None:
    """Idempotent, called lazily by chain._register_listeners() on first attach."""
    global _registered
    if _registered:
        return
    event.listen(Trace, "before_update", _guard_before_update)
    event.listen(Trace, "before_delete", _guard_before_delete)
    event.listen(Session, "do_orm_execute", _guard_orm_execute)
    _registered = True

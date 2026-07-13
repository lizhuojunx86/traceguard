"""Canonical serialization for the evidence hash (audit algo v1).

This is an EVIDENCE hash, deliberately distinct from the business hash in
:mod:`traceguard.sdk.normalizer` (``input_hash`` answers "is this the same
input?"; ``row_hash`` answers "is this the same stored row?"). It never calls
the normalizer, and changing THIS algorithm is an audit-layer
``algo_version`` bump (stored per entry), not a SPEC-major event.

Byte-stability rules (frozen as algo v1; pinned by golden tests):

- ``json.dumps(payload, sort_keys=True, ensure_ascii=True,
  separators=(",", ":"), allow_nan=False).encode("ascii")``.
  ``ensure_ascii=True`` is load-bearing: with False, astral characters change
  byte length across surrogate-pair round-trips and lone surrogates cannot be
  UTF-8 encoded at all — ASCII escaping makes both deterministic (lone
  surrogates therefore canonicalize FINE and are attested, not a failure).
- dict keys are coerced to their JSON round-trip form (``2`` → ``"2"``) BEFORE
  sorting, so pre-round-trip (write) and post-round-trip (verify) values hash
  identically; keys that collide after coercion or are not JSON-representable
  raise.
- datetime → ``value.astimezone(UTC).isoformat()``, applied HERE (mapper
  ``after_insert`` sees raw constructor values, before the ``UTCDateTime``
  bind processor runs — write-time and verify-time values must be normalized
  by the same function, not by the DB layer).
- Decimal → ``str()``. (Only reachable via ``cost_at_event`` snapshots and
  cost-event values, which are stringified before they get here; the trace
  content hash excludes ``cost_usd`` entirely.)
- Everything else must already be JSON-native; anything ``json.dumps`` rejects
  (NaN/Inf, unsortable mixed-type keys, unknown objects) raises
  :class:`CanonicalizationError`, which the chain writer turns into a
  fail-open ``canon_status='failed'`` marker entry.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from traceguard.store.models import Trace

ALGO_VERSION = 1

GENESIS_PREV_HASH = hashlib.sha256(b"traceguard-audit-genesis-v1").hexdigest()

CANON_ERROR_KEY = "__canonicalization_error__"

# Trace content covered by the evidence hash — frozen as algo v1.
# cost_usd is EXCLUDED: it has a legal in-place UPDATE path (reprice backfill /
# rollback, SPEC §3.1 "list price at write; reconciliation out of scope").
# Its history is bound into the envelope via AuditChainEntry.cost_at_event and
# audit_cost_events instead.
TRACE_CONTENT_FIELDS: tuple[str, ...] = (
    "trace_id",
    "project",
    "component",
    "operation",
    "parent_trace_id",
    "correlation_id",
    "input_hash",
    "input_summary",
    "model_id",
    "prompt_template_id",
    "prompt_template_hash",
    "output_parsed",
    "parse_status",
    "latency_ms",
    "tokens_in",
    "tokens_out",
    "feature_as_of",
    "invoked_at",
    "error_class",
    "error_message",
)

COST_EVENT_CONTENT_FIELDS: tuple[str, ...] = (
    "event_id",
    "trace_id",
    "event_type",
    "old_value",
    "new_value",
    "reason",
    "batch_id",
    "occurred_at",
)


class CanonicalizationError(ValueError):
    """Raised when a value cannot be deterministically serialized (algo v1)."""


def _canon_key(key: Any) -> str:
    """Coerce a dict key exactly the way a JSON round-trip does.

    Write-time hashing sees pre-round-trip Python values, verify-time hashing
    sees post-round-trip ones; non-str keys (legal through the JSON column)
    would otherwise SORT differently before vs after coercion ({2: .., 10: ..}
    sorts numerically pre-trip, lexicographically post-trip) and every such
    row would verify as a false tamper BREAK.
    """
    if isinstance(key, str):
        return key
    if key is True:
        return "true"
    if key is False:
        return "false"
    if key is None:
        return "null"
    if isinstance(key, (int, float)):
        return str(key)
    raise CanonicalizationError(
        f"dict key of type {type(key).__name__!r} is not JSON-representable"
    )


def _canon_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # Contract columns are tz-aware by construction (UTCDateTime rejects
            # naive on bind), but hash inputs must not depend on that guarantee.
            raise CanonicalizationError("naive datetime in audit hash input")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        out = {_canon_key(k): _canon_value(v) for k, v in value.items()}
        if len(out) != len(value):
            # e.g. {1: .., "1": ..} — json.dumps would emit duplicate keys and
            # json.loads keeps only the last, so no stable round-trip exists.
            raise CanonicalizationError("dict keys collide after JSON key coercion")
        return out
    if isinstance(value, (list, tuple)):
        return [_canon_value(v) for v in value]
    return value


def canonical_json_bytes(payload: Any) -> bytes:
    """Deterministic ASCII bytes for ``payload`` (audit algo v1)."""
    try:
        return json.dumps(
            _canon_value(payload),
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except CanonicalizationError:
        raise
    except Exception as exc:  # noqa: BLE001 - any dumps failure is a canon failure
        raise CanonicalizationError(str(exc)) from exc


def trace_content(trace: Trace) -> dict[str, Any]:
    """The hash-covered content of one traces row (works on both the pre-flush
    ORM object in ``after_insert`` and a DB-round-tripped row at verify time —
    round-trip identity of every covered field type is pinned by tests)."""
    return {name: getattr(trace, name) for name in TRACE_CONTENT_FIELDS}


def canon_error_content(canon_error: str | None) -> dict[str, Any]:
    """The deterministic stand-in content for a ``canon_status='failed'`` entry.

    Built ONLY from ``canon_error`` as persisted on the entry, so verify can
    recompute the exact same marker without re-raising the original error.
    """
    return {CANON_ERROR_KEY: canon_error or "unknown"}


def entry_payload(
    *,
    entry_type: str,
    trace_id: int | None,
    event_id: int | None,
    cost_at_event: str | None,
    note: str | None,
    canon_status: str,
    canon_error: str | None,
    created_at: datetime,
    content: Any,
) -> dict[str, Any]:
    """The full hash preimage payload for one chain entry.

    Entry METADATA is deliberately inside the preimage: hashing only the
    content would let entry_type swaps / trace_id re-pointing / cost snapshot
    edits go undetected by verify.
    """
    return {
        "algo_version": ALGO_VERSION,
        "entry_type": entry_type,
        "trace_id": trace_id,
        "event_id": event_id,
        "cost_at_event": cost_at_event,
        "note": note,
        "canon_status": canon_status,
        "canon_error": canon_error,
        "created_at": created_at,
        "content": content,
    }


def compute_row_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    """``sha256(prev_hash_hex || canonical_json_bytes(payload))``."""
    return hashlib.sha256(
        prev_hash.encode("ascii") + canonical_json_bytes(payload)
    ).hexdigest()

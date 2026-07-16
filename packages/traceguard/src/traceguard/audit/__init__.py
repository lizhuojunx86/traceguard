"""traceguard.audit — opt-in, tamper-evident audit trail for traces (EXPERIMENTAL).

Off the frozen 29-symbol public surface (SPEC §6.6 posture, like
``exporters.otel`` and ``contamination``): import from THIS submodule path,
not from ``traceguard``. Experimental means the audit API may evolve in minor
releases; the core contract is untouched either way. Zero extra dependencies
(stdlib ``hashlib``/``json`` only).

Three honestly-layered capabilities (docs/audit.md has the full table):

1. **ORM-layer append-only guard** — anti-footgun. Blocks accidental ORM
   UPDATE/DELETE (and session-level bulk DML) against traces; Core SQL, raw
   drivers, file edits, and non-attached processes bypass it.
2. **Hash chain** — tamper-EVIDENT, not tamper-proof. Every ORM-inserted trace
   is chained; ``verify_chain`` detects post-hoc modification of covered
   fields, destroyed rows, and forged entries. It is not a MAC: with no key in
   v1, a full-chain rewrite or tail truncation is undetectable without an
   externally stored anchor.
3. **Exportable anchor** — ``export_anchor()`` emits the chain head digest for
   storage OUTSIDE the DB (git commit, email, third-party timestamping).

Importing this module has no side effects; nothing activates until
:func:`enable` / :func:`attach`. Chain failures are fail-open by default
(SPEC §4.1) — strict mode via ``enable(strict=True)`` or
``TRACEGUARD_AUDIT_STRICT=1``.

Quickstart::

    import traceguard
    from traceguard import audit

    engine = traceguard.make_engine("sqlite:///traces.db")
    audit.enable(engine)               # tables + settings + backfill + attach
    traceguard.tracer.configure(engine)
    # ... traces written through the tracer are now chained ...
    print(audit.verify_chain(engine).summary())
    print(audit.export_anchor(engine).to_json())   # store this OUTSIDE the DB
"""
from __future__ import annotations

from traceguard.audit.canonical import (
    ALGO_VERSION,
    GENESIS_PREV_HASH,
    TRACE_CONTENT_FIELDS,
    CanonicalizationError,
    canonical_json_bytes,
    compute_row_hash,
)
from traceguard.audit.chain import (
    AppendOnlyViolationError,
    AuditChainError,
    AuditNotEnabledError,
    VALID_COST_EVENT_TYPES,
    attach,
    backfill_traces,
    detach,
    disable,
    enable,
    is_attached,
    is_enabled,
    is_strict,
    record_cost_event,
    record_deletion,
    set_strict,
)
from traceguard.audit.guard import UPDATE_ALLOWED_FIELDS
from traceguard.audit.models import (
    AuditChainEntry,
    AuditCostEvent,
    AuditSettings,
    ensure_audit_tables,
)
from traceguard.audit.verify import (
    ChainAnchor,
    ChainFinding,
    ChainVerificationResult,
    export_anchor,
    verify_chain,
)

__all__ = [
    # activation
    "enable",
    "disable",
    "attach",
    "detach",
    "is_attached",
    "is_enabled",
    "backfill_traces",
    "ensure_audit_tables",
    "set_strict",
    "is_strict",
    # evidence writes
    "record_cost_event",
    "record_deletion",
    "VALID_COST_EVENT_TYPES",
    # verification + anchoring
    "verify_chain",
    "export_anchor",
    "ChainAnchor",
    "ChainFinding",
    "ChainVerificationResult",
    # hash algo (frozen v1)
    "ALGO_VERSION",
    "GENESIS_PREV_HASH",
    "TRACE_CONTENT_FIELDS",
    "canonical_json_bytes",
    "compute_row_hash",
    # ORM
    "AuditSettings",
    "AuditChainEntry",
    "AuditCostEvent",
    "UPDATE_ALLOWED_FIELDS",
    # exceptions
    "AppendOnlyViolationError",
    "AuditChainError",
    "AuditNotEnabledError",
    "CanonicalizationError",
]

"""traceguard.audit — opt-in, tamper-evident audit trail for traces.

Off the frozen 29-symbol public surface (SPEC §6.6 posture, like
``exporters.otel`` and ``contamination``): import from THIS submodule path,
not from ``traceguard``. **Stable since SPEC v1.1 (2026-08-27)**: ``__all__``
below is contract-bound under SPEC §6.3 (a new defaulted parameter or a new
symbol is a minor; removing a parameter, changing its semantics, or removing
a symbol is a major), the verify finding kinds and severities are frozen
(:data:`FINDING_SEVERITY`), and the three boundary statements in
docs/audit.md are normative. Guarded mechanically by
tests/test_audit_api_surface.py in the contract-guard CI job. Zero extra
dependencies (stdlib ``hashlib``/``json``/``urllib``/``subprocess`` only).

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

v2 adds two layers on top, both implementation (SPEC §6.6 keeps them out of
the MUST set):

4. **Anchor sinks + periodic anchoring** (:mod:`traceguard.audit.anchors`) —
   ``anchor_to(engine, [FileAnchorSink(...), GitNoteAnchorSink(...),
   WebhookAnchorSink(...)])`` and ``AnchorScheduler`` shrink "anchoring
   frequency = exposure window" to an interval you choose.
5. **Out-of-band reconciliation** (:mod:`traceguard.audit.reconcile`) —
   ``reconcile()`` compares self-reported token volume per model and window
   with the provider's usage report; disagreement is a ``capture_mismatch``
   finding. Storage integrity (the chain) and capture fidelity (this) are
   different questions.

6. **Per-request existence reconciliation** (SPEC v1.2, layer L1.5) —
   ``reconcile_requests()`` joins ``traces.provider_response_id`` against a
   ``request-ledger/v1`` document from an out-of-band source, one call at a
   time. Totals can cancel (an under-report and an over-report net out, and
   the Usage API gives no call counts at all); existence cannot. A call
   present on one side only is ``capture_unmatched`` (WARN) carrying a
   ``direction``. It still cannot vouch for a matched call's CONTENT — that
   is L2, and L2 stays unbuilt.

7. **Evidence bundle** (:mod:`traceguard.audit.bundle`, SPEC v1.2) —
   ``export_bundle()`` emits an ``evidence-bundle/v1`` JSON document and
   ``verify_bundle()`` checks it with no DB, no network and no signature
   verification. ``content_mode='hash_only'`` strips the hash-covered
   content, which makes entry hashes unrecomputable — so that mode
   verifies chain LINKAGE and the head against an anchor only, and says
   so in different words. Format: ``docs/specs/evidence-bundle.md``.

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
    FINDING_SEVERITY,
    ChainAnchor,
    ChainFinding,
    ChainVerificationResult,
    export_anchor,
    verify_chain,
)
from traceguard.audit.anchors import (
    AnchorScheduler,
    AnchorSink,
    AnchorSinkError,
    FileAnchorSink,
    GitNoteAnchorSink,
    WebhookAnchorSink,
    anchor_to,
    parse_sink_spec,
)
from traceguard.audit.bundle import (
    BUNDLE_SCHEMA,
    CONTENT_NOT_RECOMPUTED,
    VALID_ANCHOR_KINDS,
    BundleVerifyResult,
    anchor_record,
    export_bundle,
    load_bundle,
    verify_bundle,
    write_bundle,
)
from traceguard.audit.reconcile import (
    CAPTURE_MISMATCH,
    CAPTURE_UNMATCHED,
    DIRECTION_OUT_OF_BAND_ONLY,
    DIRECTION_SELF_REPORTED_ONLY,
    DIRECTION_TEXT,
    REQUEST_LEDGER_SCHEMA,
    LedgerRequest,
    RequestLedger,
    RequestReconcileResult,
    load_request_ledger,
    parse_request_ledger,
    reconcile_requests,
    ModelComparison,
    ReconcileResult,
    SideTotals,
    UsageBucket,
    align_window,
    fetch_anthropic_usage,
    load_usage_report,
    reconcile,
    traces_usage,
    usage_from_report,
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
    "FINDING_SEVERITY",
    # anchor sinks + periodic anchoring (v2)
    "AnchorSink",
    "AnchorSinkError",
    "FileAnchorSink",
    "GitNoteAnchorSink",
    "WebhookAnchorSink",
    "anchor_to",
    "AnchorScheduler",
    "parse_sink_spec",
    # out-of-band reconciliation (v2, L1)
    "reconcile",
    "ReconcileResult",
    "ModelComparison",
    "SideTotals",
    "UsageBucket",
    "CAPTURE_MISMATCH",
    "align_window",
    "usage_from_report",
    "load_usage_report",
    "fetch_anthropic_usage",
    "traces_usage",
    # per-request existence reconciliation (SPEC v1.2, L1.5)
    "CAPTURE_UNMATCHED",
    "REQUEST_LEDGER_SCHEMA",
    "DIRECTION_OUT_OF_BAND_ONLY",
    "DIRECTION_SELF_REPORTED_ONLY",
    "DIRECTION_TEXT",
    "LedgerRequest",
    "RequestLedger",
    "RequestReconcileResult",
    "parse_request_ledger",
    "load_request_ledger",
    "reconcile_requests",
    # evidence bundle export + offline verify (SPEC v1.2)
    "BUNDLE_SCHEMA",
    "CONTENT_NOT_RECOMPUTED",
    "VALID_ANCHOR_KINDS",
    "BundleVerifyResult",
    "anchor_record",
    "export_bundle",
    "verify_bundle",
    "write_bundle",
    "load_bundle",
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

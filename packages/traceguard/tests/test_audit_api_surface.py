"""Freeze the traceguard.audit contract surface (SPEC §6.6, stable since v1.1).

Runs in the contract-guard CI job next to test_public_api_surface.py. Three
promises from docs/spec-changes/2026-08-27, made mechanical:

1. API surface — ``traceguard.audit.__all__`` is frozen; a symbol may be ADDED
   (update EXPECTED here, SemVer minor), never removed or renamed (major). The
   parameter names of the public functions may only GROW, and every added
   parameter must carry a default (SPEC §6.3).
2. Finding kinds — the kind → severity table is frozen; a new kind is a minor,
   changing or removing one is a major.
3. Boundary statements — the three statements in docs/audit.md are normative;
   their load-bearing sentences must still be there, verbatim.

Plus the algo v1 envelope, which the golden tests pin byte-for-byte — here it is
stated as a contract fact: the columns added after SPEC v1.0 (``agent_id``,
``session_id`` in v1.1, ``provider_response_id`` in v1.2) are OUTSIDE it.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from traceguard import audit

EXPECTED_AUDIT_API = {
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
    # OpenTimestamps anchor (SPEC v1.2) — ADDED deliberately (minor, §6.3).
    "OtsAnchorSink",
    "OtsProof",
    "ots_digest",
    "parse_ots_proof",
    "verify_ots_proof",
    "upgrade_proof",
    "OTS_DEFAULT_CALENDARS",
    "OTS_PENDING",
    "OTS_COMPLETE",
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
    # per-request existence reconciliation (SPEC v1.2, L1.5) — ADDED
    # deliberately; new symbols on this surface are a SemVer minor (§6.3).
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
    # evidence bundle (SPEC v1.2) — ADDED deliberately (minor, §6.3).
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
}

# kind -> severity, frozen since SPEC v1.1 (8 v1 kinds + capture_mismatch),
# plus capture_unmatched from SPEC v1.2. A new kind is a MINOR and updating
# this table is the deliberate act that records it (2026-08-27 revision A);
# changing or removing an existing one is a major and this test must fight it.
FROZEN_FINDING_SEVERITY = {
    "anchor_mismatch": "BREAK",
    "link_broken": "BREAK",
    "hash_mismatch": "BREAK",
    "missing_trace": "BREAK",
    "missing_cost_event": "BREAK",
    "cost_mismatch": "WARN",
    "deleted_with_record": "WARN",
    "coverage_gap": "GAP",
    "capture_mismatch": "WARN",
    "capture_unmatched": "WARN",
}

# Parameter names of the public functions as of SPEC v1.1. The test allows the
# actual signature to be a SUPERSET, provided every extra parameter has a
# default — that is exactly the §6.3 minor/major line.
FROZEN_PARAMETERS = {
    "enable": ("engine", "append_only", "backfill", "strict"),
    "disable": ("engine",),
    "attach": ("engine",),
    "detach": ("engine",),
    "is_attached": ("engine",),
    "is_enabled": ("engine",),
    "backfill_traces": ("engine", "chunk_size"),
    "ensure_audit_tables": ("engine",),
    "set_strict": ("value",),
    "is_strict": (),
    "record_cost_event": (
        "engine",
        "trace_id",
        "event_type",
        "old_value",
        "new_value",
        "reason",
        "batch_id",
    ),
    "record_deletion": ("engine", "trace_id", "reason"),
    "verify_chain": ("engine", "from_anchor", "incremental"),
    "export_anchor": ("engine",),
    "anchor_to": ("engine", "sinks"),
    "parse_sink_spec": ("spec",),
    "reconcile": (
        "engine",
        "starting_at",
        "ending_at",
        "provider",
        "tolerance",
        "absolute_floor",
        "project",
        "operation",
        "model_map",
    ),
    "align_window": ("starting_at", "ending_at", "bucket_width"),
    "usage_from_report": ("pages",),
    "load_usage_report": ("path",),
    "fetch_anthropic_usage": (
        "starting_at",
        "ending_at",
        "admin_key",
        "bucket_width",
        "models",
        "api_key_ids",
        "workspace_ids",
        "base_url",
        "timeout",
        "opener",
        "user_agent",
    ),
    "traces_usage": ("engine", "starting_at", "ending_at", "project", "operation"),
    "canonical_json_bytes": ("payload",),
    "compute_row_hash": ("prev_hash", "payload"),
    # SPEC v1.2 additions. Frozen from the moment they entered __all__ — the
    # 08-27 precedent is that every public function on this surface is
    # parameter-frozen, and a function that is exported but unlisted here is
    # contract-bound with nothing enforcing §6.3 on it.
    "reconcile_requests": (
        "engine",
        "ledger",
        "starting_at",
        "ending_at",
        "project",
        "operation",
    ),
    "parse_request_ledger": ("payload",),
    "load_request_ledger": ("path",),
    "export_bundle": (
        "engine",
        "since",
        "until",
        "trace_ids",
        "content_mode",
        "include_sources",
        "anchors",
    ),
    "verify_bundle": ("bundle",),
    "write_bundle": ("bundle", "path"),
    "load_bundle": ("path",),
    "anchor_record": ("anchor", "kind", "location"),
    "ots_digest": ("anchor",),
    "parse_ots_proof": ("data",),
    "verify_ots_proof": ("proof", "anchor"),
    "upgrade_proof": ("path", "calendars", "calendar_factory"),
}


def test_every_public_callable_is_parameter_frozen():
    """No public function may sit on this surface unfrozen.

    Without this, adding a symbol to ``EXPECTED_AUDIT_API`` and forgetting
    ``FROZEN_PARAMETERS`` silently exempts it from §6.3 — it looks covered
    because the surface test passes.
    """
    unfrozen = sorted(
        name
        for name in audit.__all__
        if callable(getattr(audit, name))
        and not isinstance(getattr(audit, name), type)
        and name not in FROZEN_PARAMETERS
    )
    assert not unfrozen, (
        f"public function(s) {unfrozen} are exported but not in FROZEN_PARAMETERS — "
        "add them (with their current parameters) so §6.3 is enforced on them too"
    )

BOUNDARY_SENTENCES = (
    "哈希链不是 MAC,v1 无密钥。",
    "锚定频率 = 暴露窗口",
    'backfill 条目只证明"启用审计那一刻该行长这样"',
    "`cost_usd` 在哈希信封之外",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
AUDIT_DOC = REPO_ROOT / "docs" / "audit.md"


def test_audit_public_surface_is_frozen():
    assert set(audit.__all__) == EXPECTED_AUDIT_API


def test_audit_all_has_no_duplicates_and_is_importable():
    assert len(audit.__all__) == len(set(audit.__all__))
    for name in audit.__all__:
        assert hasattr(audit, name), f"{name} listed in __all__ but not importable"


def test_finding_kinds_and_severities_are_frozen():
    assert dict(audit.FINDING_SEVERITY) == FROZEN_FINDING_SEVERITY
    with pytest.raises(TypeError):  # read-only view, not a dict anyone can edit at runtime
        audit.FINDING_SEVERITY["new_kind"] = "WARN"  # type: ignore[index]


@pytest.mark.parametrize("name", sorted(FROZEN_PARAMETERS))
def test_public_function_parameters_only_grow_with_defaults(name: str):
    fn = getattr(audit, name)
    params = inspect.signature(fn).parameters
    frozen = FROZEN_PARAMETERS[name]
    missing = [p for p in frozen if p not in params]
    assert not missing, f"{name} lost parameter(s) {missing} — that is a SemVer major"
    for pname, param in params.items():
        if pname in frozen:
            continue
        assert param.default is not inspect.Parameter.empty, (
            f"{name} gained required parameter {pname!r}; new parameters must have defaults "
            "(SPEC §6.3) — or this is a major and FROZEN_PARAMETERS must be updated deliberately"
        )


def test_algo_v1_envelope_excludes_the_post_1_0_columns_and_cost_usd():
    assert audit.ALGO_VERSION == 1
    for outside in ("agent_id", "session_id", "provider_response_id", "cost_usd"):
        assert outside not in audit.TRACE_CONTENT_FIELDS


def test_boundary_statements_are_still_verbatim_in_the_docs():
    if not AUDIT_DOC.is_file():
        pytest.skip(f"docs/audit.md not reachable from the package tree: {AUDIT_DOC}")
    text = AUDIT_DOC.read_text(encoding="utf-8")
    assert "边界声明(逐字级,不许弱化)" in text
    for sentence in BOUNDARY_SENTENCES:
        assert sentence in text, f"boundary statement weakened or removed: {sentence!r}"
    # The finding table in the docs must name every frozen kind.
    for kind in FROZEN_FINDING_SEVERITY:
        assert f"`{kind}`" in text, f"docs/audit.md does not document finding kind {kind}"

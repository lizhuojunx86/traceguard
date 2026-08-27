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
stated as a contract fact: the SPEC v1.1 columns are OUTSIDE it.
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

# kind -> severity, frozen since SPEC v1.1 (8 v1 kinds + capture_mismatch).
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
}

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


def test_algo_v1_envelope_excludes_the_v1_1_columns_and_cost_usd():
    assert audit.ALGO_VERSION == 1
    for outside in ("agent_id", "session_id", "cost_usd"):
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

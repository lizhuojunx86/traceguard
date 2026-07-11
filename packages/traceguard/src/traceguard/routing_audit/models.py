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
from decimal import Decimal

from sqlalchemy import Boolean, Integer, Numeric, String, Text
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


class RoutingAuditTaskTag(RoutingAuditBase):
    """task_type label for one tagging unit of a main-thread session.

    A unit is an idle-gap segment of a session's human turns (see
    ``task_tags.iter_session_units``): ``unit_id = <session_id>#s<NN>`` with
    the half-open time window ``[ts_start, ts_end)`` (``ts_end`` NULL = until
    session end). Traces join by session_id + invoked_at window at report
    time — the contract ``traces`` table is not touched. No prompt content
    is stored here; summaries exist only in the export CSV.
    """

    __tablename__ = "routing_audit_task_tags"

    unit_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project: Mapped[str] = mapped_column(String(128), nullable=False)
    ts_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    ts_end: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    n_turns: Mapped[int] = mapped_column(Integer, nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # "heuristic" (keyword classifier) or "manual" (CSV re-import; never
    # overwritten by later heuristic runs).
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


class RoutingDecision(RoutingAuditBase):
    """One policy-vs-actual verdict per (tagging unit, component).

    A *policy deviation audit* row, not a diary entry: the declarable routing
    policy (``routing_policy.yaml``) says which tier each
    (project, component, task_type) should use; this table records where the
    observed traces landed and whether that crossed a tier boundary.

    Grain = ``(unit_id, component)`` → ``decision_id = <unit_id>#<component>``.
    Within a unit's time window a component may run several models; the
    dominant one (most traces) is the ``actual_model``. ``deviation`` is a
    tier mismatch (same-tier substitutions, e.g. opus↔fable, are not
    deviations). ``reason`` / ``outcome`` start empty and are filled by the
    manual CSV round-trip; ``source="manual"`` rows are never regenerated.
    """

    __tablename__ = "routing_decisions"

    decision_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    unit_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project: Mapped[str] = mapped_column(String(128), nullable=False)
    component: Mapped[str] = mapped_column(String(128), nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    expected_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actual_model: Mapped[str] = mapped_column(String(128), nullable=False)
    actual_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    deviation: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)

    n_traces: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)

    # Filled by the manual CSV round-trip (default NULL / "unknown").
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    # "generated" (policy vs actual) or "manual" (reason/outcome supplied).
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="generated")

    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


class RerunResult(RoutingAuditBase):
    """One rerun of a self-contained consult on a target model (harness output).

    ``est_cost_usd`` is the dry-run list-price estimate (target-model
    re-pricing of the unit's tokens); the ``actual_*`` / ``*_answer`` columns
    stay NULL until a real execution fills them. ``original_answer`` and
    ``rerun_answer`` are LOCAL-ONLY (DB is gitignored) and are NEVER written to
    any export/CSV/report — only hashes and costs leave the DB.
    """

    __tablename__ = "routing_audit_rerun_results"

    rerun_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    unit_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    project: Mapped[str] = mapped_column(String(128), nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_model: Mapped[str] = mapped_column(String(128), nullable=False)
    target_model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Redacted ≤100-char question label for the blind sheet (local-only).
    prompt_summary: Mapped[str | None] = mapped_column(String(120), nullable=True)

    est_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # LOCAL-ONLY answer bodies — never leave the DB.
    original_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    rerun_answer: Mapped[str | None] = mapped_column(Text, nullable=True)

    # "estimated" (dry-run) | "completed" | "failed" | "skipped"
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="estimated")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


class BlindEval(RoutingAuditBase):
    """Blind A/B verdict for one rerun; the position→model map lives in-DB only.

    The exported blind sheet shows answer A / answer B in a fixed but
    label-free order; ``position_a_model`` / ``position_b_model`` are the
    unblinding key kept here, revealed only after ``verdict`` is imported.
    """

    __tablename__ = "routing_audit_blind_eval"

    blind_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    unit_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    position_a_model: Mapped[str] = mapped_column(String(128), nullable=False)
    position_b_model: Mapped[str] = mapped_column(String(128), nullable=False)
    # a_better | b_better | tie  (NULL until imported)
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Reviewer-added: any external facts the answer relied on (local note), and
    # whether the reviewer recognised the original: none | soft | hard.
    external_facts: Mapped[str | None] = mapped_column(Text, nullable=True)
    recognition: Mapped[str | None] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


class RoutingAuditRepriceLog(RoutingAuditBase):
    """Audit trail for a reprice run: one row per trace whose cost_usd changed.

    Records the old value (NULL for a first-time fill) and the new value under
    a ``batch_id`` so a run is idempotent (already-priced rows are skipped) and
    reversible (:func:`reprice.rollback_reprice` restores ``old_cost_usd``).
    """

    __tablename__ = "routing_audit_reprice_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trace_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    old_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    new_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    repriced_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


def ensure_tables(engine: Engine) -> None:
    """Create the routing_audit tables if missing (idempotent, additive-only).

    KNOWN DEBT — no migrations. ``create_all`` only CREATEs missing tables; it
    never ALTERs an existing one. So adding a column to a table that a
    long-lived local DB already has (as ``prompt_summary`` was added to
    ``routing_audit_rerun_results``) does NOT reach that DB — fresh DBs get it,
    existing ones need a one-off ``ALTER TABLE ... ADD COLUMN`` by hand. This is
    accepted for these contract-external local tables; do NOT add a migration
    framework for them (the SDK deliberately has none — same posture as the
    core store, where ``feature_as_of`` shipped create_all-only in 0.9.0).
    """
    RoutingAuditBase.metadata.create_all(engine)

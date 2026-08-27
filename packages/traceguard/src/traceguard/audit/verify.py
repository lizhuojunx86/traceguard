"""Chain verification and external anchoring.

``verify_chain`` is deliberately TWO passes (a chain-only walk is provably
blind to rows inserted while audit was disabled — they have no entry at all):

1. Walk ``audit_chain_entries`` in ``seq`` order (never assumed contiguous —
   Postgres sequences skip): check linkage (``prev_hash`` == previous entry's
   stored ``row_hash``) and recompute every entry's ``row_hash`` from its
   persisted metadata plus the CURRENT content of the row it references.
   Entry metadata is inside the preimage, so forging entry_type / re-pointing
   trace_id / editing cost snapshots all surface as ``hash_mismatch``.
2. Sweep the traces table for rows with no write/backfill entry
   (``coverage_gap``) and cross-check every trace's live ``cost_usd`` against
   the newest chained cost evidence (``cost_mismatch``). Pass 2 always runs in
   full, in every mode.

Anchor modes: passing ``from_anchor`` ALWAYS adds an anchor-consistency check
(the entry at the anchor's seq must still carry the anchored hash — catching
tail truncation and rewrites since the export) on top of the default full
walk, making an anchored verify strictly stronger than a plain one. Only with
``incremental=True`` does the walk start AT the anchor instead of genesis:
hash work becomes proportional to what was appended since the export, but
everything before the anchor is *trusted, not verified* (its metadata is still
read — unverified — so coverage and cost checks stay complete).

Legal deletions & trace_id reuse: a write/backfill entry that is followed
(in seq order) by a deletion tombstone for the same trace_id is *superseded*:
its referenced row is gone, and a row later re-inserted under the same
trace_id (SQLite reuses rowids; the contract ``traces`` table has no
``sqlite_autoincrement``) is a different generation, so superseded entries
report ``deleted_with_record`` (WARN) instead of a false ``hash_mismatch``.

What verify CANNOT see (documented, do not overclaim): the chain is a hash
chain, not a MAC — there is no key in v1. Anyone who can write the DB file can
rewrite history and recompute every hash, and a tail truncation leaves a
perfectly valid shorter chain. Both are detectable ONLY against an anchor
exported earlier and stored OUTSIDE the DB; anchors protect entries up to the
moment they were exported, so anchoring frequency defines the exposure window.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
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
from traceguard.audit.models import AuditChainEntry, AuditCostEvent
from traceguard.store.models import Trace

_CHUNK = 500
_MAX_ITEMIZED_FINDINGS = 50  # per kind; the rest are aggregated into one line
_COST_QUANTUM = Decimal("0.000001")  # Numeric(12, 6) storage granularity

BREAK = "BREAK"
WARN = "WARN"
GAP = "GAP"


@dataclass(frozen=True)
class ChainFinding:
    kind: str
    severity: str
    seq: int | None
    trace_id: int | None
    detail: str


@dataclass(frozen=True)
class ChainAnchor:
    """Exportable chain head digest — the external trust root.

    Store the JSON line OUTSIDE the audited DB (git commit message, email,
    third-party timestamping); an anchor kept next to the chain protects
    nothing.
    """

    seq: int
    row_hash: str
    algo_version: int
    entry_count: int
    exported_at: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "traceguard_audit_anchor": 1,
                "seq": self.seq,
                "row_hash": self.row_hash,
                "algo_version": self.algo_version,
                "entry_count": self.entry_count,
                "exported_at": self.exported_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> ChainAnchor:
        data = json.loads(text)
        return cls(
            seq=int(data["seq"]),
            row_hash=str(data["row_hash"]),
            algo_version=int(data["algo_version"]),
            entry_count=int(data["entry_count"]),
            exported_at=str(data["exported_at"]),
        )


@dataclass
class ChainVerificationResult:
    ok: bool
    findings: list[ChainFinding] = field(default_factory=list)
    entries_checked: int = 0
    head_seq: int | None = None
    head_hash: str | None = None
    traces_total: int = 0
    chained_traces: int = 0
    coverage_gap_traces: int = 0
    cost_events: int = 0

    @property
    def first_break(self) -> ChainFinding | None:
        return next((f for f in self.findings if f.severity == BREAK), None)

    def summary(self) -> str:
        breaks = sum(1 for f in self.findings if f.severity == BREAK)
        warns = sum(1 for f in self.findings if f.severity == WARN)
        status = "OK" if self.ok else "TAMPER-EVIDENT FAILURE"
        return (
            f"chain {status}: {self.entries_checked} entries checked, "
            f"{breaks} break(s), {warns} warning(s), "
            f"{self.coverage_gap_traces}/{self.traces_total} trace(s) uncovered "
            f"(head seq={self.head_seq})"
        )


def _entry_content(entry: AuditChainEntry, referenced: Any) -> Any:
    """Recompute the hash-covered content exactly as the writer built it."""
    if entry.canon_status == "failed":
        return canon_error_content(entry.canon_error)
    if entry.entry_type in ("write", "backfill"):
        return trace_content(referenced)
    if entry.entry_type == "cost_event":
        return {name: getattr(referenced, name) for name in COST_EVENT_CONTENT_FIELDS}
    return None  # 'deletion' (and unknown types hash their metadata only)


def _recompute_row_hash(entry: AuditChainEntry, referenced: Any) -> str:
    payload = entry_payload(
        entry_type=entry.entry_type,
        trace_id=entry.trace_id,
        event_id=entry.event_id,
        cost_at_event=entry.cost_at_event,
        note=entry.note,
        canon_status=entry.canon_status,
        canon_error=entry.canon_error,
        created_at=entry.created_at,
        content=_entry_content(entry, referenced),
    )
    return compute_row_hash(entry.prev_hash, payload)


def _cost_equal(expected: str | None, actual: Decimal | None) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    try:
        expected_d = Decimal(expected)
        actual_d = Decimal(actual)
    except InvalidOperation:
        return False
    if expected_d == actual_d:  # Decimal == is numeric: 0.5 == 0.500000
        return True
    try:
        # Tolerate Numeric(12,6) storage rounding of >6dp snapshots. quantize
        # overflows for huge coefficients — those were caught by == above or
        # are genuinely unequal.
        return expected_d.quantize(_COST_QUANTUM) == actual_d.quantize(_COST_QUANTUM)
    except InvalidOperation:
        return False


class _FindingCollector:
    def __init__(self) -> None:
        self.findings: list[ChainFinding] = []
        self._per_kind: dict[str, int] = {}
        self._suppressed: dict[str, int] = {}

    def add(self, kind: str, severity: str, seq: int | None, trace_id: int | None, detail: str) -> None:
        n = self._per_kind.get(kind, 0)
        if n < _MAX_ITEMIZED_FINDINGS:
            self.findings.append(ChainFinding(kind, severity, seq, trace_id, detail))
        else:
            self._suppressed[kind] = self._suppressed.get(kind, 0) + 1
        self._per_kind[kind] = n + 1

    def finalize(self, severity_of: dict[str, str]) -> list[ChainFinding]:
        for kind, extra in self._suppressed.items():
            self.findings.append(
                ChainFinding(
                    kind,
                    severity_of.get(kind, WARN),
                    None,
                    None,
                    f"... and {extra} more {kind} finding(s) suppressed "
                    f"(itemized cap {_MAX_ITEMIZED_FINDINGS})",
                )
            )
        return self.findings


_SEVERITY = {
    "anchor_mismatch": BREAK,
    "link_broken": BREAK,
    "hash_mismatch": BREAK,
    "missing_trace": BREAK,
    "missing_cost_event": BREAK,
    "cost_mismatch": WARN,
    "deleted_with_record": WARN,
    "coverage_gap": GAP,
    # v2 (SPEC v1.1): produced by traceguard.audit.reconcile, not by
    # verify_chain — the self-reported token volume disagrees with the
    # provider's out-of-band report for the same model and window.
    "capture_mismatch": WARN,
}

#: Finding kind → severity. Contract-frozen since SPEC v1.1 (§6.6): adding a
#: kind is a minor, changing or removing one is a major; guarded by
#: tests/test_audit_api_surface.py in the contract-guard CI job.
FINDING_SEVERITY: Mapping[str, str] = MappingProxyType(_SEVERITY)


def verify_chain(
    engine: Engine,
    *,
    from_anchor: ChainAnchor | None = None,
    incremental: bool = False,
) -> ChainVerificationResult:
    """Recompute the chain against current DB content.

    Default is a full O(n) walk (~26k rows verify in well under a second);
    ``from_anchor`` additionally checks the stored chain against a previously
    exported anchor. ``incremental=True`` (only meaningful with an anchor)
    starts the hash walk at the anchor instead of genesis — pre-anchor content
    is then trusted, not verified. Coverage and cost checks (pass 2) always
    run in full.
    """
    result = ChainVerificationResult(ok=True)
    collector = _FindingCollector()

    with Session(engine) as sess:
        # trace_id -> newest deletion-tombstone seq (for supersede checks)
        tombstone_max: dict[int, int] = {}
        for trace_id, seq in sess.execute(
            select(AuditChainEntry.trace_id, AuditChainEntry.seq)
            .where(AuditChainEntry.entry_type == "deletion")
            .where(AuditChainEntry.trace_id.is_not(None))
        ):
            tombstone_max[trace_id] = max(seq, tombstone_max.get(trace_id, -1))

        anchor_trusted = False
        if from_anchor is not None:
            if from_anchor.seq > 0:
                anchored = sess.get(AuditChainEntry, from_anchor.seq)
                if anchored is None or anchored.row_hash != from_anchor.row_hash:
                    collector.add(
                        "anchor_mismatch",
                        BREAK,
                        from_anchor.seq,
                        None,
                        "anchored entry is missing — the chain was truncated or "
                        "rewritten since the anchor was exported"
                        if anchored is None
                        else f"anchor row_hash {from_anchor.row_hash} != stored {anchored.row_hash}",
                    )
                else:
                    anchor_trusted = True
            else:
                anchor_trusted = True  # empty-chain anchor: nothing to compare

        walk_from_anchor = incremental and anchor_trusted and from_anchor.seq > 0

        # cost evidence per trace: write/backfill baseline, overridden by
        # cost_event entries in seq order; entries superseded by a later
        # deletion tombstone contribute nothing (their generation is gone).
        expected_cost: dict[int, str | None] = {}
        baseline_seen: set[int] = set()

        def _superseded(trace_id: int | None, seq: int) -> bool:
            return trace_id is not None and tombstone_max.get(trace_id, -1) > seq

        def _apply_cost_state(
            entry_type: str, trace_id: int | None, cost_at_event: str | None, seq: int
        ) -> None:
            if trace_id is None or _superseded(trace_id, seq):
                return
            if entry_type in ("write", "backfill"):
                baseline_seen.add(trace_id)
                expected_cost[trace_id] = cost_at_event
            elif entry_type == "cost_event":
                expected_cost[trace_id] = cost_at_event

        if walk_from_anchor:
            # Seed pass-2 state from pre-anchor entry METADATA (trusted, not
            # verified — no hash recompute), so coverage/cost stay complete.
            for entry_type, trace_id, cost_at_event, seq in sess.execute(
                select(
                    AuditChainEntry.entry_type,
                    AuditChainEntry.trace_id,
                    AuditChainEntry.cost_at_event,
                    AuditChainEntry.seq,
                )
                .where(AuditChainEntry.seq <= from_anchor.seq)
                .order_by(AuditChainEntry.seq.asc())
            ):
                _apply_cost_state(entry_type, trace_id, cost_at_event, seq)
            prev_hash = from_anchor.row_hash
            entries_query = (
                select(AuditChainEntry)
                .where(AuditChainEntry.seq > from_anchor.seq)
                .order_by(AuditChainEntry.seq.asc())
            )
        else:
            prev_hash = GENESIS_PREV_HASH
            entries_query = select(AuditChainEntry).order_by(AuditChainEntry.seq.asc())

        entries: list[AuditChainEntry] = list(sess.scalars(entries_query))
        result.entries_checked = len(entries)

        for start in range(0, len(entries), _CHUNK):
            chunk = entries[start : start + _CHUNK]
            trace_ids = {
                e.trace_id
                for e in chunk
                if e.entry_type in ("write", "backfill") and e.trace_id is not None
            }
            traces: dict[int, Trace] = {
                t.trace_id: t
                for t in sess.scalars(select(Trace).where(Trace.trace_id.in_(trace_ids)))
            }
            event_ids = {e.event_id for e in chunk if e.entry_type == "cost_event"}
            events: dict[int, AuditCostEvent] = {
                ev.event_id: ev
                for ev in sess.scalars(
                    select(AuditCostEvent).where(AuditCostEvent.event_id.in_(event_ids))
                )
            }

            for entry in chunk:
                if entry.prev_hash != prev_hash:
                    collector.add(
                        "link_broken",
                        BREAK,
                        entry.seq,
                        entry.trace_id,
                        f"prev_hash {entry.prev_hash} != previous entry row_hash {prev_hash}",
                    )
                # Content checks continue against the entry's own stored
                # linkage so one broken link doesn't cascade into noise.
                referenced: Any = None
                skip_recompute = False
                if entry.entry_type in ("write", "backfill") and entry.trace_id is not None:
                    if _superseded(entry.trace_id, entry.seq):
                        # The referenced generation was legally deleted; a row
                        # re-inserted under the same trace_id is a DIFFERENT
                        # row and must not be hashed against this entry.
                        collector.add(
                            "deleted_with_record",
                            WARN,
                            entry.seq,
                            entry.trace_id,
                            "chained trace was deleted with a tombstone; its "
                            "content is no longer verifiable",
                        )
                        skip_recompute = True
                    else:
                        referenced = traces.get(entry.trace_id)
                        if referenced is None:
                            collector.add(
                                "missing_trace",
                                BREAK,
                                entry.seq,
                                entry.trace_id,
                                "chained trace row no longer exists (destruction of "
                                "covered evidence, no tombstone)",
                            )
                            skip_recompute = True
                elif entry.entry_type == "cost_event":
                    referenced = events.get(entry.event_id)
                    if referenced is None:
                        collector.add(
                            "missing_cost_event",
                            BREAK,
                            entry.seq,
                            entry.trace_id,
                            f"chained cost event {entry.event_id} no longer exists",
                        )
                        skip_recompute = True

                if not skip_recompute:
                    try:
                        recomputed = _recompute_row_hash(entry, referenced)
                    except CanonicalizationError as exc:
                        recomputed = None
                        collector.add(
                            "hash_mismatch",
                            BREAK,
                            entry.seq,
                            entry.trace_id,
                            f"content no longer canonicalizes ({exc}) although the "
                            "entry was written with canon_status='ok' — content changed",
                        )
                    if recomputed is not None and recomputed != entry.row_hash:
                        collector.add(
                            "hash_mismatch",
                            BREAK,
                            entry.seq,
                            entry.trace_id,
                            f"recomputed {recomputed} != stored {entry.row_hash}",
                        )

                _apply_cost_state(
                    entry.entry_type, entry.trace_id, entry.cost_at_event, entry.seq
                )
                prev_hash = entry.row_hash

        if entries:
            result.head_seq = entries[-1].seq
            result.head_hash = entries[-1].row_hash
        elif from_anchor is not None and anchor_trusted:
            result.head_seq = from_anchor.seq
            result.head_hash = from_anchor.row_hash

        result.cost_events = (
            sess.scalar(
                select(func.count())
                .select_from(AuditChainEntry)
                .where(AuditChainEntry.entry_type == "cost_event")
            )
            or 0
        )
        result.chained_traces = len(baseline_seen)

        # ── pass 2 (always full): coverage sweep + cost evidence check ──
        # (a chain-only walk is silent about rows that never got an entry)
        gap_sample: list[int] = []
        gaps = 0
        for trace_id, cost_usd in sess.execute(select(Trace.trace_id, Trace.cost_usd)):
            result.traces_total += 1
            if trace_id not in baseline_seen:
                gaps += 1
                if len(gap_sample) < 10:
                    gap_sample.append(trace_id)
            if trace_id in expected_cost and not _cost_equal(
                expected_cost[trace_id], cost_usd
            ):
                collector.add(
                    "cost_mismatch",
                    WARN,
                    None,
                    trace_id,
                    f"current cost_usd={cost_usd} does not match the newest chained "
                    f"cost evidence ({expected_cost[trace_id]!r}); cost_usd is "
                    "outside the hash envelope — record legal writes via "
                    "record_cost_event",
                )
        result.coverage_gap_traces = gaps
        if gaps:
            collector.add(
                "coverage_gap",
                GAP,
                None,
                None,
                f"{gaps} trace(s) have no chain entry (pre-enable rows, disable "
                f"windows, or fail-open skips); sample trace_ids: {gap_sample}",
            )

    result.findings = collector.finalize(_SEVERITY)
    result.ok = all(f.severity != BREAK for f in result.findings)
    return result


def export_anchor(engine: Engine) -> ChainAnchor:
    """The current chain head as an externally storable trust root.

    An empty chain anchors as ``(seq=0, genesis)``. Anything appended after an
    export is not protected by that anchor — export regularly.
    """
    with Session(engine) as sess:
        head = sess.execute(
            select(AuditChainEntry.seq, AuditChainEntry.row_hash, AuditChainEntry.algo_version)
            .order_by(AuditChainEntry.seq.desc())
            .limit(1)
        ).first()
        count = sess.scalar(select(func.count()).select_from(AuditChainEntry)) or 0
    if head is None:
        return ChainAnchor(
            seq=0,
            row_hash=GENESIS_PREV_HASH,
            algo_version=ALGO_VERSION,
            entry_count=0,
            exported_at=datetime.now(timezone.utc).isoformat(),
        )
    return ChainAnchor(
        seq=head.seq,
        row_hash=head.row_hash,
        algo_version=head.algo_version,
        entry_count=count,
        exported_at=datetime.now(timezone.utc).isoformat(),
    )

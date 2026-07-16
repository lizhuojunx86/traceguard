"""verify_chain finding taxonomy + anchor semantics.

The two-pass structure and every finding kind get exercised here, including
the honesty-critical negatives: tail truncation is INVISIBLE to a plain walk
(only an external anchor catches it), and a pre-anchor tamper is invisible to
an anchored (incremental) walk.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard import audit
from traceguard.store.models import Trace, make_engine


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:", create_all=True)
    yield eng
    audit.detach(eng)
    audit.set_strict(False)


def _add_trace(engine: Engine, *, cost: Decimal | None = Decimal("0.001")) -> int:
    with Session(engine) as sess:
        t = Trace(
            project="p", component="c", operation="llm_complete",
            input_hash="h" * 64, parse_status="success",
            invoked_at=datetime.now(timezone.utc), cost_usd=cost,
        )
        sess.add(t)
        sess.commit()
        return t.trace_id


def _kinds(result) -> set[str]:
    return {f.kind for f in result.findings}


def test_clean_chain_is_ok(engine: Engine) -> None:
    audit.enable(engine, backfill=False)
    for _ in range(5):
        _add_trace(engine)
    result = audit.verify_chain(engine)
    assert result.ok
    assert result.entries_checked == 5
    assert result.chained_traces == 5
    assert result.coverage_gap_traces == 0
    assert result.findings == []
    assert "OK" in result.summary()


def test_covered_field_tamper_is_hash_mismatch(engine: Engine) -> None:
    audit.enable(engine, backfill=False)
    tid = _add_trace(engine)
    _add_trace(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"UPDATE traces SET model_id='forged' WHERE trace_id={tid}")
    result = audit.verify_chain(engine)
    assert not result.ok
    assert result.first_break.kind == "hash_mismatch"
    assert result.first_break.trace_id == tid


def test_entry_metadata_forgery_is_detected(engine: Engine) -> None:
    # Flipping entry_type (write → backfill) changes the evidence semantics
    # without touching the trace row; metadata is in the preimage so it breaks.
    audit.enable(engine, backfill=False)
    _add_trace(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE audit_chain_entries SET entry_type='backfill' WHERE seq=1")
    result = audit.verify_chain(engine)
    assert not result.ok and result.first_break.kind == "hash_mismatch"


def test_cost_snapshot_forgery_is_detected(engine: Engine) -> None:
    audit.enable(engine, backfill=False)
    _add_trace(engine, cost=Decimal("0.001"))
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE audit_chain_entries SET cost_at_event='0.000001' WHERE seq=1")
    result = audit.verify_chain(engine)
    assert not result.ok and result.first_break.kind == "hash_mismatch"


def test_missing_trace_is_break_tombstoned_is_warn(engine: Engine) -> None:
    audit.enable(engine, backfill=False)
    gone = _add_trace(engine)
    tombstoned = _add_trace(engine)
    audit.record_deletion(engine, trace_id=tombstoned, reason="legal cleanup")
    with engine.begin() as conn:
        conn.exec_driver_sql(f"DELETE FROM traces WHERE trace_id IN ({gone}, {tombstoned})")
    result = audit.verify_chain(engine)
    assert not result.ok
    by_kind = {f.kind: f for f in result.findings}
    assert by_kind["missing_trace"].trace_id == gone
    assert by_kind["missing_trace"].severity == "BREAK"
    assert by_kind["deleted_with_record"].trace_id == tombstoned
    assert by_kind["deleted_with_record"].severity == "WARN"


def test_missing_cost_event_is_break(engine: Engine) -> None:
    audit.enable(engine, backfill=False)
    tid = _add_trace(engine)
    audit.record_cost_event(
        engine, trace_id=tid, event_type="correction",
        old_value=Decimal("0.001"), new_value=Decimal("0.002"),
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(f"UPDATE traces SET cost_usd=0.002 WHERE trace_id={tid}")
        conn.exec_driver_sql("DELETE FROM audit_cost_events")
    result = audit.verify_chain(engine)
    assert "missing_cost_event" in _kinds(result) and not result.ok


def test_unrecorded_cost_change_is_warn_recorded_is_clean(engine: Engine) -> None:
    audit.enable(engine, backfill=False)
    tid = _add_trace(engine, cost=Decimal("0.001"))
    with engine.begin() as conn:  # silent edit of the one uncovered field
        conn.exec_driver_sql(f"UPDATE traces SET cost_usd=0.003 WHERE trace_id={tid}")
    result = audit.verify_chain(engine)
    assert result.ok  # WARN, not BREAK: cost_usd is outside the hash envelope
    assert "cost_mismatch" in _kinds(result)

    audit.record_cost_event(
        engine, trace_id=tid, event_type="correction",
        old_value=Decimal("0.001"), new_value=Decimal("0.003"), reason="test",
    )
    result = audit.verify_chain(engine)
    assert result.ok and "cost_mismatch" not in _kinds(result)


def test_rows_inserted_while_disabled_are_coverage_gaps(engine: Engine) -> None:
    audit.enable(engine, backfill=False)
    _add_trace(engine)
    audit.disable(engine)
    _add_trace(engine)  # inserted in the disable window → never chained
    audit.enable(engine, backfill=False)  # do NOT backfill: the gap must show
    result = audit.verify_chain(engine)
    assert result.ok  # a gap is absence of evidence, not evidence of tampering
    assert result.coverage_gap_traces == 1
    assert "coverage_gap" in _kinds(result)


def test_tail_truncation_needs_an_anchor(engine: Engine) -> None:
    audit.enable(engine, backfill=False)
    for _ in range(3):
        _add_trace(engine)
    anchor = audit.export_anchor(engine)
    with engine.begin() as conn:  # truncate: drop the tail trace AND its entry
        conn.exec_driver_sql(
            "DELETE FROM audit_chain_entries WHERE seq=(SELECT MAX(seq) FROM audit_chain_entries)"
        )
        conn.exec_driver_sql(
            "DELETE FROM traces WHERE trace_id=(SELECT MAX(trace_id) FROM traces)"
        )
    plain = audit.verify_chain(engine)
    assert plain.ok  # the surviving prefix is a perfectly valid chain
    anchored = audit.verify_chain(engine, from_anchor=anchor)
    assert not anchored.ok
    assert anchored.first_break.kind == "anchor_mismatch"


def test_anchor_roundtrips_through_json(engine: Engine) -> None:
    audit.enable(engine, backfill=False)
    _add_trace(engine)
    anchor = audit.export_anchor(engine)
    restored = audit.ChainAnchor.from_json(anchor.to_json())
    assert restored == anchor
    assert audit.verify_chain(engine, from_anchor=restored).ok


def test_anchored_default_is_full_walk_plus_anchor_check(engine: Engine) -> None:
    """from_anchor ADDS the anchor check to a full walk by default — an
    anchored verify is strictly stronger than a plain one."""
    audit.enable(engine, backfill=False)
    early = _add_trace(engine)
    anchor = audit.export_anchor(engine)
    _add_trace(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"UPDATE traces SET model_id='forged' WHERE trace_id={early}")
    anchored = audit.verify_chain(engine, from_anchor=anchor)
    assert not anchored.ok  # pre-anchor tamper caught: default walks from genesis
    assert anchored.first_break.trace_id == early
    assert anchored.entries_checked == 2


def test_incremental_anchored_walk_is_blind_to_pre_anchor_tampering(engine: Engine) -> None:
    """incremental=True trusts (does not verify) pre-anchor content —
    documented trade-off for hash work proportional to new entries."""
    audit.enable(engine, backfill=False)
    early = _add_trace(engine)
    anchor = audit.export_anchor(engine)
    _add_trace(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"UPDATE traces SET model_id='forged' WHERE trace_id={early}")
    anchored = audit.verify_chain(engine, from_anchor=anchor, incremental=True)
    assert anchored.ok  # blind by design in incremental mode
    assert anchored.entries_checked == 1  # only the post-anchor entry
    full = audit.verify_chain(engine)
    assert not full.ok and full.first_break.trace_id == early


def test_incremental_anchored_walk_keeps_coverage_and_cost_checks_full(engine: Engine) -> None:
    """Pass 2 must stay complete in incremental mode: pre-anchor metadata is
    seeded (trusted, not verified) so gaps and cost evidence still surface."""
    gap = _add_trace(engine)  # pre-enable → never chained
    audit.enable(engine, backfill=False)
    chained = _add_trace(engine, cost=Decimal("0.001"))
    anchor = audit.export_anchor(engine)
    _add_trace(engine)
    with engine.begin() as conn:  # silent cost edit of the PRE-anchor chained row
        conn.exec_driver_sql(f"UPDATE traces SET cost_usd=0.777 WHERE trace_id={chained}")
    result = audit.verify_chain(engine, from_anchor=anchor, incremental=True)
    assert result.ok  # cost is WARN, gap is GAP — no BREAKs
    assert result.coverage_gap_traces == 1
    assert "coverage_gap" in _kinds(result)
    mismatches = [f for f in result.findings if f.kind == "cost_mismatch"]
    assert [f.trace_id for f in mismatches] == [chained]
    assert gap is not None


def test_trace_id_reuse_after_tombstoned_deletion_is_not_a_false_break(engine: Engine) -> None:
    """SQLite reuses rowids (the contract traces table has no
    sqlite_autoincrement): after the documented legal-deletion flow, a new row
    under the old trace_id is a different generation — the superseded entry
    must report deleted_with_record, not hash_mismatch/cost_mismatch."""
    audit.enable(engine, backfill=False)
    tid = _add_trace(engine, cost=Decimal("0.001"))
    audit.record_deletion(engine, trace_id=tid, reason="legal cleanup")
    with engine.begin() as conn:  # the sanctioned outside-the-guard deletion
        conn.exec_driver_sql(f"DELETE FROM traces WHERE trace_id={tid}")
    reused = _add_trace(engine, cost=Decimal("0.9"))  # SQLite reuses the id
    assert reused == tid
    result = audit.verify_chain(engine)
    assert result.ok, result.findings
    kinds = _kinds(result)
    assert "deleted_with_record" in kinds
    assert "hash_mismatch" not in kinds and "cost_mismatch" not in kinds
    assert result.coverage_gap_traces == 0  # the new generation is chained


def test_cost_check_not_gated_on_baseline(engine: Engine) -> None:
    """A trace with cost evidence but no write/backfill baseline (e.g. inserted
    in a disable window, then corrected) must still be cost-checked."""
    audit.enable(engine, backfill=False)
    audit.disable(engine)
    tid = _add_trace(engine, cost=Decimal("0.001"))  # never chained
    audit.enable(engine, backfill=False)
    audit.record_cost_event(
        engine, trace_id=tid, event_type="correction",
        old_value=Decimal("0.001"), new_value=Decimal("0.002"),
    )
    # evidence says 0.002 but the column still holds 0.001 → WARN even though
    # the trace has no baseline entry (it is also a coverage gap)
    result = audit.verify_chain(engine)
    assert result.ok
    assert any(f.kind == "cost_mismatch" and f.trace_id == tid for f in result.findings)
    assert result.coverage_gap_traces == 1


def test_cost_equal_handles_huge_and_messy_values() -> None:
    from traceguard.audit.verify import _cost_equal

    assert _cost_equal("1E+30", Decimal("1E+30"))  # quantize would overflow
    assert _cost_equal("0.5", Decimal("0.500000"))
    assert not _cost_equal("0.5", Decimal("0.6"))
    assert not _cost_equal("not-a-number", Decimal("1"))
    assert _cost_equal(None, None)
    assert not _cost_equal("1", None) and not _cost_equal(None, Decimal("1"))


def test_empty_chain_anchor_is_genesis(engine: Engine) -> None:
    audit.ensure_audit_tables(engine)
    anchor = audit.export_anchor(engine)
    assert anchor.seq == 0
    assert anchor.row_hash == audit.GENESIS_PREV_HASH
    assert anchor.entry_count == 0

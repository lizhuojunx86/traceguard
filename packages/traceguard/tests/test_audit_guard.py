"""ORM-layer append-only guard: what it blocks, what legally passes, and the
documented bypasses (which are the hash chain's job to detect, not the
guard's to prevent)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterator

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard import audit
from traceguard.audit.models import AuditChainEntry
from traceguard.store.models import Trace, make_engine


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:", create_all=True)
    yield eng
    audit.detach(eng)
    audit.set_strict(False)


@pytest.fixture
def enabled(engine: Engine) -> Engine:
    audit.enable(engine, backfill=False)
    return engine


def _add_trace(engine: Engine) -> int:
    with Session(engine) as sess:
        t = Trace(
            project="p", component="c", operation="llm_complete",
            input_hash="h" * 64, parse_status="success",
            invoked_at=datetime.now(timezone.utc), cost_usd=Decimal("0.001"),
        )
        sess.add(t)
        sess.commit()
        return t.trace_id


def test_orm_update_of_covered_field_is_blocked(enabled: Engine) -> None:
    trace_id = _add_trace(enabled)
    with Session(enabled) as sess:
        sess.get(Trace, trace_id).input_hash = "tampered"
        with pytest.raises(audit.AppendOnlyViolationError):
            sess.commit()
        sess.rollback()
    with Session(enabled) as sess:  # nothing landed
        assert sess.get(Trace, trace_id).input_hash == "h" * 64


def test_cost_usd_only_orm_update_passes(enabled: Engine) -> None:
    trace_id = _add_trace(enabled)
    with Session(enabled) as sess:
        sess.get(Trace, trace_id).cost_usd = Decimal("0.002")
        sess.commit()
    with Session(enabled) as sess:
        assert sess.get(Trace, trace_id).cost_usd == Decimal("0.002")


def test_mixed_update_including_cost_is_blocked(enabled: Engine) -> None:
    trace_id = _add_trace(enabled)
    with Session(enabled) as sess:
        row = sess.get(Trace, trace_id)
        row.cost_usd = Decimal("0.002")
        row.model_id = "sneaky-model"
        with pytest.raises(audit.AppendOnlyViolationError):
            sess.commit()
        sess.rollback()


def test_orm_delete_is_blocked(enabled: Engine) -> None:
    trace_id = _add_trace(enabled)
    with Session(enabled) as sess:
        sess.delete(sess.get(Trace, trace_id))
        with pytest.raises(audit.AppendOnlyViolationError):
            sess.commit()
        sess.rollback()


def test_bulk_dml_update_is_blocked(enabled: Engine) -> None:
    _add_trace(enabled)
    with Session(enabled) as sess:
        with pytest.raises(audit.AppendOnlyViolationError):
            sess.execute(update(Trace).values(input_hash="x"))
        sess.rollback()


def test_bulk_cost_only_update_passes_reprice_shape(enabled: Engine) -> None:
    # Exactly the reprice statement shape — must stay allowed unmodified.
    trace_id = _add_trace(enabled)
    with Session(enabled) as sess:
        sess.execute(
            update(Trace).where(Trace.trace_id == trace_id).values(cost_usd=Decimal("9"))
        )
        sess.commit()
    with Session(enabled) as sess:
        assert sess.get(Trace, trace_id).cost_usd == Decimal("9")


def test_bulk_by_pk_cost_only_update_passes(enabled: Engine) -> None:
    """Regression: the bulk-by-PK form passes PKs as row SELECTORS in the
    parameters; counting them as assigned columns falsely blocked a sanctioned
    cost-only write."""
    trace_id = _add_trace(enabled)
    with Session(enabled) as sess:
        sess.execute(update(Trace), [{"trace_id": trace_id, "cost_usd": Decimal("0.7")}])
        sess.commit()
    with Session(enabled) as sess:
        assert sess.get(Trace, trace_id).cost_usd == Decimal("0.7")


def test_bulk_by_pk_covered_field_still_blocked(enabled: Engine) -> None:
    trace_id = _add_trace(enabled)
    with Session(enabled) as sess:
        with pytest.raises(audit.AppendOnlyViolationError):
            sess.execute(update(Trace), [{"trace_id": trace_id, "model_id": "evil"}])
        sess.rollback()


def test_multibind_session_update_is_blocked(enabled: Engine) -> None:
    """Regression: the guard must resolve the bind FOR THE MAPPER — a
    multi-bind session's default bind can be a different (unattached) engine."""
    other = make_engine("sqlite:///:memory:", create_all=True)
    trace_id = _add_trace(enabled)
    with Session(other, binds={Trace: enabled}) as sess:
        with pytest.raises(audit.AppendOnlyViolationError):
            sess.execute(
                update(Trace).where(Trace.trace_id == trace_id).values(input_hash="evil")
            )
        sess.rollback()


def test_bulk_dml_delete_is_blocked(enabled: Engine) -> None:
    _add_trace(enabled)
    with Session(enabled) as sess:
        with pytest.raises(audit.AppendOnlyViolationError):
            sess.execute(delete(Trace))
        sess.rollback()


def test_core_engine_update_bypasses_guard_but_chain_detects(enabled: Engine) -> None:
    """Documented bypass: engine-level SQL never sees ORM events. The guard is
    anti-footgun, not an integrity boundary — detection is verify_chain's job."""
    trace_id = _add_trace(enabled)
    with enabled.begin() as conn:
        conn.exec_driver_sql(
            f"UPDATE traces SET input_summary='EVIL' WHERE trace_id={trace_id}"
        )
    result = audit.verify_chain(enabled)
    assert not result.ok and result.first_break.kind == "hash_mismatch"


def test_evidence_tables_are_orm_immutable(enabled: Engine) -> None:
    _add_trace(enabled)
    with Session(enabled) as sess:
        entry = sess.scalars(select(AuditChainEntry)).first()
        entry.entry_type = "backfill"
        with pytest.raises(audit.AppendOnlyViolationError):
            sess.commit()
        sess.rollback()
    with Session(enabled) as sess:
        entry = sess.scalars(select(AuditChainEntry)).first()
        sess.delete(entry)
        with pytest.raises(audit.AppendOnlyViolationError):
            sess.commit()
        sess.rollback()


def test_guard_inactive_without_attach(engine: Engine) -> None:
    trace_id = _add_trace(engine)
    with Session(engine) as sess:  # never attached → guard must not interfere
        sess.get(Trace, trace_id).input_hash = "fine"
        sess.commit()


def test_guard_lifts_after_disable(enabled: Engine) -> None:
    trace_id = _add_trace(enabled)
    audit.disable(enabled)
    with Session(enabled) as sess:
        sess.get(Trace, trace_id).input_summary = "edited while disabled"
        sess.commit()  # guard is off; the chain still flags it
    assert not audit.verify_chain(enabled).ok

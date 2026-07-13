"""Cost event ledger + the additive reprice integration (on_cost_write)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard import audit
from traceguard.audit.models import AuditChainEntry, AuditCostEvent
from traceguard.routing_audit.reprice import reprice_null_costs, rollback_reprice
from traceguard.store.models import Trace, make_engine

USAGE = {
    "input_tokens": 1000, "output_tokens": 2000,
    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
}


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'audit_reprice.db'}"


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    eng = make_engine(db_url, create_all=True)
    yield eng
    audit.detach(eng)
    audit.set_strict(False)


def _add_priceable_trace(engine: Engine) -> int:
    with Session(engine) as sess:
        t = Trace(
            project="p", component="c", operation="llm_complete",
            input_hash="h" * 64, parse_status="success",
            model_id="claude-sonnet-5",
            output_parsed={"usage": USAGE},
            invoked_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
            cost_usd=None,
        )
        sess.add(t)
        sess.commit()
        return t.trace_id


def test_record_cost_event_validates_event_type(engine: Engine) -> None:
    audit.enable(engine, backfill=False)
    with pytest.raises(ValueError):
        audit.record_cost_event(
            engine, trace_id=1, event_type="reconciliation",  # not a valid type
            old_value=None, new_value=Decimal("1"),
        )


def test_record_cost_event_requires_enabled(engine: Engine) -> None:
    with pytest.raises(audit.AuditNotEnabledError):
        audit.record_cost_event(
            engine, trace_id=1, event_type="correction",
            old_value=None, new_value=Decimal("1"),
        )


def test_record_cost_event_is_chained(engine: Engine) -> None:
    audit.enable(engine, backfill=False)
    tid = _add_priceable_trace(engine)
    event_id = audit.record_cost_event(
        engine, trace_id=tid, event_type="correction",
        old_value=None, new_value=Decimal("0.022"), reason="r", batch_id="b",
    )
    with Session(engine) as sess:
        event = sess.get(AuditCostEvent, event_id)
        assert (event.old_value, event.new_value, event.batch_id) == (None, "0.022", "b")
        entry = sess.scalars(
            select(AuditChainEntry).where(AuditChainEntry.entry_type == "cost_event")
        ).one()
        assert entry.event_id == event_id and entry.trace_id == tid
        assert entry.cost_at_event == "0.022"


def test_reprice_default_behavior_unchanged(db_url: str, engine: Engine) -> None:
    # No hook, audit never involved: exactly the pre-1.1.0 behavior — not even
    # the audit tables come into existence.
    _add_priceable_trace(engine)
    stats = reprice_null_costs(db_url, write=True)
    assert stats.written == 1
    with engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'audit_%'"
            )
        }
    assert tables == set()


def test_reprice_with_hook_records_chained_events(db_url: str, engine: Engine) -> None:
    audit.enable(engine, backfill=False)
    tid = _add_priceable_trace(engine)

    def on_cost_write(**kwargs: object) -> None:
        audit.record_cost_event(engine, **kwargs)  # type: ignore[arg-type]

    stats = reprice_null_costs(db_url, write=True, on_cost_write=on_cost_write)
    assert stats.written == 1

    with Session(engine) as sess:
        event = sess.scalars(select(AuditCostEvent)).one()
        assert event.trace_id == tid
        assert event.event_type == "deferred_first_write"
        assert event.old_value is None
        assert Decimal(event.new_value) == sess.get(Trace, tid).cost_usd
        assert event.batch_id == stats.batch_id

    # the repriced cost matches the chained evidence → verify is fully green
    result = audit.verify_chain(engine)
    assert result.ok and not [f for f in result.findings if f.kind == "cost_mismatch"]

    # rollback with the hook records the reverse event and stays green
    n = rollback_reprice(stats.batch_id, db_url, on_cost_write=on_cost_write)
    assert n == 1
    with Session(engine) as sess:
        events = list(sess.scalars(select(AuditCostEvent).order_by(AuditCostEvent.event_id)))
        assert [e.event_type for e in events] == ["deferred_first_write", "rollback"]
        assert events[1].new_value is None  # restored the original NULL
        assert sess.get(Trace, tid).cost_usd is None
    result = audit.verify_chain(engine)
    assert result.ok and not [f for f in result.findings if f.kind == "cost_mismatch"]


def test_reprice_audit_flag_preflights_before_any_write(db_url: str, engine: Engine) -> None:
    """Regression: hooks fire post-commit, so a not-enabled failure discovered
    mid-run would leave cost writes committed with no recorded events (and a
    rollback would have destroyed its reprice-log evidence). --audit must
    refuse up front."""
    from traceguard.routing_audit.reprice import main

    tid = _add_priceable_trace(engine)
    rc = main(["--db", db_url, "--write", "--audit"])
    assert rc == 2
    with Session(engine) as sess:  # nothing was written
        assert sess.get(Trace, tid).cost_usd is None


def test_reprice_without_hook_shows_cost_mismatch_warn(db_url: str, engine: Engine) -> None:
    """Reprice remains legal without the hook (Core UPDATE bypasses the guard by
    the same mechanism that exempts it) — but the unrecorded write surfaces as
    a WARN, nudging toward --audit."""
    audit.enable(engine, backfill=False)
    _add_priceable_trace(engine)
    reprice_null_costs(db_url, write=True)  # no on_cost_write
    result = audit.verify_chain(engine)
    assert result.ok  # WARN only — cost_usd is outside the hash envelope
    assert any(f.kind == "cost_mismatch" for f in result.findings)

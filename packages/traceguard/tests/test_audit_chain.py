"""Chain write path: activation scoping, atomicity, fail-open/strict, backfill."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard import audit
from traceguard.audit.models import AuditChainEntry
from traceguard.store.models import Trace, make_engine


def _add_trace(engine: Engine, *, cost: Decimal | None = None, output=None, project="p") -> int:
    with Session(engine) as sess:
        t = Trace(
            project=project,
            component="c",
            operation="llm_complete",
            input_hash="h" * 64,
            parse_status="success",
            output_parsed=output,
            invoked_at=datetime.now(timezone.utc),
            cost_usd=cost,
        )
        sess.add(t)
        sess.commit()
        return t.trace_id


def _entries(engine: Engine) -> list[AuditChainEntry]:
    with Session(engine) as sess:
        return list(sess.scalars(select(AuditChainEntry).order_by(AuditChainEntry.seq)))


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:", create_all=True)
    yield eng
    audit.detach(eng)
    audit.set_strict(False)


def test_unattached_engine_is_untouched(engine: Engine) -> None:
    # No enable/attach: inserts must neither chain nor fail, and no audit
    # tables may appear (import alone has zero side effects).
    _add_trace(engine)
    with engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "audit_chain_entries" not in tables


def test_enable_backfills_and_live_writes_chain(engine: Engine) -> None:
    pre = _add_trace(engine, cost=Decimal("0.5"))
    assert audit.enable(engine) == 1  # backfilled the pre-existing row

    live = _add_trace(engine, cost=Decimal("0.001"))
    entries = _entries(engine)
    assert [(e.entry_type, e.trace_id) for e in entries] == [
        ("backfill", pre),
        ("write", live),
    ]
    # genesis + linear linkage + cost snapshots
    assert entries[0].prev_hash == audit.GENESIS_PREV_HASH
    assert entries[1].prev_hash == entries[0].row_hash
    # backfill snapshots the DB-round-tripped value (Numeric(12,6) scale),
    # live writes snapshot the constructor value — numerically identical.
    assert Decimal(entries[0].cost_at_event) == Decimal("0.5")
    assert Decimal(entries[1].cost_at_event) == Decimal("0.001")
    assert all(e.algo_version == audit.ALGO_VERSION for e in entries)
    assert all(e.canon_status == "ok" for e in entries)


def test_enable_is_idempotent(engine: Engine) -> None:
    _add_trace(engine)
    assert audit.enable(engine) == 1
    assert audit.enable(engine) == 0  # nothing left to backfill, no duplicates
    assert len(_entries(engine)) == 1


def test_trace_rollback_leaves_no_entry(engine: Engine) -> None:
    audit.enable(engine, backfill=False)
    with Session(engine) as sess:
        sess.add(
            Trace(
                project="p", component="c", operation="parse", input_hash="h",
                parse_status="success", invoked_at=datetime.now(timezone.utc),
            )
        )
        sess.flush()  # after_insert fired, entry written on the same connection
        sess.rollback()  # host transaction aborts → entry must abort with it
    assert _entries(engine) == []
    assert _add_trace(engine) is not None  # chain continues cleanly afterwards
    assert len(_entries(engine)) == 1


def test_fail_open_broken_audit_table(engine: Engine) -> None:
    audit.enable(engine, backfill=False)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE audit_chain_entries")
    trace_id = _add_trace(engine)  # must not raise: fail-open
    with Session(engine) as sess:
        assert sess.get(Trace, trace_id) is not None  # host write survived


def test_strict_mode_fails_closed(engine: Engine) -> None:
    audit.enable(engine, backfill=False, strict=True)
    assert audit.is_strict()
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE audit_chain_entries")
    with pytest.raises(Exception):  # noqa: B017 - chain failure must propagate
        _add_trace(engine)
    with Session(engine) as sess:  # and the host insert must NOT have landed
        assert sess.scalar(select(Trace.trace_id).limit(1)) is None


def test_disable_stops_chaining_reenable_backfills_the_window(engine: Engine) -> None:
    audit.enable(engine, backfill=False)
    chained = _add_trace(engine)
    audit.disable(engine)
    window = _add_trace(engine)  # inserted while disabled → no entry
    assert {e.trace_id for e in _entries(engine)} == {chained}

    audit.enable(engine)  # re-enable backfills the uncovered window row
    entries = _entries(engine)
    assert {(e.entry_type, e.trace_id) for e in entries} == {
        ("write", chained),
        ("backfill", window),
    }


def test_canon_failure_is_fail_open_and_chained(engine: Engine) -> None:
    audit.enable(engine, backfill=False)
    # NaN passes SQLAlchemy's default JSON serializer but is rejected by the
    # canonical dumps (allow_nan=False) → marker entry, chain stays linear.
    trace_id = _add_trace(engine, output={"x": float("nan")})
    ok_id = _add_trace(engine)
    entries = _entries(engine)
    assert [e.canon_status for e in entries] == ["failed", "ok"]
    assert entries[0].canon_error is not None
    assert entries[0].trace_id == trace_id and entries[1].trace_id == ok_id
    assert entries[1].prev_hash == entries[0].row_hash
    # the marker hashes deterministically → verify stays green
    assert audit.verify_chain(engine).ok


def test_record_deletion_requires_enabled(engine: Engine) -> None:
    with pytest.raises(audit.AuditNotEnabledError):
        audit.record_deletion(engine, trace_id=1, reason="x")


def test_int_key_output_parsed_verifies_clean(engine: Engine) -> None:
    """Regression: non-str dict keys sort numerically pre-round-trip and
    lexicographically post-round-trip; without key coercion this row would
    verify as a permanent false hash_mismatch BREAK."""
    audit.enable(engine, backfill=False)
    _add_trace(engine, output={2: "a", 10: "b"})
    result = audit.verify_chain(engine)
    assert result.ok, result.findings
    assert _entries(engine)[0].canon_status == "ok"  # attested, not a canon failure


def test_unreadable_settings_fail_open_default_strict_raises(engine: Engine) -> None:
    """Regression: silently reading 'disabled' in strict mode would let strict
    chaining stop without a trace."""
    audit.enable(engine, backfill=False)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE audit_settings")
    trace_id = _add_trace(engine)  # default: fail-open, host write survives
    with Session(engine) as sess:
        assert sess.get(Trace, trace_id) is not None

    audit.set_strict(True)
    with pytest.raises(Exception):  # noqa: B017 - unreadable settings must propagate
        _add_trace(engine)


def test_enable_strict_warns_when_tracer_is_fail_open(engine: Engine, caplog) -> None:
    """strict chain + fail-open tracer silently loses trace AND evidence; the
    least enable() can do is say so loudly."""
    import logging

    with caplog.at_level(logging.WARNING, logger="traceguard.audit"):
        audit.enable(engine, backfill=False, strict=True)
    assert any("strict_persistence" in r.message for r in caplog.records)


def test_concurrent_first_enable_is_race_tolerant(tmp_path) -> None:
    """Two processes racing the FIRST enable(): the loser of the settings-row
    insert must fall back to an update, not blow up with IntegrityError."""
    import threading

    url = f"sqlite:///{tmp_path / 'race.db'}"
    engines = [make_engine(url) for _ in range(2)]
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def go(eng: Engine) -> None:
        try:
            barrier.wait()
            audit.enable(eng, backfill=False)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(e,)) for e in engines]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    for eng in engines:
        assert audit.is_enabled(eng)
        audit.detach(eng)


def test_is_enabled(engine: Engine) -> None:
    assert not audit.is_enabled(engine)  # tables don't even exist
    audit.enable(engine, backfill=False)
    assert audit.is_enabled(engine)
    audit.disable(engine)
    assert not audit.is_enabled(engine)


def test_chain_only_mode_keeps_append_only_off(engine: Engine) -> None:
    trace_id = _add_trace(engine)
    audit.enable(engine, append_only=False)
    with Session(engine) as sess:  # guard is off: ORM update passes...
        sess.get(Trace, trace_id).input_summary = "edited"
        sess.commit()
    result = audit.verify_chain(engine)  # ...but the chain still detects it
    assert not result.ok
    assert result.first_break.kind == "hash_mismatch"

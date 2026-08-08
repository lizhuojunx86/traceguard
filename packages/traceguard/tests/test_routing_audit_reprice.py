"""Tests for reprice (NULL cost_usd backfill). Synthetic fixtures only."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from traceguard.routing_audit.pricing import (
    PRICES,
    SONNET5_INTRO,
    SONNET5_STANDARD,
    compute_cost_usd,
    price_for,
)
from traceguard.routing_audit.reprice import reprice_null_costs, rollback_reprice
from traceguard.store.models import Trace, make_engine

USAGE = {
    "input_tokens": 1000, "output_tokens": 2000,
    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
}


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'reprice.db'}"


def _insert(engine, *, model_id, invoked_at, usage=USAGE, cost=None) -> int:
    with Session(engine) as sess:
        t = Trace(
            project="quant_alpha_v2", component="workflow-subagent", operation="llm_complete",
            input_hash="h" * 64, parse_status="success", model_id=model_id,
            output_parsed={"session_id": "s", "usage": usage},
            invoked_at=invoked_at, cost_usd=cost,
        )
        sess.add(t)
        sess.commit()
        return t.trace_id


def test_price_for_sonnet_eras() -> None:
    intro_day = datetime(2026, 7, 15, tzinfo=timezone.utc)
    std_day = datetime(2026, 9, 15, tzinfo=timezone.utc)
    assert price_for("claude-sonnet-5", intro_day) is SONNET5_INTRO
    assert price_for("claude-sonnet-5", std_day) is SONNET5_STANDARD
    # no invoked_at → base entry (intro)
    assert price_for("claude-sonnet-5", None) is SONNET5_INTRO
    # cost differs by era for the same usage
    c_intro = compute_cost_usd("claude-sonnet-5", USAGE, intro_day)
    c_std = compute_cost_usd("claude-sonnet-5", USAGE, std_day)
    assert c_std > c_intro
    # hand-check intro: (1000*2 + 2000*10)/1e6
    assert c_intro == (Decimal(1000) * Decimal(2) + Decimal(2000) * Decimal(10)) / Decimal(1_000_000)


def test_reprice_fills_null(db_url: str) -> None:
    engine = make_engine(db_url)
    tid = _insert(engine, model_id="claude-sonnet-5",
                  invoked_at=datetime(2026, 7, 15, tzinfo=timezone.utc))
    # a genuinely-unpriced row is left alone
    _insert(engine, model_id="unknown-model",
            invoked_at=datetime(2026, 7, 15, tzinfo=timezone.utc))

    dry = reprice_null_costs(db_url, write=False)
    assert dry.null_rows == 2 and dry.priced == 1 and dry.unpriced == 1
    with Session(engine) as sess:  # dry-run wrote nothing
        assert sess.get(Trace, tid).cost_usd is None

    stats = reprice_null_costs(db_url, write=True)
    assert stats.written == 1 and stats.by_model == {"claude-sonnet-5": 1}
    with Session(engine) as sess:
        assert sess.get(Trace, tid).cost_usd == compute_cost_usd(
            "claude-sonnet-5", USAGE, datetime(2026, 7, 15, tzinfo=timezone.utc)
        )


def test_reprice_idempotent_and_rollback(db_url: str) -> None:
    engine = make_engine(db_url)
    tid = _insert(engine, model_id="claude-sonnet-5",
                  invoked_at=datetime(2026, 7, 15, tzinfo=timezone.utc))
    first = reprice_null_costs(db_url, write=True)
    assert first.written == 1
    # second run finds nothing NULL → no double write
    second = reprice_null_costs(db_url, write=True)
    assert second.written == 0 and second.null_rows == 0

    n = rollback_reprice(first.batch_id, db_url)
    assert n == 1
    with Session(engine) as sess:
        assert sess.get(Trace, tid).cost_usd is None  # restored to NULL
    # after rollback it is repriceable again
    third = reprice_null_costs(db_url, write=False)
    assert third.priced == 1


def test_reprice_standard_era(db_url: str) -> None:
    engine = make_engine(db_url)
    tid = _insert(engine, model_id="claude-sonnet-5",
                  invoked_at=datetime(2026, 9, 15, tzinfo=timezone.utc))
    reprice_null_costs(db_url, write=True)
    with Session(engine) as sess:
        cost = sess.get(Trace, tid).cost_usd
    assert cost == compute_cost_usd(
        "claude-sonnet-5", USAGE, datetime(2026, 9, 15, tzinfo=timezone.utc)
    )
    # standard era: (1000*3 + 2000*15)/1e6
    assert cost == (Decimal(1000) * Decimal(3) + Decimal(2000) * Decimal(15)) / Decimal(1_000_000)


# ---------------------------------------------------------------------------
# recompute_costs — correcting rows that ALREADY hold a value.
#
# reprice_null_costs cannot do this: it filters cost_usd IS NULL and hard-codes
# old_cost_usd=None, because it only ever performs a deferred FIRST write.
# ---------------------------------------------------------------------------

FLAT_1H = {
    "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 10_000,
    "cache_creation_5m": 0, "cache_creation_1h": 10_000,
}


def test_recompute_rewrites_a_wrong_existing_cost(db_url: str) -> None:
    from traceguard.routing_audit.reprice import recompute_costs

    engine = make_engine(db_url)
    day = datetime(2026, 8, 1, tzinfo=timezone.utc)
    # A cost stored under the old all-5m rule (1.25x on the whole lot).
    p = PRICES["claude-opus-4-8"]
    wrong = (10_000 * p.input_per_mtok * p.cache_write_5m_mult) / Decimal(1_000_000)
    tid = _insert(engine, model_id="claude-opus-4-8", invoked_at=day,
                  usage=FLAT_1H, cost=wrong.quantize(Decimal("0.000001")))

    dry = recompute_costs(db_url)
    assert dry.changed == 1 and dry.written == 0
    assert dry.delta > 0, "dry-run must report a non-zero delta, not always 0"
    assert dry.old_total == wrong.quantize(Decimal("0.000001"))

    stats = recompute_costs(db_url, write=True)
    assert stats.changed == 1 and stats.written == 1

    right = (10_000 * p.input_per_mtok * p.cache_write_1h_mult) / Decimal(1_000_000)
    with Session(engine) as sess:
        assert sess.get(Trace, tid).cost_usd == right.quantize(Decimal("0.000001"))


def test_recompute_logs_the_real_old_value_and_rolls_back(db_url: str) -> None:
    """The whole point: old_cost_usd must be the prior value, not None."""
    from traceguard.routing_audit.models import RoutingAuditRepriceLog
    from traceguard.routing_audit.reprice import recompute_costs

    engine = make_engine(db_url)
    day = datetime(2026, 8, 1, tzinfo=timezone.utc)
    stale = Decimal("0.001000")
    tid = _insert(engine, model_id="claude-opus-4-8", invoked_at=day,
                  usage=FLAT_1H, cost=stale)

    stats = recompute_costs(db_url, write=True)
    with Session(engine) as sess:
        log = sess.query(RoutingAuditRepriceLog).filter_by(batch_id=stats.batch_id).one()
        assert log.old_cost_usd == stale, "a None here would make the change irreversible"
        assert log.new_cost_usd != stale

    restored = rollback_reprice(stats.batch_id, db_url)
    assert restored == 1
    with Session(engine) as sess:
        assert sess.get(Trace, tid).cost_usd == stale


def test_recompute_is_idempotent(db_url: str) -> None:
    from traceguard.routing_audit.reprice import recompute_costs

    engine = make_engine(db_url)
    day = datetime(2026, 8, 1, tzinfo=timezone.utc)
    _insert(engine, model_id="claude-opus-4-8", invoked_at=day, usage=FLAT_1H,
            cost=Decimal("0.001000"))
    recompute_costs(db_url, write=True)
    again = recompute_costs(db_url)
    assert again.changed == 0 and again.delta == Decimal("0")


def test_recompute_never_nulls_a_row_it_can_no_longer_price(db_url: str) -> None:
    """A regressed rule must not destroy a real number."""
    from traceguard.routing_audit.reprice import recompute_costs

    engine = make_engine(db_url)
    day = datetime(2026, 8, 1, tzinfo=timezone.utc)
    kept = Decimal("0.500000")
    tid = _insert(engine, model_id="model-with-no-price", invoked_at=day,
                  usage=FLAT_1H, cost=kept)

    stats = recompute_costs(db_url, write=True)
    assert stats.unpriceable == 1 and stats.written == 0
    with Session(engine) as sess:
        assert sess.get(Trace, tid).cost_usd == kept


def test_recompute_leaves_null_rows_to_the_other_path(db_url: str) -> None:
    from traceguard.routing_audit.reprice import recompute_costs

    engine = make_engine(db_url)
    day = datetime(2026, 8, 1, tzinfo=timezone.utc)
    _insert(engine, model_id="claude-opus-4-8", invoked_at=day, usage=FLAT_1H, cost=None)
    stats = recompute_costs(db_url, write=True)
    assert stats.scanned == 0 and stats.written == 0

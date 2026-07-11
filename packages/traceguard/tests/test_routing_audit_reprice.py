"""Tests for reprice (NULL cost_usd backfill). Synthetic fixtures only."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from traceguard.routing_audit.pricing import (
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

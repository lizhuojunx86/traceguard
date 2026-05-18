"""Tests for ORM round-trip (SPEC §3.1, §3.2)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.store.models import ModelRegistryEntry, Trace


def test_trace_insert_and_query(engine):
    now = datetime.now(timezone.utc)
    with Session(engine) as sess:
        row = Trace(
            project="demo",
            component="extractor",
            operation="llm_complete",
            input_hash="a" * 64,
            parse_status="success",
            feature_as_of=now,
            cost_usd=Decimal("0.001234"),
            output_parsed={"answer": 42},
        )
        sess.add(row)
        sess.commit()
        tid = row.trace_id

    with Session(engine) as sess:
        fetched = sess.get(Trace, tid)
        assert fetched is not None
        assert fetched.project == "demo"
        assert fetched.output_parsed == {"answer": 42}
        assert fetched.cost_usd == Decimal("0.001234")
        assert fetched.parse_status == "success"


def test_model_registry_roundtrip(engine):
    with Session(engine) as sess:
        entry = ModelRegistryEntry(
            model_id="claude-sonnet-4-5-20260101",
            model_family="anthropic",
            capability_class="general-llm",
            released_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            available_to_us_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        sess.add(entry)
        sess.commit()

    with Session(engine) as sess:
        rows = sess.scalars(select(ModelRegistryEntry)).all()
        assert len(rows) == 1
        assert rows[0].capability_class == "general-llm"

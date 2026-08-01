"""usage_report: the six-field usage-drift-log record (clauderank spec).

Covers: field shape, human-message semantics (sidechain / tool_result /
month boundary), recursive corpus count, month-to-date cost from the traces
store, append-only history, and the drop-warning discriminator.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from traceguard.routing_audit.usage_report import (
    build_record,
    drift_warning,
    emit,
    read_prior,
)
from traceguard.store.models import Trace, make_engine

NOW = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)
LOCAL_NOW = NOW.astimezone()
MONTH = LOCAL_NOW.strftime("%Y-%m")


def _user_line(ts: datetime, content, sidechain: bool = False) -> str:
    rec = {
        "type": "user",
        "uuid": "u",
        "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "message": {"role": "user", "content": content},
    }
    if sidechain:
        rec["isSidechain"] = True
    return json.dumps(rec)


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    """Two main transcripts + one subagent transcript, mixed record kinds."""
    projects = tmp_path / "projects"
    slug = projects / "-Users-dev-alpha"
    slug.mkdir(parents=True)

    in_month = LOCAL_NOW - timedelta(hours=2)
    prev_month = (LOCAL_NOW.replace(day=1) - timedelta(days=3)).replace(hour=12)

    main = slug / "sess-1.jsonl"
    main.write_text(
        "\n".join(
            [
                _user_line(in_month, "real human prompt"),           # counts
                _user_line(in_month, [{"type": "text", "text": "hi"}]),  # counts
                _user_line(
                    in_month,
                    [{"type": "tool_result", "tool_use_id": "t", "content": "out"}],
                ),                                                    # pure tool_result: no
                _user_line(prev_month, "last month"),                 # wrong month: no
                json.dumps({"type": "assistant", "message": {"role": "assistant"}}),
                "{not valid json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    sub = slug / "sess-1" / "subagents" / "agent-a.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_text(
        _user_line(in_month, "parent-agent prompt", sidechain=True) + "\n",
        encoding="utf-8",
    )
    return projects


@pytest.fixture()
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'traces.db'}"
    engine = make_engine(url)
    with Session(engine) as session:
        def trace(cost, at):
            return Trace(
                project="p", component="c", operation="llm_call",
                input_hash="h", parse_status="ok",
                cost_usd=cost, invoked_at=at,
            )

        session.add(trace(Decimal("1.25"), NOW - timedelta(hours=1)))       # this month
        session.add(trace(Decimal("2.00"), NOW - timedelta(minutes=5)))     # this month
        session.add(trace(None, NOW - timedelta(hours=2)))                  # unpriced: ignored
        session.add(trace(Decimal("99.0"), NOW - timedelta(days=40)))       # prior month
        session.commit()
    return url


def test_build_record_shape_and_semantics(corpus: Path, db_url: str) -> None:
    record = build_record(corpus, db_url, now=NOW)

    assert set(record) == {"at", "month", "cost_usd", "messages", "corpus"}
    assert record["month"] == MONTH
    # `at` keeps a UTC offset (never a bare timestamp)
    assert datetime.fromisoformat(record["at"]).tzinfo is not None
    # two human turns count; tool_result-only, sidechain and last-month do not
    assert record["messages"] == 2
    # recursive: both main files' records live in 2 files (main + subagent)
    assert record["corpus"]["files"] == 2
    assert record["corpus"]["bytes"] > 0
    # month-to-date cost: 1.25 + 2.00, unpriced and prior-month excluded
    assert record["cost_usd"] == pytest.approx(3.25)


def test_emit_appends_and_never_rewrites(corpus: Path, db_url: str, tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    rec1, warn1 = emit(corpus, db_url, history_path=history, now=NOW)
    rec2, warn2 = emit(corpus, db_url, history_path=history, now=NOW + timedelta(hours=1))

    lines = history.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == rec1
    assert json.loads(lines[1]) == rec2
    # cumulative totals did not fall between the runs: no warning
    assert warn1 is None and warn2 is None


def test_drift_warning_discriminates_causes() -> None:
    current = {"cost_usd": 10.0, "corpus": {"files": 100, "bytes": 1}}

    # within the 2% band: silent
    assert drift_warning({"cost_usd": 10.1, "corpus": {"files": 100}}, current) is None
    # drop with fewer files: data removed
    w = drift_warning({"cost_usd": 11.0, "corpus": {"files": 120}}, current)
    assert w is not None and "removed" in w and "20 files" in w
    # drop with same files: rewritten in place
    w = drift_warning({"cost_usd": 11.0, "corpus": {"files": 100}}, current)
    assert w is not None and "rewritten in place" in w
    # no prior: silent
    assert drift_warning(None, current) is None


def test_read_prior_picks_latest_same_month(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    rows = [
        {"month": MONTH, "cost_usd": 1.0, "corpus": {"files": 1, "bytes": 1}},
        {"month": "1999-01", "cost_usd": 9.0, "corpus": {"files": 9, "bytes": 9}},
        {"month": MONTH, "cost_usd": 2.0, "corpus": {"files": 2, "bytes": 2}},
    ]
    history.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    prior = read_prior(history, MONTH)
    assert prior is not None and prior["cost_usd"] == 2.0
    assert read_prior(history, "1888-01") is None
    assert read_prior(tmp_path / "missing.jsonl", MONTH) is None


def test_warning_fires_across_emits(corpus: Path, db_url: str, tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    # a fabricated prior run for the same month with a much higher total
    prior = {
        "at": "x", "month": MONTH, "cost_usd": 50.0,
        "messages": 5, "corpus": {"files": 2, "bytes": 10},
    }
    history.write_text(json.dumps(prior) + "\n", encoding="utf-8")

    record, warning = emit(corpus, db_url, history_path=history, now=NOW)
    assert warning is not None and "rewritten in place" in warning
    # the warning did not stop the append
    assert len(history.read_text(encoding="utf-8").splitlines()) == 2
    assert record["cost_usd"] == pytest.approx(3.25)

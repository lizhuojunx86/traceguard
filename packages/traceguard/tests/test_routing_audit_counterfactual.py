"""Tests for the cost counterfactual engine (pure arithmetic, no API calls).

Synthetic fixtures only.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from test_routing_audit_ingest import USAGE_CACHED, _assistant_line
from traceguard.routing_audit.counterfactual import (
    CANDIDATE_PRICES,
    _is_substantive_consult,
    _price_tokens,
    _tokens_of,
    candidates_for,
    compute_counterfactuals,
    format_matrix,
    format_top,
    quality_candidates,
    token_factor,
)
from sqlalchemy import select

from traceguard.routing_audit.models import RoutingAuditTaskTag
from traceguard.routing_audit.ingest_claude_code import ingest
from traceguard.store.models import Trace, make_engine

CWD = "/Users/test/Desktop/APP/novel_project"
SESS = "dddd4444-0000-0000-0000-00000000000f"
T0 = "2026-06-05T10:00:00.000Z"


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'cf_test.db'}"


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    proj = root / "-Users-test-Desktop-APP-novel_project"
    proj.mkdir(parents=True)
    (proj / f"{SESS}.jsonl").write_text(
        "\n".join(
            [
                _user_prompt(SESS, T0, "你觉得该不该把主线换成更便宜的模型？给点建议"),
                _assistant_line(
                    session_id=SESS, message_id="mf", uuid="uf", ts="2026-06-05T10:01:00.000Z",
                    cwd=CWD, model="claude-fable-5", usage=USAGE_CACHED,
                ),
            ]
        ),
        encoding="utf-8",
    )
    return root


def _user_prompt(session_id: str, ts: str, text: str) -> str:
    import json

    return json.dumps(
        {
            "type": "user", "uuid": f"u-{ts}", "sessionId": session_id,
            "timestamp": ts, "cwd": CWD, "gitBranch": "main",
            "message": {"role": "user", "content": text},
        }
    )


def _tag(db_url: str, task_type: str = "decision-advisor") -> None:
    engine = make_engine(db_url)
    with Session(engine) as sess:
        sess.add(
            RoutingAuditTaskTag(
                unit_id=f"{SESS}#s01", session_id=SESS, project="novel_project",
                ts_start=datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc), ts_end=None,
                n_turns=1, task_type=task_type, source="heuristic", batch_id="t",
            )
        )
        sess.commit()


def test_token_factor() -> None:
    # newer → newer = 1:1
    assert token_factor("claude-opus-4-8", "claude-sonnet-5-intro") == Decimal(1)
    assert token_factor("claude-fable-5", "claude-opus-4-8") == Decimal(1)
    # newer → haiku (older) = ÷1.3
    assert token_factor("claude-fable-5", "claude-haiku-4-5-20251001") == Decimal(1) / Decimal("1.3")
    # haiku (older) → newer = ×1.3 (symmetric)
    assert token_factor("claude-haiku-4-5-20251001", "claude-sonnet-5-intro") == Decimal("1.3")


def test_candidates_for() -> None:
    # fable gets opus-4-8 added
    c = candidates_for("claude-fable-5")
    assert "claude-opus-4-8" in c
    assert "claude-sonnet-5-intro" in c and "claude-sonnet-5-standard" in c
    assert "claude-haiku-4-5-20251001" in c
    # a haiku unit excludes the haiku candidate (same base model)
    c2 = candidates_for("claude-haiku-4-5-20251001")
    assert "claude-haiku-4-5-20251001" not in c2
    # opus-4-8 unit does not re-list opus (only via fable path)
    assert "claude-opus-4-8" not in candidates_for("claude-opus-4-8")


def test_price_tokens_matches_hand_calc() -> None:
    # No cache/factor: sonnet-5 intro $2/$10.
    tokens = {"base_input": 1000, "cache_read": 0, "cache_5m": 0, "cache_1h": 0, "output": 2000}
    price = CANDIDATE_PRICES["claude-sonnet-5-intro"]
    expected = (Decimal(1000) * Decimal(2) + Decimal(2000) * Decimal(10)) / Decimal(1_000_000)
    assert _price_tokens(price, tokens, Decimal(1)) == expected.quantize(Decimal("0.000001"))


def test_tokens_of_cache_split() -> None:
    t = _tokens_of(
        {
            "input_tokens": 500, "output_tokens": 100, "cache_read_input_tokens": 10_000,
            "cache_creation_input_tokens": 3_000,
            "cache_creation_5m": 2_000, "cache_creation_1h": 1_000,
        }
    )
    assert t == {"base_input": 500, "cache_read": 10_000, "cache_5m": 2_000,
                 "cache_1h": 1_000, "output": 100}
    # fallback: no nested split → all creation is 5m
    t2 = _tokens_of({"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                     "cache_creation_input_tokens": 4_000})
    assert t2["cache_5m"] == 4_000 and t2["cache_1h"] == 0


def test_haiku_target_divides_tokens(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    _tag(db_url)
    rows = compute_counterfactuals(db_url)
    # find the fable→haiku row
    haiku = next(r for r in rows if r.candidate == "claude-haiku-4-5-20251001")
    sonnet = next(r for r in rows if r.candidate == "claude-sonnet-5-intro")
    # Recompute from the STORED usage (ingest splits cache into 5m/1h fields).
    engine = make_engine(db_url)
    with Session(engine) as sess:
        stored_usage = sess.scalars(select(Trace)).one().output_parsed["usage"]
    tokens = _tokens_of(stored_usage)
    price = CANDIDATE_PRICES["claude-haiku-4-5-20251001"]
    expected = _price_tokens(price, tokens, Decimal(1) / Decimal("1.3"))
    assert haiku.cf_cost == expected
    # sonnet-5 (newer tokenizer) uses factor 1 → strictly more tokens counted.
    sonnet_expected = _price_tokens(CANDIDATE_PRICES["claude-sonnet-5-intro"], tokens, Decimal(1))
    assert sonnet.cf_cost == sonnet_expected
    assert haiku.cf_cost < sonnet_expected  # ÷1.3 fewer tokens AND cheaper rates


def test_matrix_and_top_smoke(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    _tag(db_url)
    matrix = format_matrix(db_url)
    assert "IF QUALITY HOLDS" in matrix
    assert "decision-advisor" in matrix
    top = format_top(db_url, n=5)
    assert "IF QUALITY HOLDS" in top
    # every fable saving row present; savings are positive (fable is priciest)
    rows = [r for r in compute_counterfactuals(db_url) if r.saving > 0]
    assert rows  # fable → cheaper candidates all save


def test_quality_candidates_filters(source_root: Path, db_url: str, tmp_path: Path) -> None:
    ingest(source_root, db_url, write=True)
    _tag(db_url)
    rows = quality_candidates(db_url, source_root, limit=15)
    assert len(rows) == 1
    r = rows[0]
    assert r["unit_id"] == f"{SESS}#s01"
    assert r["current_model"] == "claude-fable-5"
    assert "该不该" in r["summary"]

    # CSV export path works.
    csv_path = tmp_path / "cand.csv"
    from traceguard.routing_audit.counterfactual import format_candidates

    format_candidates(rows, csv_path)
    got = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert got[0]["unit_id"] == f"{SESS}#s01"


def test_is_substantive_consult() -> None:
    assert _is_substantive_consult("你觉得该不该换模型，给点建议")
    assert not _is_substantive_consult("请继续")
    assert not _is_substantive_consult("同意")
    assert not _is_substantive_consult("同意你的建议，请依次处理。")
    assert not _is_substantive_consult("<task-notification>\n<task-id>abc</task-id>")
    assert not _is_substantive_consult("好")
    assert not _is_substantive_consult("开始 Phase 3")
    assert _is_substantive_consult("请制定一个对齐和上线的方案")


# ---------------------------------------------------------------------------
# as_of coercion — the SQLite NUMERIC-affinity silent-empty trap.
#
# traces.invoked_at is declared DATETIME → NUMERIC affinity in SQLite. A bound
# str that converts to a number ("20260705") is coerced to an INTEGER by that
# affinity, and SQLite sorts every TEXT value above every INTEGER, so
# `invoked_at <= 20260705` is false for every row. Empty result, no error.
#
# "2026-06-06" survives today only because it is NOT numeric-convertible, so it
# falls through to a text comparison — the CLI is safe by accident of format,
# not by design. These tests pin the coercion that makes it safe on purpose.
# ---------------------------------------------------------------------------


def test_as_of_numeric_string_does_not_silently_return_empty(
    source_root: Path, db_url: str
) -> None:
    """The trap itself: a numeric-convertible date string must still match rows."""
    from traceguard.routing_audit.counterfactual import aggregate_unit_models

    ingest(source_root, db_url, write=True)
    _tag(db_url)

    baseline = list(aggregate_unit_models(db_url))
    assert baseline, "fixture must produce at least one aggregate for this test to mean anything"

    # 20260606 IS convertible to an integer — the exact shape that silently
    # matches nothing when passed straight through to SQL.
    coerced = list(aggregate_unit_models(db_url, as_of="20260606"))
    assert coerced, (
        "as_of='20260606' returned an empty set — the SQLite affinity trap is back; "
        "a numeric-looking freeze point must not silently exclude every row"
    )
    assert len(coerced) == len(baseline)


def test_as_of_string_and_datetime_agree(source_root: Path, db_url: str) -> None:
    from traceguard.routing_audit.counterfactual import aggregate_unit_models, parse_as_of

    ingest(source_root, db_url, write=True)
    _tag(db_url)

    as_str = list(aggregate_unit_models(db_url, as_of="2026-06-06"))
    as_dt = list(aggregate_unit_models(db_url, as_of=parse_as_of("2026-06-06")))
    assert len(as_str) == len(as_dt) == 1


def test_as_of_before_the_data_excludes_everything(source_root: Path, db_url: str) -> None:
    """The other direction: a genuine empty result must still be reachable.

    Without this, a coercion bug that made every as_of match everything would
    pass the trap test above.
    """
    from traceguard.routing_audit.counterfactual import aggregate_unit_models

    ingest(source_root, db_url, write=True)
    _tag(db_url)

    assert list(aggregate_unit_models(db_url, as_of="20260604")) == []
    assert list(aggregate_unit_models(db_url, as_of=datetime(2026, 6, 4, tzinfo=timezone.utc))) == []


def test_as_of_rejects_junk_instead_of_matching_nothing(source_root: Path, db_url: str) -> None:
    from traceguard.routing_audit.counterfactual import aggregate_unit_models

    ingest(source_root, db_url, write=True)
    with pytest.raises(ValueError, match="as-of"):
        list(aggregate_unit_models(db_url, as_of="not-a-date"))
    with pytest.raises(TypeError):
        list(aggregate_unit_models(db_url, as_of=20260606))


def test_blind_premium_accepts_a_string_as_of(source_root: Path, db_url: str) -> None:
    """blind.py reaches the same comparison through compute_counterfactuals."""
    from traceguard.routing_audit.blind import intra_tier_premium

    ingest(source_root, db_url, write=True)
    _tag(db_url)
    intra_tier_premium(db_url, as_of="20260606")  # must not raise, must not be silently empty-by-trap

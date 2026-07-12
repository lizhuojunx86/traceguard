"""Tests for the rerun harness (dry-run only; no API calls)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_routing_audit_ingest import USAGE_CACHED, _assistant_line
from traceguard.routing_audit.ingest_claude_code import ingest
from traceguard.routing_audit.models import RerunResult, RoutingAuditTaskTag
from traceguard.routing_audit.rerun import (
    extract_consult,
    main as rerun_main,
    plan_reruns,
    select_candidates,
)
from traceguard.store.models import make_engine

CWD = "/Users/test/Desktop/APP/novel_project"
SESS = "ffff6666-0000-0000-0000-000000000abc"
T_PROMPT = "2026-06-05T10:00:00.000Z"
T_ANSWER = "2026-06-05T10:01:00.000Z"
PROMPT = "你觉得该不该把主线换成更便宜的模型？请给出完整建议和理由。"


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'rr.db'}"


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    proj = root / "-Users-test-Desktop-APP-novel_project"
    proj.mkdir(parents=True)
    user_line = json.dumps(
        {
            "type": "user", "uuid": "up", "sessionId": SESS, "timestamp": T_PROMPT,
            "cwd": CWD, "gitBranch": "main", "message": {"role": "user", "content": PROMPT},
        }
    )
    (proj / f"{SESS}.jsonl").write_text(
        "\n".join(
            [
                user_line,
                _assistant_line(
                    session_id=SESS, message_id="mf", uuid="uf", ts=T_ANSWER, cwd=CWD,
                    model="claude-fable-5", usage=USAGE_CACHED,
                ),
            ]
        ),
        encoding="utf-8",
    )
    return root


def _tag(db_url: str) -> None:
    engine = make_engine(db_url)
    with Session(engine) as sess:
        sess.add(
            RoutingAuditTaskTag(
                unit_id=f"{SESS}#s01", session_id=SESS, project="novel_project",
                ts_start=datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc), ts_end=None,
                n_turns=1, task_type="decision-advisor", source="heuristic", batch_id="t",
            )
        )
        sess.commit()


def test_select_candidates_includes_fable_unit(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    _tag(db_url)
    cands = select_candidates(db_url, source_root)
    ids = {c.unit_id for c in cands}
    assert f"{SESS}#s01" in ids
    c = next(c for c in cands if c.unit_id == f"{SESS}#s01")
    assert c.source_model == "claude-fable-5"
    assert c.session_id == SESS


def test_extract_consult_reads_prompt_answer_usage(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    _tag(db_url)
    cand = next(c for c in select_candidates(db_url, source_root) if c.unit_id == f"{SESS}#s01")
    prompt, answer, usage = extract_consult(cand, source_root)
    assert prompt == PROMPT
    assert answer is not None  # the assistant text
    assert usage is not None and usage["input_tokens"] == USAGE_CACHED["input_tokens"]


def test_plan_dry_run_writes_estimates_no_api(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    _tag(db_url)
    stats = plan_reruns(db_url, source_root, target_model="claude-opus-4-8", max_cost=Decimal("30"))
    assert stats.candidates >= 1
    assert not stats.rejected
    engine = make_engine(db_url)
    with Session(engine) as sess:
        rr = sess.get(RerunResult, f"{SESS}#s01#claude-opus-4-8")
    assert rr is not None
    assert rr.status == "estimated"
    assert rr.est_cost_usd is not None and rr.est_cost_usd > 0
    assert rr.actual_cost_usd is None  # nothing executed
    assert rr.original_answer is not None  # local-only body captured
    assert rr.prompt_summary and "该不该" in rr.prompt_summary


def test_max_cost_rejects_batch(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    _tag(db_url)
    stats = plan_reruns(
        db_url, source_root, target_model="claude-opus-4-8", max_cost=Decimal("0.0000001")
    )
    assert stats.rejected is True
    engine = make_engine(db_url)
    with Session(engine) as sess:
        rr = sess.get(RerunResult, f"{SESS}#s01#claude-opus-4-8")
    assert rr.status == "skipped"  # nothing looks ready to execute


def test_execute_flag_rejects_over_cap(source_root: Path, db_url: str) -> None:
    # Supersedes the old test_execute_flag_refuses (which asserted the harness
    # ALWAYS refused --execute — that build no longer exists; execute is now
    # implemented). Invariant now: --execute rejects (rc 2) and makes NO API
    # call when the estimate exceeds --max-cost.
    ingest(source_root, db_url, write=True)
    _tag(db_url)
    rc = rerun_main(
        ["--execute", "--db", db_url, "--source", str(source_root),
         "--max-cost", "0.0000001"]
    )
    assert rc == 2


def test_rerun_id_upsert_idempotent(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    _tag(db_url)
    plan_reruns(db_url, source_root, max_cost=Decimal("30"))
    plan_reruns(db_url, source_root, max_cost=Decimal("30"))
    engine = make_engine(db_url)
    with Session(engine) as sess:
        rows = list(sess.scalars(select(RerunResult)))
    assert len({r.rerun_id for r in rows}) == len(rows)  # no dupes


def test_execute_with_mocked_caller_no_api(source_root: Path, db_url: str) -> None:
    from traceguard.routing_audit.models import RoutingAuditIngestLog
    from traceguard.routing_audit.rerun import execute_reruns
    from traceguard.store.models import Trace

    ingest(source_root, db_url, write=True)
    _tag(db_url)
    calls = []

    def fake_caller(prompt: str, model: str):
        calls.append((prompt, model))
        return "FAKE_OPUS_ANSWER", {"input_tokens": 1000, "output_tokens": 500}

    stats = execute_reruns(
        db_url, source_root, target_model="claude-opus-4-8",
        max_cost=Decimal("30"), caller=fake_caller,
    )
    assert not stats.rejected
    assert stats.executed >= 1 and len(calls) == stats.executed
    assert stats.actual_total > 0

    engine = make_engine(db_url)
    with Session(engine) as sess:
        rr = sess.get(RerunResult, f"{SESS}#s01#claude-opus-4-8")
        assert rr.status == "completed"
        assert rr.rerun_answer == "FAKE_OPUS_ANSWER"  # local-only body
        assert rr.tokens_in == 1000 and rr.tokens_out == 500
        # self-audit: a traceguard/rerun-harness trace per executed rerun
        harness = list(sess.scalars(select(Trace).where(Trace.component == "rerun-harness")))
        assert len(harness) == stats.executed
        assert all(t.project == "traceguard" for t in harness)
        logs = list(sess.scalars(
            select(RoutingAuditIngestLog).where(RoutingAuditIngestLog.batch_id == stats.batch_id)
        ))
        assert len(logs) == stats.executed


def test_execute_rejects_over_cap_no_call(source_root: Path, db_url: str) -> None:
    from traceguard.routing_audit.rerun import execute_reruns

    ingest(source_root, db_url, write=True)
    _tag(db_url)
    calls = []

    def fake_caller(prompt: str, model: str):
        calls.append(1)
        return "x", {"input_tokens": 1, "output_tokens": 1}

    stats = execute_reruns(db_url, source_root, max_cost=Decimal("0.0000001"), caller=fake_caller)
    assert stats.rejected is True
    assert len(calls) == 0 and stats.executed == 0  # rejected before any API call

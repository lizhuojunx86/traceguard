"""Tests for the blind eval loop, intra-tier premium, and report generator."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from test_routing_audit_ingest import USAGE_CACHED, USAGE_SIMPLE, _assistant_line
from traceguard.routing_audit.blind import (
    _fable_is_a,
    export_blind_sheet,
    format_blind_premium,
    import_blind,
    intra_tier_premium,
)
from traceguard.routing_audit.ingest_claude_code import ingest
from traceguard.routing_audit.models import BlindEval, RerunResult, RoutingAuditTaskTag
from traceguard.routing_audit.report import (
    build_alias_map,
    gather,
    generate_reports,
    refresh_chain,
    render,
)
from traceguard.routing_audit.routing_decisions import generate_decisions
from traceguard.store.models import make_engine

CWD_FABLE = "/Users/test/Desktop/APP/novel_project"
CWD_OTHER = "/Users/test/Desktop/APP/huadian"
SESS_F = "aaaa0000-0000-0000-0000-0000000fable"
SESS_H = "bbbb0000-0000-0000-0000-00000huadian"


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'br.db'}"


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    # fable session (decision-advisor)
    pf = root / "-Users-test-Desktop-APP-novel_project"
    pf.mkdir(parents=True)
    (pf / f"{SESS_F}.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "uuid": "u", "sessionId": SESS_F,
                            "timestamp": "2026-06-05T10:00:00.000Z", "cwd": CWD_FABLE,
                            "message": {"role": "user", "content": "该不该换模型，给建议"}}),
                _assistant_line(session_id=SESS_F, message_id="mf", uuid="uf",
                                ts="2026-06-05T10:01:00.000Z", cwd=CWD_FABLE,
                                model="claude-fable-5", usage=USAGE_CACHED),
            ]
        ),
        encoding="utf-8",
    )
    # huadian session on haiku (cheap tier)
    ph = root / "-Users-test-Desktop-APP-huadian"
    ph.mkdir(parents=True)
    (ph / f"{SESS_H}.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "uuid": "u2", "sessionId": SESS_H,
                            "timestamp": "2026-06-06T10:00:00.000Z", "cwd": CWD_OTHER,
                            "message": {"role": "user", "content": "调研一下竞品"}}),
                _assistant_line(session_id=SESS_H, message_id="mh", uuid="uh",
                                ts="2026-06-06T10:01:00.000Z", cwd=CWD_OTHER,
                                model="claude-haiku-4-5-20251001", usage=USAGE_SIMPLE),
            ]
        ),
        encoding="utf-8",
    )
    return root


def _tags(db_url: str) -> None:
    engine = make_engine(db_url)
    with Session(engine) as sess:
        sess.add_all([
            RoutingAuditTaskTag(
                unit_id=f"{SESS_F}#s01", session_id=SESS_F, project="novel_project",
                ts_start=datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc), ts_end=None,
                n_turns=1, task_type="decision-advisor", source="heuristic", batch_id="t"),
            RoutingAuditTaskTag(
                unit_id=f"{SESS_H}#s01", session_id=SESS_H, project="huadian",
                ts_start=datetime(2026, 6, 6, 9, 0, tzinfo=timezone.utc), ts_end=None,
                n_turns=1, task_type="research-explore", source="heuristic", batch_id="t"),
        ])
        sess.commit()


# ── intra-tier premium ──

def test_intra_tier_premium_50pct(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    _tags(db_url)
    rows = intra_tier_premium(db_url)
    assert any(p.task_type == "decision-advisor" for p in rows)
    p = next(p for p in rows if p.task_type == "decision-advisor")
    # opus is exactly half of fable → premium == opus_cf, i.e. 50%.
    assert p.premium == p.opus_cf
    assert p.fable_actual == p.premium * 2


# ── blind eval loop ──

def test_fable_is_a_deterministic() -> None:
    a = _fable_is_a("unit-x#opus")
    assert a == _fable_is_a("unit-x#opus")  # stable
    assert isinstance(a, bool)


def test_export_blind_empty_without_reruns(source_root: Path, db_url: str, tmp_path: Path) -> None:
    ingest(source_root, db_url, write=True)
    _tags(db_url)
    stats = export_blind_sheet(tmp_path / "blind.csv", db_url)
    assert stats.exported == 0  # no completed reruns yet


def test_blind_roundtrip_with_seeded_rerun(source_root: Path, db_url: str, tmp_path: Path) -> None:
    ingest(source_root, db_url, write=True)
    _tags(db_url)
    # Seed a completed rerun with both answer bodies (simulating a departed run).
    engine = make_engine(db_url)
    with Session(engine) as sess:
        sess.add(RerunResult(
            rerun_id="R1", batch_id="b", unit_id=f"{SESS_F}#s01", project="novel_project",
            task_type="decision-advisor", source_model="claude-fable-5",
            target_model="claude-opus-4-8", prompt_hash="h", prompt_summary="该不该换模型",
            est_cost_usd=Decimal("1"), original_answer="FABLE_ANS", rerun_answer="OPUS_ANS",
            status="completed"))
        sess.commit()

    csv_path = tmp_path / "blind.csv"
    stats = export_blind_sheet(csv_path, db_url)
    assert stats.exported == 1
    row = next(csv.DictReader(csv_path.open(encoding="utf-8")))
    # answers present but label-free; position map hidden in DB
    assert {row["answer_a"], row["answer_b"]} == {"FABLE_ANS", "OPUS_ANS"}
    assert row["question_summary"] == "该不该换模型"

    # Fill a verdict aligned so that FABLE wins, import, check unblinded premium.
    fable_a = _fable_is_a("R1")
    row["verdict"] = "a_better" if fable_a else "b_better"
    row["reason"] = "fable clearer"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=row.keys())
        w.writeheader()
        w.writerow(row)
    imp = import_blind(csv_path, db_url)
    assert imp.imported == 1
    with Session(engine) as sess:
        be = sess.get(BlindEval, "R1")
    assert be.verdict in ("a_better", "b_better") and be.reason == "fable clearer"

    report = format_blind_premium(db_url)
    assert "fable better: 1" in report


def test_blind_premium_pending_message(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    _tags(db_url)
    assert "[PENDING: blind-eval]" in format_blind_premium(db_url)


# ── report generator ──

def test_alias_map_stable_and_external(source_root: Path, db_url: str, tmp_path: Path) -> None:
    ingest(source_root, db_url, write=True)
    path = tmp_path / "alias.json"
    m1 = build_alias_map(db_url, path)
    m2 = build_alias_map(db_url, path)  # reload from file → identical
    assert m1 == m2
    assert all(v.startswith("Project-") for v in m1.values())


def test_render_has_sections_and_pending(source_root: Path, db_url: str, tmp_path: Path) -> None:
    ingest(source_root, db_url, write=True)
    _tags(db_url)
    generate_decisions(db_url, write=True)
    data = gather(db_url)
    alias = build_alias_map(db_url, tmp_path / "a.json")
    zh = render(data, lang="zh", audience="external", alias=alias)
    en = render(data, lang="en", audience="personal", alias=alias)
    for sec in ("§1", "§2", "§3", "§4", "§5", "§6"):
        assert sec in zh and sec in en
    assert "[PENDING: manual-tags]" in zh  # tags still heuristic
    assert "IF QUALITY HOLDS" in en
    assert "若质量不降" in zh
    # external aliases the project; personal keeps the real name
    assert "Project-" in zh
    assert "novel_project" in en


def test_generate_reports_writes_files(source_root: Path, db_url: str, tmp_path: Path) -> None:
    ingest(source_root, db_url, write=True)
    _tags(db_url)
    generate_decisions(db_url, write=True)
    paths = generate_reports(db_url, out_dir=tmp_path, alias_path=tmp_path / "a.json")
    assert {p.name for p in paths} == {"report_zh.md", "report_en.md"}
    assert all(p.exists() for p in paths)


def test_refresh_chain_idempotent(source_root: Path, db_url: str, tmp_path: Path) -> None:
    ingest(source_root, db_url, write=True)
    _tags(db_url)
    log1 = refresh_chain(db_url, out_dir=tmp_path)
    log2 = refresh_chain(db_url, out_dir=tmp_path)
    assert any("decisions generate" in s for s in log1)
    assert any("reports:" in s for s in log2)


def test_as_of_freezes_snapshot(source_root: Path, db_url: str) -> None:
    from traceguard.routing_audit.counterfactual import parse_as_of

    ingest(source_root, db_url, write=True)
    _tags(db_url)
    full = gather(db_url)
    # fable trace is 2026-06-05, huadian trace is 2026-06-06.
    frozen = gather(db_url, as_of=parse_as_of("2026-06-05"))
    assert full.total_traces == 2
    assert frozen.total_traces == 1  # only the 06-05 fable trace survives the freeze
    assert frozen.total_cost < full.total_cost


def test_parse_as_of_date_is_end_of_day() -> None:
    from traceguard.routing_audit.counterfactual import parse_as_of

    d = parse_as_of("2026-06-15")
    assert d is not None
    assert (d.hour, d.minute) == (23, 59)  # a bare date means through end of day
    assert parse_as_of(None) is None

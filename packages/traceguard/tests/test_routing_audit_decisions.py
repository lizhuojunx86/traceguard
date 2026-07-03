"""Tests for the routing_decisions policy-deviation audit.

Synthetic fixtures only. Reuses the assistant-line builder from the ingest
test to populate traces, and writes task_tags directly.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_routing_audit_ingest import USAGE_SIMPLE, _assistant_line
from traceguard.routing_audit.ingest_claude_code import ingest
from traceguard.routing_audit.models import RoutingAuditTaskTag, RoutingDecision
from traceguard.routing_audit.routing_decisions import (
    Policy,
    export_deviations_csv,
    format_report,
    generate_decisions,
    import_decisions_csv,
    load_policy,
)
from traceguard.store.models import make_engine

CWD = "/Users/test/Desktop/APP/huadian"
SESS = "cccc3333-0000-0000-0000-000000000009"
# main uses opus (frontier, on-policy); a workflow-subagent in the same window
# uses opus too (policy wants mid → deviation).
T0 = "2026-06-05T10:00:00.000Z"
T1 = "2026-06-05T10:05:00.000Z"


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'dec_test.db'}"


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    proj = root / "-Users-test-Desktop-APP-huadian"
    proj.mkdir(parents=True)
    (proj / f"{SESS}.jsonl").write_text(
        _assistant_line(
            session_id=SESS, message_id="m1", uuid="u1", ts=T0, cwd=CWD,
            model="claude-opus-4-8", usage=USAGE_SIMPLE,
        ),
        encoding="utf-8",
    )
    # workflow-subagent trace on opus inside the same session window.
    sub = proj / SESS / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-w1.jsonl").write_text(
        _assistant_line(
            session_id=SESS, message_id="m2", uuid="u2", ts=T1, cwd=CWD,
            model="claude-opus-4-8", usage=USAGE_SIMPLE,
        ),
        encoding="utf-8",
    )
    (sub / "agent-w1.meta.json").write_text(
        '{"agentType": "workflow-subagent"}', encoding="utf-8"
    )
    return root


def _tag_unit(db_url: str, task_type: str = "coding-implement") -> None:
    engine = make_engine(db_url)
    with Session(engine) as sess:
        sess.add(
            RoutingAuditTaskTag(
                unit_id=f"{SESS}#s01",
                session_id=SESS,
                project="huadian",
                ts_start=datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc),
                ts_end=None,
                n_turns=1,
                task_type=task_type,
                source="heuristic",
                batch_id="tag-test",
            )
        )
        sess.commit()


def test_policy_match_specificity() -> None:
    policy = load_policy()
    # main (no matching task_type rule) → frontier
    tier, model = policy.match("huadian", "main", "coding-implement")
    assert tier == "frontier"
    # workflow-subagent → mid
    tier, _ = policy.match("huadian", "workflow-subagent", "coding-implement")
    assert tier == "mid"
    # Explore → cheap (component rule)
    tier, _ = policy.match("q", "Explore", "coding-implement")
    assert tier == "cheap"
    # tie-break: component=main and task_type=decision-advisor both spec=1;
    # decision-advisor appears earlier → frontier/fable either way.
    tier, model = policy.match("q", "main", "decision-advisor")
    assert tier == "frontier"


def test_policy_tier_of() -> None:
    policy = load_policy()
    assert policy.tier_of("claude-opus-4-8") == "frontier"
    assert policy.tier_of("claude-fable-5") == "frontier"
    assert policy.tier_of("claude-sonnet-5") == "mid"
    assert policy.tier_of("claude-haiku-4-5-20251001") == "cheap"
    assert policy.tier_of("unknown-model") == "unknown"
    assert policy.tier_of(None) == "unknown"


def test_generate_deviation_and_onpolicy(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    _tag_unit(db_url)
    stats = generate_decisions(db_url, write=True)
    assert stats.decisions == 2  # (unit, main) + (unit, workflow-subagent)
    assert stats.inserted == 2

    engine = make_engine(db_url)
    with Session(engine) as sess:
        by_id = {d.decision_id: d for d in sess.scalars(select(RoutingDecision))}
    main = by_id[f"{SESS}#s01#main"]
    sub = by_id[f"{SESS}#s01#workflow-subagent"]
    # main on opus = frontier expected → on-policy.
    assert main.actual_model == "claude-opus-4-8"
    assert main.expected_tier == "frontier" and not main.deviation
    # workflow-subagent on opus but policy wants mid → deviation.
    assert sub.expected_tier == "mid" and sub.actual_tier == "frontier"
    assert sub.deviation is True
    assert stats.deviations == 1


def test_same_tier_substitution_not_deviation(source_root: Path, db_url: str) -> None:
    # Retag as decision-advisor: policy expects fable-5 (frontier). main runs
    # opus-4-8 (also frontier) → same tier → NOT a deviation.
    ingest(source_root, db_url, write=True)
    _tag_unit(db_url, task_type="decision-advisor")
    generate_decisions(db_url, write=True)
    engine = make_engine(db_url)
    with Session(engine) as sess:
        main = sess.get(RoutingDecision, f"{SESS}#s01#main")
    assert main.expected_model == "claude-fable-5"
    assert main.actual_model == "claude-opus-4-8"
    assert main.deviation is False  # opus and fable are both frontier


def test_export_import_manual_roundtrip(source_root: Path, db_url: str, tmp_path: Path) -> None:
    ingest(source_root, db_url, write=True)
    _tag_unit(db_url)
    generate_decisions(db_url, write=True)

    csv_path = tmp_path / "devs.csv"
    n = export_deviations_csv(csv_path, db_url)
    assert n == 1  # only the workflow-subagent deviation
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert rows[0]["decision_id"] == f"{SESS}#s01#workflow-subagent"
    assert rows[0]["reason"] == "" and rows[0]["outcome"] == "unknown"

    # Fill reason + outcome, re-import.
    rows[0]["reason"] = "intentional: heavy fan-out needed frontier"
    rows[0]["outcome"] = "adopted"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    stats = import_decisions_csv(csv_path, db_url)
    assert stats.updated == 1

    engine = make_engine(db_url)
    with Session(engine) as sess:
        d = sess.get(RoutingDecision, f"{SESS}#s01#workflow-subagent")
    assert d.source == "manual" and d.outcome == "adopted"
    assert d.reason.startswith("intentional")

    # Regeneration must not overwrite the manual row.
    stats2 = generate_decisions(db_url, write=True)
    assert stats2.manual_kept == 1
    with Session(engine) as sess:
        d = sess.get(RoutingDecision, f"{SESS}#s01#workflow-subagent")
    assert d.source == "manual" and d.outcome == "adopted"


def test_import_rejects_bad_outcome(source_root: Path, db_url: str, tmp_path: Path) -> None:
    ingest(source_root, db_url, write=True)
    _tag_unit(db_url)
    generate_decisions(db_url, write=True)
    csv_path = tmp_path / "bad.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["decision_id", "reason", "outcome"])
        w.writeheader()
        w.writerow({"decision_id": f"{SESS}#s01#workflow-subagent", "reason": "x", "outcome": "bogus"})
    stats = import_decisions_csv(csv_path, db_url)
    assert stats.updated == 0


def test_report_smoke(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    _tag_unit(db_url)
    generate_decisions(db_url, write=True)
    report = format_report(db_url)
    assert "deviation rate by task_type" in report
    assert "coding-implement" in report


def test_dry_run_writes_nothing(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    _tag_unit(db_url)
    stats = generate_decisions(db_url, write=False)
    assert stats.decisions == 2 and stats.inserted == 0
    engine = make_engine(db_url)
    with Session(engine) as sess:
        assert sess.scalar(select(RoutingDecision).limit(1)) is None


def test_policy_default_and_unknown_via_custom_yaml(tmp_path: Path) -> None:
    p = tmp_path / "pol.yaml"
    p.write_text(
        "tiers:\n  cheap: [m-cheap]\ndefault_tier: cheap\nrules:\n"
        "  - project: special\n    component: main\n    expected_tier: cheap\n",
        encoding="utf-8",
    )
    policy = load_policy(p)
    assert isinstance(policy, Policy)
    # specific rule matches
    assert policy.match("special", "main", "x") == ("cheap", None)
    # no rule → default
    assert policy.match("other", "main", "x") == ("cheap", None)
    assert Decimal("0") == Decimal("0")  # keep Decimal import meaningful

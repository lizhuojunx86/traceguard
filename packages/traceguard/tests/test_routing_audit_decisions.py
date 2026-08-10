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
    # main (no matching task_type rule) → frontier, via a real rule
    tier, model, rule_idx = policy.match("huadian", "main", "coding-implement")
    assert tier == "frontier" and rule_idx is not None
    # workflow-subagent → mid
    tier, _, _ = policy.match("huadian", "workflow-subagent", "coding-implement")
    assert tier == "mid"
    # Explore → cheap (component rule)
    tier, _, _ = policy.match("q", "Explore", "coding-implement")
    assert tier == "cheap"
    # tie-break: component=main and task_type=decision-advisor both spec=1;
    # decision-advisor appears earlier → frontier/fable either way.
    tier, model, _ = policy.match("q", "main", "decision-advisor")
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

    # Regeneration must not overwrite the HUMAN columns of a manual row.
    # It used to skip the row entirely (manual_kept), which also froze the
    # derived half — see test_manual_rows_get_their_derived_columns_refreshed.
    stats2 = generate_decisions(db_url, write=True)
    assert stats2.manual_refreshed == 1
    with Session(engine) as sess:
        d = sess.get(RoutingDecision, f"{SESS}#s01#workflow-subagent")
    assert d.source == "manual" and d.outcome == "adopted"
    assert d.reason.startswith("intentional")


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
    assert stats.decisions == 2
    # `inserted` used to be 0 here for the wrong reason: dry-run returned early
    # before ever looking at the existing rows, so the counters a dry-run exists
    # to show were structurally always 0. They now PREVIEW the write. This
    # test's subject — that nothing is persisted — is the assertion below, and
    # is unchanged. Preview coverage: test_dry_run_previews_what_a_write_changes.
    assert stats.inserted == 2
    engine = make_engine(db_url)
    with Session(engine) as sess:
        assert sess.scalar(select(RoutingDecision).limit(1)) is None


def test_policy_no_rule_returns_none_not_default(tmp_path: Path) -> None:
    """No applicable rule → (None, None, None), never default_tier.

    The fall-through default fabricated verdicts in both directions (a
    compliant product-manager and a deviant claude-code-guide, each on a rule
    nobody wrote). default_tier is still parsed — a MATCHED rule that omits
    expected_tier falls back to it — but it no longer stands in for a missing
    rule.
    """
    p = tmp_path / "pol.yaml"
    p.write_text(
        "tiers:\n  cheap: [m-cheap]\ndefault_tier: cheap\nrules:\n"
        "  - project: special\n    component: main\n    expected_tier: cheap\n"
        "  - project: tierless\n",
        encoding="utf-8",
    )
    policy = load_policy(p)
    assert isinstance(policy, Policy)
    # specific rule matches, and says which rule
    assert policy.match("special", "main", "x") == ("cheap", None, 0)
    # no rule → unresolved, NOT default
    assert policy.match("other", "main", "x") == (None, None, None)
    # a matched rule with no expected_tier still falls back to default_tier
    assert policy.match("tierless", "main", "x") == ("cheap", None, 1)
    assert Decimal("0") == Decimal("0")  # keep Decimal import meaningful


def test_dry_run_previews_what_a_write_changes(source_root: Path, db_url: str) -> None:
    """A dry-run whose counters cannot move is not a dry-run.

    inserted/updated/manual_refreshed were hard-zero because generate_decisions
    skipped the session entirely when write=False, so the operator could not see
    what a write would do without doing it.
    """
    ingest(source_root, db_url, write=True)
    _tag_unit(db_url)

    preview = generate_decisions(db_url, write=False)
    assert preview.inserted > 0 and preview.updated == 0

    written = generate_decisions(db_url, write=True)
    assert written.inserted == preview.inserted

    # Second pass: the same rows now exist, so a preview must say "update".
    second = generate_decisions(db_url, write=False)
    assert second.updated == written.inserted and second.inserted == 0


def test_dry_run_reports_manual_rows_it_would_keep(source_root: Path, db_url: str) -> None:
    """manual_refreshed must be visible BEFORE the write, not only after it."""
    ingest(source_root, db_url, write=True)
    _tag_unit(db_url)
    generate_decisions(db_url, write=True)

    engine = make_engine(db_url)
    with Session(engine) as sess:
        row = sess.scalars(select(RoutingDecision)).first()
        decision_id = row.decision_id
        row.source = "manual"
        row.reason = "hand-reviewed"
        sess.commit()

    preview = generate_decisions(db_url, write=False)
    assert preview.manual_refreshed == 1

    generate_decisions(db_url, write=True)
    with Session(engine) as sess:
        kept = sess.get(RoutingDecision, decision_id)
        assert kept.source == "manual" and kept.reason == "hand-reviewed"


# ---------------------------------------------------------------------------
# Column partition: human columns are never recomputed, derived columns always
# are. Before 2026-08-08 `generate` skipped manual rows wholesale, so a human
# annotation also froze expected_tier / actual_tier / deviation / cost_usd —
# the 96 annotated rows became permanently exempt from routing policy, and they
# were 60% of all deviations.
# ---------------------------------------------------------------------------


def test_routing_decisions_columns_are_exactly_partitioned() -> None:
    """Every column belongs to exactly one set; a new column must be classified.

    Needs no fixture and no data. Adding a column to RoutingDecision fails this
    until its author decides whether a human writes it or a machine derives it —
    the choice that, made by default, created the frozen-island bug.
    """
    from traceguard.routing_audit.routing_decisions import (
        DERIVED_COLUMNS,
        IDENTITY_COLUMNS,
        MANUAL_COLUMNS,
    )

    actual = {c.name for c in RoutingDecision.__table__.columns}
    declared = MANUAL_COLUMNS | DERIVED_COLUMNS | IDENTITY_COLUMNS

    assert not (actual - declared), (
        f"unclassified column(s) {sorted(actual - declared)}: add each to "
        f"MANUAL_COLUMNS (a human writes it, never recomputed) or "
        f"DERIVED_COLUMNS (recomputed on every generate) or IDENTITY_COLUMNS"
    )
    assert not (declared - actual), (
        f"declared column(s) that no longer exist: {sorted(declared - actual)}"
    )
    # Exactly one set each — no column may be both protected and recomputed.
    for a, b in (
        (MANUAL_COLUMNS, DERIVED_COLUMNS),
        (MANUAL_COLUMNS, IDENTITY_COLUMNS),
        (DERIVED_COLUMNS, IDENTITY_COLUMNS),
    ):
        assert not (a & b), f"column in two sets: {sorted(a & b)}"


def test_manual_rows_get_their_derived_columns_refreshed(
    source_root: Path, db_url: str, tmp_path: Path
) -> None:
    """A manual annotation must not exempt the row from the policy.

    This is the regression that matters: the row keeps reason/outcome/source and
    picks up the current verdict, cost and batch_id.
    """
    from traceguard.routing_audit.routing_decisions import MANUAL_COLUMNS

    ingest(source_root, db_url, write=True)
    _tag_unit(db_url)
    generate_decisions(db_url, write=True)

    engine = make_engine(db_url)
    with Session(engine) as sess:
        row = sess.scalars(select(RoutingDecision)).first()
        decision_id = row.decision_id
        # Annotate it, and corrupt every derived column so a refresh is visible.
        row.source = "manual"
        row.reason = "intentional — reviewed by hand"
        row.outcome = "adopted"
        row.deviation = not row.deviation
        row.cost_usd = Decimal("999.999999")
        row.expected_tier = "bogus-tier"
        row.actual_tier = "bogus-tier"
        row.n_traces = 12345
        row.batch_id = "stale-batch"
        sess.commit()
        before_created = row.created_at

    stats = generate_decisions(db_url, write=True)
    assert stats.manual_refreshed >= 1

    with Session(engine) as sess:
        after = sess.get(RoutingDecision, decision_id)
        # Human columns: untouched.
        assert after.source == "manual"
        assert after.reason == "intentional — reviewed by hand"
        assert after.outcome == "adopted"
        # Derived columns: recomputed.
        assert after.cost_usd != Decimal("999.999999")
        assert after.expected_tier != "bogus-tier"
        assert after.actual_tier != "bogus-tier"
        assert after.n_traces != 12345
        assert after.batch_id == stats.batch_id, "batch_id must say when the derived half was computed"
        # Identity: preserved.
        assert after.created_at == before_created
    assert MANUAL_COLUMNS == {"reason", "outcome", "source"}


def test_generate_never_writes_a_manual_column(source_root: Path, db_url: str) -> None:
    """Structural guard: the values dict generate builds must be derived-only."""
    from traceguard.routing_audit.routing_decisions import DERIVED_COLUMNS, MANUAL_COLUMNS

    ingest(source_root, db_url, write=True)
    _tag_unit(db_url)
    # generate_decisions asserts this internally; make the contract explicit
    # here too so the intent survives a refactor of that assert.
    assert not (DERIVED_COLUMNS & MANUAL_COLUMNS)
    generate_decisions(db_url, write=True)


# ---------------------------------------------------------------------------
# Derived-drift watchdog.
#
# Splitting the columns made recomputation REACH every row; it does not stop a
# derived table from diverging from its source some other way. The manual-row
# freeze only surfaced because a printed total ($1945.1544) disagreed with a
# stored one ($1945.2578) — a comparison that existed nowhere machine-readable.
# ---------------------------------------------------------------------------


def test_drift_watchdog_is_silent_on_a_freshly_generated_table(
    source_root: Path, db_url: str
) -> None:
    ingest(source_root, db_url, write=True)
    _tag_unit(db_url)
    generate_decisions(db_url, write=True)

    clean = generate_decisions(db_url, write=False)
    assert clean.drifted == {}
    assert not [w for w in clean.warnings if "derived_drift" in w]


def test_drift_watchdog_names_the_columns_that_diverged(
    source_root: Path, db_url: str
) -> None:
    ingest(source_root, db_url, write=True)
    _tag_unit(db_url)
    generate_decisions(db_url, write=True)

    engine = make_engine(db_url)
    with Session(engine) as sess:
        row = sess.scalars(select(RoutingDecision)).first()
        decision_id = row.decision_id
        row.cost_usd = (row.cost_usd or Decimal("0")) + Decimal("5")
        row.actual_tier = "tampered"
        sess.commit()

    stats = generate_decisions(db_url, write=False)
    assert decision_id in stats.drifted
    assert set(stats.drifted[decision_id]) == {"cost_usd", "actual_tier"}
    hit = [w for w in stats.warnings if "derived_drift" in w]
    assert hit and "cost_usd" in hit[0] and "actual_tier" in hit[0]


def test_drift_watchdog_goes_quiet_once_corrected(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    _tag_unit(db_url)
    generate_decisions(db_url, write=True)

    engine = make_engine(db_url)
    with Session(engine) as sess:
        row = sess.scalars(select(RoutingDecision)).first()
        row.actual_tier = "tampered"
        sess.commit()

    assert generate_decisions(db_url, write=True).drifted
    assert generate_decisions(db_url, write=False).drifted == {}


def test_drift_watchdog_ignores_batch_id(source_root: Path, db_url: str) -> None:
    """batch_id changes every run by design; counting it would make drift constant."""
    ingest(source_root, db_url, write=True)
    _tag_unit(db_url)
    generate_decisions(db_url, write=True)
    stats = generate_decisions(db_url, write=False)
    assert all("batch_id" not in cols for cols in stats.drifted.values())


def test_drift_warning_uses_a_registered_kind(source_root: Path, db_url: str) -> None:
    from traceguard.routing_audit.ingest_claude_code import WARNING_KINDS

    ingest(source_root, db_url, write=True)
    _tag_unit(db_url)
    generate_decisions(db_url, write=True)
    engine = make_engine(db_url)
    with Session(engine) as sess:
        row = sess.scalars(select(RoutingDecision)).first()
        row.actual_tier = "tampered"
        sess.commit()

    for w in generate_decisions(db_url, write=False).warnings:
        assert w[1:].split("]", 1)[0] in WARNING_KINDS


# ---------------------------------------------------------------------------
# Unresolved verdicts + the two coverage counts.
#
# A fall-through default fabricates verdicts in both directions: on the real
# corpus it scored product-manager COMPLIANT and claude-code-guide DEVIANT,
# each on a rule nobody wrote. And an actual model in no tier compared
# "unknown" against an expected tier and produced a deviation the same way
# (the claude-opus-5 fortnight). Whenever a verdict cannot be resolved, the
# honest output is a third state — and the summary carries its two duals:
# decisions no rule reached, rules no decision reached.
# ---------------------------------------------------------------------------


def _tree_with_subagent(tmp_path: Path, agent_type: str, model: str) -> Path:
    """A main-thread opus trace plus one subagent trace of the given type/model."""
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
    sub = proj / SESS / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-x1.jsonl").write_text(
        _assistant_line(
            session_id=SESS, message_id="m2", uuid="u2", ts=T1, cwd=CWD,
            model=model, usage=USAGE_SIMPLE,
        ),
        encoding="utf-8",
    )
    (sub / "agent-x1.meta.json").write_text(
        f'{{"agentType": "{agent_type}"}}', encoding="utf-8"
    )
    return root


def test_no_rule_frontier_is_unresolved_not_compliant(tmp_path: Path, db_url: str) -> None:
    """product-manager on Opus scored compliant off the default. No more."""
    root = _tree_with_subagent(tmp_path, "product-manager", "claude-opus-4-8")
    ingest(root, db_url, write=True)
    _tag_unit(db_url)
    stats = generate_decisions(db_url, write=True)

    engine = make_engine(db_url)
    with Session(engine) as sess:
        d = sess.get(RoutingDecision, f"{SESS}#s01#product-manager")
    assert d.verdict == "unresolved:no_rule"
    assert d.deviation is False  # sentinel only; verdict is authoritative
    assert d.expected_tier == "unresolved"
    assert stats.unresolved == 1 and stats.unresolved_no_rule == 1
    assert stats.deviations == 0


def test_no_rule_cheap_is_unresolved_not_deviation(tmp_path: Path, db_url: str) -> None:
    """claude-code-guide on Haiku scored DEVIANT off the default — a
    fabricated deviation is noise in the one table the audit asks you to
    trust. The routing was right; the policy was silent."""
    root = _tree_with_subagent(tmp_path, "claude-code-guide", "claude-haiku-4-5-20251001")
    ingest(root, db_url, write=True)
    _tag_unit(db_url)
    stats = generate_decisions(db_url, write=True)

    engine = make_engine(db_url)
    with Session(engine) as sess:
        d = sess.get(RoutingDecision, f"{SESS}#s01#claude-code-guide")
    assert d.verdict == "unresolved:no_rule"
    assert d.deviation is False
    assert stats.deviations == 0 and stats.unresolved_no_rule == 1


def test_unknown_model_is_unresolved(tmp_path: Path, db_url: str) -> None:
    """A rule names the expectation but the actual model is in no tier: the
    old compare ("unknown" != expected) manufactured a deviation."""
    root = _tree_with_subagent(tmp_path, "workflow-subagent", "claude-opus-9")
    ingest(root, db_url, write=True)
    _tag_unit(db_url)
    stats = generate_decisions(db_url, write=True)

    engine = make_engine(db_url)
    with Session(engine) as sess:
        d = sess.get(RoutingDecision, f"{SESS}#s01#workflow-subagent")
    assert d.verdict == "unresolved:unknown_model"
    assert d.expected_tier == "mid"  # the rule still names what was expected
    assert d.actual_tier == "unknown"
    assert d.deviation is False
    assert stats.unresolved_unknown_model == 1 and stats.deviations == 0


def test_unresolved_rows_stay_out_of_deviation_export(
    tmp_path: Path, db_url: str
) -> None:
    root = _tree_with_subagent(tmp_path, "claude-code-guide", "claude-haiku-4-5-20251001")
    ingest(root, db_url, write=True)
    _tag_unit(db_url)
    generate_decisions(db_url, write=True)
    n = export_deviations_csv(tmp_path / "devs.csv", db_url)
    assert n == 0  # unresolved is not a deviation and must not reach review


def test_resolved_rows_carry_resolved_verdicts(source_root: Path, db_url: str) -> None:
    ingest(source_root, db_url, write=True)
    _tag_unit(db_url)
    generate_decisions(db_url, write=True)
    engine = make_engine(db_url)
    with Session(engine) as sess:
        by_id = {d.decision_id: d for d in sess.scalars(select(RoutingDecision))}
    assert by_id[f"{SESS}#s01#main"].verdict == "compliant"
    assert by_id[f"{SESS}#s01#workflow-subagent"].verdict == "deviation"


def test_gen_stats_carry_the_two_coverage_counts(tmp_path: Path, db_url: str) -> None:
    from traceguard.routing_audit.routing_decisions import _format_gen_stats

    root = _tree_with_subagent(tmp_path, "product-manager", "claude-opus-4-8")
    ingest(root, db_url, write=True)
    _tag_unit(db_url)
    stats = generate_decisions(db_url, write=False)

    assert stats.unresolved == 1
    # On this two-row corpus only the main rule fires; every other rule is in
    # the never-matched list — including the research-explore task rule that
    # is shadowed on the real corpus too.
    assert stats.rules_never_matched
    assert any("research-explore" in r for r in stats.rules_never_matched)

    text = _format_gen_stats(stats, wrote=False)
    assert "out of coverage" in text
    assert "rules never matched" in text
    assert "unresolved 1" in text


def test_report_carries_verdicts_and_coverage(tmp_path: Path, db_url: str) -> None:
    root = _tree_with_subagent(tmp_path, "claude-code-guide", "claude-haiku-4-5-20251001")
    ingest(root, db_url, write=True)
    _tag_unit(db_url)
    generate_decisions(db_url, write=True)

    report = format_report(db_url)
    assert "unresolved 1" in report
    assert "coverage vs current policy" in report
    assert "never matched" in report
    # the unresolved row is not silently folded into "on-policy"
    assert "1 unresolved" in report

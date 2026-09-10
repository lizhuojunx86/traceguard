"""The drift report (A5) and its counting discipline.

The discipline is the point, not the arithmetic: which snapshots count as
observations, which sources enter the denominator, and that the rate never
appears without n and an interval. Each of those is a way the number could be
made to look reassuring, so each has a test.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from traceguard import sources
from traceguard.sources.drift import compute_drift, drift_to_dict
from traceguard.sources.models import SourceSnapshotRow
from traceguard.sources.stats import Z_95, wilson_interval

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def src_engine(engine):
    sources.enable(engine)
    return engine


def _add(engine, uri, *hashes, verdict="verified", start=T0, step_days=1, trace_id=1):
    """Append one retrieval per hash, one step apart."""
    with Session(engine) as sess:
        for i, h in enumerate(hashes):
            sess.add(
                SourceSnapshotRow(
                    trace_id=trace_id,
                    source_uri=uri,
                    source_kind="vendor_api",
                    content_hash=h,
                    retrieved_at=start + timedelta(days=i * step_days),
                    verdict=verdict,
                    strict=False,
                )
            )
        sess.commit()


# ── the three-source fixture the task specifies ─────────────────────────────

@pytest.fixture
def three_sources(src_engine):
    """One stable, one changed once, one changed twice."""
    _add(src_engine, "https://v.example/stable", "a" * 64, "a" * 64, "a" * 64)
    _add(src_engine, "https://v.example/once", "b" * 64, "c" * 64)
    _add(src_engine, "https://v.example/twice", "d" * 64, "e" * 64, "f" * 64)
    return src_engine


def test_counts_and_interval_on_the_three_source_fixture(three_sources):
    report = compute_drift(three_sources)
    assert report.snapshots_scanned == 8
    assert report.sources_total == 3
    assert report.sources_comparable == 3
    assert report.sources_drifted == 2
    assert report.total_changes == 3  # 0 + 1 + 2
    assert report.drift_rate == pytest.approx(2 / 3)

    low, high = report.drift_rate_ci
    assert (low, high) == pytest.approx(wilson_interval(2, 3))
    assert low < 2 / 3 < high  # the point estimate sits inside its interval
    assert 0.0 <= low and high <= 1.0


def test_per_source_change_counts(three_sources):
    by_uri = {s.source_uri: s for s in compute_drift(three_sources).sources}
    assert by_uri["https://v.example/stable"].changes == 0
    assert by_uri["https://v.example/stable"].distinct_hashes == 1
    assert not by_uri["https://v.example/stable"].drifted
    assert by_uri["https://v.example/once"].changes == 1
    assert by_uri["https://v.example/twice"].changes == 2
    assert by_uri["https://v.example/twice"].distinct_hashes == 3


def test_a_flip_back_counts_as_two_changes(src_engine):
    """A -> B -> A is two events, not one. `distinct_hashes - 1` would say one
    and hide the more alarming pattern."""
    _add(src_engine, "https://v.example/flip", "a" * 64, "b" * 64, "a" * 64)
    src = compute_drift(src_engine).sources[0]
    assert src.distinct_hashes == 2
    assert src.changes == 2
    assert src.drifted


# ── counting discipline ─────────────────────────────────────────────────────

def test_unchecked_snapshots_are_not_observations(src_engine):
    """They would dilute the rate toward zero exactly where nobody looked."""
    _add(src_engine, "https://v.example/x", "a" * 64, "b" * 64)
    _add(src_engine, "https://v.example/y", "c" * 64, "d" * 64, verdict="unchecked")

    report = compute_drift(src_engine)
    assert report.snapshots_scanned == 4
    assert report.snapshots_unchecked == 2
    assert report.sources_total == 1  # the unchecked source contributes nothing
    assert report.drift_rate == 1.0
    # The exclusion is named in the summary, never silently applied.
    assert "2 unchecked snapshot(s)" in report.summary()


def test_unverifiable_snapshots_ARE_observations(src_engine):
    """Drift is about the bytes. `unverifiable` means only the TIMING claim was
    unprovable — the retrieval happened and the digest is real."""
    _add(src_engine, "https://v.example/u", "a" * 64, "b" * 64, verdict="unverifiable")
    report = compute_drift(src_engine)
    assert report.snapshots_unchecked == 0
    assert report.sources_comparable == 1
    assert report.sources_drifted == 1


def test_single_observation_sources_are_excluded_and_named(src_engine):
    """One retrieval is not a comparison: such a source can neither have drifted
    nor be shown not to have. Folding it into the denominator is the standard
    way to manufacture a reassuringly small percentage."""
    _add(src_engine, "https://v.example/once-only", "a" * 64)
    _add(src_engine, "https://v.example/changed", "b" * 64, "c" * 64)

    report = compute_drift(src_engine)
    assert report.sources_total == 2
    assert report.sources_comparable == 1
    assert report.sources_single_observation == 1
    assert report.drift_rate == 1.0  # 1/1, NOT 1/2
    assert "retrieved only once" in report.summary()


def test_no_comparable_source_reports_none_not_zero(src_engine):
    """A rate over an empty denominator is not 0.0 — printing 0.0 would claim a
    measurement that was never made."""
    _add(src_engine, "https://v.example/a", "a" * 64)
    report = compute_drift(src_engine)
    assert report.drift_rate is None
    assert report.drift_rate_ci == (0.0, 0.0)
    assert "no comparable source yet" in report.summary()


def test_summary_never_states_a_rate_without_n_and_an_interval(three_sources):
    summary = compute_drift(three_sources).summary()
    assert "66.7%" in summary
    assert "2/3" in summary
    assert "Wilson 95% CI" in summary


# ── ordering, filters ───────────────────────────────────────────────────────

def test_sequence_is_ordered_by_retrieval_time_not_insertion(src_engine):
    with Session(src_engine) as sess:
        for h, day in (("b" * 64, 2), ("a" * 64, 1), ("c" * 64, 3)):
            sess.add(
                SourceSnapshotRow(
                    trace_id=1,
                    source_uri="https://v.example/z",
                    source_kind="http",
                    content_hash=h,
                    retrieved_at=T0 + timedelta(days=day),
                    verdict="verified",
                    strict=False,
                )
            )
        sess.commit()
    seq = [r.content_hash[0] for r in compute_drift(src_engine).sources[0].retrievals]
    assert seq == ["a", "b", "c"]


def test_ties_on_retrieved_at_fall_back_to_insertion_order(src_engine):
    """Two retrievals can share a timestamp at the DB's resolution; without the
    snapshot_id tiebreak the change count would depend on row-return order."""
    _add(src_engine, "https://v.example/tie", "a" * 64, "b" * 64, step_days=0)
    src = compute_drift(src_engine).sources[0]
    assert [r.content_hash[0] for r in src.retrievals] == ["a", "b"]
    assert src.changes == 1


def test_since_filter_narrows_the_window(three_sources):
    report = compute_drift(three_sources, since=T0 + timedelta(days=2))
    assert report.snapshots_scanned == 2  # only the third retrieval of each 3-row source
    assert report.sources_comparable == 0


def test_source_uri_pattern_filters(three_sources):
    report = compute_drift(three_sources, source_uri="%/twice")
    assert report.sources_total == 1
    assert report.sources[0].source_uri.endswith("/twice")


# ── Wilson, and its agreement with the reference implementation ─────────────

def test_wilson_matches_the_reference_implementation_digit_for_digit():
    """analysis/eps_revision.py stays the reference; this is a reimplementation
    (the SDK must not import a repo-root analysis script), so it has to agree."""
    import importlib.util
    from pathlib import Path

    ref_path = Path(__file__).resolve().parents[3] / "analysis" / "eps_revision.py"
    if not ref_path.is_file():
        pytest.skip("analysis/eps_revision.py not reachable from the package tree")
    spec = importlib.util.spec_from_file_location("_eps_ref", ref_path)
    ref = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ref)

    assert Z_95 == ref.Z_95
    for successes, n in ((0, 10), (1, 10), (3, 7), (2, 3), (99, 100), (0, 0), (5, 5)):
        assert wilson_interval(successes, n) == ref.wilson_interval(successes, n)


def test_wilson_stays_inside_the_unit_interval_at_the_extremes():
    """The reason Wilson was chosen over the normal approximation."""
    for successes, n in ((0, 3), (3, 3), (1, 2)):
        low, high = wilson_interval(successes, n)
        assert 0.0 <= low <= high <= 1.0


def test_wilson_with_no_observations_is_not_an_interval():
    assert wilson_interval(0, 0) == (0.0, 0.0)


# ── JSON output ─────────────────────────────────────────────────────────────

def test_json_keys_are_stable_and_ordered(three_sources):
    payload = drift_to_dict(compute_drift(three_sources))
    assert list(payload) == [
        "since",
        "source_uri_pattern",
        "snapshots_scanned",
        "snapshots_unchecked",
        "sources_total",
        "sources_comparable",
        "sources_single_observation",
        "sources_drifted",
        "total_changes",
        "drift_rate",
        "drift_rate_ci",
        "sources",
    ]
    assert list(payload["sources"][0]) == [
        "source_uri",
        "observations",
        "distinct_hashes",
        "changes",
        "comparable",
        "drifted",
        "first_seen",
        "last_seen",
        "sequence",
    ]
    json.dumps(payload)  # round-trips


def test_json_drift_rate_is_null_when_nothing_is_comparable(src_engine):
    _add(src_engine, "https://v.example/a", "a" * 64)
    payload = drift_to_dict(compute_drift(src_engine))
    assert payload["drift_rate"] is None
    assert payload["drift_rate_ci"] is None


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_drift_exits_1_when_a_source_changed(three_sources, tmp_path, capsys):
    from traceguard.sources.__main__ import main

    url = f"sqlite:///{tmp_path/'t.db'}"
    # Rebuild the fixture in a file-backed DB the CLI can open by URL.
    import traceguard

    eng = traceguard.make_engine(url)
    sources.enable(eng)
    _add(eng, "https://v.example/once", "b" * 64, "c" * 64)

    assert main(["--db", url, "drift"]) == 1
    out = capsys.readouterr().out
    assert "CHANGED" in out
    assert "Wilson 95% CI" in out


def test_cli_drift_exits_0_when_nothing_changed(tmp_path, capsys):
    from traceguard.sources.__main__ import main
    import traceguard

    url = f"sqlite:///{tmp_path/'t.db'}"
    eng = traceguard.make_engine(url)
    sources.enable(eng)
    _add(eng, "https://v.example/stable", "a" * 64, "a" * 64)

    assert main(["--db", url, "drift"]) == 0

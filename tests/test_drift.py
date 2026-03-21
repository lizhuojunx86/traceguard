"""Tests for drift detector."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from guardian.optimizer.drift_detector import (
    DriftResult,
    PipelineDriftReport,
    detect_drift,
)
from guardian.store.models import Base, EvalTrace
from guardian.store.reader import TraceReader


@pytest.fixture
def db_url():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    url = f"sqlite:///{path}"
    yield url
    os.unlink(path)


def _seed_traces(db_url: str, traces: list[dict]) -> None:
    """Seed database with trace records."""
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for t in traces:
            s.add(EvalTrace(**t))
        s.commit()


def _make_trace(
    pipeline: str = "pipe-a",
    step: str = "step_01",
    passed: bool = True,
    score: float = 0.9,
    days_ago: int = 0,
) -> dict:
    return {
        "pipeline_name": pipeline,
        "step_name": step,
        "action": "pass" if passed else "retry",
        "passed": passed,
        "score": score,
        "issues": "[]" if passed else '["issue"]',
        "attempt": 1,
        "created_at": datetime.now(timezone.utc) - timedelta(days=days_ago),
    }


class TestDriftResultDataclass:
    def test_defaults(self):
        r = DriftResult(pipeline_name="p", step_name="s")
        assert r.drifted is False
        assert r.trend == "stable"
        assert r.signals == []

    def test_drifted(self):
        r = DriftResult(
            pipeline_name="p",
            step_name="s",
            drifted=True,
            trend="degrading",
            signals=["score drop"],
        )
        assert r.drifted is True


class TestDetectDriftNoData:
    def test_empty_db(self, db_url):
        reader = TraceReader(db_url)
        report = detect_drift(reader, "nonexistent")
        assert report.has_drift is False
        assert "No traces" in report.summary

    def test_insufficient_data(self, db_url):
        _seed_traces(db_url, [_make_trace(days_ago=0)])
        reader = TraceReader(db_url)
        report = detect_drift(reader, "pipe-a", recent_days=1, baseline_days=7)
        # Only 1 day of data → insufficient
        assert any("Insufficient" in s for r in report.step_results for s in r.signals)


class TestDetectDriftStable:
    def test_stable_pipeline(self, db_url):
        """Consistent high scores → no drift."""
        traces = []
        for day in range(14):
            traces.append(_make_trace(score=0.9, passed=True, days_ago=day))
        _seed_traces(db_url, traces)

        reader = TraceReader(db_url)
        report = detect_drift(reader, "pipe-a", recent_days=3, baseline_days=14)
        assert report.has_drift is False
        assert report.step_results[0].trend == "stable"


class TestDetectDriftDegrading:
    def test_score_degradation(self, db_url):
        """High baseline, low recent → drift detected."""
        traces = []
        # Baseline: days 13..3 → high scores
        for day in range(3, 14):
            traces.append(_make_trace(score=0.95, passed=True, days_ago=day))
        # Recent: days 2..0 → low scores
        for day in range(3):
            traces.append(_make_trace(score=0.5, passed=False, days_ago=day))
        _seed_traces(db_url, traces)

        reader = TraceReader(db_url)
        report = detect_drift(reader, "pipe-a", recent_days=3, baseline_days=14)
        assert report.has_drift is True
        step_result = report.step_results[0]
        assert step_result.drifted is True
        assert step_result.trend == "degrading"
        assert any("dropped" in s.lower() for s in step_result.signals)

    def test_pass_rate_degradation(self, db_url):
        """Pass rate drops significantly."""
        traces = []
        # Baseline: all passing
        for day in range(3, 14):
            traces.append(_make_trace(score=0.9, passed=True, days_ago=day))
        # Recent: mostly failing
        for day in range(3):
            traces.append(_make_trace(score=0.8, passed=False, days_ago=day))
        _seed_traces(db_url, traces)

        reader = TraceReader(db_url)
        report = detect_drift(reader, "pipe-a", recent_days=3, baseline_days=14)
        assert report.has_drift is True
        assert any("pass rate" in s.lower() for r in report.step_results for s in r.signals)


class TestDetectDriftImproving:
    def test_improving_trend(self, db_url):
        """Low baseline, high recent → improving trend."""
        traces = []
        for day in range(3, 14):
            traces.append(_make_trace(score=0.5, passed=False, days_ago=day))
        for day in range(3):
            traces.append(_make_trace(score=0.95, passed=True, days_ago=day))
        _seed_traces(db_url, traces)

        reader = TraceReader(db_url)
        report = detect_drift(reader, "pipe-a", recent_days=3, baseline_days=14)
        step_result = report.step_results[0]
        assert step_result.trend == "improving"


class TestDetectDriftMultiStep:
    def test_multiple_steps(self, db_url):
        """Different steps can have different drift states."""
        traces = []
        # step_01: stable
        for day in range(14):
            traces.append(_make_trace(step="step_01", score=0.9, passed=True, days_ago=day))
        # step_02: degrading
        for day in range(3, 14):
            traces.append(_make_trace(step="step_02", score=0.9, passed=True, days_ago=day))
        for day in range(3):
            traces.append(_make_trace(step="step_02", score=0.4, passed=False, days_ago=day))
        _seed_traces(db_url, traces)

        reader = TraceReader(db_url)
        report = detect_drift(reader, "pipe-a", recent_days=3, baseline_days=14)

        s1 = next(r for r in report.step_results if r.step_name == "step_01")
        s2 = next(r for r in report.step_results if r.step_name == "step_02")
        assert s1.drifted is False
        assert s2.drifted is True
        assert report.has_drift is True
        assert "step_02" in report.summary


class TestPipelineDriftReport:
    def test_report_shape(self, db_url):
        traces = [_make_trace(days_ago=i) for i in range(14)]
        _seed_traces(db_url, traces)

        reader = TraceReader(db_url)
        report = detect_drift(reader, "pipe-a")

        assert isinstance(report, PipelineDriftReport)
        assert report.pipeline_name == "pipe-a"
        assert isinstance(report.step_results, list)
        assert isinstance(report.summary, str)

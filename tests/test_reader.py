"""Tests for eval trace reader."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from guardian.store.models import Base, EvalTrace
from guardian.store.reader import TraceReader


@pytest.fixture
def db_url():
    """Create a temporary SQLite database."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    url = f"sqlite:///{path}"
    yield url
    os.unlink(path)


@pytest.fixture
def seeded_reader(db_url):
    """Create a reader with seeded test data."""
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    now = datetime.now(timezone.utc)

    with Session(engine) as s:
        # Pipeline A, step_01 — 10 traces over 10 days
        for i in range(10):
            s.add(EvalTrace(
                pipeline_name="pipeline-a",
                step_name="step_01",
                action="pass" if i % 3 != 0 else "retry",
                passed=i % 3 != 0,
                score=0.9 - (i * 0.05),
                issues="[]" if i % 3 != 0 else '["issue"]',
                attempt=1,
                created_at=now - timedelta(days=i),
            ))

        # Pipeline A, step_02 — 5 traces over 5 days
        for i in range(5):
            s.add(EvalTrace(
                pipeline_name="pipeline-a",
                step_name="step_02",
                action="pass",
                passed=True,
                score=0.95,
                issues="[]",
                attempt=1,
                created_at=now - timedelta(days=i),
            ))

        # Pipeline B — 3 traces
        for i in range(3):
            s.add(EvalTrace(
                pipeline_name="pipeline-b",
                step_name="step_01",
                action="abort",
                passed=False,
                score=0.3,
                issues='["bad"]',
                attempt=1,
                created_at=now - timedelta(days=i),
            ))

        s.commit()

    return TraceReader(db_url)


class TestListPipelines:
    def test_lists_all_pipelines(self, seeded_reader):
        pipelines = seeded_reader.list_pipelines()
        names = [p["pipeline_name"] for p in pipelines]
        assert "pipeline-a" in names
        assert "pipeline-b" in names

    def test_pipeline_metadata(self, seeded_reader):
        pipelines = seeded_reader.list_pipelines()
        pa = next(p for p in pipelines if p["pipeline_name"] == "pipeline-a")
        assert pa["step_count"] == 2
        assert pa["trace_count"] == 15
        assert pa["latest_trace"] is not None

    def test_empty_db(self, db_url):
        reader = TraceReader(db_url)
        assert reader.list_pipelines() == []


class TestQueryTraces:
    def test_query_all(self, seeded_reader):
        traces = seeded_reader.query_traces(days=30)
        assert len(traces) == 18  # 10 + 5 + 3

    def test_filter_by_pipeline(self, seeded_reader):
        traces = seeded_reader.query_traces(pipeline_name="pipeline-b", days=30)
        assert all(t["pipeline_name"] == "pipeline-b" for t in traces)
        assert len(traces) == 3

    def test_filter_by_step(self, seeded_reader):
        traces = seeded_reader.query_traces(
            pipeline_name="pipeline-a", step_name="step_02", days=30
        )
        assert all(t["step_name"] == "step_02" for t in traces)
        assert len(traces) == 5

    def test_days_filter(self, seeded_reader):
        traces = seeded_reader.query_traces(pipeline_name="pipeline-a", days=3)
        # step_01: days 0,1,2 = 3 traces; step_02: days 0,1,2 = 3 traces
        assert len(traces) <= 6

    def test_limit(self, seeded_reader):
        traces = seeded_reader.query_traces(days=30, limit=5)
        assert len(traces) == 5

    def test_ordered_descending(self, seeded_reader):
        traces = seeded_reader.query_traces(days=30)
        dates = [t["created_at"] for t in traces]
        assert dates == sorted(dates, reverse=True)

    def test_trace_dict_shape(self, seeded_reader):
        traces = seeded_reader.query_traces(days=30, limit=1)
        t = traces[0]
        assert "id" in t
        assert "pipeline_name" in t
        assert "step_name" in t
        assert "action" in t
        assert "passed" in t
        assert "score" in t
        assert "issues" in t
        assert isinstance(t["issues"], list)


class TestGetStepStats:
    def test_stats_pipeline_a_step_01(self, seeded_reader):
        stats = seeded_reader.get_step_stats("pipeline-a", "step_01", days=30)
        assert stats["total"] == 10
        assert stats["pipeline_name"] == "pipeline-a"
        assert stats["avg_score"] is not None
        assert 0 <= stats["pass_rate"] <= 1
        assert "pass" in stats["action_counts"] or "retry" in stats["action_counts"]

    def test_stats_empty(self, seeded_reader):
        stats = seeded_reader.get_step_stats("nonexistent", "step_01", days=30)
        assert stats["total"] == 0
        assert stats["pass_rate"] is None


class TestGetDailyScores:
    def test_daily_scores(self, seeded_reader):
        daily = seeded_reader.get_daily_scores("pipeline-a", "step_01", days=30)
        assert len(daily) > 0
        for d in daily:
            assert "date" in d
            assert "avg_score" in d
            assert "count" in d
            assert "pass_rate" in d

    def test_daily_scores_empty(self, seeded_reader):
        daily = seeded_reader.get_daily_scores("nonexistent", "step_01", days=30)
        assert daily == []

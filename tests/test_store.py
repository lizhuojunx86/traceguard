"""Tests for eval trace storage (models + writer)."""
import os
import tempfile

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from guardian.store.models import Base, EvalTrace
from guardian.store.writer import TraceWriter


@pytest.fixture
def db_path():
    """Create a temporary SQLite database file."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


@pytest.fixture
def writer(db_path):
    """Create a TraceWriter with a temporary database."""
    return TraceWriter(f"sqlite:///{db_path}")


@pytest.fixture
def session(db_path, writer):
    """Create a SQLAlchemy session for assertions."""
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        yield s


class TestEvalTraceModel:
    """Tests for the EvalTrace SQLAlchemy model."""

    def test_create_table(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        assert "eval_traces" in Base.metadata.tables

    def test_model_fields(self):
        trace = EvalTrace(
            pipeline_name="test-pipe",
            step_name="step_01",
            action="pass",
            passed=True,
            score=1.0,
            issues="[]",
            attempt=1,
        )
        assert trace.pipeline_name == "test-pipe"
        assert trace.passed is True
        assert trace.score == 1.0

    def test_insert_and_query(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            trace = EvalTrace(
                pipeline_name="p",
                step_name="s",
                action="abort",
                passed=False,
                score=0.4,
                issues='["missing field: x"]',
                attempt=1,
            )
            s.add(trace)
            s.commit()

            result = s.execute(select(EvalTrace)).scalar_one()
            assert result.pipeline_name == "p"
            assert result.action == "abort"
            assert result.id is not None
            assert result.created_at is not None


class TestTraceWriter:
    """Tests for TraceWriter."""

    def test_init_creates_tables(self, db_path):
        TraceWriter(f"sqlite:///{db_path}")
        engine = create_engine(f"sqlite:///{db_path}")
        with Session(engine) as s:
            # Table should exist — querying should not raise
            result = s.execute(select(EvalTrace)).all()
            assert result == []

    def test_write_trace(self, writer, session):
        writer.write(
            pipeline_name="my-pipeline",
            step_name="step_01",
            action="pass",
            passed=True,
            score=1.0,
            issues=[],
            attempt=1,
        )
        traces = session.execute(select(EvalTrace)).scalars().all()
        assert len(traces) == 1
        assert traces[0].pipeline_name == "my-pipeline"
        assert traces[0].passed is True
        assert traces[0].issues == "[]"

    def test_write_multiple_traces(self, writer, session):
        for i in range(3):
            writer.write(
                pipeline_name="pipe",
                step_name=f"step_{i:02d}",
                action="pass",
                passed=True,
                score=1.0,
                issues=[],
                attempt=1,
            )
        traces = session.execute(select(EvalTrace)).scalars().all()
        assert len(traces) == 3

    def test_write_failed_trace_with_issues(self, writer, session):
        issues = ["Missing field: data", "Output too short: 5 chars"]
        writer.write(
            pipeline_name="pipe",
            step_name="step_01",
            action="retry",
            passed=False,
            score=0.6,
            issues=issues,
            attempt=2,
        )
        trace = session.execute(select(EvalTrace)).scalar_one()
        assert trace.passed is False
        assert trace.score == 0.6
        assert trace.attempt == 2
        assert "Missing field: data" in trace.issues

    def test_write_with_output_preview(self, writer, session):
        writer.write(
            pipeline_name="pipe",
            step_name="s",
            action="pass",
            passed=True,
            score=1.0,
            issues=[],
            attempt=1,
            output_preview="first 200 chars of output...",
        )
        trace = session.execute(select(EvalTrace)).scalar_one()
        assert trace.output_preview == "first 200 chars of output..."

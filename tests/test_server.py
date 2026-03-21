"""Tests for FastAPI dashboard API."""
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from guardian.api.server import app
from guardian.store.models import Base, EvalTrace


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


@pytest.fixture
def seeded_db(db_path):
    """Seed test database and return the path."""
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    now = datetime.now(timezone.utc)

    with Session(engine) as s:
        for i in range(10):
            s.add(EvalTrace(
                pipeline_name="test-pipeline",
                step_name="step_01",
                action="pass" if i % 2 == 0 else "retry",
                passed=i % 2 == 0,
                score=0.9 - (i * 0.05),
                issues="[]" if i % 2 == 0 else '["problem"]',
                attempt=1,
                created_at=now - timedelta(days=i),
            ))
        for i in range(5):
            s.add(EvalTrace(
                pipeline_name="test-pipeline",
                step_name="step_02",
                action="pass",
                passed=True,
                score=0.95,
                issues="[]",
                attempt=1,
                created_at=now - timedelta(days=i),
            ))
        s.commit()

    return db_path


@pytest.fixture
def client(seeded_db):
    """Create a test client with the seeded database."""
    db_url = f"sqlite:///{seeded_db}"
    with patch.dict(os.environ, {"GUARDIAN_DB_URL": db_url}):
        yield TestClient(app)


class TestHealthEndpoint:
    def test_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


class TestPipelinesEndpoint:
    def test_list_pipelines(self, client):
        r = client.get("/api/pipelines")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["pipeline_name"] == "test-pipeline"
        assert data[0]["step_count"] == 2
        assert data[0]["trace_count"] == 15

    def test_empty_db(self, db_path):
        db_url = f"sqlite:///{db_path}"
        # Create tables but don't seed
        engine = create_engine(db_url)
        Base.metadata.create_all(engine)
        with patch.dict(os.environ, {"GUARDIAN_DB_URL": db_url}):
            c = TestClient(app)
            r = c.get("/api/pipelines")
            assert r.status_code == 200
            assert r.json() == []


class TestTracesEndpoint:
    def test_query_all(self, client):
        r = client.get("/api/traces", params={"days": 30})
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 15

    def test_filter_by_pipeline(self, client):
        r = client.get("/api/traces", params={"pipeline": "test-pipeline", "days": 30})
        assert r.status_code == 200
        assert all(t["pipeline_name"] == "test-pipeline" for t in r.json())

    def test_filter_by_step(self, client):
        r = client.get("/api/traces", params={
            "pipeline": "test-pipeline",
            "step": "step_02",
            "days": 30,
        })
        assert r.status_code == 200
        data = r.json()
        assert all(t["step_name"] == "step_02" for t in data)
        assert len(data) == 5

    def test_limit(self, client):
        r = client.get("/api/traces", params={"days": 30, "limit": 3})
        assert r.status_code == 200
        assert len(r.json()) == 3

    def test_trace_shape(self, client):
        r = client.get("/api/traces", params={"days": 30, "limit": 1})
        t = r.json()[0]
        assert "id" in t
        assert "pipeline_name" in t
        assert "action" in t
        assert "score" in t
        assert "issues" in t
        assert isinstance(t["issues"], list)


class TestStatsEndpoint:
    def test_step_stats(self, client):
        r = client.get("/api/stats", params={
            "pipeline": "test-pipeline",
            "step": "step_01",
            "days": 30,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 10
        assert data["pass_rate"] is not None
        assert data["avg_score"] is not None
        assert "action_counts" in data

    def test_stats_nonexistent(self, client):
        r = client.get("/api/stats", params={
            "pipeline": "nonexistent",
            "step": "step_01",
        })
        assert r.status_code == 200
        assert r.json()["total"] == 0


class TestDriftEndpoint:
    def test_drift_report(self, client):
        r = client.get("/api/drift", params={
            "pipeline": "test-pipeline",
            "recent_days": 3,
            "baseline_days": 14,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["pipeline_name"] == "test-pipeline"
        assert "has_drift" in data
        assert "summary" in data
        assert "steps" in data
        assert len(data["steps"]) > 0

        step = data["steps"][0]
        assert "step_name" in step
        assert "drifted" in step
        assert "trend" in step
        assert "signals" in step

    def test_drift_nonexistent_pipeline(self, client):
        r = client.get("/api/drift", params={"pipeline": "nonexistent"})
        assert r.status_code == 200
        assert r.json()["has_drift"] is False

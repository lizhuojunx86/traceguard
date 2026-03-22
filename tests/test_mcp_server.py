"""Tests for MCP Server tools (direct function calls, no MCP protocol)."""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from guardian.env import LLMEndpoint, LLMMode, reset_endpoint
from guardian.mcp_server import (
    guardian_check,
    guardian_drift_detect,
    guardian_list_pipelines,
    guardian_query_traces,
    guardian_step_stats,
    guardian_suggest,
)
from guardian.store.models import Base, EvalTrace


@pytest.fixture(autouse=True)
def _mock_env():
    reset_endpoint()
    ep = LLMEndpoint(mode=LLMMode.DEGRADED, reason="test")
    with patch("guardian.env.probe_llm_environment", return_value=ep):
        with patch("guardian.validators.semantic.probe_llm_environment", return_value=ep):
            yield
    reset_endpoint()


@pytest.fixture
def db_url():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    url = f"sqlite:///{path}"
    yield url
    os.unlink(path)


@pytest.fixture
def seeded_db(db_url):
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        for i in range(8):
            s.add(EvalTrace(
                pipeline_name="market-intelligence",
                step_name="step_01_collect",
                action="pass" if i % 2 == 0 else "retry",
                passed=i % 2 == 0,
                score=0.9 if i % 2 == 0 else 0.4,
                issues="[]" if i % 2 == 0 else '["Missing required field: data"]',
                attempt=1,
                created_at=now - timedelta(days=i),
            ))
        s.commit()
    return db_url


class TestGuardianCheck:
    def test_check_pass_with_inline_data(self, db_url):
        data = json.dumps({
            "data": [{"symbol": "AAPL", "price": 185.5}],
            "timestamp": "2026-03-21T10:00:00Z",
            "source": "api",
        })
        result = json.loads(guardian_check(
            pipeline_config_path="configs/examples/market_intel.yaml",
            step_name="step_01_collect",
            output_data=data,
            db_url=db_url,
        ))
        assert result["action"] == "pass"
        assert result["score"] == 1.0

    def test_check_fail_missing_fields(self, db_url):
        result = json.loads(guardian_check(
            pipeline_config_path="configs/examples/market_intel.yaml",
            step_name="step_01_collect",
            output_data='{"wrong": "data"}',
            db_url=db_url,
        ))
        assert result["action"] in ("retry", "abort")
        assert len(result["issues"]) > 0

    def test_check_step_not_found(self, db_url):
        result = json.loads(guardian_check(
            pipeline_config_path="configs/examples/market_intel.yaml",
            step_name="nonexistent",
            output_data="{}",
            db_url=db_url,
        ))
        assert "error" in result

    def test_check_no_input(self, db_url):
        result = json.loads(guardian_check(
            pipeline_config_path="configs/examples/market_intel.yaml",
            step_name="step_01_collect",
            db_url=db_url,
        ))
        assert "error" in result

    def test_check_with_file(self, db_url):
        data = {
            "data": [{"symbol": "GOOG", "price": 142.3}],
            "timestamp": "2026-03-21T10:00:00Z",
            "source": "api",
        }
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        try:
            result = json.loads(guardian_check(
                pipeline_config_path="configs/examples/market_intel.yaml",
                step_name="step_01_collect",
                input_file_path=path,
                db_url=db_url,
            ))
            assert result["action"] == "pass"
        finally:
            os.unlink(path)


class TestGuardianListPipelines:
    def test_list_with_data(self, seeded_db):
        result = json.loads(guardian_list_pipelines(db_url=seeded_db))
        assert len(result) == 1
        assert result[0]["pipeline_name"] == "market-intelligence"

    def test_list_empty(self, db_url):
        # Create tables but no data
        engine = create_engine(db_url)
        Base.metadata.create_all(engine)
        result = json.loads(guardian_list_pipelines(db_url=db_url))
        assert result == []


class TestGuardianQueryTraces:
    def test_query_all(self, seeded_db):
        result = json.loads(guardian_query_traces(days=30, db_url=seeded_db))
        assert len(result) == 8

    def test_query_with_filter(self, seeded_db):
        result = json.loads(guardian_query_traces(
            pipeline_name="market-intelligence",
            step_name="step_01_collect",
            days=3,
            db_url=seeded_db,
        ))
        assert all(t["pipeline_name"] == "market-intelligence" for t in result)


class TestGuardianStepStats:
    def test_stats(self, seeded_db):
        result = json.loads(guardian_step_stats(
            pipeline_name="market-intelligence",
            step_name="step_01_collect",
            days=30,
            db_url=seeded_db,
        ))
        assert result["total"] == 8
        assert result["pass_rate"] is not None


class TestGuardianDriftDetect:
    def test_drift(self, seeded_db):
        result = json.loads(guardian_drift_detect(
            pipeline_name="market-intelligence",
            db_url=seeded_db,
        ))
        assert "has_drift" in result
        assert "summary" in result


class TestGuardianSuggest:
    @pytest.mark.asyncio
    async def test_suggest_degraded(self, seeded_db):
        result = json.loads(await guardian_suggest(
            pipeline_config_path="configs/examples/market_intel.yaml",
            step_name="step_01_collect",
            db_url=seeded_db,
        ))
        assert result["pipeline"] == "market-intelligence"
        assert "root_causes" in result

    @pytest.mark.asyncio
    async def test_suggest_no_failures(self, db_url):
        engine = create_engine(db_url)
        Base.metadata.create_all(engine)
        now = datetime.now(timezone.utc)
        with Session(engine) as s:
            for i in range(3):
                s.add(EvalTrace(
                    pipeline_name="market-intelligence",
                    step_name="step_01_collect",
                    action="pass", passed=True, score=0.95,
                    issues="[]", attempt=1,
                    created_at=now - timedelta(days=i),
                ))
            s.commit()

        result = json.loads(await guardian_suggest(
            pipeline_config_path="configs/examples/market_intel.yaml",
            step_name="step_01_collect",
            db_url=db_url,
        ))
        assert "No failures" in result.get("message", "")

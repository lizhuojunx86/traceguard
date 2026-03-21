"""Tests for root cause analyzer."""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from guardian.env import LLMEndpoint, LLMMode, reset_endpoint
from guardian.optimizer.root_cause import (
    FailurePattern,
    RootCauseReport,
    _build_analysis_prompt,
    _parse_analysis,
    _rule_based_root_causes,
    analyze_root_causes,
    extract_failure_pattern,
)
from guardian.store.models import Base, EvalTrace
from guardian.store.reader import TraceReader


@pytest.fixture(autouse=True)
def _mock_env():
    reset_endpoint()
    ep = LLMEndpoint(mode=LLMMode.FULL, api_base="https://api.example.com/v1",
                     model="gpt-4o-mini", provider="openai")
    with patch("guardian.env.probe_llm_environment", return_value=ep):
        yield
    reset_endpoint()


# -- Fixtures --

@pytest.fixture
def db_url():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield f"sqlite:///{path}"
    os.unlink(path)


@pytest.fixture
def seeded_reader(db_url):
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)

    with Session(engine) as s:
        # 7 passing traces
        for i in range(7):
            s.add(EvalTrace(
                pipeline_name="pipe-a", step_name="step_01",
                action="pass", passed=True, score=0.9,
                issues="[]", attempt=1,
                created_at=now - timedelta(days=i),
            ))
        # 5 failing traces with various issues
        fail_issues = [
            ["Missing required field: data", "Output too short"],
            ["JSON Schema validation failed"],
            ["Missing required field: data"],
            ["Output too short", "Language mismatch"],
            ["Missing required field: data", "JSON Schema validation failed"],
        ]
        for i, issues in enumerate(fail_issues):
            s.add(EvalTrace(
                pipeline_name="pipe-a", step_name="step_01",
                action="retry", passed=False, score=0.4,
                issues=json.dumps(issues), attempt=1,
                output_preview=f"Failed output sample {i}",
                created_at=now - timedelta(days=i),
            ))
        s.commit()

    return TraceReader(db_url)


def _mock_llm_response(root_causes: list[dict], summary: str) -> httpx.Response:
    body = {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "root_causes": root_causes,
                    "summary": summary,
                })
            }
        }]
    }
    return httpx.Response(
        status_code=200, json=body,
        request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
    )


# -- Tests --

class TestExtractFailurePattern:
    def test_basic_extraction(self, seeded_reader):
        pattern = extract_failure_pattern(seeded_reader, "pipe-a", "step_01", days=30)
        assert pattern.total_traces == 12
        assert pattern.failed_traces == 5
        assert pattern.failure_rate == round(5 / 12, 4)
        assert pattern.avg_score > 0

    def test_issue_counts(self, seeded_reader):
        pattern = extract_failure_pattern(seeded_reader, "pipe-a", "step_01", days=30)
        assert "Missing required field: data" in pattern.issue_counts
        assert pattern.issue_counts["Missing required field: data"] == 3

    def test_score_distribution(self, seeded_reader):
        pattern = extract_failure_pattern(seeded_reader, "pipe-a", "step_01", days=30)
        assert sum(pattern.score_distribution.values()) == 12

    def test_sample_previews(self, seeded_reader):
        pattern = extract_failure_pattern(
            seeded_reader, "pipe-a", "step_01", days=30, max_samples=3
        )
        assert len(pattern.sample_previews) <= 3
        assert all("Failed output" in p for p in pattern.sample_previews)

    def test_empty_step(self, seeded_reader):
        pattern = extract_failure_pattern(seeded_reader, "pipe-a", "nonexistent", days=30)
        assert pattern.total_traces == 0
        assert pattern.failed_traces == 0

    def test_all_passing(self, db_url):
        engine = create_engine(db_url)
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            for i in range(5):
                s.add(EvalTrace(
                    pipeline_name="p", step_name="s",
                    action="pass", passed=True, score=0.95,
                    issues="[]", attempt=1,
                    created_at=datetime.now(timezone.utc),
                ))
            s.commit()

        reader = TraceReader(db_url)
        pattern = extract_failure_pattern(reader, "p", "s", days=30)
        assert pattern.failed_traces == 0
        assert pattern.failure_rate == 0.0
        assert pattern.issue_counts == {}


class TestBuildAnalysisPrompt:
    def test_prompt_contains_key_info(self):
        pattern = FailurePattern(
            pipeline_name="pipe", step_name="step_01",
            total_traces=100, failed_traces=30,
            failure_rate=0.3, avg_score=0.65,
            issue_counts={"Missing field: x": 20, "Too short": 10},
            score_distribution={"0.0-0.2": 5, "0.2-0.4": 10, "0.4-0.6": 15, "0.6-0.8": 30, "0.8-1.0": 40},
            sample_previews=["sample output 1"],
        )
        prompt = _build_analysis_prompt(pattern)
        assert "step_01" in prompt
        assert "30.0%" in prompt  # failure rate
        assert "Missing field: x" in prompt
        assert "sample output 1" in prompt


class TestParseAnalysis:
    def test_valid_json(self):
        raw = json.dumps({
            "root_causes": [{"cause": "bad prompts", "evidence": "data", "severity": "high", "frequency": "60%"}],
            "summary": "The main issue is bad prompts.",
        })
        result = _parse_analysis(raw)
        assert len(result["root_causes"]) == 1
        assert result["root_causes"][0]["cause"] == "bad prompts"

    def test_markdown_fence(self):
        inner = json.dumps({"root_causes": [], "summary": "ok"})
        result = _parse_analysis(f"```json\n{inner}\n```")
        assert result["summary"] == "ok"

    def test_invalid_json(self):
        result = _parse_analysis("not json")
        assert result["root_causes"] == []


class TestAnalyzeRootCauses:
    @pytest.mark.asyncio
    async def test_no_failures_skips_llm(self):
        pattern = FailurePattern(
            pipeline_name="p", step_name="s",
            total_traces=10, failed_traces=0,
        )
        report = await analyze_root_causes(pattern)
        assert "No failures" in report.summary
        assert report.root_causes == []

    @pytest.mark.asyncio
    async def test_analyzes_failures(self):
        mock_response = _mock_llm_response(
            root_causes=[
                {"cause": "Schema mismatch", "evidence": "field errors", "severity": "high", "frequency": "60%"},
                {"cause": "Truncated output", "evidence": "too short", "severity": "medium", "frequency": "30%"},
            ],
            summary="Primary issue is schema mismatch.",
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        pattern = FailurePattern(
            pipeline_name="pipe", step_name="step_01",
            total_traces=100, failed_traces=30,
            failure_rate=0.3, avg_score=0.65,
            issue_counts={"Missing field": 20},
        )

        with patch.dict(os.environ, {"GUARDIAN_LLM_API_KEY": "test-key"}):
            report = await analyze_root_causes(
                pattern, http_client=mock_client
            )

        assert len(report.root_causes) == 2
        assert report.root_causes[0]["cause"] == "Schema mismatch"
        assert "schema mismatch" in report.summary.lower()
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_api_key_degrades_to_rules(self):
        """Missing API key should degrade to rule-based, not crash."""
        pattern = FailurePattern(
            pipeline_name="p", step_name="s",
            total_traces=10, failed_traces=5,
            issue_counts={"Missing field": 3},
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GUARDIAN_LLM_API_KEY", None)
            report = await analyze_root_causes(pattern)
        assert len(report.root_causes) > 0
        assert "rule-based" in report.summary.lower()

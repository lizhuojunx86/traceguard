"""Tests for degraded mode (rule-based fallbacks)."""
import json
import os
import tempfile
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from guardian.cli import cli
from guardian.env import LLMEndpoint, LLMMode, reset_endpoint
from guardian.optimizer.root_cause import FailurePattern, RootCauseReport, _rule_based_root_causes
from guardian.optimizer.suggestion import Suggestion, _rule_based_suggestions
from guardian.store.models import Base, EvalTrace


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_endpoint()
    yield
    reset_endpoint()


# -- Rule-based root causes --

class TestRuleBasedRootCauses:
    def test_generates_from_pattern(self):
        pattern = FailurePattern(
            pipeline_name="p", step_name="s",
            total_traces=100, failed_traces=30,
            issue_counts={"Missing field: data": 20, "Output too short": 5},
        )
        causes = _rule_based_root_causes(pattern)
        assert len(causes) == 2
        assert causes[0]["cause"] == "Missing field: data"
        assert causes[0]["severity"] == "high"  # 20/30 > 50%
        assert causes[1]["severity"] == "low"   # 5/30 < 20%

    def test_empty_pattern(self):
        pattern = FailurePattern(
            pipeline_name="p", step_name="s",
            failed_traces=0, issue_counts={},
        )
        causes = _rule_based_root_causes(pattern)
        assert causes == []

    def test_medium_severity(self):
        pattern = FailurePattern(
            pipeline_name="p", step_name="s",
            failed_traces=10,
            issue_counts={"Schema error": 4},  # 40% → medium
        )
        causes = _rule_based_root_causes(pattern)
        assert causes[0]["severity"] == "medium"


# -- Rule-based suggestions --

class TestRuleBasedSuggestions:
    def test_high_failure_rate(self):
        report = RootCauseReport(
            pipeline_name="p", step_name="s",
            pattern=FailurePattern(
                pipeline_name="p", step_name="s",
                failure_rate=0.5, issue_counts={},
            ),
        )
        suggestions = _rule_based_suggestions(report, "old hint")
        assert any(s.type == "action_config" for s in suggestions)

    def test_missing_field_suggestion(self):
        report = RootCauseReport(
            pipeline_name="p", step_name="s",
            pattern=FailurePattern(
                pipeline_name="p", step_name="s",
                failure_rate=0.1,
                issue_counts={"Missing required field: data": 10},
            ),
        )
        suggestions = _rule_based_suggestions(report, "Fix it")
        assert any(s.type == "retry_hint" for s in suggestions)

    def test_length_issue_suggestion(self):
        report = RootCauseReport(
            pipeline_name="p", step_name="s",
            pattern=FailurePattern(
                pipeline_name="p", step_name="s",
                failure_rate=0.1,
                issue_counts={"Output too short: 5 chars": 8},
            ),
        )
        suggestions = _rule_based_suggestions(report, None)
        assert any(s.type == "structural_config" for s in suggestions)

    def test_empty_issues(self):
        report = RootCauseReport(
            pipeline_name="p", step_name="s",
            pattern=FailurePattern(
                pipeline_name="p", step_name="s",
                failure_rate=0.1, issue_counts={},
            ),
        )
        suggestions = _rule_based_suggestions(report, None)
        # No field/length issues → no suggestions of those types
        assert not any(s.type == "retry_hint" for s in suggestions)


# -- CLI integration: suggest in degraded mode --

class TestSuggestDegradedCLI:
    @pytest.fixture
    def seeded_db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_url = f"sqlite:///{path}"
        engine = create_engine(db_url)
        Base.metadata.create_all(engine)
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        with Session(engine) as s:
            for i in range(5):
                s.add(EvalTrace(
                    pipeline_name="market-intelligence",
                    step_name="step_01_collect",
                    action="pass", passed=True, score=0.9,
                    issues="[]", attempt=1,
                    created_at=now - timedelta(days=i),
                ))
            for i in range(5):
                s.add(EvalTrace(
                    pipeline_name="market-intelligence",
                    step_name="step_01_collect",
                    action="retry", passed=False, score=0.4,
                    issues=json.dumps(["Missing required field: data"]),
                    attempt=1,
                    created_at=now - timedelta(days=i),
                ))
            s.commit()
        yield db_url
        os.unlink(path)

    def test_suggest_degraded_exits_zero(self, seeded_db):
        """suggest command should exit 0 even with no LLM available."""
        ep = LLMEndpoint(mode=LLMMode.DEGRADED, reason="test")
        with patch("guardian.env.probe_llm_environment", return_value=ep):
            runner = CliRunner()
            result = runner.invoke(cli, [
                "suggest",
                "--pipeline", "configs/examples/market_intel.yaml",
                "--step", "step_01_collect",
                "--db", seeded_db,
                "--days", "30",
            ])
        assert result.exit_code == 0
        assert "rule-based" in result.output.lower() or "suggestion" in result.output.lower()

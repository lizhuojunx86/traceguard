"""Tests for degraded mode (rule-based diagnostic suggestions)."""
import json
import os
import tempfile
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from guardian.cli import cli
from guardian.env import LLMEndpoint, LLMMode, reset_endpoint
from guardian.optimizer.root_cause import FailurePattern, RootCauseReport, _rule_based_root_causes
from guardian.optimizer.suggestion import (
    Suggestion,
    _extract_field_names,
    _extract_lengths_from_issues,
    _extract_percentage_from_issues,
    _rule_based_suggestions,
    format_suggestion_report,
)
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
            issue_counts={"Missing field: data": 20, "Output too short: 5 chars": 5},
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


# -- Helpers extraction --

class TestExtractHelpers:
    def test_extract_percentage(self):
        msgs = [
            "Language mismatch: expected 'zh', but only 28% of text matches",
            "Language mismatch: expected 'zh', but only 32% of text matches",
        ]
        avg = _extract_percentage_from_issues(msgs)
        assert avg == 30  # (28+32)/2

    def test_extract_percentage_none(self):
        assert _extract_percentage_from_issues(["no match here"]) is None

    def test_extract_lengths(self):
        msgs = [
            "Output too short: 45 chars (minimum: 100)",
            "Output too short: 52 chars (minimum: 100)",
        ]
        lengths = _extract_lengths_from_issues(msgs)
        assert lengths == [45, 52]

    def test_extract_field_names(self):
        msgs = [
            "Missing required field: data",
            "Missing required field: timestamp",
            "Missing field: source",
        ]
        names = _extract_field_names(msgs)
        assert names == ["data", "source", "timestamp"]


# -- Language mismatch suggestions --

class TestLanguageMismatchSuggestion:
    def test_severe_language_mixing(self):
        """21/30 failures with ~28% ratio → severe diagnosis."""
        report = RootCauseReport(
            pipeline_name="p", step_name="s",
            pattern=FailurePattern(
                pipeline_name="p", step_name="s",
                total_traces=30, failed_traces=21,
                failure_rate=0.7,
                issue_counts={
                    "Language mismatch: expected 'zh', but only 28% of text matches": 21,
                },
            ),
        )
        suggestions = _rule_based_suggestions(report, None)
        assert len(suggestions) >= 1
        s = suggestions[0]
        assert s.type == "prompt_change"
        assert "language" in s.title.lower()
        assert "21/21" in s.diagnosis
        assert "28%" in s.diagnosis
        assert "paraphrase" in s.proposed.lower() or "target language" in s.proposed.lower()
        assert s.alternative  # Should suggest threshold adjustment as alternative

    def test_moderate_language_mixing(self):
        """Language mismatch but ratio is ~45% → moderate."""
        report = RootCauseReport(
            pipeline_name="p", step_name="s",
            pattern=FailurePattern(
                pipeline_name="p", step_name="s",
                total_traces=20, failed_traces=10,
                failure_rate=0.5,
                issue_counts={
                    "Language mismatch: expected 'zh', but only 45% of text matches": 10,
                },
            ),
        )
        suggestions = _rule_based_suggestions(report, None)
        s = suggestions[0]
        assert "moderate" in s.title.lower()

    def test_no_generic_max_retries_for_language(self):
        """Language issues should NOT suggest 'increase max_retries'."""
        report = RootCauseReport(
            pipeline_name="p", step_name="s",
            pattern=FailurePattern(
                pipeline_name="p", step_name="s",
                total_traces=30, failed_traces=21,
                failure_rate=0.7,
                issue_counts={
                    "Language mismatch: expected 'zh', but only 28% of text matches": 21,
                },
            ),
        )
        suggestions = _rule_based_suggestions(report, None)
        for s in suggestions:
            assert "increase max_retries" not in s.proposed.lower()


# -- Length suggestions --

class TestLengthSuggestions:
    def test_systematic_short_output(self):
        """Clustered short lengths → prompt change, not retries."""
        report = RootCauseReport(
            pipeline_name="p", step_name="s",
            pattern=FailurePattern(
                pipeline_name="p", step_name="s",
                total_traces=30, failed_traces=20,
                failure_rate=0.67,
                issue_counts={
                    "Output too short: 85 chars (minimum: 100)": 8,
                    "Output too short: 90 chars (minimum: 100)": 7,
                    "Output too short: 88 chars (minimum: 100)": 5,
                },
            ),
        )
        suggestions = _rule_based_suggestions(report, None)
        short_suggestions = [s for s in suggestions if "short" in s.title.lower() or "length" in s.title.lower()]
        assert len(short_suggestions) >= 1
        s = short_suggestions[0]
        assert "systematic" in s.title.lower() or "systematic" in s.diagnosis.lower()
        assert "prompt" in s.proposed.lower()

    def test_inconsistent_lengths(self):
        """Widely varying lengths → retries + format constraints."""
        report = RootCauseReport(
            pipeline_name="p", step_name="s",
            pattern=FailurePattern(
                pipeline_name="p", step_name="s",
                total_traces=30, failed_traces=10,
                failure_rate=0.33,
                issue_counts={
                    "Output too short: 10 chars (minimum: 100)": 4,
                    "Output too short: 80 chars (minimum: 100)": 3,
                    "Output too short: 5 chars (minimum: 100)": 3,
                },
            ),
        )
        suggestions = _rule_based_suggestions(report, None)
        short_suggestions = [s for s in suggestions if "length" in s.title.lower() or "inconsistent" in s.title.lower() or "variance" in s.title.lower()]
        assert len(short_suggestions) >= 1
        s = short_suggestions[0]
        assert "variance" in s.diagnosis.lower() or "vary" in s.diagnosis.lower() or "unstable" in s.root_cause_hypothesis.lower()


# -- Missing fields suggestions --

class TestMissingFieldSuggestions:
    def test_specific_field_names(self):
        report = RootCauseReport(
            pipeline_name="p", step_name="s",
            pattern=FailurePattern(
                pipeline_name="p", step_name="s",
                total_traces=30, failed_traces=15,
                failure_rate=0.5,
                issue_counts={
                    "Missing required field: data": 10,
                    "Missing required field: timestamp": 5,
                },
            ),
        )
        suggestions = _rule_based_suggestions(report, "Fix fields")
        field_suggestions = [s for s in suggestions if "field" in s.title.lower() or "omit" in s.title.lower()]
        assert len(field_suggestions) >= 1
        s = field_suggestions[0]
        assert s.type == "prompt_change"
        assert "data" in s.diagnosis
        assert "timestamp" in s.diagnosis
        assert "schema" in s.proposed.lower() or "field" in s.proposed.lower()


# -- Generic fallback --

class TestGenericFallback:
    def test_unknown_pattern_gets_generic(self):
        """Unrecognized issue patterns should get generic fallback."""
        report = RootCauseReport(
            pipeline_name="p", step_name="s",
            pattern=FailurePattern(
                pipeline_name="p", step_name="s",
                total_traces=30, failed_traces=10,
                failure_rate=0.33,
                issue_counts={"Some unknown error type": 10},
            ),
        )
        suggestions = _rule_based_suggestions(report, None)
        assert len(suggestions) == 1
        assert "generic fallback" in suggestions[0].title.lower()

    def test_empty_issues_no_suggestions(self):
        report = RootCauseReport(
            pipeline_name="p", step_name="s",
            pattern=FailurePattern(
                pipeline_name="p", step_name="s",
                failure_rate=0.1, issue_counts={},
            ),
        )
        suggestions = _rule_based_suggestions(report, None)
        assert suggestions == []


# -- Format output --

class TestFormatDiagnosticReport:
    def test_format_with_diagnosis_fields(self):
        from guardian.optimizer.suggestion import SuggestionReport
        report = SuggestionReport(
            pipeline_name="pipe", step_name="step_01",
            suggestions=[Suggestion(
                type="prompt_change",
                title="Language mixing",
                diagnosis="21/21 failures are language mismatch",
                root_cause_hypothesis="Agent embeds source language verbatim",
                proposed="Add rule: paraphrase in target language",
                expected_impact="Increase target language ratio",
                alternative="Adjust threshold to 25%",
            )],
            overall_strategy="Focus on prompt changes.",
            root_cause_summary="Language issues.",
        )
        output = format_suggestion_report(report)
        assert "Diagnosis:" in output
        assert "Root cause hypothesis:" in output
        assert "Recommended change:" in output
        assert "Alternative:" in output
        assert "21/21" in output


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

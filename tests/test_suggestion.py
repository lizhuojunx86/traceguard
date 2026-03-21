"""Tests for suggestion engine."""
import json
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from guardian.env import LLMEndpoint, LLMMode, reset_endpoint
from guardian.optimizer.root_cause import FailurePattern, RootCauseReport
from guardian.optimizer.suggestion import (
    Suggestion,
    SuggestionReport,
    _parse_suggestions,
    format_suggestion_report,
    generate_suggestions,
)


@pytest.fixture(autouse=True)
def _mock_env():
    reset_endpoint()
    ep = LLMEndpoint(mode=LLMMode.FULL, api_base="https://api.example.com/v1",
                     model="gpt-4o-mini", provider="openai")
    with patch("guardian.env.probe_llm_environment", return_value=ep):
        yield
    reset_endpoint()


# -- Helpers --

def _make_root_cause_report(
    with_causes: bool = True,
) -> RootCauseReport:
    pattern = FailurePattern(
        pipeline_name="pipe-a", step_name="step_01",
        total_traces=100, failed_traces=30,
        failure_rate=0.3, avg_score=0.65,
        issue_counts={"Missing field: data": 20, "Output too short": 10},
    )
    if with_causes:
        return RootCauseReport(
            pipeline_name="pipe-a", step_name="step_01",
            pattern=pattern,
            root_causes=[
                {"cause": "Agent not including required fields", "evidence": "field errors", "severity": "high", "frequency": "60%"},
                {"cause": "Output truncation", "evidence": "too short", "severity": "medium", "frequency": "30%"},
            ],
            summary="Agent consistently omits required data fields.",
        )
    return RootCauseReport(
        pipeline_name="pipe-a", step_name="step_01",
        pattern=pattern, root_causes=[], summary="",
    )


SAMPLE_CONFIG_YAML = """\
structural:
  required_fields: ["data", "timestamp"]
  min_length: 100
  max_length: 50000
actions:
  on_structural_fail: retry
  max_retries: 2
  retry_hint: "Please include all required fields."
"""


def _mock_suggestion_response(suggestions: list[dict], strategy: str) -> httpx.Response:
    body = {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "suggestions": suggestions,
                    "overall_strategy": strategy,
                })
            }
        }]
    }
    return httpx.Response(
        status_code=200, json=body,
        request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
    )


# -- Tests --

class TestParseSuggestions:
    def test_valid_json(self):
        raw = json.dumps({
            "suggestions": [
                {"type": "retry_hint", "title": "Improve hint", "current": "old", "proposed": "new", "rationale": "better", "expected_impact": "higher pass rate"}
            ],
            "overall_strategy": "Focus on hints.",
        })
        result = _parse_suggestions(raw)
        assert len(result["suggestions"]) == 1

    def test_markdown_fence(self):
        inner = json.dumps({"suggestions": [], "overall_strategy": "none"})
        result = _parse_suggestions(f"```json\n{inner}\n```")
        assert result["overall_strategy"] == "none"

    def test_invalid_json(self):
        result = _parse_suggestions("broken")
        assert result["suggestions"] == []


class TestGenerateSuggestions:
    @pytest.mark.asyncio
    async def test_no_root_causes_skips(self):
        report = await generate_suggestions(
            _make_root_cause_report(with_causes=False),
            SAMPLE_CONFIG_YAML,
        )
        assert report.suggestions == []
        assert "No root causes" in report.overall_strategy

    @pytest.mark.asyncio
    async def test_generates_suggestions(self):
        mock_response = _mock_suggestion_response(
            suggestions=[
                {
                    "type": "retry_hint",
                    "title": "More specific retry hint",
                    "current": "Please include all required fields.",
                    "proposed": "Your output MUST include a 'data' array and a 'timestamp' field in ISO 8601 format. Do not omit any fields.",
                    "rationale": "The current hint is too vague; the agent needs explicit field names.",
                    "expected_impact": "Reduce missing-field failures by ~50%",
                },
                {
                    "type": "structural_config",
                    "title": "Increase max retries",
                    "current": "max_retries: 2",
                    "proposed": "max_retries: 3",
                    "rationale": "Many failures are transient and recover on second retry.",
                    "expected_impact": "Reduce abort rate by ~20%",
                },
            ],
            strategy="Improve retry hints and allow one more retry attempt.",
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.dict(os.environ, {"GUARDIAN_LLM_API_KEY": "test-key"}):
            report = await generate_suggestions(
                _make_root_cause_report(),
                SAMPLE_CONFIG_YAML,
                current_retry_hint="Please include all required fields.",
                http_client=mock_client,
            )

        assert len(report.suggestions) == 2
        assert report.suggestions[0].type == "retry_hint"
        assert "MUST include" in report.suggestions[0].proposed
        assert report.overall_strategy != ""
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_api_key_degrades_to_rules(self):
        """Missing API key should degrade to rule-based, not crash."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GUARDIAN_LLM_API_KEY", None)
            report = await generate_suggestions(
                _make_root_cause_report(), SAMPLE_CONFIG_YAML
            )
        assert "rule-based" in report.overall_strategy.lower()

    @pytest.mark.asyncio
    async def test_passes_config_in_prompt(self):
        mock_response = _mock_suggestion_response([], "nothing")
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.dict(os.environ, {"GUARDIAN_LLM_API_KEY": "key"}):
            await generate_suggestions(
                _make_root_cause_report(),
                SAMPLE_CONFIG_YAML,
                current_retry_hint="Fix it",
                http_client=mock_client,
            )

        call_args = mock_client.post.call_args
        user_msg = call_args[1]["json"]["messages"][1]["content"]
        assert "required_fields" in user_msg
        assert "Fix it" in user_msg


class TestFormatSuggestionReport:
    def test_format_with_suggestions(self):
        report = SuggestionReport(
            pipeline_name="pipe-a", step_name="step_01",
            suggestions=[
                Suggestion(
                    type="retry_hint",
                    title="Better retry hint",
                    current="old hint",
                    proposed="new detailed hint",
                    rationale="old was too vague",
                    expected_impact="50% fewer retries",
                ),
            ],
            overall_strategy="Improve prompt specificity.",
            root_cause_summary="Agent omits required fields.",
        )
        output = format_suggestion_report(report)
        assert "OPTIMIZATION SUGGESTIONS" in output
        assert "pipe-a" in output
        assert "step_01" in output
        assert "- old hint" in output
        assert "+ new detailed hint" in output
        assert "Rationale:" in output
        assert "Review before applying" in output

    def test_format_empty(self):
        report = SuggestionReport(
            pipeline_name="p", step_name="s",
        )
        output = format_suggestion_report(report)
        assert "No suggestions" in output

    def test_format_multiline_values(self):
        report = SuggestionReport(
            pipeline_name="p", step_name="s",
            suggestions=[
                Suggestion(
                    type="retry_hint",
                    title="Multi-line hint",
                    current="line1\nline2",
                    proposed="new1\nnew2\nnew3",
                    rationale="reason",
                    expected_impact="impact",
                ),
            ],
        )
        output = format_suggestion_report(report)
        assert "- line1" in output
        assert "- line2" in output
        assert "+ new1" in output
        assert "+ new3" in output

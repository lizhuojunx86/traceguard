"""Tests for semantic validator (LLM-as-Judge)."""
import json
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from guardian.core.config import SemanticCheckConfig
from guardian.core.step import StepOutput
from guardian.validators.semantic import (
    SemanticResult,
    _build_user_prompt,
    _parse_llm_response,
    validate_semantic,
)


# -- Helpers --

def _make_output(data: str | dict = "This is a well-structured report.") -> StepOutput:
    return StepOutput(step_name="test_step", output_data=data)


_DEFAULT_CRITERIA = ["Output is coherent", "Output is relevant"]


def _make_config(
    enabled: bool = True,
    model: str = "gpt-4o-mini",
    criteria: list[str] | None = None,
    min_score: int = 3,
    api_base: str = "https://api.example.com/v1",
    api_key_env: str = "GUARDIAN_LLM_API_KEY",
) -> SemanticCheckConfig:
    return SemanticCheckConfig(
        enabled=enabled,
        model=model,
        criteria=_DEFAULT_CRITERIA if criteria is None else criteria,
        min_score=min_score,
        api_base=api_base,
        api_key_env=api_key_env,
    )


def _mock_llm_response(score: int, issues: list[str]) -> httpx.Response:
    """Create a mock httpx.Response mimicking an OpenAI chat completion."""
    body = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({"score": score, "issues": issues})
                }
            }
        ]
    }
    return httpx.Response(
        status_code=200,
        json=body,
        request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
    )


# -- Tests --

class TestSemanticResult:
    """Tests for SemanticResult dataclass."""

    def test_passed(self):
        r = SemanticResult(passed=True, score=4, issues=[])
        assert r.passed is True
        assert r.score == 4

    def test_failed(self):
        r = SemanticResult(passed=False, score=2, issues=["Incoherent output"])
        assert r.passed is False
        assert len(r.issues) == 1


class TestBuildUserPrompt:
    """Tests for prompt construction."""

    def test_contains_output_and_criteria(self):
        prompt = _build_user_prompt("Hello world", ["Is coherent", "Has data"])
        assert "Hello world" in prompt
        assert "Is coherent" in prompt
        assert "Has data" in prompt
        assert "1." in prompt
        assert "2." in prompt

    def test_truncates_long_output(self):
        long_text = "x" * 10000
        prompt = _build_user_prompt(long_text, ["check"])
        # Should truncate to 5000 chars
        assert len(prompt) < 10000


class TestParseLlmResponse:
    """Tests for _parse_llm_response."""

    def test_valid_json(self):
        raw = json.dumps({"score": 4, "issues": ["minor issue"]})
        result = _parse_llm_response(raw, min_score=3)
        assert result.passed is True
        assert result.score == 4
        assert result.issues == ["minor issue"]

    def test_score_below_threshold(self):
        raw = json.dumps({"score": 2, "issues": ["bad output"]})
        result = _parse_llm_response(raw, min_score=3)
        assert result.passed is False
        assert result.score == 2

    def test_score_at_threshold(self):
        raw = json.dumps({"score": 3, "issues": []})
        result = _parse_llm_response(raw, min_score=3)
        assert result.passed is True

    def test_perfect_score(self):
        raw = json.dumps({"score": 5, "issues": []})
        result = _parse_llm_response(raw, min_score=3)
        assert result.passed is True
        assert result.score == 5
        assert result.issues == []

    def test_markdown_code_fence(self):
        inner = json.dumps({"score": 4, "issues": []})
        raw = f"```json\n{inner}\n```"
        result = _parse_llm_response(raw, min_score=3)
        assert result.passed is True
        assert result.score == 4

    def test_invalid_json(self):
        result = _parse_llm_response("not json at all", min_score=3)
        assert result.passed is False
        assert result.score == 1
        assert any("not valid JSON" in i for i in result.issues)

    def test_score_out_of_range_clamped(self):
        raw = json.dumps({"score": 10, "issues": []})
        result = _parse_llm_response(raw, min_score=3)
        assert 1 <= result.score <= 5

    def test_issues_not_list(self):
        raw = json.dumps({"score": 3, "issues": "single string"})
        result = _parse_llm_response(raw, min_score=3)
        assert isinstance(result.issues, list)
        assert len(result.issues) == 1

    def test_raw_response_preserved(self):
        raw = json.dumps({"score": 2, "issues": ["x"]})
        result = _parse_llm_response(raw, min_score=3)
        assert result.raw_response == raw


class TestValidateSemantic:
    """Tests for validate_semantic async function."""

    @pytest.mark.asyncio
    async def test_disabled_returns_pass(self):
        config = _make_config(enabled=False)
        result = await validate_semantic(_make_output(), config)
        assert result.passed is True
        assert result.score == 5

    @pytest.mark.asyncio
    async def test_empty_criteria_returns_pass(self):
        config = _make_config(enabled=True, criteria=[])
        result = await validate_semantic(_make_output(), config)
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_missing_api_key_raises(self):
        config = _make_config(api_key_env="NONEXISTENT_KEY_12345")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NONEXISTENT_KEY_12345", None)
            with pytest.raises(ValueError, match="NONEXISTENT_KEY_12345"):
                await validate_semantic(_make_output(), config)

    @pytest.mark.asyncio
    async def test_high_score_passes(self):
        mock_response = _mock_llm_response(score=5, issues=[])
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.dict(os.environ, {"GUARDIAN_LLM_API_KEY": "test-key"}):
            result = await validate_semantic(
                _make_output(), _make_config(min_score=3), http_client=mock_client
            )

        assert result.passed is True
        assert result.score == 5
        assert result.issues == []
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_low_score_fails(self):
        mock_response = _mock_llm_response(
            score=2, issues=["Output lacks coherence", "Missing key data"]
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.dict(os.environ, {"GUARDIAN_LLM_API_KEY": "test-key"}):
            result = await validate_semantic(
                _make_output(), _make_config(min_score=3), http_client=mock_client
            )

        assert result.passed is False
        assert result.score == 2
        assert len(result.issues) == 2

    @pytest.mark.asyncio
    async def test_uses_correct_endpoint_and_model(self):
        mock_response = _mock_llm_response(score=4, issues=[])
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        config = _make_config(
            model="claude-3-haiku",
            api_base="https://custom.api.com/v1/",
        )
        with patch.dict(os.environ, {"GUARDIAN_LLM_API_KEY": "key-123"}):
            await validate_semantic(_make_output(), config, http_client=mock_client)

        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://custom.api.com/v1/chat/completions"
        payload = call_args[1]["json"]
        assert payload["model"] == "claude-3-haiku"
        assert call_args[1]["headers"]["Authorization"] == "Bearer key-123"

    @pytest.mark.asyncio
    async def test_api_error_propagates(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        error_response = httpx.Response(
            status_code=500,
            request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
        )
        mock_client.post = AsyncMock(return_value=error_response)

        with patch.dict(os.environ, {"GUARDIAN_LLM_API_KEY": "test-key"}):
            with pytest.raises(httpx.HTTPStatusError):
                await validate_semantic(
                    _make_output(), _make_config(), http_client=mock_client
                )

    @pytest.mark.asyncio
    async def test_dict_output_serialized(self):
        mock_response = _mock_llm_response(score=4, issues=[])
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        output = _make_output({"key": "value", "count": 42})
        with patch.dict(os.environ, {"GUARDIAN_LLM_API_KEY": "test-key"}):
            result = await validate_semantic(output, _make_config(), http_client=mock_client)

        # Verify the prompt contains the serialized dict
        call_args = mock_client.post.call_args
        user_msg = call_args[1]["json"]["messages"][1]["content"]
        assert "key" in user_msg
        assert "value" in user_msg
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_custom_api_key_env(self):
        mock_response = _mock_llm_response(score=4, issues=[])
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        config = _make_config(api_key_env="MY_CUSTOM_KEY")
        with patch.dict(os.environ, {"MY_CUSTOM_KEY": "custom-secret"}):
            await validate_semantic(_make_output(), config, http_client=mock_client)

        headers = mock_client.post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer custom-secret"

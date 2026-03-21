"""Tests for Guardian node core logic."""
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from guardian.core.config import (
    ActionConfig,
    GuardianConfig,
    SemanticCheckConfig,
    StructuralCheckConfig,
)
from guardian.core.guardian_node import (
    GuardianDecision,
    evaluate,
    evaluate_async,
)
from guardian.core.step import StepOutput


def _make_output(data: str | dict = "valid output data here") -> StepOutput:
    return StepOutput(step_name="test_step", output_data=data)


def _make_config(
    required_fields: list[str] | None = None,
    on_structural_fail: str = "abort",
    max_retries: int = 2,
    retry_hint: str | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
) -> GuardianConfig:
    return GuardianConfig(
        structural=StructuralCheckConfig(
            required_fields=required_fields or [],
            min_length=min_length,
            max_length=max_length,
        ),
        actions=ActionConfig(
            on_structural_fail=on_structural_fail,
            max_retries=max_retries,
            retry_hint=retry_hint,
        ),
    )


def _mock_llm_response(score: int, issues: list[str]) -> httpx.Response:
    body = {
        "choices": [
            {"message": {"content": json.dumps({"score": score, "issues": issues})}}
        ]
    }
    return httpx.Response(
        status_code=200,
        json=body,
        request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
    )


class TestGuardianDecision:
    """Tests for GuardianDecision dataclass."""

    def test_pass_decision(self):
        d = GuardianDecision(action="pass", issues=[], score=1.0)
        assert d.action == "pass"
        assert d.score == 1.0
        assert d.semantic_score is None

    def test_retry_decision(self):
        d = GuardianDecision(
            action="retry",
            issues=["bad field"],
            score=0.0,
            retry_hint="fix it",
            semantic_score=2,
        )
        assert d.retry_hint == "fix it"
        assert d.semantic_score == 2


class TestEvaluatePass:
    """Tests for outputs that should pass."""

    def test_no_checks_configured(self):
        output = _make_output()
        config = _make_config()
        decision = evaluate(output, config)
        assert decision.action == "pass"
        assert decision.issues == []
        assert decision.score == 1.0

    def test_required_fields_present(self):
        output = _make_output({"name": "x", "value": 42})
        config = _make_config(required_fields=["name", "value"])
        decision = evaluate(output, config)
        assert decision.action == "pass"

    def test_length_within_bounds(self):
        output = _make_output("a" * 50)
        config = _make_config(min_length=10, max_length=100)
        decision = evaluate(output, config)
        assert decision.action == "pass"


class TestEvaluateRetry:
    """Tests for outputs that trigger retry."""

    def test_missing_fields_retry(self):
        output = _make_output({"name": "x"})
        config = _make_config(
            required_fields=["name", "value"],
            on_structural_fail="retry",
        )
        decision = evaluate(output, config)
        assert decision.action == "retry"
        assert any("value" in i for i in decision.issues)
        assert decision.score < 1.0

    def test_retry_includes_hint(self):
        output = _make_output("short")
        config = _make_config(
            min_length=100,
            on_structural_fail="retry",
            retry_hint="Make the output longer",
        )
        decision = evaluate(output, config)
        assert decision.action == "retry"
        assert decision.retry_hint == "Make the output longer"

    def test_retry_count_tracking(self):
        output = _make_output("short")
        config = _make_config(min_length=100, on_structural_fail="retry", max_retries=3)
        d1 = evaluate(output, config, attempt=1)
        assert d1.action == "retry"
        d2 = evaluate(output, config, attempt=3)
        assert d2.action == "abort"


class TestEvaluateAbort:
    """Tests for outputs that trigger abort."""

    def test_structural_fail_abort(self):
        output = _make_output({"name": "x"})
        config = _make_config(
            required_fields=["name", "missing_field"],
            on_structural_fail="abort",
        )
        decision = evaluate(output, config)
        assert decision.action == "abort"

    def test_retry_exhausted_becomes_abort(self):
        output = _make_output("x")
        config = _make_config(
            min_length=1000,
            on_structural_fail="retry",
            max_retries=2,
        )
        decision = evaluate(output, config, attempt=2)
        assert decision.action == "abort"


class TestEvaluateAlert:
    """Tests for outputs that trigger alert."""

    def test_structural_fail_alert(self):
        output = _make_output("short")
        config = _make_config(
            min_length=1000,
            on_structural_fail="alert",
        )
        decision = evaluate(output, config)
        assert decision.action == "alert"

    def test_alert_still_reports_issues(self):
        output = _make_output("x")
        config = _make_config(
            min_length=100,
            on_structural_fail="alert",
        )
        decision = evaluate(output, config)
        assert len(decision.issues) > 0


class TestEvaluatePassthrough:
    """Tests for passthrough action."""

    def test_structural_fail_passthrough(self):
        output = _make_output("short")
        config = _make_config(
            min_length=1000,
            on_structural_fail="passthrough",
        )
        decision = evaluate(output, config)
        assert decision.action == "passthrough"
        assert len(decision.issues) > 0
        assert decision.score < 1.0


class TestScoreCalculation:
    """Tests for score calculation logic."""

    def test_perfect_score_on_pass(self):
        output = _make_output({"a": 1, "b": 2})
        config = _make_config(required_fields=["a", "b"])
        decision = evaluate(output, config)
        assert decision.score == 1.0

    def test_partial_score_on_some_failures(self):
        output = _make_output({"a": 1})
        config = _make_config(
            required_fields=["a", "b", "c"],
            min_length=5,
            on_structural_fail="alert",
        )
        decision = evaluate(output, config)
        assert 0.0 < decision.score < 1.0

    def test_low_score_on_multiple_failures(self):
        output = _make_output("x")
        config = _make_config(
            required_fields=["a", "b", "c", "d"],
            min_length=1000,
            on_structural_fail="alert",
        )
        decision = evaluate(output, config)
        assert decision.score < 1.0
        assert len(decision.issues) >= 2


# -- Semantic integration tests (async) --

class TestEvaluateAsyncSemanticPass:
    """Tests for semantic evaluation that passes."""

    @pytest.mark.asyncio
    async def test_semantic_pass_high_score(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_mock_llm_response(5, [])
        )

        import os
        from unittest.mock import patch

        config = GuardianConfig(
            semantic=SemanticCheckConfig(
                enabled=True,
                model="test-model",
                criteria=["Is coherent"],
                min_score=3,
                api_base="https://api.example.com/v1",
                api_key_env="TEST_LLM_KEY",
            ),
        )
        output = _make_output({"data": "valid"})

        with patch.dict(os.environ, {"TEST_LLM_KEY": "fake-key"}):
            decision = await evaluate_async(
                output, config, http_client=mock_client
            )

        assert decision.action == "pass"
        assert decision.semantic_score == 5
        assert decision.score > 0.5

    @pytest.mark.asyncio
    async def test_semantic_disabled_skips(self):
        config = GuardianConfig(
            semantic=SemanticCheckConfig(enabled=False),
        )
        decision = await evaluate_async(_make_output(), config)
        assert decision.action == "pass"
        assert decision.semantic_score is None


class TestEvaluateAsyncSemanticFail:
    """Tests for semantic evaluation that fails."""

    @pytest.mark.asyncio
    async def test_low_semantic_score_triggers_action(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_mock_llm_response(2, ["Output is incoherent", "Missing data"])
        )

        import os
        from unittest.mock import patch

        config = GuardianConfig(
            semantic=SemanticCheckConfig(
                enabled=True,
                model="test-model",
                criteria=["Is coherent", "Has data"],
                min_score=3,
                api_base="https://api.example.com/v1",
                api_key_env="TEST_LLM_KEY",
            ),
            actions=ActionConfig(on_semantic_low="alert"),
        )
        output = _make_output("some text that passes structural")

        with patch.dict(os.environ, {"TEST_LLM_KEY": "fake-key"}):
            decision = await evaluate_async(
                output, config, http_client=mock_client
            )

        assert decision.action == "alert"
        assert decision.semantic_score == 2
        assert len(decision.issues) == 2
        assert "incoherent" in decision.issues[0].lower()

    @pytest.mark.asyncio
    async def test_semantic_retry_with_hint(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_mock_llm_response(1, ["Completely irrelevant"])
        )

        import os
        from unittest.mock import patch

        config = GuardianConfig(
            semantic=SemanticCheckConfig(
                enabled=True,
                model="test-model",
                criteria=["Is relevant"],
                min_score=3,
                api_base="https://api.example.com/v1",
                api_key_env="TEST_LLM_KEY",
            ),
            actions=ActionConfig(
                on_semantic_low="retry",
                retry_hint="Make the output more relevant",
                max_retries=3,
            ),
        )
        output = _make_output("irrelevant text")

        with patch.dict(os.environ, {"TEST_LLM_KEY": "fake-key"}):
            decision = await evaluate_async(
                output, config, attempt=1, http_client=mock_client
            )

        assert decision.action == "retry"
        assert decision.retry_hint == "Make the output more relevant"

    @pytest.mark.asyncio
    async def test_semantic_retry_exhausted(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_mock_llm_response(1, ["Still bad"])
        )

        import os
        from unittest.mock import patch

        config = GuardianConfig(
            semantic=SemanticCheckConfig(
                enabled=True,
                model="test-model",
                criteria=["Is good"],
                min_score=3,
                api_base="https://api.example.com/v1",
                api_key_env="TEST_LLM_KEY",
            ),
            actions=ActionConfig(on_semantic_low="retry", max_retries=2),
        )

        with patch.dict(os.environ, {"TEST_LLM_KEY": "fake-key"}):
            decision = await evaluate_async(
                _make_output(), config, attempt=2, http_client=mock_client
            )

        assert decision.action == "abort"


class TestEvaluateAsyncStructuralBlocksSemantic:
    """Tests that structural failure skips semantic evaluation."""

    @pytest.mark.asyncio
    async def test_structural_fail_no_llm_call(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock()

        config = GuardianConfig(
            structural=StructuralCheckConfig(required_fields=["missing"]),
            semantic=SemanticCheckConfig(
                enabled=True,
                criteria=["Something"],
                api_key_env="TEST_LLM_KEY",
            ),
            actions=ActionConfig(on_structural_fail="abort"),
        )
        output = _make_output({"other": "field"})

        decision = await evaluate_async(
            output, config, http_client=mock_client
        )

        assert decision.action == "abort"
        mock_client.post.assert_not_called()
        assert decision.semantic_score is None


class TestCombinedScore:
    """Tests for combined structural + semantic scoring."""

    @pytest.mark.asyncio
    async def test_combined_score_weights(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        # Semantic score 5 → normalized to 1.0
        mock_client.post = AsyncMock(
            return_value=_mock_llm_response(5, [])
        )

        import os
        from unittest.mock import patch

        config = GuardianConfig(
            semantic=SemanticCheckConfig(
                enabled=True,
                criteria=["Good"],
                min_score=3,
                api_base="https://api.example.com/v1",
                api_key_env="TEST_LLM_KEY",
            ),
        )

        with patch.dict(os.environ, {"TEST_LLM_KEY": "fake-key"}):
            decision = await evaluate_async(
                _make_output(), config, http_client=mock_client
            )

        # struct=1.0*0.4 + sem=1.0*0.6 = 1.0
        assert decision.score == 1.0

    @pytest.mark.asyncio
    async def test_combined_score_low_semantic(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        # Semantic score 1 → normalized to 0.0
        mock_client.post = AsyncMock(
            return_value=_mock_llm_response(1, ["Terrible"])
        )

        import os
        from unittest.mock import patch

        config = GuardianConfig(
            semantic=SemanticCheckConfig(
                enabled=True,
                criteria=["Good"],
                min_score=3,
                api_base="https://api.example.com/v1",
                api_key_env="TEST_LLM_KEY",
            ),
            actions=ActionConfig(on_semantic_low="alert"),
        )

        with patch.dict(os.environ, {"TEST_LLM_KEY": "fake-key"}):
            decision = await evaluate_async(
                _make_output(), config, http_client=mock_client
            )

        # struct=1.0*0.4 + sem=0.0*0.6 = 0.4
        assert decision.score == 0.4
        assert decision.action == "alert"

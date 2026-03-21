"""Tests for Guardian node core logic."""
import pytest

from guardian.core.config import (
    ActionConfig,
    GuardianConfig,
    StructuralCheckConfig,
)
from guardian.core.guardian_node import GuardianDecision, evaluate
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


class TestGuardianDecision:
    """Tests for GuardianDecision dataclass."""

    def test_pass_decision(self):
        d = GuardianDecision(action="pass", issues=[], score=1.0)
        assert d.action == "pass"
        assert d.score == 1.0

    def test_retry_decision(self):
        d = GuardianDecision(
            action="retry",
            issues=["bad field"],
            score=0.0,
            retry_hint="fix it",
        )
        assert d.retry_hint == "fix it"


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
        # First attempt
        d1 = evaluate(output, config, attempt=1)
        assert d1.action == "retry"
        # At max retries, should abort
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
        # 5 issues (not JSON + 4 missing fields... actually "not JSON object" + "too short")
        # Many issues should push score well below 1.0
        assert decision.score < 1.0
        assert len(decision.issues) >= 2

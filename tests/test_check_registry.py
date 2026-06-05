"""Tests for the structural check registry (E2: pluggable dispatch).

Verifies that checks register by name, dispatch in order, self-skip when
unconfigured, and that the advisory ``flag_type`` aggregates correctly
(a suspicion never masks a real hard failure).
"""
import pytest

from guardian.core.config import StructuralCheckConfig
from guardian.core.step import StepOutput
from guardian.validators import structural
from guardian.validators.structural import (
    FLAG_STANDARD,
    FLAG_SUSPICION,
    CheckContext,
    CheckOutcome,
    list_structural_checks,
    register_structural_check,
    validate_structural,
)


@pytest.fixture
def preserve_registry():
    """Snapshot and restore the global check registry around a test."""
    saved = dict(structural._STRUCTURAL_CHECKS)
    try:
        yield
    finally:
        structural._STRUCTURAL_CHECKS.clear()
        structural._STRUCTURAL_CHECKS.update(saved)


def test_builtin_checks_registered_in_order():
    assert list_structural_checks() == [
        "json_schema",
        "required_fields",
        "length",
        "language",
        "reverse_calc",
    ]


def test_custom_check_dispatched(preserve_registry):
    @register_structural_check("always_fail")
    def _c(ctx: CheckContext) -> CheckOutcome:
        return CheckOutcome(issues=["custom boom"])

    out = StepOutput(step_name="s", output_data={"a": 1})
    result = validate_structural(out, StructuralCheckConfig())
    assert result.passed is False
    assert "custom boom" in result.issues


def test_custom_check_self_skip(preserve_registry):
    @register_structural_check("noop")
    def _c(ctx: CheckContext) -> CheckOutcome:
        return CheckOutcome()  # never contributes issues

    out = StepOutput(step_name="s", output_data="anything")
    result = validate_structural(out, StructuralCheckConfig())
    assert result.passed is True
    assert result.flag_type == FLAG_STANDARD


def test_suspicion_flag_propagates(preserve_registry):
    @register_structural_check("suspect")
    def _c(ctx: CheckContext) -> CheckOutcome:
        return CheckOutcome(
            issues=["looks reverse-calculated"], flag_type=FLAG_SUSPICION
        )

    out = StepOutput(step_name="s", output_data={"a": 1})
    result = validate_structural(out, StructuralCheckConfig())
    assert result.passed is False
    assert result.flag_type == FLAG_SUSPICION


def test_hard_fail_not_masked_by_suspicion(preserve_registry):
    @register_structural_check("suspect")
    def _suspect(ctx: CheckContext) -> CheckOutcome:
        return CheckOutcome(issues=["suspicion"], flag_type=FLAG_SUSPICION)

    # required_fields on a non-dict output is a standard hard failure
    out = StepOutput(step_name="s", output_data="plain text")
    config = StructuralCheckConfig(required_fields=["data"])
    result = validate_structural(out, config)
    assert result.passed is False
    # A standard hard-fail is present → aggregate must stay "standard".
    assert result.flag_type == FLAG_STANDARD


def test_reader_context_threaded(preserve_registry):
    seen = {}

    @register_structural_check("ctx_probe")
    def _c(ctx: CheckContext) -> CheckOutcome:
        seen["reader"] = ctx.reader
        seen["pipeline_name"] = ctx.pipeline_name
        seen["step_name"] = ctx.step_name
        return CheckOutcome()

    out = StepOutput(step_name="s", output_data={"a": 1})
    validate_structural(
        out,
        StructuralCheckConfig(),
        reader="R",
        pipeline_name="P",
        step_name="S",
    )
    assert seen == {"reader": "R", "pipeline_name": "P", "step_name": "S"}

"""Guardian node — core checkpoint logic.

Receives a step output and guardian configuration, runs structural
validation, and produces a decision: pass, retry, abort, alert, or passthrough.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from guardian.core.config import GuardianConfig
from guardian.core.step import StepOutput
from guardian.validators.structural import StructuralResult, validate_structural


@dataclass
class GuardianDecision:
    """The outcome of a Guardian evaluation.

    Attributes:
        action: Decided action — 'pass', 'retry', 'abort', 'alert', or 'passthrough'.
        issues: List of human-readable issue descriptions.
        score: Quality score from 0.0 (all checks failed) to 1.0 (all passed).
        retry_hint: Optional hint message for retry attempts.
    """

    action: str
    issues: list[str] = field(default_factory=list)
    score: float = 1.0
    retry_hint: str | None = None


def evaluate(
    output: StepOutput,
    config: GuardianConfig,
    attempt: int = 1,
) -> GuardianDecision:
    """Evaluate a step output against its guardian configuration.

    Args:
        output: The step output to evaluate.
        config: Guardian configuration for this step.
        attempt: Current attempt number (1-based). Used to determine
                 whether retries are exhausted.

    Returns:
        A GuardianDecision with the action to take.
    """
    structural_result = validate_structural(output, config.structural)
    score = _compute_score(structural_result)

    if structural_result.passed:
        return GuardianDecision(action="pass", issues=[], score=score)

    action = _resolve_action(config, structural_result, attempt)
    retry_hint = config.actions.retry_hint if action == "retry" else None

    return GuardianDecision(
        action=action,
        issues=structural_result.issues,
        score=score,
        retry_hint=retry_hint,
    )


def _resolve_action(
    config: GuardianConfig,
    result: StructuralResult,
    attempt: int,
) -> str:
    """Determine the action to take based on config and attempt count.

    If the configured action is 'retry' but max_retries has been reached,
    escalate to 'abort'.
    """
    action = config.actions.on_structural_fail

    if action == "retry" and attempt >= config.actions.max_retries:
        return "abort"

    return action


def _compute_score(result: StructuralResult) -> float:
    """Compute a quality score based on structural validation results.

    Returns 1.0 if all checks passed, otherwise a score inversely
    proportional to the number of issues found.
    """
    if result.passed:
        return 1.0

    issue_count = len(result.issues)
    # Simple scoring: each issue reduces the score
    # Cap at 5 issues for scoring purposes to avoid negative scores
    penalty = min(issue_count, 5) / 5.0
    return round(max(1.0 - penalty, 0.0), 2)

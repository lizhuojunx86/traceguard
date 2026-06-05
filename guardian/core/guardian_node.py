"""Guardian node — core checkpoint logic.

Receives a step output and guardian configuration, runs structural
validation and (optionally) semantic evaluation, then produces a
decision: pass, retry, abort, alert, or passthrough.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
from dataclasses import dataclass, field

import httpx

from guardian.core.config import GuardianConfig
from guardian.core.step import StepOutput
from guardian.validators.semantic import SemanticResult, validate_semantic
from guardian.validators.structural import StructuralResult, validate_structural

logger = logging.getLogger(__name__)

# All network-related exceptions to catch for graceful degradation
_NETWORK_ERRORS = (
    ValueError,
    httpx.HTTPStatusError,
    httpx.ProxyError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    ssl.SSLError,
    OSError,
)


@dataclass
class GuardianDecision:
    """The outcome of a Guardian evaluation.

    Attributes:
        action: Decided action — 'pass', 'retry', 'abort', 'alert', or 'passthrough'.
        issues: List of human-readable issue descriptions.
        score: Quality score from 0.0 (all checks failed) to 1.0 (all passed).
        retry_hint: Optional hint message for retry attempts.
        semantic_score: LLM-assigned semantic score (1-5), or None if not evaluated.
        semantic_status: Status of semantic evaluation ('evaluated', 'skipped ...', or None).
        flag_type: Audit-flag class — "standard" or "suspicion" (advisory).
    """

    action: str
    issues: list[str] = field(default_factory=list)
    score: float = 1.0
    retry_hint: str | None = None
    semantic_score: int | None = None
    semantic_status: str | None = None
    flag_type: str = "standard"


def evaluate(
    output: StepOutput,
    config: GuardianConfig,
    attempt: int = 1,
    *,
    reader: object | None = None,
    pipeline_name: str | None = None,
    step_name: str | None = None,
) -> GuardianDecision:
    """Evaluate a step output (sync wrapper).

    Runs structural checks synchronously. If semantic evaluation is enabled,
    runs it via asyncio.

    Args:
        output: The step output to evaluate.
        config: Guardian configuration for this step.
        attempt: Current attempt number (1-based).
        reader: Optional TraceReader for history-aware structural checks.
        pipeline_name: Pipeline name (for history-aware checks).
        step_name: Step name (for history-aware checks).

    Returns:
        A GuardianDecision with the action to take.
    """
    structural_result = validate_structural(
        output,
        config.structural,
        reader=reader,
        pipeline_name=pipeline_name,
        step_name=step_name,
    )

    # If structural checks fail, don't bother with semantic evaluation
    if not structural_result.passed:
        score = _compute_score(structural_result, semantic_result=None)
        action = _resolve_action(
            config.actions.on_structural_fail, config, attempt
        )
        retry_hint = config.actions.retry_hint if action == "retry" else None
        return GuardianDecision(
            action=action,
            issues=structural_result.issues,
            score=score,
            retry_hint=retry_hint,
            flag_type=structural_result.flag_type,
        )

    # Structural passed — run semantic if enabled
    semantic_result = None
    semantic_status = None
    if config.semantic.enabled and config.semantic.criteria:
        try:
            semantic_result = asyncio.run(
                validate_semantic(output, config.semantic)
            )
        except RuntimeError:
            # Already in an async event loop — use the running loop
            loop = asyncio.get_event_loop()
            semantic_result = loop.run_until_complete(
                validate_semantic(output, config.semantic)
            )
        except _NETWORK_ERRORS as e:
            logger.warning("Semantic evaluation skipped: %s", e)
            semantic_result = None
            semantic_status = f"skipped ({e})"

    # Detect DEGRADED skip (validate_semantic returns pass with skip marker)
    if semantic_result and semantic_result.raw_response.startswith("skipped:"):
        semantic_status = "skipped (no LLM available)"
        semantic_result = None  # treat as not evaluated for scoring

    if semantic_result is not None and semantic_status is None:
        semantic_status = "evaluated"

    score = _compute_score(structural_result, semantic_result)

    if semantic_result and not semantic_result.passed:
        action = _resolve_action(
            config.actions.on_semantic_low, config, attempt
        )
        retry_hint = config.actions.retry_hint if action == "retry" else None
        return GuardianDecision(
            action=action,
            issues=semantic_result.issues,
            score=score,
            retry_hint=retry_hint,
            semantic_score=semantic_result.score,
            semantic_status=semantic_status,
        )

    return GuardianDecision(
        action="pass",
        issues=[],
        score=score,
        semantic_score=semantic_result.score if semantic_result else None,
        semantic_status=semantic_status,
    )


async def evaluate_async(
    output: StepOutput,
    config: GuardianConfig,
    attempt: int = 1,
    http_client: httpx.AsyncClient | None = None,
    *,
    reader: object | None = None,
    pipeline_name: str | None = None,
    step_name: str | None = None,
) -> GuardianDecision:
    """Evaluate a step output (async version).

    Preferred in async contexts. Runs structural checks synchronously
    then awaits semantic evaluation if enabled.

    Args:
        output: The step output to evaluate.
        config: Guardian configuration for this step.
        attempt: Current attempt number (1-based).
        http_client: Optional shared httpx client for LLM calls.
        reader: Optional TraceReader for history-aware structural checks.
        pipeline_name: Pipeline name (for history-aware checks).
        step_name: Step name (for history-aware checks).

    Returns:
        A GuardianDecision with the action to take.
    """
    structural_result = validate_structural(
        output,
        config.structural,
        reader=reader,
        pipeline_name=pipeline_name,
        step_name=step_name,
    )

    if not structural_result.passed:
        score = _compute_score(structural_result, semantic_result=None)
        action = _resolve_action(
            config.actions.on_structural_fail, config, attempt
        )
        retry_hint = config.actions.retry_hint if action == "retry" else None
        return GuardianDecision(
            action=action,
            issues=structural_result.issues,
            score=score,
            retry_hint=retry_hint,
            flag_type=structural_result.flag_type,
        )

    semantic_result = None
    semantic_status = None
    if config.semantic.enabled and config.semantic.criteria:
        try:
            semantic_result = await validate_semantic(
                output, config.semantic, http_client=http_client
            )
        except _NETWORK_ERRORS as e:
            logger.warning("Semantic evaluation skipped: %s", e)
            semantic_result = None
            semantic_status = f"skipped ({e})"

    if semantic_result and semantic_result.raw_response.startswith("skipped:"):
        semantic_status = "skipped (no LLM available)"
        semantic_result = None

    if semantic_result is not None and semantic_status is None:
        semantic_status = "evaluated"

    score = _compute_score(structural_result, semantic_result)

    if semantic_result and not semantic_result.passed:
        action = _resolve_action(
            config.actions.on_semantic_low, config, attempt
        )
        retry_hint = config.actions.retry_hint if action == "retry" else None
        return GuardianDecision(
            action=action,
            issues=semantic_result.issues,
            score=score,
            retry_hint=retry_hint,
            semantic_score=semantic_result.score,
            semantic_status=semantic_status,
        )

    return GuardianDecision(
        action="pass",
        issues=[],
        score=score,
        semantic_score=semantic_result.score if semantic_result else None,
        semantic_status=semantic_status,
    )


def _resolve_action(
    configured_action: str,
    config: GuardianConfig,
    attempt: int,
) -> str:
    """Determine the action to take based on config and attempt count.

    If the configured action is 'retry' but max_retries has been reached,
    escalate to 'abort'.
    """
    if configured_action == "retry" and attempt >= config.actions.max_retries:
        return "abort"
    return configured_action


def _compute_score(
    structural: StructuralResult,
    semantic_result: SemanticResult | None,
) -> float:
    """Compute a combined quality score.

    Structural score: 0.0-1.0 based on issue count.
    Semantic score: normalized from 1-5 to 0.0-1.0.
    Combined: weighted average (structural 40%, semantic 60%) when both present.

    Returns 1.0 if all checks passed with no semantic evaluation.
    """
    # Structural score
    if structural.passed:
        struct_score = 1.0
    else:
        issue_count = len(structural.issues)
        penalty = min(issue_count, 5) / 5.0
        struct_score = max(1.0 - penalty, 0.0)

    if semantic_result is None:
        return round(struct_score, 2)

    # Semantic score: normalize 1-5 to 0.0-1.0
    sem_score = (semantic_result.score - 1) / 4.0

    # Weighted combination
    combined = struct_score * 0.4 + sem_score * 0.6
    return round(combined, 2)

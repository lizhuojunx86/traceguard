"""Drift detector for pipeline quality monitoring.

Compares recent evaluation metrics against a baseline period to detect
quality degradation or improvement trends.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field

from guardian.store.reader import TraceReader

logger = logging.getLogger(__name__)

# Default thresholds
SCORE_DROP_THRESHOLD = 0.15   # Flag if avg score drops by this much
PASS_RATE_DROP_THRESHOLD = 0.20  # Flag if pass rate drops by this much


@dataclass
class DriftResult:
    """Result of drift analysis for a single step.

    Attributes:
        pipeline_name: Pipeline name.
        step_name: Step name.
        drifted: Whether quality drift was detected.
        signals: List of human-readable drift signals.
        baseline_avg_score: Average score in baseline period.
        recent_avg_score: Average score in recent period.
        baseline_pass_rate: Pass rate in baseline period.
        recent_pass_rate: Pass rate in recent period.
        trend: Overall trend direction ('stable', 'degrading', 'improving').
    """

    pipeline_name: str
    step_name: str
    drifted: bool = False
    signals: list[str] = field(default_factory=list)
    baseline_avg_score: float | None = None
    recent_avg_score: float | None = None
    baseline_pass_rate: float | None = None
    recent_pass_rate: float | None = None
    trend: str = "stable"


@dataclass
class PipelineDriftReport:
    """Drift report for an entire pipeline.

    Attributes:
        pipeline_name: Pipeline name.
        has_drift: Whether any step shows drift.
        step_results: Per-step drift results.
        summary: Human-readable summary.
    """

    pipeline_name: str
    has_drift: bool = False
    step_results: list[DriftResult] = field(default_factory=list)
    summary: str = ""


def detect_drift(
    reader: TraceReader,
    pipeline_name: str,
    recent_days: int = 3,
    baseline_days: int = 14,
    score_threshold: float = SCORE_DROP_THRESHOLD,
    pass_rate_threshold: float = PASS_RATE_DROP_THRESHOLD,
) -> PipelineDriftReport:
    """Detect quality drift across all steps of a pipeline.

    Compares the most recent N days against the full baseline period.
    Drift is flagged when avg score or pass rate drops below threshold.

    Args:
        reader: TraceReader instance.
        pipeline_name: Pipeline to analyze.
        recent_days: Number of recent days to treat as "current".
        baseline_days: Total lookback period (includes recent_days).
        score_threshold: Minimum score drop to flag as drift.
        pass_rate_threshold: Minimum pass rate drop to flag as drift.

    Returns:
        PipelineDriftReport with per-step results.
    """
    # Get all daily scores for the baseline period
    traces = reader.query_traces(pipeline_name=pipeline_name, days=baseline_days, limit=10000)

    if not traces:
        return PipelineDriftReport(
            pipeline_name=pipeline_name,
            summary="No traces found for drift analysis.",
        )

    # Discover steps
    step_names = sorted(set(t["step_name"] for t in traces))

    report = PipelineDriftReport(pipeline_name=pipeline_name)

    for step_name in step_names:
        daily = reader.get_daily_scores(pipeline_name, step_name, days=baseline_days)

        if len(daily) < 2:
            result = DriftResult(
                pipeline_name=pipeline_name,
                step_name=step_name,
                signals=["Insufficient data for drift analysis"],
            )
            report.step_results.append(result)
            continue

        result = _analyze_step_drift(
            pipeline_name=pipeline_name,
            step_name=step_name,
            daily_scores=daily,
            recent_days=recent_days,
            score_threshold=score_threshold,
            pass_rate_threshold=pass_rate_threshold,
        )
        report.step_results.append(result)

    report.has_drift = any(r.drifted for r in report.step_results)
    drifted_steps = [r.step_name for r in report.step_results if r.drifted]
    if drifted_steps:
        report.summary = f"Drift detected in {len(drifted_steps)} step(s): {', '.join(drifted_steps)}"
    else:
        report.summary = "No drift detected across all steps."

    return report


def _analyze_step_drift(
    pipeline_name: str,
    step_name: str,
    daily_scores: list[dict],
    recent_days: int,
    score_threshold: float,
    pass_rate_threshold: float,
) -> DriftResult:
    """Analyze drift for a single step using daily aggregated data."""
    # Split into baseline (older) and recent
    total = len(daily_scores)
    split_idx = max(total - recent_days, 1)
    baseline = daily_scores[:split_idx]
    recent = daily_scores[split_idx:]

    if not baseline or not recent:
        return DriftResult(
            pipeline_name=pipeline_name,
            step_name=step_name,
            signals=["Insufficient data to split baseline vs recent"],
        )

    baseline_scores = [d["avg_score"] for d in baseline]
    recent_scores = [d["avg_score"] for d in recent]
    baseline_pass_rates = [d["pass_rate"] for d in baseline]
    recent_pass_rates = [d["pass_rate"] for d in recent]

    b_avg_score = statistics.mean(baseline_scores)
    r_avg_score = statistics.mean(recent_scores)
    b_pass_rate = statistics.mean(baseline_pass_rates)
    r_pass_rate = statistics.mean(recent_pass_rates)

    result = DriftResult(
        pipeline_name=pipeline_name,
        step_name=step_name,
        baseline_avg_score=round(b_avg_score, 4),
        recent_avg_score=round(r_avg_score, 4),
        baseline_pass_rate=round(b_pass_rate, 4),
        recent_pass_rate=round(r_pass_rate, 4),
    )

    signals: list[str] = []

    # Check score drift
    score_delta = b_avg_score - r_avg_score
    if score_delta > score_threshold:
        signals.append(
            f"Average score dropped: {b_avg_score:.3f} → {r_avg_score:.3f} "
            f"(Δ={score_delta:+.3f})"
        )

    # Check pass rate drift
    pass_delta = b_pass_rate - r_pass_rate
    if pass_delta > pass_rate_threshold:
        signals.append(
            f"Pass rate dropped: {b_pass_rate:.1%} → {r_pass_rate:.1%} "
            f"(Δ={pass_delta:+.1%})"
        )

    # Check variance increase (instability)
    if len(recent_scores) >= 2 and len(baseline_scores) >= 2:
        b_stdev = statistics.stdev(baseline_scores)
        r_stdev = statistics.stdev(recent_scores)
        if r_stdev > b_stdev * 2 and r_stdev > 0.05:
            signals.append(
                f"Score variance increased: stdev {b_stdev:.3f} → {r_stdev:.3f}"
            )

    # Determine trend
    if score_delta > score_threshold or pass_delta > pass_rate_threshold:
        result.trend = "degrading"
    elif score_delta < -score_threshold:
        result.trend = "improving"
    else:
        result.trend = "stable"

    result.drifted = len(signals) > 0
    result.signals = signals
    return result

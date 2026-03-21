"""Root cause analyzer for low-scoring pipeline steps.

Extracts failure patterns from eval traces and uses an LLM to
identify common root causes behind recurring issues.
"""
from __future__ import annotations

import json
import logging
import os
from collections import Counter
from dataclasses import dataclass, field

import httpx

from guardian.store.reader import TraceReader

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.openai.com/v1"

ANALYSIS_SYSTEM_PROMPT = """\
You are an expert at diagnosing quality issues in AI pipeline outputs.
You will receive a summary of recurring failure patterns from a pipeline step,
including the most common issues, score distribution, and sample output previews.

Analyze the data and identify the root causes. Be specific and actionable.

You MUST respond with ONLY a valid JSON object in this exact format:
{
  "root_causes": [
    {
      "cause": "<concise description of the root cause>",
      "evidence": "<which patterns/issues support this conclusion>",
      "severity": "<high|medium|low>",
      "frequency": "<what percentage of failures this likely explains>"
    }
  ],
  "summary": "<one paragraph overall diagnosis>"
}

Be concrete. Reference specific issue messages and patterns from the data.\
"""


@dataclass
class FailurePattern:
    """Aggregated failure pattern for a step.

    Attributes:
        pipeline_name: Pipeline name.
        step_name: Step name.
        total_traces: Total number of traces analyzed.
        failed_traces: Number of failed traces.
        failure_rate: Proportion of failures.
        issue_counts: Counter of issue messages.
        avg_score: Average score across all traces.
        score_distribution: Histogram buckets of scores.
        sample_previews: Sample output previews from failed traces.
    """

    pipeline_name: str
    step_name: str
    total_traces: int = 0
    failed_traces: int = 0
    failure_rate: float = 0.0
    issue_counts: dict[str, int] = field(default_factory=dict)
    avg_score: float = 0.0
    score_distribution: dict[str, int] = field(default_factory=dict)
    sample_previews: list[str] = field(default_factory=list)


@dataclass
class RootCauseReport:
    """Result of root cause analysis.

    Attributes:
        pipeline_name: Pipeline name.
        step_name: Step name.
        pattern: Failure pattern used as input.
        root_causes: List of identified root causes.
        summary: Overall diagnosis.
        raw_response: Raw LLM response for debugging.
    """

    pipeline_name: str
    step_name: str
    pattern: FailurePattern
    root_causes: list[dict] = field(default_factory=list)
    summary: str = ""
    raw_response: str = ""


def extract_failure_pattern(
    reader: TraceReader,
    pipeline_name: str,
    step_name: str,
    days: int = 14,
    max_samples: int = 5,
) -> FailurePattern:
    """Extract failure patterns from recent traces.

    Args:
        reader: TraceReader instance.
        pipeline_name: Pipeline name.
        step_name: Step name.
        days: Lookback period in days.
        max_samples: Maximum number of output previews to collect.

    Returns:
        FailurePattern with aggregated failure data.
    """
    traces = reader.query_traces(
        pipeline_name=pipeline_name,
        step_name=step_name,
        days=days,
        limit=1000,
    )

    pattern = FailurePattern(
        pipeline_name=pipeline_name,
        step_name=step_name,
    )

    if not traces:
        return pattern

    pattern.total_traces = len(traces)
    failed = [t for t in traces if not t["passed"]]
    pattern.failed_traces = len(failed)
    pattern.failure_rate = round(len(failed) / len(traces), 4) if traces else 0

    scores = [t["score"] for t in traces]
    pattern.avg_score = round(sum(scores) / len(scores), 4) if scores else 0

    # Score distribution buckets
    buckets = {"0.0-0.2": 0, "0.2-0.4": 0, "0.4-0.6": 0, "0.6-0.8": 0, "0.8-1.0": 0}
    for s in scores:
        if s < 0.2:
            buckets["0.0-0.2"] += 1
        elif s < 0.4:
            buckets["0.2-0.4"] += 1
        elif s < 0.6:
            buckets["0.4-0.6"] += 1
        elif s < 0.8:
            buckets["0.6-0.8"] += 1
        else:
            buckets["0.8-1.0"] += 1
    pattern.score_distribution = buckets

    # Count issues
    issue_counter: Counter[str] = Counter()
    for t in failed:
        issues = t.get("issues", [])
        if isinstance(issues, list):
            for issue in issues:
                issue_counter[issue] += 1
    pattern.issue_counts = dict(issue_counter.most_common(20))

    # Collect sample previews from failures
    for t in failed[:max_samples]:
        preview = t.get("output_preview")
        if preview:
            pattern.sample_previews.append(preview)

    return pattern


def _build_analysis_prompt(pattern: FailurePattern) -> str:
    """Build the user prompt for root cause analysis."""
    top_issues = list(pattern.issue_counts.items())[:10]
    issues_text = "\n".join(
        f"  - \"{issue}\" (occurred {count} times)"
        for issue, count in top_issues
    )

    previews_text = ""
    if pattern.sample_previews:
        previews_text = "\n\n## Sample Failed Outputs\n" + "\n---\n".join(
            f"```\n{p[:300]}\n```" for p in pattern.sample_previews
        )

    dist_text = ", ".join(f"{k}: {v}" for k, v in pattern.score_distribution.items())

    return f"""\
## Step: {pattern.step_name} (Pipeline: {pattern.pipeline_name})

## Statistics
- Total traces: {pattern.total_traces}
- Failed traces: {pattern.failed_traces} ({pattern.failure_rate:.1%})
- Average score: {pattern.avg_score:.3f}
- Score distribution: [{dist_text}]

## Top Issues
{issues_text}
{previews_text}

Analyze the failure patterns and identify root causes."""


def _rule_based_root_causes(pattern: FailurePattern) -> list[dict]:
    """Generate rule-based root cause analysis from failure patterns.

    Uses statistical heuristics when no LLM is available.
    """
    causes = []
    for issue, count in list(pattern.issue_counts.items())[:5]:
        pct = count / pattern.failed_traces if pattern.failed_traces else 0
        severity = "high" if pct > 0.5 else ("medium" if pct > 0.2 else "low")
        causes.append({
            "cause": issue,
            "evidence": f"Occurred in {count}/{pattern.failed_traces} failures ({pct:.0%})",
            "severity": severity,
            "frequency": f"{pct:.0%}",
        })
    return causes


async def analyze_root_causes(
    pattern: FailurePattern,
    model: str = "gpt-4o-mini",
    api_base: str | None = None,
    api_key_env: str = "GUARDIAN_LLM_API_KEY",
    http_client: httpx.AsyncClient | None = None,
) -> RootCauseReport:
    """Analyze failure patterns and identify root causes.

    Uses LLM when available, falls back to rule-based analysis
    when no LLM is reachable (graceful degradation).

    Args:
        pattern: Extracted failure pattern.
        model: LLM model to use.
        api_base: Base URL for OpenAI-compatible API.
        api_key_env: Environment variable name for API key.
        http_client: Optional pre-configured httpx client.

    Returns:
        RootCauseReport with identified root causes.
    """
    from guardian.env import LLMMode, probe_llm_environment

    report = RootCauseReport(
        pipeline_name=pattern.pipeline_name,
        step_name=pattern.step_name,
        pattern=pattern,
    )

    if pattern.failed_traces == 0:
        report.summary = "No failures found — nothing to analyze."
        return report

    # Environment-aware probe
    endpoint = await probe_llm_environment(
        config_api_base=api_base,
        config_api_key_env=api_key_env,
        config_model=model,
        http_client=http_client,
    )

    if endpoint.mode == LLMMode.DEGRADED:
        report.root_causes = _rule_based_root_causes(pattern)
        report.summary = "LLM unavailable — rule-based analysis only."
        return report

    # Resolve endpoint
    actual_base = (endpoint.api_base or DEFAULT_API_BASE).rstrip("/")
    actual_model = endpoint.model or model

    if endpoint.mode == LLMMode.FULL:
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            report.root_causes = _rule_based_root_causes(pattern)
            report.summary = "API key not set — rule-based analysis only."
            return report
    else:
        api_key = os.environ.get(api_key_env, "no-key-needed")

    url = f"{actual_base}/chat/completions"
    payload = {
        "model": actual_model,
        "messages": [
            {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": _build_analysis_prompt(pattern)},
        ],
        "temperature": 0.2,
        "max_tokens": 1024,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    should_close = http_client is None
    client = http_client or httpx.AsyncClient(timeout=60.0)

    try:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()

        body = response.json()
        raw = body["choices"][0]["message"]["content"]
        report.raw_response = raw

        parsed = _parse_analysis(raw)
        report.root_causes = parsed.get("root_causes", [])
        report.summary = parsed.get("summary", "")

    except (
        httpx.HTTPStatusError, httpx.ProxyError, httpx.ConnectError,
        httpx.ConnectTimeout, httpx.ReadTimeout, OSError,
    ) as e:
        logger.warning("Root cause LLM call failed, falling back to rules: %s", e)
        report.root_causes = _rule_based_root_causes(pattern)
        report.summary = f"LLM call failed — rule-based analysis only."

    finally:
        if should_close:
            await client.aclose()

    return report


def _parse_analysis(raw: str) -> dict:
    """Parse the LLM's JSON response."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Failed to parse root cause response: %s", raw[:200])
        return {
            "root_causes": [],
            "summary": "Failed to parse LLM response.",
        }

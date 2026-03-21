"""Suggestion engine for prompt and configuration optimization.

Takes root cause analysis results and the current pipeline configuration,
then generates actionable suggestions for improving pipeline quality.
Suggestions are human-reviewed, never auto-applied.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

import httpx

from guardian.optimizer.root_cause import RootCauseReport

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.openai.com/v1"

SUGGESTION_SYSTEM_PROMPT = """\
You are an expert AI pipeline optimization consultant.
You will receive:
1. A root cause analysis of recurring failures in a pipeline step
2. The current guardian configuration for that step (YAML)
3. The current retry_hint (the prompt correction sent when retries happen)

Your job is to suggest concrete improvements. Focus on:
- Improving the retry_hint to better guide the agent
- Adjusting structural validation thresholds if they are too strict or too lenient
- Adding or modifying evaluation criteria

You MUST respond with ONLY a valid JSON object in this exact format:
{
  "suggestions": [
    {
      "type": "<retry_hint|structural_config|semantic_criteria|action_config>",
      "title": "<short description>",
      "current": "<current value or 'not set'>",
      "proposed": "<proposed new value>",
      "rationale": "<why this change would help, referencing root causes>",
      "expected_impact": "<what improvement to expect>"
    }
  ],
  "overall_strategy": "<one paragraph describing the optimization strategy>"
}

Be specific. Write actual values, not placeholders.
The retry_hint should be a complete, ready-to-use prompt instruction.\
"""


@dataclass
class Suggestion:
    """A single optimization suggestion.

    Attributes:
        type: Category of the suggestion.
        title: Short description.
        current: Current value.
        proposed: Proposed new value.
        rationale: Why this change helps.
        expected_impact: Expected improvement.
    """

    type: str
    title: str
    current: str
    proposed: str
    rationale: str
    expected_impact: str


@dataclass
class SuggestionReport:
    """Collection of optimization suggestions for a step.

    Attributes:
        pipeline_name: Pipeline name.
        step_name: Step name.
        suggestions: List of suggestions.
        overall_strategy: High-level optimization strategy.
        root_cause_summary: Summary from root cause analysis.
        raw_response: Raw LLM response for debugging.
    """

    pipeline_name: str
    step_name: str
    suggestions: list[Suggestion] = field(default_factory=list)
    overall_strategy: str = ""
    root_cause_summary: str = ""
    raw_response: str = ""


def _build_suggestion_prompt(
    root_cause_report: RootCauseReport,
    guardian_config_yaml: str,
    current_retry_hint: str | None,
) -> str:
    """Build the user prompt for the suggestion engine."""
    causes_text = ""
    for i, rc in enumerate(root_cause_report.root_causes, 1):
        causes_text += (
            f"  {i}. [{rc.get('severity', '?')}] {rc.get('cause', 'Unknown')}\n"
            f"     Evidence: {rc.get('evidence', 'N/A')}\n"
            f"     Frequency: {rc.get('frequency', 'N/A')}\n"
        )

    pattern = root_cause_report.pattern
    top_issues = list(pattern.issue_counts.items())[:5]
    issues_text = "\n".join(f"  - \"{iss}\" ({cnt}x)" for iss, cnt in top_issues)

    return f"""\
## Root Cause Analysis
Summary: {root_cause_report.summary}

Identified causes:
{causes_text}

## Failure Statistics
- Failure rate: {pattern.failure_rate:.1%}
- Average score: {pattern.avg_score:.3f}
- Top issues:
{issues_text}

## Current Guardian Configuration
```yaml
{guardian_config_yaml}
```

## Current Retry Hint
```
{current_retry_hint or "(not configured)"}
```

Generate specific, actionable suggestions to reduce the failure rate."""


async def generate_suggestions(
    root_cause_report: RootCauseReport,
    guardian_config_yaml: str,
    current_retry_hint: str | None = None,
    model: str = "gpt-4o-mini",
    api_base: str | None = None,
    api_key_env: str = "GUARDIAN_LLM_API_KEY",
    http_client: httpx.AsyncClient | None = None,
) -> SuggestionReport:
    """Generate optimization suggestions using an LLM.

    Args:
        root_cause_report: Root cause analysis results.
        guardian_config_yaml: Current guardian config as YAML string.
        current_retry_hint: Current retry hint text.
        model: LLM model to use.
        api_base: Base URL for OpenAI-compatible API.
        api_key_env: Environment variable name for API key.
        http_client: Optional pre-configured httpx client.

    Returns:
        SuggestionReport with optimization suggestions.

    Raises:
        ValueError: If API key is not set.
    """
    report = SuggestionReport(
        pipeline_name=root_cause_report.pipeline_name,
        step_name=root_cause_report.step_name,
        root_cause_summary=root_cause_report.summary,
    )

    if not root_cause_report.root_causes:
        report.overall_strategy = "No root causes identified — no suggestions to generate."
        return report

    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        raise ValueError(f"Environment variable '{api_key_env}' is not set")

    base = (api_base or DEFAULT_API_BASE).rstrip("/")
    url = f"{base}/chat/completions"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SUGGESTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _build_suggestion_prompt(
                    root_cause_report, guardian_config_yaml, current_retry_hint
                ),
            },
        ],
        "temperature": 0.3,
        "max_tokens": 2048,
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

        parsed = _parse_suggestions(raw)
        report.suggestions = [
            Suggestion(**s) for s in parsed.get("suggestions", [])
        ]
        report.overall_strategy = parsed.get("overall_strategy", "")

    finally:
        if should_close:
            await client.aclose()

    return report


def _parse_suggestions(raw: str) -> dict:
    """Parse the LLM's JSON response."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Failed to parse suggestion response: %s", raw[:200])
        return {"suggestions": [], "overall_strategy": "Failed to parse LLM response."}


def format_suggestion_report(report: SuggestionReport) -> str:
    """Format a SuggestionReport as human-readable text output.

    Args:
        report: The suggestion report to format.

    Returns:
        Formatted string with diff-style current vs proposed comparisons.
    """
    lines: list[str] = []
    lines.append(f"{'=' * 60}")
    lines.append(f"  OPTIMIZATION SUGGESTIONS")
    lines.append(f"  Pipeline: {report.pipeline_name}")
    lines.append(f"  Step: {report.step_name}")
    lines.append(f"{'=' * 60}")
    lines.append("")

    if report.root_cause_summary:
        lines.append(f"Root Cause Summary:")
        lines.append(f"  {report.root_cause_summary}")
        lines.append("")

    if not report.suggestions:
        lines.append("No suggestions generated.")
        return "\n".join(lines)

    for i, s in enumerate(report.suggestions, 1):
        lines.append(f"--- Suggestion {i}: {s.title} [{s.type}] ---")
        lines.append("")
        lines.append(f"  Current:")
        for cl in s.current.split("\n"):
            lines.append(f"    - {cl}")
        lines.append(f"  Proposed:")
        for pl in s.proposed.split("\n"):
            lines.append(f"    + {pl}")
        lines.append("")
        lines.append(f"  Rationale: {s.rationale}")
        lines.append(f"  Expected Impact: {s.expected_impact}")
        lines.append("")

    if report.overall_strategy:
        lines.append(f"Overall Strategy:")
        lines.append(f"  {report.overall_strategy}")
        lines.append("")

    lines.append(f"{'=' * 60}")
    lines.append("  NOTE: These are suggestions only. Review before applying.")
    lines.append(f"{'=' * 60}")

    return "\n".join(lines)

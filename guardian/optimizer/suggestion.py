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
        type: Category (prompt_change, structural_config, action_config, etc.).
        title: Short description.
        diagnosis: What the data shows (quantified).
        root_cause_hypothesis: Why this is likely happening.
        proposed: Recommended change.
        expected_impact: What improvement to expect.
        alternative: Optional alternative approach.
        current: Current value (for backward compatibility).
        rationale: Legacy field mapped to root_cause_hypothesis.
    """

    type: str
    title: str
    diagnosis: str = ""
    root_cause_hypothesis: str = ""
    proposed: str = ""
    expected_impact: str = ""
    alternative: str = ""
    # Backward-compatible fields
    current: str = ""
    rationale: str = ""


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


def _rule_based_suggestions(
    root_report: RootCauseReport,
    current_retry_hint: str | None,
) -> list[Suggestion]:
    """Generate pattern-specific diagnostic suggestions when no LLM is available.

    Analyzes issue messages to classify failure modes and produce
    actionable, targeted suggestions rather than generic advice.
    """
    suggestions = []
    pattern = root_report.pattern
    if not pattern.issue_counts:
        return suggestions

    all_issues = list(pattern.issue_counts.items())
    total_failed = pattern.failed_traces or 1

    # Classify issues by failure mode
    lang_issues = [(m, c) for m, c in all_issues if "language" in m.lower() or "language mismatch" in m.lower()]
    short_issues = [(m, c) for m, c in all_issues if "too short" in m.lower()]
    long_issues = [(m, c) for m, c in all_issues if "too long" in m.lower()]
    field_issues = [(m, c) for m, c in all_issues if "missing" in m.lower() and "field" in m.lower()]
    schema_issues = [(m, c) for m, c in all_issues if "schema" in m.lower()]
    not_json_issues = [(m, c) for m, c in all_issues if "not a json" in m.lower() or "not valid json" in m.lower()]

    matched = False

    # -- Language mismatch diagnosis --
    if lang_issues:
        matched = True
        total_lang = sum(c for _, c in lang_issues)
        lang_rate = total_lang / total_failed

        # Try to extract actual ratio from message like "only 28% of text matches"
        avg_ratio = _extract_percentage_from_issues([m for m, _ in lang_issues])

        if avg_ratio is not None and avg_ratio < 30:
            severity = "severe"
            hypothesis = (
                "Upstream agent is embedding source-language text verbatim "
                "(e.g. pasting English abstracts into a Chinese report) "
                "rather than paraphrasing in the target language."
            )
            action = (
                'Add to upstream prompt/SOUL: "All content MUST be written in '
                '[target language]. Paraphrase all source material — do NOT '
                'copy-paste text in other languages."'
            )
        else:
            severity = "moderate"
            hypothesis = (
                "Agent output contains partial mixed-language content, "
                "possibly from technical terms or insufficient translation."
            )
            action = (
                'Add to upstream prompt: "Translate all content including '
                'technical terms. Use target-language equivalents or provide '
                'translations in parentheses."'
            )

        ratio_str = f"avg ratio={avg_ratio}%" if avg_ratio is not None else "below threshold"
        suggestions.append(Suggestion(
            type="prompt_change",
            title=f"Language mixing detected in output ({severity})",
            diagnosis=(
                f"{total_lang}/{total_failed} failures are language mismatch "
                f"({lang_rate:.0%} of failures, {ratio_str})"
            ),
            root_cause_hypothesis=hypothesis,
            proposed=action,
            expected_impact="Target-language ratio should increase significantly",
            alternative=(
                "If multilingual citations are intentional, adjust language "
                "check threshold or disable for this step."
            ),
        ))

    # -- Output too short diagnosis --
    if short_issues:
        matched = True
        total_short = sum(c for _, c in short_issues)
        short_rate = total_short / total_failed

        # Extract actual lengths from messages like "Output too short: 45 chars (minimum: 100)"
        lengths = _extract_lengths_from_issues([m for m, _ in short_issues])
        if lengths and len(lengths) >= 2:
            import statistics
            mean_len = statistics.mean(lengths)
            stdev_len = statistics.stdev(lengths) if len(lengths) > 1 else 0
            is_systematic = stdev_len < mean_len * 0.3 if mean_len > 0 else True

            if is_systematic:
                suggestions.append(Suggestion(
                    type="prompt_change",
                    title="Systematic output length deficit",
                    diagnosis=(
                        f"{total_short}/{total_failed} failures are 'too short' "
                        f"({short_rate:.0%}). Lengths cluster around {mean_len:.0f} chars "
                        f"(stdev={stdev_len:.0f}) — systematic, not random."
                    ),
                    root_cause_hypothesis=(
                        "Agent consistently produces output near but below the minimum. "
                        "The upstream prompt likely lacks an explicit length requirement."
                    ),
                    proposed=(
                        "Add to upstream prompt: explicit minimum word/character count. "
                        'E.g. "Your response must be at least [N] characters. '
                        'Verify length before submitting."'
                    ),
                    expected_impact="Shift output length distribution above minimum threshold",
                ))
            else:
                suggestions.append(Suggestion(
                    type="action_config",
                    title="Inconsistent output length (high variance)",
                    diagnosis=(
                        f"{total_short}/{total_failed} failures are 'too short' "
                        f"({short_rate:.0%}). Lengths vary widely (mean={mean_len:.0f}, "
                        f"stdev={stdev_len:.0f}) — quality is unstable."
                    ),
                    root_cause_hypothesis=(
                        "Agent output length varies significantly across inputs. "
                        "Some inputs produce adequate length, others don't."
                    ),
                    proposed=(
                        "Increase max_retries to allow recovery on short outputs. "
                        "Also add format constraints to the upstream prompt."
                    ),
                    expected_impact="Reduce abort rate from length failures",
                ))
        else:
            suggestions.append(Suggestion(
                type="prompt_change",
                title="Output too short",
                diagnosis=f"{total_short}/{total_failed} failures are 'too short' ({short_rate:.0%})",
                root_cause_hypothesis="Agent not producing enough content for this step.",
                proposed="Add explicit minimum length requirement to upstream prompt.",
                expected_impact="Reduce short-output failures",
            ))

    # -- Output too long diagnosis --
    if long_issues:
        matched = True
        total_long = sum(c for _, c in long_issues)
        suggestions.append(Suggestion(
            type="prompt_change",
            title="Output exceeds length limit",
            diagnosis=f"{total_long}/{total_failed} failures are 'too long' ({total_long/total_failed:.0%})",
            root_cause_hypothesis=(
                "Agent is producing overly verbose output, possibly stuck in a "
                "repetition loop or including unnecessary detail."
            ),
            proposed=(
                "Add to upstream prompt: strict output length cap. "
                '"Limit your response to [N] characters maximum. Be concise."'
            ),
            expected_impact="Eliminate over-length failures",
        ))

    # -- Missing fields diagnosis --
    if field_issues:
        matched = True
        total_field = sum(c for _, c in field_issues)
        field_names = _extract_field_names([m for m, _ in field_issues])
        names_str = ", ".join(field_names) if field_names else "required fields"

        suggestions.append(Suggestion(
            type="prompt_change",
            title="Agent omitting required output fields",
            diagnosis=(
                f"{total_field}/{total_failed} failures are missing-field errors "
                f"({total_field/total_failed:.0%}). Missing: {names_str}"
            ),
            root_cause_hypothesis=(
                "Upstream agent does not include all required fields in its JSON output. "
                "The agent prompt may lack a clear output schema specification."
            ),
            proposed=(
                f"Add to upstream prompt: explicit output schema with required fields "
                f"({names_str}). Include a concrete example of the expected JSON structure."
            ),
            expected_impact="Reduce missing-field failures significantly",
        ))

    # -- JSON Schema violation --
    if schema_issues:
        matched = True
        total_schema = sum(c for _, c in schema_issues)
        suggestions.append(Suggestion(
            type="prompt_change",
            title="Output does not match JSON Schema",
            diagnosis=f"{total_schema}/{total_failed} failures are schema violations ({total_schema/total_failed:.0%})",
            root_cause_hypothesis=(
                "Agent output structure doesn't match the expected schema. "
                "Field types, nesting, or enum values may be wrong."
            ),
            proposed=(
                "Include the exact JSON Schema (or a simplified version) in the "
                "upstream prompt so the agent knows the expected structure."
            ),
            expected_impact="Reduce schema-related failures",
        ))

    # -- Not valid JSON --
    if not_json_issues:
        matched = True
        total_nj = sum(c for _, c in not_json_issues)
        suggestions.append(Suggestion(
            type="prompt_change",
            title="Agent output is not valid JSON",
            diagnosis=f"{total_nj}/{total_failed} failures: output is not parseable JSON ({total_nj/total_failed:.0%})",
            root_cause_hypothesis=(
                "Agent is returning plain text, markdown, or malformed JSON "
                "instead of a clean JSON object."
            ),
            proposed=(
                'Add to upstream prompt: "Your response must be ONLY a valid JSON object. '
                'Do not include markdown fences, explanatory text, or any content outside the JSON."'
            ),
            expected_impact="Eliminate JSON parse failures",
        ))

    # -- Generic fallback only if no specific pattern matched --
    if not matched:
        suggestions.append(Suggestion(
            type="action_config",
            title="[generic fallback — no specific pattern detected]",
            diagnosis=(
                f"{pattern.failed_traces}/{pattern.total_traces} failures "
                f"({pattern.failure_rate:.0%}). Top issues: "
                + "; ".join(f'"{m}" ({c}x)' for m, c in all_issues[:3])
            ),
            root_cause_hypothesis="No recognized failure pattern could be matched to a specific diagnosis.",
            proposed="Review the top issues manually and consider adjusting thresholds or retry hints.",
            expected_impact="Depends on manual investigation",
        ))

    return suggestions


def _extract_percentage_from_issues(messages: list[str]) -> int | None:
    """Extract percentage values from language mismatch messages.

    Looks for patterns like 'only 28% of text matches'.
    Returns the average percentage, or None if not found.
    """
    import re

    percentages = []
    for msg in messages:
        match = re.search(r"(\d+)%\s*of\s*text\s*matches", msg)
        if match:
            percentages.append(int(match.group(1)))
    return round(sum(percentages) / len(percentages)) if percentages else None


def _extract_lengths_from_issues(messages: list[str]) -> list[int]:
    """Extract actual output lengths from 'too short' messages.

    Looks for patterns like 'Output too short: 45 chars'.
    """
    import re

    lengths = []
    for msg in messages:
        match = re.search(r"too short:\s*(\d+)\s*chars", msg, re.IGNORECASE)
        if match:
            lengths.append(int(match.group(1)))
    return lengths


def _extract_field_names(messages: list[str]) -> list[str]:
    """Extract field names from 'Missing required field: X' messages."""
    import re

    names = set()
    for msg in messages:
        match = re.search(r"[Mm]issing\s+(?:required\s+)?field:\s*(\w+)", msg)
        if match:
            names.add(match.group(1))
    return sorted(names)


async def generate_suggestions(
    root_cause_report: RootCauseReport,
    guardian_config_yaml: str,
    current_retry_hint: str | None = None,
    model: str = "gpt-4o-mini",
    api_base: str | None = None,
    api_key_env: str = "GUARDIAN_LLM_API_KEY",
    http_client: httpx.AsyncClient | None = None,
) -> SuggestionReport:
    """Generate optimization suggestions.

    Uses LLM when available, falls back to rule-based suggestions
    when no LLM is reachable (graceful degradation).

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
    """
    from guardian.env import LLMMode, probe_llm_environment

    report = SuggestionReport(
        pipeline_name=root_cause_report.pipeline_name,
        step_name=root_cause_report.step_name,
        root_cause_summary=root_cause_report.summary,
    )

    if not root_cause_report.root_causes:
        report.overall_strategy = "No root causes identified — no suggestions to generate."
        return report

    # Environment-aware probe
    endpoint = await probe_llm_environment(
        config_api_base=api_base,
        config_api_key_env=api_key_env,
        config_model=model,
        http_client=http_client,
    )

    if endpoint.mode == LLMMode.DEGRADED:
        report.suggestions = _rule_based_suggestions(root_cause_report, current_retry_hint)
        report.overall_strategy = "LLM unavailable — rule-based suggestions only."
        return report

    # Resolve endpoint
    actual_base = (endpoint.api_base or DEFAULT_API_BASE).rstrip("/")
    actual_model = endpoint.model or model

    if endpoint.mode == LLMMode.FULL:
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            report.suggestions = _rule_based_suggestions(root_cause_report, current_retry_hint)
            report.overall_strategy = "API key not set — rule-based suggestions only."
            return report
    else:
        api_key = os.environ.get(api_key_env, "no-key-needed")

    url = f"{actual_base}/chat/completions"
    payload = {
        "model": actual_model,
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

    except (
        httpx.HTTPStatusError, httpx.ProxyError, httpx.ConnectError,
        httpx.ConnectTimeout, httpx.ReadTimeout, OSError,
    ) as e:
        logger.warning("Suggestion LLM call failed, falling back to rules: %s", e)
        report.suggestions = _rule_based_suggestions(root_cause_report, current_retry_hint)
        report.overall_strategy = "LLM call failed — rule-based suggestions only."

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

    Uses the enhanced format with Diagnosis / Root Cause / Recommended Change
    when those fields are available, falls back to Current/Proposed for
    backward compatibility.

    Args:
        report: The suggestion report to format.

    Returns:
        Formatted string with diagnostic details.
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

        # Enhanced format (diagnosis-driven)
        if s.diagnosis:
            lines.append(f"  Diagnosis:")
            for dl in s.diagnosis.split("\n"):
                lines.append(f"    {dl}")
            lines.append("")

        if s.root_cause_hypothesis:
            lines.append(f"  Root cause hypothesis:")
            for rl in s.root_cause_hypothesis.split("\n"):
                lines.append(f"    {rl}")
            lines.append("")

        if s.proposed:
            lines.append(f"  Recommended change:")
            for pl in s.proposed.split("\n"):
                lines.append(f"    + {pl}")
            lines.append("")

        if s.expected_impact:
            lines.append(f"  Expected impact: {s.expected_impact}")

        if s.alternative:
            lines.append(f"  Alternative: {s.alternative}")

        # Backward-compatible fallback: show Current/Proposed if no diagnosis
        if not s.diagnosis and s.current:
            lines.append(f"  Current:")
            for cl in s.current.split("\n"):
                lines.append(f"    - {cl}")
            lines.append(f"  Proposed:")
            for pl in s.proposed.split("\n"):
                lines.append(f"    + {pl}")
            if s.rationale:
                lines.append(f"  Rationale: {s.rationale}")
            if s.expected_impact:
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

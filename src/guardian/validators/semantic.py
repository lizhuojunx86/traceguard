"""Semantic validator using LLM-as-Judge.

Calls an OpenAI-compatible API to evaluate step output against
a set of human-defined criteria. The LLM returns a structured
JSON response with a score (1-5) and a list of issues.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

import httpx

from guardian.core.config import SemanticCheckConfig
from guardian.core.step import StepOutput

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.openai.com/v1"

JUDGE_SYSTEM_PROMPT = """\
You are an expert quality evaluator for AI pipeline outputs.
You will receive an output from a pipeline step and a list of evaluation criteria.
Evaluate the output against EACH criterion carefully.

You MUST respond with ONLY a valid JSON object in this exact format:
{
  "score": <integer 1-5>,
  "issues": [<list of strings describing problems found, empty if none>]
}

Scoring guide:
- 5: Excellent — meets all criteria perfectly
- 4: Good — meets most criteria with minor issues
- 3: Acceptable — meets minimum requirements but has notable gaps
- 2: Poor — fails to meet several criteria
- 1: Unacceptable — fundamentally fails to meet criteria

Be strict but fair. Only report genuine issues.\
"""


def _build_user_prompt(output_text: str, criteria: list[str]) -> str:
    """Build the user prompt for the LLM judge."""
    criteria_text = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(criteria))
    return f"""\
## Step Output
```
{output_text[:5000]}
```

## Evaluation Criteria
{criteria_text}

Evaluate the output against each criterion and respond with JSON only."""


@dataclass
class SemanticResult:
    """Result of semantic evaluation.

    Attributes:
        passed: Whether the semantic score meets the minimum threshold.
        score: LLM-assigned score (1-5).
        issues: List of issue descriptions from the LLM.
        raw_response: The raw LLM response text for debugging.
    """

    passed: bool
    score: int
    issues: list[str] = field(default_factory=list)
    raw_response: str = ""


async def validate_semantic(
    output: StepOutput,
    config: SemanticCheckConfig,
    http_client: httpx.AsyncClient | None = None,
) -> SemanticResult:
    """Run LLM-as-Judge semantic evaluation on a step output.

    Args:
        output: The step output to evaluate.
        config: Semantic check configuration.
        http_client: Optional pre-configured httpx client (for testing).

    Returns:
        SemanticResult with score, pass/fail, and issues.

    Raises:
        ValueError: If API key environment variable is not set.
        httpx.HTTPStatusError: If the API returns an error status.
    """
    if not config.enabled or not config.criteria:
        return SemanticResult(passed=True, score=5, issues=[])

    api_key = os.environ.get(config.api_key_env, "")
    if not api_key:
        raise ValueError(
            f"Environment variable '{config.api_key_env}' is not set"
        )

    api_base = (config.api_base or DEFAULT_API_BASE).rstrip("/")
    url = f"{api_base}/chat/completions"

    output_text = output.output_as_string()
    user_prompt = _build_user_prompt(output_text, config.criteria)

    payload = {
        "model": config.model or "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 512,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    should_close = http_client is None
    client = http_client or httpx.AsyncClient(timeout=30.0)

    try:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()

        body = response.json()
        raw_content = body["choices"][0]["message"]["content"]

        return _parse_llm_response(raw_content, config.min_score)

    finally:
        if should_close:
            await client.aclose()


def _parse_llm_response(raw: str, min_score: int) -> SemanticResult:
    """Parse the LLM's JSON response into a SemanticResult.

    Handles common issues like markdown code fences around JSON.

    Args:
        raw: Raw response text from the LLM.
        min_score: Minimum acceptable score.

    Returns:
        Parsed SemanticResult.
    """
    cleaned = raw.strip()

    # Strip markdown code fences if present
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first line (```json) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM response as JSON: %s", raw[:200])
        return SemanticResult(
            passed=False,
            score=1,
            issues=["LLM response was not valid JSON"],
            raw_response=raw,
        )

    score = data.get("score", 1)
    if not isinstance(score, int) or score < 1 or score > 5:
        score = max(1, min(5, int(score))) if isinstance(score, (int, float)) else 1

    issues = data.get("issues", [])
    if not isinstance(issues, list):
        issues = [str(issues)]
    issues = [str(i) for i in issues]

    return SemanticResult(
        passed=score >= min_score,
        score=score,
        issues=issues,
        raw_response=raw,
    )

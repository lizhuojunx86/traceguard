"""MCP Server for Pipeline Guardian.

Exposes traceguard's quality assurance tools via the Model Context Protocol,
enabling any MCP-compatible client (Claude Desktop, Cursor, VS Code, etc.)
to run guardian checks, query traces, detect drift, and generate suggestions.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "pipeline-guardian",
    instructions=(
        "Pipeline Guardian is a quality assurance system for multi-agent LLM pipelines. "
        "Use these tools to validate step outputs, query evaluation history, "
        "detect quality drift, and generate optimization suggestions."
    ),
)


def _db_url(db_url: str | None = None) -> str:
    """Resolve database URL: parameter > env var > default."""
    return db_url or os.environ.get("GUARDIAN_DB_URL", "sqlite:///traces.db")


def _json(obj: object) -> str:
    """Serialize an object to JSON string."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        obj = dataclasses.asdict(obj)
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


# -- Tools --


@mcp.tool()
def guardian_check(
    pipeline_config_path: str,
    step_name: str,
    output_data: str = "",
    input_file_path: str = "",
    attempt: int = 1,
    db_url: str | None = None,
) -> str:
    """Validate a pipeline step's output against its guardian configuration.

    Runs structural checks (JSON schema, required fields, length, language)
    and semantic evaluation (LLM-as-Judge) if configured.

    Args:
        pipeline_config_path: Path to the pipeline YAML config file.
        step_name: Name of the step to check.
        output_data: Inline step output data (string or JSON). Use this OR input_file_path.
        input_file_path: Path to a file containing the step output. Use this OR output_data.
        attempt: Current attempt number (1-based). Affects retry logic.
        db_url: SQLite database URL for storing trace. Defaults to GUARDIAN_DB_URL env or sqlite:///traces.db.

    Returns:
        JSON with action (pass/retry/abort/alert), score, issues, and semantic status.
    """
    from guardian.core.config import load_pipeline
    from guardian.core.guardian_node import evaluate
    from guardian.core.step import StepOutput, load_step_output
    from guardian.store.writer import TraceWriter

    config = load_pipeline(pipeline_config_path)

    step_config = None
    for s in config.steps:
        if s.name == step_name:
            step_config = s
            break

    if step_config is None:
        return _json({"error": f"Step '{step_name}' not found in pipeline '{config.name}'"})

    if step_config.guardian is None:
        return _json({"error": f"Step '{step_name}' has no guardian configuration"})

    # Load step output from inline data or file
    if output_data:
        # Try to parse as JSON dict
        try:
            parsed = json.loads(output_data)
            data: str | dict = parsed if isinstance(parsed, dict) else output_data
        except (json.JSONDecodeError, TypeError):
            data = output_data
        output = StepOutput(step_name=step_name, output_data=data)
    elif input_file_path:
        output = load_step_output(step_name, input_file_path)
    else:
        return _json({"error": "Either output_data or input_file_path must be provided"})

    decision = evaluate(output, step_config.guardian, attempt=attempt)

    # Write trace
    resolved_db = _db_url(db_url)
    try:
        writer = TraceWriter(resolved_db)
        writer.write(
            pipeline_name=config.name,
            step_name=step_name,
            action=decision.action,
            passed=decision.action == "pass",
            score=decision.score,
            issues=decision.issues,
            attempt=attempt,
            output_preview=output.output_as_string()[:200],
        )
    except Exception as e:
        logger.warning("Failed to write trace: %s", e)

    result = {
        "pipeline": config.name,
        "step": step_name,
        "action": decision.action,
        "score": decision.score,
        "issues": decision.issues,
        "attempt": attempt,
    }
    if decision.semantic_score is not None:
        result["semantic_score"] = decision.semantic_score
    if decision.semantic_status is not None:
        result["semantic"] = decision.semantic_status
    if decision.retry_hint:
        result["retry_hint"] = decision.retry_hint

    return _json(result)


@mcp.tool()
def guardian_list_pipelines(db_url: str | None = None) -> str:
    """List all pipelines that have recorded evaluation traces.

    Args:
        db_url: Database URL. Defaults to GUARDIAN_DB_URL env or sqlite:///traces.db.

    Returns:
        JSON array of pipelines with step_count, trace_count, and latest_trace.
    """
    from guardian.store.reader import TraceReader

    reader = TraceReader(_db_url(db_url))
    return _json(reader.list_pipelines())


@mcp.tool()
def guardian_query_traces(
    pipeline_name: str | None = None,
    step_name: str | None = None,
    days: int = 7,
    limit: int = 50,
    db_url: str | None = None,
) -> str:
    """Query historical evaluation traces with optional filters.

    Args:
        pipeline_name: Filter by pipeline name.
        step_name: Filter by step name.
        days: Number of days to look back (default 7).
        limit: Maximum number of traces to return (default 50).
        db_url: Database URL.

    Returns:
        JSON array of trace records ordered by time descending.
    """
    from guardian.store.reader import TraceReader

    reader = TraceReader(_db_url(db_url))
    return _json(reader.query_traces(
        pipeline_name=pipeline_name,
        step_name=step_name,
        days=days,
        limit=limit,
    ))


@mcp.tool()
def guardian_step_stats(
    pipeline_name: str,
    step_name: str,
    days: int = 7,
    db_url: str | None = None,
) -> str:
    """Get aggregated statistics for a specific pipeline step.

    Args:
        pipeline_name: Pipeline name.
        step_name: Step name.
        days: Number of days to look back (default 7).
        db_url: Database URL.

    Returns:
        JSON with total traces, pass_rate, avg_score, and action_counts breakdown.
    """
    from guardian.store.reader import TraceReader

    reader = TraceReader(_db_url(db_url))
    return _json(reader.get_step_stats(pipeline_name, step_name, days=days))


@mcp.tool()
def guardian_drift_detect(
    pipeline_name: str,
    recent_days: int = 3,
    baseline_days: int = 14,
    db_url: str | None = None,
) -> str:
    """Detect quality drift in a pipeline by comparing recent vs baseline metrics.

    Args:
        pipeline_name: Pipeline name to analyze.
        recent_days: Number of recent days (default 3).
        baseline_days: Total baseline period in days (default 14).
        db_url: Database URL.

    Returns:
        JSON with has_drift flag, summary, and per-step drift analysis
        including trend (stable/degrading/improving) and signals.
    """
    from guardian.optimizer.drift_detector import detect_drift
    from guardian.store.reader import TraceReader

    reader = TraceReader(_db_url(db_url))
    report = detect_drift(
        reader=reader,
        pipeline_name=pipeline_name,
        recent_days=recent_days,
        baseline_days=baseline_days,
    )
    return _json(report)


@mcp.tool()
async def guardian_suggest(
    pipeline_config_path: str,
    step_name: str,
    days: int = 14,
    db_url: str | None = None,
) -> str:
    """Analyze failure patterns and generate optimization suggestions for a step.

    Performs three-step analysis:
    1. Extract failure patterns from trace history
    2. Identify root causes (LLM or rule-based fallback)
    3. Generate actionable suggestions

    Suggestions are for human review only — nothing is auto-applied.

    Args:
        pipeline_config_path: Path to the pipeline YAML config file.
        step_name: Name of the step to optimize.
        days: Lookback period in days (default 14).
        db_url: Database URL containing historical traces.

    Returns:
        JSON with failure_pattern, root_causes, suggestions, and overall_strategy.
    """
    import yaml

    from guardian.core.config import load_pipeline
    from guardian.optimizer.root_cause import analyze_root_causes, extract_failure_pattern
    from guardian.optimizer.suggestion import generate_suggestions
    from guardian.store.reader import TraceReader

    config = load_pipeline(pipeline_config_path)

    step_config = None
    for s in config.steps:
        if s.name == step_name:
            step_config = s
            break

    if step_config is None:
        return _json({"error": f"Step '{step_name}' not found in pipeline '{config.name}'"})

    # Extract guardian config as YAML
    guardian_yaml = ""
    current_hint = None
    if step_config.guardian:
        guardian_dict = step_config.guardian.model_dump(exclude_none=True)
        guardian_yaml = yaml.dump(guardian_dict, default_flow_style=False)
        current_hint = step_config.guardian.actions.retry_hint

    reader = TraceReader(_db_url(db_url))
    pattern = extract_failure_pattern(reader, config.name, step_name, days=days)

    if pattern.failed_traces == 0:
        return _json({
            "pipeline": config.name,
            "step": step_name,
            "message": f"No failures found in the last {days} days.",
        })

    root_report = await analyze_root_causes(pattern)
    suggestion_report = await generate_suggestions(
        root_report, guardian_yaml, current_hint
    )

    return _json({
        "pipeline": config.name,
        "step": step_name,
        "failure_pattern": {
            "total": pattern.total_traces,
            "failed": pattern.failed_traces,
            "rate": pattern.failure_rate,
            "avg_score": pattern.avg_score,
            "top_issues": pattern.issue_counts,
        },
        "root_causes": root_report.root_causes,
        "root_cause_summary": root_report.summary,
        "suggestions": [dataclasses.asdict(s) for s in suggestion_report.suggestions],
        "overall_strategy": suggestion_report.overall_strategy,
    })


def main() -> None:
    """Entry point for the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

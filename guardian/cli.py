"""CLI entry point for Guardian.

Provides a command-line interface to run Guardian checks
on pipeline step outputs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import os
import socket
import sys

import click

from guardian.actions.alert import send_telegram_alert
from guardian.core.config import load_pipeline
from guardian.core.guardian_node import evaluate
from guardian.core.step import load_step_output
from guardian.store.writer import TraceWriter


def _is_port_in_use(host: str, port: int) -> bool:
    """Check if a TCP port is already listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((host, port)) == 0


def _start_dashboard_bg(host: str, port: int, db: str) -> None:
    """Start the dashboard API server in a background process."""
    import uvicorn

    os.environ["GUARDIAN_DB_URL"] = db
    uvicorn.run("guardian.api.server:app", host=host, port=port, log_level="warning")


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging.")
def cli(verbose: bool) -> None:
    """Pipeline Guardian — self-healing AI workflow system."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


@cli.command()
@click.option(
    "--pipeline",
    required=True,
    type=click.Path(exists=True),
    help="Path to pipeline YAML config.",
)
@click.option(
    "--step",
    required=True,
    help="Name of the step to check.",
)
@click.option(
    "--input",
    "input_file",
    required=True,
    type=click.Path(exists=True),
    help="Path to the step output file.",
)
@click.option(
    "--attempt",
    default=1,
    type=int,
    help="Current attempt number (1-based).",
)
@click.option(
    "--db",
    default=None,
    type=str,
    help="SQLite database URL for storing traces. E.g. sqlite:///traces.db",
)
@click.option(
    "--serve",
    is_flag=True,
    default=False,
    help="Auto-start the dashboard server alongside the check.",
)
@click.option("--serve-port", default=8000, type=int, help="Dashboard port (used with --serve).")
def check(
    pipeline: str,
    step: str,
    input_file: str,
    attempt: int,
    db: str | None,
    serve: bool,
    serve_port: int,
) -> None:
    """Run Guardian checks on a step's output.

    Loads the pipeline config, finds the specified step, validates the
    output, writes the eval trace, and outputs the result as JSON.
    """
    logger = logging.getLogger("guardian.cli")

    # Auto-start dashboard if requested and not already running
    if serve and db:
        host = "127.0.0.1"
        if not _is_port_in_use(host, serve_port):
            proc = multiprocessing.Process(
                target=_start_dashboard_bg,
                args=(host, serve_port, db),
                daemon=True,
            )
            proc.start()
            logger.info("Dashboard started at http://%s:%d", host, serve_port)
        else:
            logger.debug("Dashboard already running on port %d", serve_port)

    # Load pipeline config
    config = load_pipeline(pipeline)

    # Find the step
    step_config = None
    for s in config.steps:
        if s.name == step:
            step_config = s
            break

    if step_config is None:
        click.echo(
            json.dumps({"error": f"Step '{step}' not found in pipeline '{config.name}'"}),
            err=True,
        )
        sys.exit(1)

    if step_config.guardian is None:
        click.echo(
            json.dumps({"error": f"Step '{step}' has no guardian configuration"}),
            err=True,
        )
        sys.exit(1)

    # Load step output
    output = load_step_output(step, input_file)

    # Evaluate
    decision = evaluate(output, step_config.guardian, attempt=attempt)

    # Write trace if db is configured
    if db:
        writer = TraceWriter(db)
        preview = output.output_as_string()[:200]
        writer.write(
            pipeline_name=config.name,
            step_name=step,
            action=decision.action,
            passed=decision.action == "pass",
            score=decision.score,
            issues=decision.issues,
            attempt=attempt,
            output_preview=preview,
        )
        logger.info("Trace written to %s", db)

    # Send alert if action is alert or abort
    if decision.action in ("alert", "abort"):
        alert_channel = step_config.guardian.actions.alert_channel
        if alert_channel == "telegram":
            try:
                asyncio.run(
                    send_telegram_alert(
                        pipeline_name=config.name,
                        step_name=step,
                        action=decision.action,
                        issues=decision.issues,
                        score=decision.score,
                    )
                )
            except (ValueError, Exception) as e:
                logger.warning("Failed to send Telegram alert: %s", e)

    # Print environment status to stderr
    from guardian.env import get_cached_endpoint, print_status
    cached = get_cached_endpoint()
    if cached:
        print_status(cached)

    # Output result as JSON
    result = {
        "pipeline": config.name,
        "step": step,
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

    click.echo(json.dumps(result, indent=2))

    # Exit with non-zero if aborted
    if decision.action == "abort":
        sys.exit(2)


@cli.command()
@click.option(
    "--pipeline",
    required=True,
    type=click.Path(exists=True),
    help="Path to pipeline YAML config.",
)
@click.option("--step", required=True, help="Name of the step to optimize.")
@click.option(
    "--db",
    required=True,
    type=str,
    help="Database URL containing eval traces.",
)
@click.option("--days", default=14, type=int, help="Lookback period in days.")
@click.option("--model", default="gpt-4o-mini", help="LLM model for analysis.")
@click.option("--api-base", default=None, help="OpenAI-compatible API base URL.")
@click.option("--json-output", "json_out", is_flag=True, help="Output as JSON instead of text.")
def suggest(
    pipeline: str,
    step: str,
    db: str,
    days: int,
    model: str,
    api_base: str | None,
    json_out: bool,
) -> None:
    """Generate optimization suggestions for a pipeline step.

    Analyzes recent failure patterns, identifies root causes via LLM,
    and produces actionable suggestions. Human review required — nothing
    is auto-applied.
    """
    import yaml

    from guardian.optimizer.root_cause import analyze_root_causes, extract_failure_pattern
    from guardian.optimizer.suggestion import format_suggestion_report, generate_suggestions
    from guardian.store.reader import TraceReader

    logger = logging.getLogger("guardian.cli")

    # Load pipeline config to get guardian YAML
    config = load_pipeline(pipeline)
    step_config = None
    for s in config.steps:
        if s.name == step:
            step_config = s
            break

    if step_config is None:
        click.echo(f"Error: Step '{step}' not found in pipeline '{config.name}'", err=True)
        sys.exit(1)

    # Extract guardian config as YAML for the prompt
    guardian_yaml = ""
    current_hint = None
    if step_config.guardian:
        guardian_dict = step_config.guardian.model_dump(exclude_none=True)
        guardian_yaml = yaml.dump(guardian_dict, default_flow_style=False)
        current_hint = step_config.guardian.actions.retry_hint

    # Step 1: Extract failure patterns
    click.echo(f"Analyzing traces for {config.name}/{step} (last {days} days)...")
    reader = TraceReader(db)
    pattern = extract_failure_pattern(reader, config.name, step, days=days)

    if pattern.failed_traces == 0:
        click.echo(f"No failures found in the last {days} days. Nothing to optimize.")
        sys.exit(0)

    click.echo(
        f"Found {pattern.failed_traces}/{pattern.total_traces} failures "
        f"({pattern.failure_rate:.1%}). Top issues:"
    )
    for issue, count in list(pattern.issue_counts.items())[:5]:
        click.echo(f"  - {issue} ({count}x)")

    # Step 2: Root cause analysis
    click.echo("\nRunning root cause analysis...")
    root_report = asyncio.run(
        analyze_root_causes(pattern, model=model, api_base=api_base)
    )

    # Print environment status
    from guardian.env import get_cached_endpoint, print_status
    cached = get_cached_endpoint()
    if cached:
        print_status(cached)

    if root_report.root_causes:
        click.echo(f"Identified {len(root_report.root_causes)} root cause(s):")
        for rc in root_report.root_causes:
            click.echo(f"  [{rc.get('severity', '?')}] {rc.get('cause', 'Unknown')}")

    # Step 3: Generate suggestions
    click.echo("\nGenerating optimization suggestions...")
    suggestion_report = asyncio.run(
        generate_suggestions(
            root_report, guardian_yaml, current_hint,
            model=model, api_base=api_base,
        )
    )

    # Output
    if json_out:
        output = {
            "pipeline": config.name,
            "step": step,
            "failure_pattern": {
                "total": pattern.total_traces,
                "failed": pattern.failed_traces,
                "rate": pattern.failure_rate,
                "avg_score": pattern.avg_score,
                "top_issues": pattern.issue_counts,
            },
            "root_causes": root_report.root_causes,
            "root_cause_summary": root_report.summary,
            "suggestions": [
                {
                    "type": s.type,
                    "title": s.title,
                    "current": s.current,
                    "proposed": s.proposed,
                    "rationale": s.rationale,
                    "expected_impact": s.expected_impact,
                }
                for s in suggestion_report.suggestions
            ],
            "overall_strategy": suggestion_report.overall_strategy,
        }
        click.echo(json.dumps(output, indent=2))
    else:
        click.echo("")
        click.echo(format_suggestion_report(suggestion_report))


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind host.")
@click.option("--port", default=8000, type=int, help="Bind port.")
@click.option(
    "--db",
    default="sqlite:///traces.db",
    help="Database URL. Default: sqlite:///traces.db",
)
def serve(host: str, port: int, db: str) -> None:
    """Start the Guardian dashboard API server."""
    import os

    import uvicorn

    os.environ.setdefault("GUARDIAN_DB_URL", db)
    click.echo(f"Starting Guardian API on http://{host}:{port}")
    click.echo(f"Database: {db}")
    click.echo(f"Docs: http://{host}:{port}/docs")
    uvicorn.run("guardian.api.server:app", host=host, port=port, log_level="info")


def main() -> None:
    """Entry point for the guardian CLI."""
    cli()


if __name__ == "__main__":
    main()

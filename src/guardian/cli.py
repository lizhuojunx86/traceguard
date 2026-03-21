"""CLI entry point for Guardian.

Provides a command-line interface to run Guardian checks
on pipeline step outputs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

import click

from guardian.actions.alert import send_telegram_alert
from guardian.core.config import load_pipeline
from guardian.core.guardian_node import evaluate
from guardian.core.step import load_step_output
from guardian.store.writer import TraceWriter


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
def check(
    pipeline: str,
    step: str,
    input_file: str,
    attempt: int,
    db: str | None,
) -> None:
    """Run Guardian checks on a step's output.

    Loads the pipeline config, finds the specified step, validates the
    output, writes the eval trace, and outputs the result as JSON.
    """
    logger = logging.getLogger("guardian.cli")

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
    if decision.retry_hint:
        result["retry_hint"] = decision.retry_hint

    click.echo(json.dumps(result, indent=2))

    # Exit with non-zero if aborted
    if decision.action == "abort":
        sys.exit(2)


def main() -> None:
    """Entry point for the guardian CLI."""
    cli()


if __name__ == "__main__":
    main()

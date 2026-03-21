"""Telegram alert action.

Sends alert messages via Telegram Bot API when Guardian detects issues.
Bot token and chat ID are read from environment variables.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def format_alert_message(
    pipeline_name: str,
    step_name: str,
    action: str,
    issues: list[str],
    score: float | None,
) -> str:
    """Format a Guardian alert as a human-readable message.

    Args:
        pipeline_name: Name of the pipeline.
        step_name: Name of the step that triggered the alert.
        action: Decision taken (retry, abort, alert, etc.).
        issues: List of issue descriptions.
        score: Quality score, or None if not computed.

    Returns:
        Formatted alert message string.
    """
    score_str = f"{score:.2f}" if score is not None else "N/A"

    lines = [
        f"🚨 Guardian Alert",
        f"Pipeline: {pipeline_name}",
        f"Step: {step_name}",
        f"Action: {action}",
        f"Score: {score_str}",
    ]

    if issues:
        lines.append("Issues:")
        for issue in issues:
            lines.append(f"  • {issue}")

    return "\n".join(lines)


async def send_telegram_alert(
    pipeline_name: str,
    step_name: str,
    action: str,
    issues: list[str],
    score: float | None,
) -> None:
    """Send an alert message via Telegram Bot API.

    Reads GUARDIAN_TELEGRAM_BOT_TOKEN and GUARDIAN_TELEGRAM_CHAT_ID
    from environment variables.

    Args:
        pipeline_name: Name of the pipeline.
        step_name: Name of the step that triggered the alert.
        action: Decision taken.
        issues: List of issue descriptions.
        score: Quality score.

    Raises:
        ValueError: If required environment variables are not set.
        httpx.HTTPStatusError: If the Telegram API returns an error.
    """
    bot_token = os.environ.get("GUARDIAN_TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise ValueError(
            "GUARDIAN_TELEGRAM_BOT_TOKEN environment variable is not set"
        )

    chat_id = os.environ.get("GUARDIAN_TELEGRAM_CHAT_ID")
    if not chat_id:
        raise ValueError(
            "GUARDIAN_TELEGRAM_CHAT_ID environment variable is not set"
        )

    message = format_alert_message(pipeline_name, step_name, action, issues, score)
    url = TELEGRAM_API_URL.format(token=bot_token)

    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
        )
        response.raise_for_status()

    logger.info(
        "Telegram alert sent: pipeline=%s step=%s action=%s",
        pipeline_name,
        step_name,
        action,
    )

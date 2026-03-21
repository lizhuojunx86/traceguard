"""Tests for Telegram alert action."""
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from guardian.actions.alert import format_alert_message, send_telegram_alert


class TestFormatAlertMessage:
    """Tests for alert message formatting."""

    def test_basic_format(self):
        msg = format_alert_message(
            pipeline_name="my-pipeline",
            step_name="step_01",
            action="abort",
            issues=["Missing field: data"],
            score=0.6,
        )
        assert "my-pipeline" in msg
        assert "step_01" in msg
        assert "abort" in msg
        assert "Missing field: data" in msg
        assert "0.6" in msg

    def test_multiple_issues(self):
        msg = format_alert_message(
            pipeline_name="p",
            step_name="s",
            action="retry",
            issues=["issue 1", "issue 2", "issue 3"],
            score=0.4,
        )
        assert "issue 1" in msg
        assert "issue 2" in msg
        assert "issue 3" in msg

    def test_no_issues(self):
        msg = format_alert_message(
            pipeline_name="p",
            step_name="s",
            action="pass",
            issues=[],
            score=1.0,
        )
        assert "p" in msg
        assert "pass" in msg

    def test_none_score(self):
        msg = format_alert_message(
            pipeline_name="p",
            step_name="s",
            action="alert",
            issues=["bad"],
            score=None,
        )
        assert "N/A" in msg or "None" not in msg


class TestSendTelegramAlert:
    """Tests for send_telegram_alert async function."""

    @pytest.mark.asyncio
    async def test_sends_request(self):
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None

        with patch.dict(os.environ, {
            "GUARDIAN_TELEGRAM_BOT_TOKEN": "fake-token",
            "GUARDIAN_TELEGRAM_CHAT_ID": "12345",
        }):
            with patch("guardian.actions.alert.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_cls.return_value = mock_client

                await send_telegram_alert(
                    pipeline_name="pipe",
                    step_name="step_01",
                    action="abort",
                    issues=["bad output"],
                    score=0.3,
                )

                mock_client.post.assert_called_once()
                call_args = mock_client.post.call_args
                assert "fake-token" in call_args[0][0]
                assert call_args[1]["json"]["chat_id"] == "12345"

    @pytest.mark.asyncio
    async def test_missing_token_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            # Remove both env vars
            os.environ.pop("GUARDIAN_TELEGRAM_BOT_TOKEN", None)
            os.environ.pop("GUARDIAN_TELEGRAM_CHAT_ID", None)
            with pytest.raises(ValueError, match="GUARDIAN_TELEGRAM_BOT_TOKEN"):
                await send_telegram_alert(
                    pipeline_name="p",
                    step_name="s",
                    action="abort",
                    issues=[],
                    score=0.0,
                )

    @pytest.mark.asyncio
    async def test_missing_chat_id_raises(self):
        with patch.dict(os.environ, {
            "GUARDIAN_TELEGRAM_BOT_TOKEN": "tok",
        }, clear=True):
            os.environ.pop("GUARDIAN_TELEGRAM_CHAT_ID", None)
            with pytest.raises(ValueError, match="GUARDIAN_TELEGRAM_CHAT_ID"):
                await send_telegram_alert(
                    pipeline_name="p",
                    step_name="s",
                    action="abort",
                    issues=[],
                    score=0.0,
                )

    @pytest.mark.asyncio
    async def test_http_error_raises(self):
        mock_response = AsyncMock()
        mock_response.status_code = 403
        mock_response.raise_for_status = lambda: (_ for _ in ()).throw(
            httpx.HTTPStatusError(
                "Forbidden", request=AsyncMock(), response=mock_response
            )
        )

        with patch.dict(os.environ, {
            "GUARDIAN_TELEGRAM_BOT_TOKEN": "tok",
            "GUARDIAN_TELEGRAM_CHAT_ID": "123",
        }):
            with patch("guardian.actions.alert.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_cls.return_value = mock_client

                with pytest.raises(httpx.HTTPStatusError):
                    await send_telegram_alert(
                        pipeline_name="p",
                        step_name="s",
                        action="abort",
                        issues=["err"],
                        score=0.0,
                    )

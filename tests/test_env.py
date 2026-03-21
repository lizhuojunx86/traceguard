"""Tests for environment probe and LLM endpoint resolution."""
import io
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from guardian.env import (
    LLMEndpoint,
    LLMMode,
    _detect_sandbox,
    _discover_local_llm,
    _probe_external_api,
    get_cached_endpoint,
    print_status,
    probe_llm_environment,
    reset_endpoint,
)


@pytest.fixture(autouse=True)
def clean_cache():
    """Reset the singleton cache before and after each test."""
    reset_endpoint()
    yield
    reset_endpoint()


# -- LLMMode --

class TestLLMMode:
    def test_enum_values(self):
        assert LLMMode.FULL == "FULL"
        assert LLMMode.LOCAL == "LOCAL"
        assert LLMMode.DEGRADED == "DEGRADED"


# -- Sandbox Detection --

class TestDetectSandbox:
    def test_dockerenv_detected(self):
        with patch.object(Path, "exists", return_value=True):
            assert _detect_sandbox() is True

    def test_no_sandbox_indicators(self):
        with patch.object(Path, "exists", return_value=False):
            with patch.dict(os.environ, {}, clear=True):
                assert _detect_sandbox() is False

    def test_proxy_localhost_detected(self):
        with patch.object(Path, "exists", return_value=False):
            with patch.dict(os.environ, {"HTTP_PROXY": "http://localhost:8080"}, clear=True):
                assert _detect_sandbox() is True

    def test_proxy_127_detected(self):
        with patch.object(Path, "exists", return_value=False):
            with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:3128"}, clear=True):
                assert _detect_sandbox() is True

    def test_real_proxy_not_sandbox(self):
        with patch.object(Path, "exists", return_value=False):
            with patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.corp.com:8080"}, clear=True):
                assert _detect_sandbox() is False


# -- External API Probe --

class TestProbeExternalAPI:
    @pytest.mark.asyncio
    async def test_full_mode_when_reachable(self):
        mock_response = httpx.Response(
            200, json={"choices": [{"message": {"content": "h"}}]},
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        result = await _probe_external_api(
            None, "test-key", "gpt-4o-mini", mock_client
        )
        assert result is not None
        assert result.mode == LLMMode.FULL
        assert result.model == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_none_on_connect_error(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        result = await _probe_external_api(None, "key", None, mock_client)
        assert result is None

    @pytest.mark.asyncio
    async def test_none_on_proxy_error(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ProxyError("blocked"))

        result = await _probe_external_api(None, "key", None, mock_client)
        assert result is None

    @pytest.mark.asyncio
    async def test_none_on_timeout(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))

        result = await _probe_external_api(None, "key", None, mock_client)
        assert result is None

    @pytest.mark.asyncio
    async def test_none_on_403(self):
        mock_response = httpx.Response(
            403, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        result = await _probe_external_api(None, "key", None, mock_client)
        assert result is None

    @pytest.mark.asyncio
    async def test_none_on_500(self):
        mock_response = httpx.Response(
            500, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        result = await _probe_external_api(None, "key", None, mock_client)
        assert result is None

    @pytest.mark.asyncio
    async def test_custom_api_base(self):
        mock_response = httpx.Response(
            200, json={},
            request=httpx.Request("POST", "https://custom.api/v1/chat/completions"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        result = await _probe_external_api(
            "https://custom.api/v1", "key", "my-model", mock_client
        )
        assert result is not None
        assert result.api_base == "https://custom.api/v1"
        assert result.model == "my-model"


# -- Local LLM Discovery --

class TestDiscoverLocalLLM:
    @pytest.mark.asyncio
    async def test_ollama_discovered(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=httpx.Response(
            200,
            json={"models": [{"name": "mistral:latest"}]},
            request=httpx.Request("GET", "http://localhost:11434/api/tags"),
        ))

        result = await _discover_local_llm(mock_client)
        assert result is not None
        assert result.mode == LLMMode.LOCAL
        assert result.provider == "ollama"
        assert result.model == "mistral:latest"

    @pytest.mark.asyncio
    async def test_no_local_llm(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        result = await _discover_local_llm(mock_client)
        assert result is None

    @pytest.mark.asyncio
    async def test_lmstudio_discovered(self):
        """Ollama fails, but LM Studio works."""
        call_count = 0

        async def mock_get(*args, **kwargs):
            raise httpx.ConnectError("refused")

        async def mock_post(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if "1234" in url:
                return httpx.Response(
                    200, json={"model": "qwen2"},
                    request=httpx.Request("POST", url),
                )
            raise httpx.ConnectError("refused")

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=mock_get)
        mock_client.post = AsyncMock(side_effect=mock_post)

        result = await _discover_local_llm(mock_client)
        assert result is not None
        assert result.provider == "lmstudio"
        assert result.model == "qwen2"

    @pytest.mark.asyncio
    async def test_ollama_empty_models_uses_default(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=httpx.Response(
            200, json={"models": []},
            request=httpx.Request("GET", "http://localhost:11434/api/tags"),
        ))

        result = await _discover_local_llm(mock_client)
        assert result is not None
        assert result.model == "llama3"


# -- probe_llm_environment (Integration) --

class TestProbeEnvironment:
    @pytest.mark.asyncio
    async def test_caches_result(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("no"))
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("no"))

        with patch.dict(os.environ, {}, clear=True):
            e1 = await probe_llm_environment(http_client=mock_client)
            e2 = await probe_llm_environment(http_client=mock_client)
            assert e1 is e2
            assert e1.mode == LLMMode.DEGRADED

    @pytest.mark.asyncio
    async def test_force_bypasses_cache(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("no"))
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("no"))

        with patch.dict(os.environ, {}, clear=True):
            e1 = await probe_llm_environment(http_client=mock_client)
            e2 = await probe_llm_environment(http_client=mock_client, force=True)
            assert e1.mode == e2.mode
            # force=True still calls the probe (different object)
            assert e1 is not e2

    @pytest.mark.asyncio
    async def test_sandbox_skips_external(self):
        """Sandbox detected → skip external probe → local also fails → DEGRADED."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with patch.dict(os.environ, {
            "GUARDIAN_LLM_API_KEY": "real-key",
            "HTTP_PROXY": "http://localhost:8080",
        }, clear=True):
            with patch.object(Path, "exists", return_value=False):
                result = await probe_llm_environment(http_client=mock_client)

        assert result.mode == LLMMode.DEGRADED

    @pytest.mark.asyncio
    async def test_full_mode_with_valid_api(self):
        mock_response = httpx.Response(
            200, json={"choices": [{"message": {"content": "h"}}]},
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(Path, "exists", return_value=False):
            with patch.dict(os.environ, {"GUARDIAN_LLM_API_KEY": "valid-key"}, clear=True):
                result = await probe_llm_environment(http_client=mock_client)

        assert result.mode == LLMMode.FULL

    @pytest.mark.asyncio
    async def test_get_cached_endpoint(self):
        assert get_cached_endpoint() is None

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("no"))
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("no"))

        with patch.dict(os.environ, {}, clear=True):
            await probe_llm_environment(http_client=mock_client)

        cached = get_cached_endpoint()
        assert cached is not None
        assert cached.mode == LLMMode.DEGRADED


# -- Print Status --

class TestPrintStatus:
    def test_full_mode(self):
        buf = io.StringIO()
        ep = LLMEndpoint(mode=LLMMode.FULL, api_base="https://api.openai.com/v1",
                         model="gpt-4o-mini", provider="openai")
        print_status(ep, file=buf)
        assert "mode=FULL" in buf.getvalue()
        assert "openai/gpt-4o-mini" in buf.getvalue()

    def test_local_mode(self):
        buf = io.StringIO()
        ep = LLMEndpoint(mode=LLMMode.LOCAL, api_base="http://localhost:11434",
                         model="llama3", provider="ollama",
                         reason="Ollama at http://localhost:11434")
        print_status(ep, file=buf)
        assert "mode=LOCAL" in buf.getvalue()
        assert "ollama/llama3" in buf.getvalue()

    def test_degraded_mode(self):
        buf = io.StringIO()
        ep = LLMEndpoint(mode=LLMMode.DEGRADED, reason="No LLM available")
        print_status(ep, file=buf)
        assert "mode=DEGRADED" in buf.getvalue()
        assert "api=none" in buf.getvalue()

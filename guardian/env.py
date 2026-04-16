"""Environment probe and LLM endpoint resolution.

Detects the runtime environment (Docker, Kubernetes, proxied, native),
probes external LLM APIs, discovers local LLM servers, and caches
the result as a process-level singleton. Enables graceful degradation
when no LLM is available.
"""
from __future__ import annotations

import asyncio
import logging
import os
import ssl
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 3.0  # seconds for external API probe
LOCAL_TIMEOUT = 2.0   # seconds for local LLM discovery


class LLMMode(str, Enum):
    """LLM availability level."""

    FULL = "FULL"          # External API reachable with valid key
    LOCAL = "LOCAL"         # Local LLM discovered (Ollama, LM Studio, etc.)
    DEGRADED = "DEGRADED"  # No LLM available — structural checks only


@dataclass(frozen=True)
class LLMEndpoint:
    """Resolved LLM endpoint after environment probing.

    Attributes:
        mode: Availability level.
        api_base: The reachable base URL (without /chat/completions).
        model: The model name to use.
        provider: Provider identifier (openai, ollama, lmstudio, llamacpp).
        reason: Human-readable reason for this mode.
    """

    mode: LLMMode
    api_base: str | None = None
    model: str | None = None
    provider: str | None = None
    reason: str = ""


# -- Process-level singleton --

_cached_endpoint: LLMEndpoint | None = None
_probe_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Lazily create the asyncio lock to avoid event-loop issues at import time."""
    global _probe_lock
    if _probe_lock is None:
        _probe_lock = asyncio.Lock()
    return _probe_lock


def get_cached_endpoint() -> LLMEndpoint | None:
    """Return the cached probe result, or None if not yet probed."""
    return _cached_endpoint


def reset_endpoint() -> None:
    """Reset the cached endpoint. For testing only."""
    global _cached_endpoint, _probe_lock
    _cached_endpoint = None
    _probe_lock = None


# -- Local LLM endpoint registry --

LOCAL_LLM_ENDPOINTS: list[tuple[str, str, str]] = [
    # (base_url, provider_name, probe_style)
    ("http://localhost:11434", "ollama", "ollama"),
    ("http://host.docker.internal:11434", "ollama", "ollama"),
    ("http://localhost:1234/v1", "lmstudio", "openai"),
    ("http://localhost:8080/v1", "llamacpp", "openai"),
]


# -- Public API --

async def probe_llm_environment(
    config_api_base: str | None = None,
    config_api_key_env: str = "GUARDIAN_LLM_API_KEY",
    config_model: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    force: bool = False,
) -> LLMEndpoint:
    """Probe the runtime environment and resolve the best LLM endpoint.

    Results are cached per-process. Subsequent calls return the cached
    result unless ``force=True``.

    Args:
        config_api_base: Configured external API base URL.
        config_api_key_env: Environment variable name holding the API key.
        config_model: Configured model name.
        http_client: Optional httpx client (for testing).
        force: Bypass cache and re-probe.

    Returns:
        LLMEndpoint describing the resolved mode and endpoint.
    """
    global _cached_endpoint

    if _cached_endpoint is not None and not force:
        return _cached_endpoint

    lock = _get_lock()
    async with lock:
        # Double-check after acquiring lock
        if _cached_endpoint is not None and not force:
            return _cached_endpoint

        endpoint = await _do_probe(
            config_api_base, config_api_key_env, config_model, http_client
        )
        _cached_endpoint = endpoint
        return endpoint


async def _do_probe(
    config_api_base: str | None,
    config_api_key_env: str,
    config_model: str | None,
    http_client: httpx.AsyncClient | None,
) -> LLMEndpoint:
    """Execute the actual probe logic."""
    is_sandboxed = _detect_sandbox()

    # If not sandboxed, try external API first
    if not is_sandboxed:
        api_key = os.environ.get(config_api_key_env, "")
        if api_key:
            result = await _probe_external_api(
                config_api_base, api_key, config_model, http_client
            )
            if result is not None:
                return result
            logger.info("External API unreachable, trying local LLM discovery")
        else:
            logger.debug("No API key set (%s), skipping external probe", config_api_key_env)
    else:
        logger.info("Sandbox environment detected, skipping external API probe")

    # Try local LLM discovery
    local = await _discover_local_llm(http_client)
    if local is not None:
        return local

    # Nothing available
    reason = "No external API or local LLM reachable"
    if is_sandboxed:
        reason = f"Sandbox detected; {reason}"
    return LLMEndpoint(mode=LLMMode.DEGRADED, reason=reason)


def _detect_sandbox() -> bool:
    """Detect if running inside a container or sandboxed environment.

    Set GUARDIAN_FORCE_EXTERNAL=1 to bypass all sandbox checks
    (useful when running locally with a proxy like Clash/V2Ray).
    """
    if os.environ.get("GUARDIAN_FORCE_EXTERNAL", "").strip() == "1":
        logger.debug("Sandbox check bypassed via GUARDIAN_FORCE_EXTERNAL=1")
        return False

    # Docker
    if Path("/.dockerenv").exists():
        logger.debug("Sandbox indicator: /.dockerenv")
        return True

    # Kubernetes
    if Path("/var/run/secrets/kubernetes.io").exists():
        logger.debug("Sandbox indicator: kubernetes secrets")
        return True

    # Proxy pointing to localhost (common in sandboxed VMs)
    # Skipped if GUARDIAN_FORCE_EXTERNAL is set (handled above)
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        proxy = os.environ.get(var, "")
        if proxy and ("localhost" in proxy or "127.0.0.1" in proxy):
            logger.debug("Sandbox indicator: %s=%s", var, proxy)
            return True

    return False


async def _probe_external_api(
    api_base: str | None,
    api_key: str,
    model: str | None,
    http_client: httpx.AsyncClient | None,
) -> LLMEndpoint | None:
    """Send a minimal probe to the external API. Returns endpoint or None."""
    base = (api_base or "https://api.openai.com/v1").rstrip("/")
    url = f"{base}/chat/completions"
    model_name = model or "gpt-4o-mini"

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    should_close = http_client is None
    client = http_client or httpx.AsyncClient(timeout=PROBE_TIMEOUT)

    try:
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code in (403, 407, 429) or response.status_code >= 500:
            logger.debug("External API returned %d", response.status_code)
            return None
        # Any 2xx or other status — consider it reachable
        provider = _guess_provider(base)
        return LLMEndpoint(
            mode=LLMMode.FULL,
            api_base=base,
            model=model_name,
            provider=provider,
        )
    except (
        httpx.ProxyError,
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        ssl.SSLError,
        httpx.HTTPStatusError,
        OSError,
    ) as e:
        logger.debug("External API probe failed: %s", e)
        return None
    finally:
        if should_close:
            await client.aclose()


def _guess_provider(api_base: str) -> str:
    """Guess the provider name from the API base URL."""
    lower = api_base.lower()
    if "openai" in lower:
        return "openai"
    if "anthropic" in lower:
        return "anthropic"
    return "openai-compatible"


async def _discover_local_llm(
    http_client: httpx.AsyncClient | None = None,
) -> LLMEndpoint | None:
    """Try local LLM endpoints in order. Return first reachable."""
    for base_url, provider, style in LOCAL_LLM_ENDPOINTS:
        result = await _probe_local_endpoint(base_url, provider, style, http_client)
        if result is not None:
            return result
    return None


async def _probe_local_endpoint(
    base_url: str,
    provider: str,
    style: str,
    http_client: httpx.AsyncClient | None,
) -> LLMEndpoint | None:
    """Probe a single local LLM endpoint."""
    should_close = http_client is None
    client = http_client or httpx.AsyncClient(timeout=LOCAL_TIMEOUT)

    try:
        if style == "ollama":
            return await _probe_ollama(client, base_url, provider)
        else:
            return await _probe_openai_compat(client, base_url, provider)
    except (
        httpx.ProxyError,
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        ssl.SSLError,
        httpx.HTTPStatusError,
        OSError,
    ):
        return None
    finally:
        if should_close:
            await client.aclose()


async def _probe_ollama(
    client: httpx.AsyncClient,
    base_url: str,
    provider: str,
) -> LLMEndpoint | None:
    """Probe an Ollama endpoint and discover available models."""
    # First check if Ollama is reachable via /api/tags
    tags_url = f"{base_url}/api/tags"
    response = await client.get(tags_url)
    if response.status_code != 200:
        return None

    # Extract best available chat model (skip embedding-only models)
    model = "llama3"
    EMBED_ONLY_PATTERNS = ("embed", "nomic-embed", "bge-", "e5-", "gte-")
    try:
        data = response.json()
        models = data.get("models", [])
        # Prefer a chat-capable model over embedding-only ones
        chat_models = [
            m.get("name", "")
            for m in models
            if not any(p in m.get("name", "").lower() for p in EMBED_ONLY_PATTERNS)
        ]
        if chat_models:
            model = chat_models[0]
        elif models:
            model = models[0].get("name", "llama3")
    except (ValueError, KeyError, IndexError):
        pass

    return LLMEndpoint(
        mode=LLMMode.LOCAL,
        api_base=base_url,
        model=model,
        provider=provider,
        reason=f"Ollama at {base_url}",
    )


async def _probe_openai_compat(
    client: httpx.AsyncClient,
    base_url: str,
    provider: str,
) -> LLMEndpoint | None:
    """Probe an OpenAI-compatible local endpoint."""
    url = f"{base_url}/chat/completions"
    payload = {
        "model": "local-model",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }

    response = await client.post(url, json=payload)
    if response.status_code >= 500:
        return None

    # Try to extract actual model name from response
    model = "local-model"
    try:
        data = response.json()
        model = data.get("model", model)
    except (ValueError, KeyError):
        pass

    return LLMEndpoint(
        mode=LLMMode.LOCAL,
        api_base=base_url,
        model=model,
        provider=provider,
        reason=f"{provider} at {base_url}",
    )


def print_status(endpoint: LLMEndpoint, file=None) -> None:
    """Print a one-liner status to stderr.

    Format: [traceguard] mode=FULL api=openai/gpt-4o-mini
    """
    if file is None:
        file = sys.stderr

    if endpoint.mode == LLMMode.DEGRADED:
        api_str = "none"
    else:
        provider = endpoint.provider or "unknown"
        model = endpoint.model or "unknown"
        api_str = f"{provider}/{model}"

    reason_part = f" ({endpoint.reason})" if endpoint.reason else ""
    print(f"[traceguard] mode={endpoint.mode.value}  api={api_str}{reason_part}", file=file)

"""Anthropic SDK auto-instrumentation (Phase 0 MVP).

Wraps an existing ``anthropic.Anthropic`` (sync) client so calls to
``client.messages.create(...)`` automatically produce a ``traces`` row.

Async client support and cost calculation land in Phase 1. The wrapper does
not modify the response object — callers see exactly what the Anthropic SDK
returned, just with a trace persisted as a side effect.
"""
from __future__ import annotations

from typing import Any

from traceguard.sdk.tracer import Tracer
from traceguard.sdk.tracer import tracer as default_tracer


class _WrappedMessages:
    def __init__(
        self,
        original: Any,
        *,
        tracer: Tracer,
        project: str,
        component: str,
    ) -> None:
        self._original = original
        self._tracer = tracer
        self._project = project
        self._component = component

    def create(self, **kwargs: Any) -> Any:
        model = kwargs.get("model")
        messages = kwargs.get("messages")
        with self._tracer.span(
            self._project,
            self._component,
            operation="llm_complete",
        ) as span:
            extra = {k: v for k, v in kwargs.items() if k not in {"model", "messages"}}
            span.record_input({"messages": messages, "model": model, "params": extra})
            if model is not None:
                span.record_model_prompt(model_id=str(model))
            try:
                response = self._original.create(**kwargs)
            except BaseException:
                # tracer.span will record_error + flush, then re-raise
                raise

            content_text = _extract_text(response)
            response_id = getattr(response, "id", None)
            stop_reason = getattr(response, "stop_reason", None)
            span.record_output(
                parsed={
                    "id": response_id,
                    "content_text": content_text,
                    "stop_reason": stop_reason,
                },
                parse_status="success",
            )

            usage = getattr(response, "usage", None)
            if usage is not None:
                span.record_perf(
                    tokens_in=getattr(usage, "input_tokens", None),
                    tokens_out=getattr(usage, "output_tokens", None),
                )
            return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


def _extract_text(response: Any) -> str | None:
    """Best-effort extraction of the assistant text from a Messages API response."""
    content = getattr(response, "content", None)
    if content is None:
        return None
    parts: list[str] = []
    try:
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    except TypeError:
        return None
    return "".join(parts) if parts else None


class WrappedAnthropicClient:
    """Delegating wrapper. ``messages.create`` is instrumented; every other
    attribute access pass-through to the original client.
    """

    def __init__(
        self,
        client: Any,
        *,
        tracer: Tracer,
        project: str,
        component: str,
    ) -> None:
        self._client = client
        self.messages = _WrappedMessages(
            client.messages,
            tracer=tracer,
            project=project,
            component=component,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def wrap_anthropic(
    client: Any,
    *,
    project: str,
    component: str,
    tracer: Tracer | None = None,
) -> WrappedAnthropicClient:
    """Return ``client`` wrapped so ``messages.create()`` produces traces."""
    return WrappedAnthropicClient(
        client,
        tracer=tracer or default_tracer,
        project=project,
        component=component,
    )

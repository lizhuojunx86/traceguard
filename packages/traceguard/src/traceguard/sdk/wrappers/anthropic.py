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
from traceguard.sdk.wrappers._base import (
    FeatureAsOf,
    _DelegatingWrapper,
    resolve_feature_as_of,
)


class _WrappedMessages(_DelegatingWrapper):
    def __init__(
        self,
        original: Any,
        *,
        tracer: Tracer,
        project: str,
        component: str,
        feature_as_of: FeatureAsOf = None,
    ) -> None:
        self._original = original
        self._tracer = tracer
        self._project = project
        self._component = component
        self._feature_as_of = feature_as_of

    def create(self, **kwargs: Any) -> Any:
        model = kwargs.get("model")
        messages = kwargs.get("messages")
        with self._tracer.span(
            self._project,
            self._component,
            operation="llm_complete",
            feature_as_of=resolve_feature_as_of(self._feature_as_of),
        ) as span:
            extra = {k: v for k, v in kwargs.items() if k not in {"model", "messages"}}
            span.record_input({"messages": messages, "model": model, "params": extra})
            if model is not None:
                span.record_model_prompt(model_id=str(model))
            # tracer.span records the error + flushes + re-raises on failure.
            response = self._original.create(**kwargs)

            if kwargs.get("stream"):
                # A streaming call returns an iterator, not a materialized
                # message: text/usage are not available until the caller drains
                # the stream, which this wrapper does not do. Record an honest
                # 'partial' instead of a false 'success' with empty text and
                # zero tokens (which would corrupt the trace dataset).
                span.record_output(
                    parsed={
                        "id": getattr(response, "id", None),
                        "streaming": True,
                        "note": "streaming response body not captured by wrap_anthropic",
                    },
                    parse_status="partial",
                )
                return response

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


class WrappedAnthropicClient(_DelegatingWrapper):
    """Delegating wrapper. ``messages.create`` is instrumented; every other
    attribute access pass-through to the original client.
    """

    _delegate_attr = "_client"

    def __init__(
        self,
        client: Any,
        *,
        tracer: Tracer,
        project: str,
        component: str,
        feature_as_of: FeatureAsOf = None,
    ) -> None:
        self._client = client
        self.messages = _WrappedMessages(
            client.messages,
            tracer=tracer,
            project=project,
            component=component,
            feature_as_of=feature_as_of,
        )


def wrap_anthropic(
    client: Any,
    *,
    project: str,
    component: str,
    tracer: Tracer | None = None,
    feature_as_of: FeatureAsOf = None,
) -> WrappedAnthropicClient:
    """Return ``client`` wrapped so ``messages.create()`` produces traces.

    ``feature_as_of`` stamps a point-in-time on every instrumented call — a fixed
    ``datetime``, a zero-arg callable resolved at each call, or ``None`` (default)
    to record no stamp. Stamping makes the resulting traces checkable by the
    look-ahead invariants (SPEC §3); a callable that raises is swallowed
    (fail-open) and that trace records ``feature_as_of=None``.
    """
    return WrappedAnthropicClient(
        client,
        tracer=tracer or default_tracer,
        project=project,
        component=component,
        feature_as_of=feature_as_of,
    )

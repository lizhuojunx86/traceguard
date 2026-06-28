"""OpenAI SDK auto-instrumentation (additive — mirrors ``wrap_anthropic``).

Wraps an existing ``openai.OpenAI`` (sync) client so calls to
``client.chat.completions.create(...)`` — and ``client.responses.create(...)``
when the installed SDK exposes the Responses API — automatically produce a
``traces`` row. The wrapper does not modify the response object: callers see
exactly what the OpenAI SDK returned, just with a trace persisted as a side
effect. A client that was never wrapped is completely unaffected.

Async client support and cost calculation are out of scope for this wrapper
(mirroring the Phase 0 Anthropic wrapper).
"""
from __future__ import annotations

from typing import Any

from traceguard.sdk.tracer import Tracer
from traceguard.sdk.tracer import tracer as default_tracer

# A streaming call returns an iterator, not a materialized response: text/usage
# are unavailable until the caller drains the stream, which this wrapper does
# not do. We record an honest 'partial' rather than a false 'success' with empty
# text and zero tokens, which would corrupt the trace dataset.
_STREAM_NOTE = "streaming response body not captured by wrap_openai"


def _chat_text(response: Any) -> str | None:
    """Best-effort extraction of the assistant text from a Chat Completions response."""
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    try:
        first = choices[0]
    except (TypeError, IndexError):
        return None
    message = getattr(first, "message", None)
    content = getattr(message, "content", None)
    return content if isinstance(content, str) else None


def _first_finish_reason(response: Any) -> str | None:
    """Best-effort extraction of the first choice's ``finish_reason``."""
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    try:
        first = choices[0]
    except (TypeError, IndexError):
        return None
    reason = getattr(first, "finish_reason", None)
    return reason if isinstance(reason, str) else None


def _responses_text(response: Any) -> str | None:
    """Best-effort extraction of the aggregated text from a Responses API response.

    The OpenAI SDK exposes ``output_text`` as a convenience property that joins
    all output text parts; we read it directly and fall back to ``None``.
    """
    text = getattr(response, "output_text", None)
    return text if isinstance(text, str) else None


class _WrappedCompletions:
    """Instruments ``client.chat.completions.create``."""

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
            # The tracer.span context manager records the error + flushes + re-raises
            # if this call fails, so no explicit try/except is needed here.
            response = self._original.create(**kwargs)

            if kwargs.get("stream"):
                span.record_output(
                    parsed={"streaming": True, "note": _STREAM_NOTE},
                    parse_status="partial",
                )
                return response

            span.record_output(
                parsed={
                    "id": getattr(response, "id", None),
                    "content_text": _chat_text(response),
                    "finish_reason": _first_finish_reason(response),
                },
                parse_status="success",
            )

            usage = getattr(response, "usage", None)
            if usage is not None:
                span.record_perf(
                    tokens_in=getattr(usage, "prompt_tokens", None),
                    tokens_out=getattr(usage, "completion_tokens", None),
                )
            return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


class _WrappedChat:
    """Exposes an instrumented ``completions``; passes everything else through."""

    def __init__(
        self,
        original: Any,
        *,
        tracer: Tracer,
        project: str,
        component: str,
    ) -> None:
        self._original = original
        self.completions = _WrappedCompletions(
            original.completions,
            tracer=tracer,
            project=project,
            component=component,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


class _WrappedResponses:
    """Instruments ``client.responses.create`` (OpenAI Responses API)."""

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
        input_ = kwargs.get("input")
        with self._tracer.span(
            self._project,
            self._component,
            operation="llm_complete",
        ) as span:
            extra = {k: v for k, v in kwargs.items() if k not in {"model", "input"}}
            span.record_input({"input": input_, "model": model, "params": extra})
            if model is not None:
                span.record_model_prompt(model_id=str(model))
            # See note in _WrappedCompletions.create — span records error + re-raises.
            response = self._original.create(**kwargs)

            if kwargs.get("stream"):
                span.record_output(
                    parsed={"streaming": True, "note": _STREAM_NOTE},
                    parse_status="partial",
                )
                return response

            span.record_output(
                parsed={
                    "id": getattr(response, "id", None),
                    "content_text": _responses_text(response),
                    "status": getattr(response, "status", None),
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


class WrappedOpenAIClient:
    """Delegating wrapper. ``chat.completions.create`` — and ``responses.create``
    when the underlying client exposes it — are instrumented; every other
    attribute access passes through to the original client.
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
        self.chat = _WrappedChat(
            client.chat,
            tracer=tracer,
            project=project,
            component=component,
        )
        # The Responses API only exists on newer openai SDKs. Wrap it only when
        # present so older clients are unaffected; absent, attribute access on
        # ``.responses`` falls through to the original client (which also lacks it).
        if hasattr(client, "responses"):
            self.responses = _WrappedResponses(
                client.responses,
                tracer=tracer,
                project=project,
                component=component,
            )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def wrap_openai(
    client: Any,
    *,
    project: str,
    component: str,
    tracer: Tracer | None = None,
) -> WrappedOpenAIClient:
    """Return ``client`` wrapped so OpenAI calls produce traces.

    Instruments ``client.chat.completions.create()`` and, when the installed
    SDK exposes it, ``client.responses.create()``. Each instrumented call
    records one ``traces`` row (input hash, model, output text/id, prompt and
    completion tokens, latency) as a side effect; the response object is
    returned untouched. Every other attribute access passes through to the
    original client, so the wrapper is a drop-in replacement.

    Args:
        client: An ``openai.OpenAI`` (or compatible) client instance.
        project: Project label recorded on every trace.
        component: Component label recorded on every trace.
        tracer: Tracer to persist into; defaults to the module-level tracer.

    Returns:
        A :class:`WrappedOpenAIClient` delegating to ``client``.
    """
    return WrappedOpenAIClient(
        client,
        tracer=tracer or default_tracer,
        project=project,
        component=component,
    )

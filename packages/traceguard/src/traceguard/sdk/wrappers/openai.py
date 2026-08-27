"""OpenAI SDK auto-instrumentation (additive — mirrors ``wrap_anthropic``).

Wraps an existing ``openai.OpenAI`` (sync) client so calls to
``client.chat.completions.create(...)`` — and ``client.responses.create(...)``
when the installed SDK exposes the Responses API — automatically produce a
``traces`` row. The wrapper does not modify the response object: callers see
exactly what the OpenAI SDK returned, just with a trace persisted as a side
effect. A client that was never wrapped is completely unaffected.

Async client support and cost calculation are out of scope for this wrapper
(mirroring the Phase 0 Anthropic wrapper).

``tokens_in`` semantics: **full prompt volume**, same as ``wrap_anthropic`` —
but reached differently, because the two providers report caching with
opposite conventions. OpenAI's ``usage.prompt_tokens`` (Responses:
``input_tokens``) ALREADY INCLUDES the cached prefix, which
``prompt_tokens_details.cached_tokens`` reports as a *subset* for pricing.
Adding the cached count here would double-count it, so the top-level field is
recorded as-is and ``cached_tokens`` is kept in ``output_parsed["usage"]`` as
detail only. (Anthropic is the opposite: its three input counts are mutually
exclusive and must be summed — see ``wrappers/anthropic.py``.)
"""
from __future__ import annotations

from typing import Any

from traceguard.sdk.tracer import Tracer
from traceguard.sdk.tracer import tracer as default_tracer
from traceguard.sdk.wrappers._base import (
    FeatureAsOf,
    _DelegatingWrapper,
    resolve_feature_as_of,
    routing_detail,
)

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


def _chat_usage_detail(usage: Any) -> dict[str, Any]:
    """Flatten a Chat Completions ``usage`` block, keeping the cached-prefix detail.

    Field names are OpenAI's own — deliberately NOT remapped onto the
    Anthropic/``routing_audit`` flat names, since that pricing table only
    covers ``claude-*`` models and a cross-provider key convention has no
    reader today. ``cached_tokens`` is a subset of ``prompt_tokens``, not an
    addition to it (see the module docstring).
    """
    details = getattr(usage, "prompt_tokens_details", None)  # getattr(None, ...) is safe
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "cached_tokens": getattr(details, "cached_tokens", None),
    }


def _responses_usage_detail(usage: Any) -> dict[str, Any]:
    """Flatten a Responses API ``usage`` block. See :func:`_chat_usage_detail`."""
    details = getattr(usage, "input_tokens_details", None)
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "cached_tokens": getattr(details, "cached_tokens", None),
    }


def _responses_text(response: Any) -> str | None:
    """Best-effort extraction of the aggregated text from a Responses API response.

    The OpenAI SDK exposes ``output_text`` as a convenience property that joins
    all output text parts; we read it directly and fall back to ``None``.
    """
    text = getattr(response, "output_text", None)
    return text if isinstance(text, str) else None


class _WrappedCompletions(_DelegatingWrapper):
    """Instruments ``client.chat.completions.create``."""

    def __init__(
        self,
        original: Any,
        *,
        tracer: Tracer,
        project: str,
        component: str,
        feature_as_of: FeatureAsOf = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self._original = original
        self._tracer = tracer
        self._project = project
        self._component = component
        self._feature_as_of = feature_as_of
        self._agent_id = agent_id
        self._session_id = session_id

    def create(self, **kwargs: Any) -> Any:
        model = kwargs.get("model")
        messages = kwargs.get("messages")
        with self._tracer.span(
            self._project,
            self._component,
            operation="llm_complete",
            feature_as_of=resolve_feature_as_of(self._feature_as_of),
            agent_id=self._agent_id,
            session_id=self._session_id,
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

            parsed: dict[str, Any] = {
                "id": getattr(response, "id", None),
                "content_text": _chat_text(response),
                "finish_reason": _first_finish_reason(response),
            }

            routing = routing_detail(model, response)
            if routing is not None:
                parsed["routing"] = routing

            usage = getattr(response, "usage", None)
            if usage is not None:
                parsed["usage"] = _chat_usage_detail(usage)
                span.record_perf(
                    tokens_in=getattr(usage, "prompt_tokens", None),
                    tokens_out=getattr(usage, "completion_tokens", None),
                )
            span.record_output(parsed=parsed, parse_status="success")
            return response


class _WrappedChat(_DelegatingWrapper):
    """Exposes an instrumented ``completions``; passes everything else through."""

    def __init__(
        self,
        original: Any,
        *,
        tracer: Tracer,
        project: str,
        component: str,
        feature_as_of: FeatureAsOf = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self._original = original
        self.completions = _WrappedCompletions(
            original.completions,
            tracer=tracer,
            project=project,
            component=component,
            feature_as_of=feature_as_of,
            agent_id=agent_id,
            session_id=session_id,
        )


class _WrappedResponses(_DelegatingWrapper):
    """Instruments ``client.responses.create`` (OpenAI Responses API)."""

    def __init__(
        self,
        original: Any,
        *,
        tracer: Tracer,
        project: str,
        component: str,
        feature_as_of: FeatureAsOf = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self._original = original
        self._tracer = tracer
        self._project = project
        self._component = component
        self._feature_as_of = feature_as_of
        self._agent_id = agent_id
        self._session_id = session_id

    def create(self, **kwargs: Any) -> Any:
        model = kwargs.get("model")
        input_ = kwargs.get("input")
        with self._tracer.span(
            self._project,
            self._component,
            operation="llm_complete",
            feature_as_of=resolve_feature_as_of(self._feature_as_of),
            agent_id=self._agent_id,
            session_id=self._session_id,
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

            parsed: dict[str, Any] = {
                "id": getattr(response, "id", None),
                "content_text": _responses_text(response),
                "status": getattr(response, "status", None),
            }

            routing = routing_detail(model, response)
            if routing is not None:
                parsed["routing"] = routing

            usage = getattr(response, "usage", None)
            if usage is not None:
                parsed["usage"] = _responses_usage_detail(usage)
                span.record_perf(
                    tokens_in=getattr(usage, "input_tokens", None),
                    tokens_out=getattr(usage, "output_tokens", None),
                )
            span.record_output(parsed=parsed, parse_status="success")
            return response


class WrappedOpenAIClient(_DelegatingWrapper):
    """Delegating wrapper. ``chat.completions.create`` — and ``responses.create``
    when the underlying client exposes it — are instrumented; every other
    attribute access passes through to the original client.
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
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self._client = client
        self.chat = _WrappedChat(
            client.chat,
            tracer=tracer,
            project=project,
            component=component,
            feature_as_of=feature_as_of,
            agent_id=agent_id,
            session_id=session_id,
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
                feature_as_of=feature_as_of,
                agent_id=agent_id,
                session_id=session_id,
            )


def wrap_openai(
    client: Any,
    *,
    project: str,
    component: str,
    tracer: Tracer | None = None,
    feature_as_of: FeatureAsOf = None,
    agent_id: str | None = None,
    session_id: str | None = None,
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
        feature_as_of: Point-in-time stamp for every instrumented call — a fixed
            ``datetime``, a zero-arg callable resolved at each call (e.g. reads a
            contextvar a backtest loop sets), or ``None`` (default) to record no
            stamp. Stamping it makes the resulting traces checkable by the
            look-ahead invariants (SPEC §3); a callable that raises is swallowed
            (fail-open) and that trace records ``feature_as_of=None``.
        agent_id: Identity of the executing principal (SPEC §3.1 v1.1), stamped
            on every trace this client produces. Falls back to
            ``TRACEGUARD_AGENT_ID`` when omitted, then NULL.
        session_id: Run / session grouping key (SPEC §3.1 v1.1); same fallback
            via ``TRACEGUARD_SESSION_ID``.

    Returns:
        A :class:`WrappedOpenAIClient` delegating to ``client``.
    """
    return WrappedOpenAIClient(
        client,
        tracer=tracer or default_tracer,
        project=project,
        component=component,
        feature_as_of=feature_as_of,
        agent_id=agent_id,
        session_id=session_id,
    )

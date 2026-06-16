"""Export TraceGuard traces as OpenTelemetry / OpenInference spans.

This is an *additive* exporter: the SQLite/SQLAlchemy store remains the source
of truth (§6.1 of the spec). It maps each ``traces`` row to one OTel span so
time-correct traces can flow into Langfuse, Phoenix, or any OTLP backend
unchanged — without replacing local storage.

Requires the ``otel`` extra::

    pip install "traceguard[otel]"

The exporter is exporter-agnostic: pass any configured ``TracerProvider``
(in-memory/console for tests, OTLP for Langfuse/Phoenix). With no provider it
uses the global one set via ``opentelemetry.trace.set_tracer_provider``.

Example (runnable offline — prints each span to the console)::

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
    from traceguard.exporters.otel import export_trace

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    export_trace(trace, tracer_provider=provider, engine=engine)

To ship spans to Langfuse / Phoenix / any OTLP collector, swap in the OTLP
exporter (the ``otel`` extra installs ``opentelemetry-exporter-otlp-proto-http``)::

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint="...")))
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterable

try:
    from opentelemetry import trace as _ot_trace
    from opentelemetry.trace import Status, StatusCode
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "traceguard OpenTelemetry export requires the 'otel' extra: "
        'pip install "traceguard[otel]"'
    ) from exc

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard.store.models import ModelRegistryEntry, Trace

if TYPE_CHECKING:  # pragma: no cover
    from opentelemetry.trace import Span, TracerProvider

# OpenInference span kind for an LLM/model call. Phoenix and other OpenInference
# consumers key off this attribute to render the span correctly.
_OPENINFERENCE_SPAN_KIND = "openinference.span.kind"

SCOPE_NAME = "traceguard"


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _to_ns(dt: datetime) -> int:
    """UTC-aware datetime -> Unix epoch nanoseconds (OTel span time unit).

    Uses integer arithmetic (datetime resolves to microseconds) to avoid the
    float rounding of ``timestamp() * 1e9``, which would corrupt span duration.
    """
    delta = dt - _EPOCH
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1_000


def _lookup_available_at(trace: Trace, engine: Engine | None) -> datetime | None:
    """Best-effort fetch of the model's ``available_to_us_at`` for the span.

    The traces table stores only ``model_id``; the availability timestamp lives
    in ``model_registry``. We surface it as a span attribute because it is the
    single most decision-relevant fact for look-ahead auditing. Returns None if
    no engine is given or the model is not registered.
    """
    if engine is None or trace.model_id is None:
        return None
    with Session(engine) as sess:
        entry = sess.get(ModelRegistryEntry, trace.model_id)
        return entry.available_to_us_at if entry is not None else None


def trace_to_attributes(
    trace: Trace, *, available_to_us_at: datetime | None = None
) -> dict[str, Any]:
    """Map a ``Trace`` row to OTel/OpenInference span attributes.

    None-valued columns are omitted (OTel attributes may not be None). Mixes
    three vocabularies: OpenInference (``openinference.span.kind``), GenAI
    semantic conventions (``gen_ai.*``), and ``traceguard.*`` for the
    time-integrity facts that have no standard equivalent (input hash, prompt
    hash, feature_as_of, model availability).
    """
    attrs: dict[str, Any] = {
        _OPENINFERENCE_SPAN_KIND: "LLM",
        "traceguard.trace_id": trace.trace_id,
        "traceguard.project": trace.project,
        "traceguard.component": trace.component,
        "traceguard.operation": trace.operation,
        "traceguard.input_hash": trace.input_hash,
        "traceguard.parse_status": trace.parse_status,
    }

    def put(key: str, value: Any) -> None:
        if value is not None:
            attrs[key] = value

    put("traceguard.correlation_id", trace.correlation_id)
    put("traceguard.parent_trace_id", trace.parent_trace_id)
    put("gen_ai.request.model", trace.model_id)
    put("gen_ai.usage.input_tokens", trace.tokens_in)
    put("gen_ai.usage.output_tokens", trace.tokens_out)
    put("traceguard.prompt_template_id", trace.prompt_template_id)
    put("traceguard.prompt_template_hash", trace.prompt_template_hash)
    put("traceguard.latency_ms", trace.latency_ms)
    if trace.cost_usd is not None:
        attrs["traceguard.cost_usd"] = float(trace.cost_usd)
    if trace.feature_as_of is not None:
        attrs["traceguard.feature_as_of"] = trace.feature_as_of.isoformat()
    if available_to_us_at is not None:
        attrs["traceguard.model.available_to_us_at"] = available_to_us_at.isoformat()
    return attrs


def export_trace(
    trace: Trace,
    *,
    tracer_provider: "TracerProvider | None" = None,
    engine: Engine | None = None,
    scope_name: str = SCOPE_NAME,
) -> "Span":
    """Emit one OpenTelemetry span for a completed ``Trace`` and return it.

    The span name is the trace's ``operation``. Span start/end times are derived
    from ``invoked_at`` and ``latency_ms`` so duration reflects the real call.
    Errors map to ``Status(ERROR)`` plus ``exception.*`` attributes. Pass
    ``engine`` to enrich the span with the model's ``available_to_us_at``.
    """
    provider = tracer_provider if tracer_provider is not None else _ot_trace.get_tracer_provider()
    tracer = provider.get_tracer(scope_name)

    attrs = trace_to_attributes(
        trace, available_to_us_at=_lookup_available_at(trace, engine)
    )

    end = trace.invoked_at
    start = end
    if trace.latency_ms is not None:
        start = end - timedelta(milliseconds=trace.latency_ms)

    span = tracer.start_span(trace.operation, start_time=_to_ns(start), attributes=attrs)
    if trace.error_class:
        span.set_status(Status(StatusCode.ERROR, trace.error_message or trace.error_class))
        span.set_attribute("exception.type", trace.error_class)
        if trace.error_message:
            span.set_attribute("exception.message", trace.error_message)
    else:
        span.set_status(Status(StatusCode.OK))
    span.end(end_time=_to_ns(end))
    return span


def export_traces(
    traces: Iterable[Trace],
    *,
    tracer_provider: "TracerProvider | None" = None,
    engine: Engine | None = None,
    scope_name: str = SCOPE_NAME,
) -> int:
    """Export many traces; returns the number of spans emitted."""
    count = 0
    for trace in traces:
        export_trace(
            trace,
            tracer_provider=tracer_provider,
            engine=engine,
            scope_name=scope_name,
        )
        count += 1
    return count


__all__ = ["trace_to_attributes", "export_trace", "export_traces", "SCOPE_NAME"]

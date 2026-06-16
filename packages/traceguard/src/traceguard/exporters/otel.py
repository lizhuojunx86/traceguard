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
from typing import TYPE_CHECKING, Any, Iterable, Mapping

try:
    from opentelemetry import trace as _ot_trace
    from opentelemetry.trace import Status, StatusCode
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "traceguard OpenTelemetry export requires the 'otel' extra: "
        'pip install "traceguard[otel]"'
    ) from exc

from sqlalchemy import select
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
    trace: Trace,
    *,
    available_to_us_at: datetime | None = None,
    model_name: str | None = None,
) -> dict[str, Any]:
    """Map a ``Trace`` row to OTel/OpenInference span attributes.

    None-valued columns are omitted (OTel attributes may not be None). Mixes
    three vocabularies: OpenInference (``openinference.span.kind``), GenAI
    semantic conventions (``gen_ai.*``), and ``traceguard.*`` for the
    time-integrity facts that have no standard equivalent (input hash, prompt
    hash, feature_as_of, model availability).

    ``gen_ai.request.model`` follows the GenAI convention, which expects the
    *vendor* model name (what Phoenix/Langfuse display). The trace stores only
    the internal ``model_id``, so pass ``model_name`` to populate it with the
    vendor name; the internal id is always preserved separately under
    ``traceguard.model_id``. With no ``model_name`` the field falls back to
    ``model_id`` (unchanged default behaviour).
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
    put("traceguard.model_id", trace.model_id)
    put("gen_ai.request.model", model_name if model_name is not None else trace.model_id)
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


def _prefetch_availability(traces: list[Trace], engine: Engine | None) -> dict[str, datetime]:
    """One query: ``{model_id: available_to_us_at}`` for the referenced models.

    Replaces the per-trace registry lookup (an N+1) that :func:`export_trace`
    does standalone; :func:`export_traces` calls this once for the whole batch.
    """
    if engine is None:
        return {}
    model_ids = {t.model_id for t in traces if t.model_id is not None}
    if not model_ids:
        return {}
    with Session(engine) as sess:
        rows = (
            sess.execute(
                select(ModelRegistryEntry).where(ModelRegistryEntry.model_id.in_(model_ids))
            )
            .scalars()
            .all()
        )
    return {r.model_id: r.available_to_us_at for r in rows}


def _build_span(
    trace: Trace,
    tracer: Any,
    *,
    available_to_us_at: datetime | None,
    model_name: str | None,
) -> "Span":
    """Emit one span from a trace with pre-resolved availability/model name (no I/O)."""
    attrs = trace_to_attributes(
        trace, available_to_us_at=available_to_us_at, model_name=model_name
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


def export_trace(
    trace: Trace,
    *,
    tracer_provider: "TracerProvider | None" = None,
    engine: Engine | None = None,
    scope_name: str = SCOPE_NAME,
    model_name: str | None = None,
) -> "Span":
    """Emit one OpenTelemetry span for a completed ``Trace`` and return it.

    The span name is the trace's ``operation``. Span start/end times are derived
    from ``invoked_at`` and ``latency_ms`` so duration reflects the real call.
    Errors map to ``Status(ERROR)`` plus ``exception.*`` attributes. Pass
    ``engine`` to enrich the span with the model's ``available_to_us_at``, and
    ``model_name`` to set ``gen_ai.request.model`` to the vendor model name (the
    internal id stays under ``traceguard.model_id``; see
    :func:`trace_to_attributes`).
    """
    provider = tracer_provider if tracer_provider is not None else _ot_trace.get_tracer_provider()
    tracer = provider.get_tracer(scope_name)
    return _build_span(
        trace,
        tracer,
        available_to_us_at=_lookup_available_at(trace, engine),
        model_name=model_name,
    )


def export_traces(
    traces: Iterable[Trace],
    *,
    tracer_provider: "TracerProvider | None" = None,
    engine: Engine | None = None,
    scope_name: str = SCOPE_NAME,
    model_name_map: "Mapping[str, str] | None" = None,
) -> int:
    """Export many traces; returns the number of spans emitted.

    Model availability is prefetched in a single query for the whole batch (not
    once per trace). ``model_name_map`` maps internal ``model_id`` -> vendor
    model name for ``gen_ai.request.model``; unmapped models fall back to their
    ``model_id`` (see :func:`trace_to_attributes`).
    """
    traces = list(traces)
    provider = tracer_provider if tracer_provider is not None else _ot_trace.get_tracer_provider()
    tracer = provider.get_tracer(scope_name)
    availability = _prefetch_availability(traces, engine)
    for trace in traces:
        model_name = (
            model_name_map.get(trace.model_id)
            if model_name_map is not None and trace.model_id is not None
            else None
        )
        _build_span(
            trace,
            tracer,
            available_to_us_at=(
                availability.get(trace.model_id) if trace.model_id is not None else None
            ),
            model_name=model_name,
        )
    return len(traces)


__all__ = ["trace_to_attributes", "export_trace", "export_traces", "SCOPE_NAME"]

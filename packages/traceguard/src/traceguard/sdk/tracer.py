"""Tracer SDK — decorator + context manager (SPEC §4.1).

Phase 0 ships sync-only instrumentation. The two entry points share a single
``Span`` object that accumulates state and flushes one row to ``traces`` on
exit (success or failure).
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from functools import wraps
from typing import Any, Callable, Iterator

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard.sdk.normalizer import input_hash
from traceguard.store.models import Trace, make_engine


_INPUT_SUMMARY_MAX = 500


def _summarize(data: Any) -> str | None:
    if data is None:
        return None
    text = data if isinstance(data, str) else repr(data)
    return text[:_INPUT_SUMMARY_MAX]


def _to_jsonable(value: Any) -> Any:
    """Best-effort coerce to a JSON-compatible structure for output_parsed."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    dump = getattr(value, "model_dump", None)  # pydantic v2
    if callable(dump):
        try:
            return _to_jsonable(dump())
        except Exception:  # noqa: BLE001 - best effort
            pass
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)[:_INPUT_SUMMARY_MAX]


class Span:
    """Accumulator for one trace row. Created by ``Tracer.span`` / ``Tracer.trace``."""

    def __init__(
        self,
        *,
        project: str,
        component: str,
        operation: str,
        engine: Engine,
        correlation_id: str | None = None,
        feature_as_of: datetime | None = None,
        parent_trace_id: int | None = None,
    ) -> None:
        self.project = project
        self.component = component
        self.operation = operation
        self.correlation_id = correlation_id
        self.feature_as_of = feature_as_of
        self.parent_trace_id = parent_trace_id

        self._engine = engine
        self._start_perf: float = time.perf_counter()
        self._committed = False

        self._input_hash: str | None = None
        self._input_summary: str | None = None
        self._model_id: str | None = None
        self._prompt_template_id: str | None = None
        self._prompt_template_hash: str | None = None
        self._output_parsed: Any = None
        self._parse_status: str | None = None
        self._latency_ms: int | None = None
        self._tokens_in: int | None = None
        self._tokens_out: int | None = None
        self._cost_usd: Decimal | None = None
        self._error_class: str | None = None
        self._error_message: str | None = None

        self.trace_id: int | None = None

    def record_input(self, data: Any) -> None:
        self._input_hash = input_hash(data)
        self._input_summary = _summarize(data)

    def record_model_prompt(
        self,
        *,
        model_id: str | None = None,
        prompt_template_id: str | None = None,
        prompt_template_hash: str | None = None,
    ) -> None:
        if model_id is not None:
            self._model_id = model_id
        if prompt_template_id is not None:
            self._prompt_template_id = prompt_template_id
        if prompt_template_hash is not None:
            self._prompt_template_hash = prompt_template_hash

    def record_output(
        self,
        *,
        parsed: Any = None,
        parse_status: str = "success",
    ) -> None:
        if parse_status not in {"success", "partial", "failed"}:
            raise ValueError(
                f"parse_status must be one of success | partial | failed, got {parse_status!r}"
            )
        self._output_parsed = _to_jsonable(parsed) if parsed is not None else None
        self._parse_status = parse_status

    def record_perf(
        self,
        *,
        latency_ms: int | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: float | Decimal | None = None,
    ) -> None:
        if latency_ms is not None:
            self._latency_ms = int(latency_ms)
        if tokens_in is not None:
            self._tokens_in = int(tokens_in)
        if tokens_out is not None:
            self._tokens_out = int(tokens_out)
        if cost_usd is not None:
            self._cost_usd = Decimal(str(cost_usd))

    def record_error(self, exc: BaseException) -> None:
        self._error_class = type(exc).__name__
        self._error_message = str(exc)
        if self._parse_status is None:
            self._parse_status = "failed"

    def _flush(self) -> None:
        if self._committed:
            return
        if self._input_hash is None:
            self._input_hash = input_hash(None)
        if self._parse_status is None:
            self._parse_status = "success" if self._error_class is None else "failed"
        if self._latency_ms is None:
            self._latency_ms = int((time.perf_counter() - self._start_perf) * 1000)
        row = Trace(
            project=self.project,
            component=self.component,
            operation=self.operation,
            parent_trace_id=self.parent_trace_id,
            correlation_id=self.correlation_id,
            input_hash=self._input_hash,
            input_summary=self._input_summary,
            model_id=self._model_id,
            prompt_template_id=self._prompt_template_id,
            prompt_template_hash=self._prompt_template_hash,
            output_parsed=self._output_parsed,
            parse_status=self._parse_status,
            latency_ms=self._latency_ms,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
            cost_usd=self._cost_usd,
            feature_as_of=self.feature_as_of,
            invoked_at=datetime.now(timezone.utc),
            error_class=self._error_class,
            error_message=self._error_message,
        )
        with Session(self._engine) as sess:
            sess.add(row)
            sess.commit()
            self.trace_id = row.trace_id
        self._committed = True


class Tracer:
    """Holds the persistence engine and emits ``Span`` objects."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = make_engine()
        return self._engine

    def configure(self, engine: Engine) -> None:
        """Override the engine — useful for tests."""
        self._engine = engine

    @contextmanager
    def span(
        self,
        project: str,
        component: str,
        operation: str,
        *,
        correlation_id: str | None = None,
        feature_as_of: datetime | None = None,
        parent_trace_id: int | None = None,
    ) -> Iterator[Span]:
        span = Span(
            project=project,
            component=component,
            operation=operation,
            engine=self.engine,
            correlation_id=correlation_id,
            feature_as_of=feature_as_of,
            parent_trace_id=parent_trace_id,
        )
        try:
            yield span
        except BaseException as exc:
            span.record_error(exc)
            span._flush()
            raise
        else:
            span._flush()

    def trace(
        self,
        project: str,
        component: str,
        operation: str,
        *,
        correlation_from: Callable[..., str] | None = None,
        feature_as_of_from: Callable[..., datetime] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator form. Best-effort auto-records (args, kwargs) as input and
        the return value as ``output_parsed``. For finer control use ``span``.
        """
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                corr = correlation_from(*args, **kwargs) if correlation_from else None
                feat = feature_as_of_from(*args, **kwargs) if feature_as_of_from else None
                with self.span(
                    project,
                    component,
                    operation,
                    correlation_id=corr,
                    feature_as_of=feat,
                ) as sp:
                    sp.record_input({"args": list(args), "kwargs": dict(kwargs)})
                    result = fn(*args, **kwargs)
                    sp.record_output(parsed=result)
                    return result

            return wrapper

        return decorator


tracer = Tracer()
"""Module-level default tracer. Configure via ``TRACEGUARD_DB_URL`` env var,
or call ``tracer.configure(engine)`` to inject a custom engine."""

"""Manual instrumentation — when wrap_openai / wrap_anthropic can't attach.

The wrappers instrument ``client.chat.completions.create`` /
``client.messages.create``. A client that calls the provider REST API directly
(no vendor SDK) or is custom has no such method to wrap — so instrument by hand
with ``Tracer.span`` + ``record_*``. You get the same trace row, including a
look-ahead-checkable ``feature_as_of`` via the public
:func:`traceguard.resolve_feature_as_of` helper (same fail-open semantics as the
wrappers — no need to re-implement callable resolution / naive-datetime handling).

Async note: ``Tracer.span`` is a *synchronous* context manager, but its body may
``await``. Exit flushes one fast synchronous SQLite write, so

    with tracer.span(...) as span:
        span.record_input(payload)
        resp = await my_async_client.post(...)   # await inside the sync CM is fine
        span.record_output(parsed=..., parse_status="success")

is correct and is the natural shape for async / bare-httpx clients.

Run (from repo root)::

    cd packages/traceguard
    uv run python ../../examples/manual_span.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "traceguard" / "src"
if _PKG_SRC.is_dir() and str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from traceguard import resolve_feature_as_of  # noqa: E402
from traceguard.sdk.tracer import Tracer  # noqa: E402
from traceguard.store.models import Trace, make_engine  # noqa: E402

# Simulate the part of a backtest that varies per call: which point in time we
# are simulating. A wrapper takes this via feature_as_of=<callable>; here we
# resolve it ourselves with the same public helper.
_AS_OF = datetime(2025, 6, 1, tzinfo=timezone.utc)


def call_bare_llm(prompt: str) -> dict:
    """Stand-in for a no-SDK client hitting a provider REST endpoint directly."""
    return {"id": "bare_001", "text": f"echo: {prompt}", "in_tokens": 11, "out_tokens": 3}


def main() -> int:
    engine = make_engine("sqlite:///:memory:", create_all=True)
    tracer = Tracer(engine=engine)

    with tracer.span("demo", "bare-client", operation="llm_complete",
                     feature_as_of=resolve_feature_as_of(lambda: _AS_OF)) as span:
        span.record_input({"prompt": "hello", "model": "my-model"})
        span.record_model_prompt(model_id="my-model")
        resp = call_bare_llm("hello")  # in an async client this would be `await`-ed
        span.record_output(
            parsed={"id": resp["id"], "content_text": resp["text"]},
            parse_status="success",
        )
        span.record_perf(tokens_in=resp["in_tokens"], tokens_out=resp["out_tokens"])

    with Session(engine) as sess:
        row = sess.scalars(select(Trace)).one()
    print(f"[manual] trace_id={row.trace_id} model={row.model_id} "
          f"feature_as_of={row.feature_as_of} tokens_in={row.tokens_in} "
          f"status={row.parse_status}")
    assert row.feature_as_of == _AS_OF  # look-ahead checkable, just like the wrappers
    print("[manual] OK — bare client instrumented by hand, trace is PIT-stamped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

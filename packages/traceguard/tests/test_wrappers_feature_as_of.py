"""feature_as_of stamping on wrap_openai / wrap_anthropic.

Stamping ``feature_as_of`` is what turns a wrapper-produced trace from "tracing
only" into one the look-ahead invariants (SPEC §3) can actually check — without
it the rows carry ``feature_as_of=NULL``. These tests pin: backward-compat
(default None), static datetime, per-call callable, fail-open on a raising
callable, and the end-to-end payoff (invariant 2 evaluating a wrapper trace).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.registry.models import register_model
from traceguard.sdk.tracer import Tracer
from traceguard.sdk.wrappers.anthropic import wrap_anthropic
from traceguard.sdk.wrappers.openai import wrap_openai
from traceguard.store.models import Trace
from traceguard.validators.lookahead import InvariantViolation, validate_model_timing

AS_OF = datetime(2024, 6, 1, tzinfo=timezone.utc)


def _openai_client():
    resp = SimpleNamespace(
        id="chatcmpl_x",
        choices=[SimpleNamespace(message=SimpleNamespace(content="bullish"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=1, total_tokens=6),
    )
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: resp)))


def _anthropic_client():
    resp = SimpleNamespace(
        id="msg_x",
        content=[SimpleNamespace(text="ok")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=5, output_tokens=1),
    )
    return SimpleNamespace(messages=SimpleNamespace(create=lambda **k: resp))


@pytest.fixture
def tg(engine):
    return Tracer(engine=engine)


def _traces(engine):
    with Session(engine) as s:
        return s.scalars(select(Trace).order_by(Trace.trace_id)).all()


def test_default_no_feature_as_of_is_backward_compatible(tg, engine):
    w = wrap_openai(_openai_client(), project="p", component="c", tracer=tg)
    w.chat.completions.create(model="gpt-x", messages=[{"role": "user", "content": "hi"}])
    assert _traces(engine)[0].feature_as_of is None


def test_static_datetime_is_stamped(tg, engine):
    w = wrap_openai(_openai_client(), project="p", component="c", tracer=tg, feature_as_of=AS_OF)
    w.chat.completions.create(model="gpt-x", messages=[])
    assert _traces(engine)[0].feature_as_of == AS_OF


def test_callable_resolved_per_call(tg, engine):
    seq = iter([AS_OF, AS_OF + timedelta(days=1), AS_OF + timedelta(days=2)])
    w = wrap_openai(
        _openai_client(), project="p", component="c", tracer=tg, feature_as_of=lambda: next(seq)
    )
    for _ in range(3):
        w.chat.completions.create(model="gpt-x", messages=[])
    assert [r.feature_as_of for r in _traces(engine)] == [
        AS_OF,
        AS_OF + timedelta(days=1),
        AS_OF + timedelta(days=2),
    ]


def test_callable_failure_is_fail_open(tg, engine):
    def boom():
        raise RuntimeError("as-of source down")

    w = wrap_openai(_openai_client(), project="p", component="c", tracer=tg, feature_as_of=boom)
    # The host call must still return (fail-open); the trace honestly records None.
    out = w.chat.completions.create(model="gpt-x", messages=[])
    assert out is not None
    assert _traces(engine)[0].feature_as_of is None


def test_responses_endpoint_also_stamped(tg, engine):
    resp = SimpleNamespace(id="resp_x", output_text="hi", status="completed", usage=None)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: None)),
        responses=SimpleNamespace(create=lambda **k: resp),
    )
    w = wrap_openai(client, project="p", component="c", tracer=tg, feature_as_of=AS_OF)
    w.responses.create(model="gpt-x", input="hi")
    assert _traces(engine)[0].feature_as_of == AS_OF


def test_wrapper_trace_is_now_invariant_checkable(tg, engine):
    # The payoff: a stamped wrapper trace can be validated by look-ahead invariant 2.
    register_model(
        "gpt-x", model_family="gpt", capability_class="chat",
        released_at=AS_OF - timedelta(days=10),
        available_to_us_at=AS_OF - timedelta(days=10), engine=engine,
    )
    w = wrap_openai(_openai_client(), project="p", component="c", tracer=tg, feature_as_of=AS_OF)
    w.chat.completions.create(model="gpt-x", messages=[])
    row = _traces(engine)[0]
    # model existed before as-of -> passes (no raise)
    validate_model_timing(row.model_id, row.feature_as_of, strict=True, engine=engine)


def test_invariant_catches_anachronism_on_wrapper_trace(tg, engine):
    # A model that did not exist yet at as-of -> invariant 2 raises (strict).
    register_model(
        "gpt-future", model_family="gpt", capability_class="chat",
        released_at=AS_OF + timedelta(days=10),
        available_to_us_at=AS_OF + timedelta(days=10), engine=engine,
    )
    resp = SimpleNamespace(
        id="x",
        choices=[SimpleNamespace(message=SimpleNamespace(content="y"), finish_reason="stop")],
        usage=None,
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: resp)))
    w = wrap_openai(client, project="p", component="c", tracer=tg, feature_as_of=AS_OF)
    w.chat.completions.create(model="gpt-future", messages=[])
    row = _traces(engine)[0]
    with pytest.raises(InvariantViolation):
        validate_model_timing(row.model_id, row.feature_as_of, strict=True, engine=engine)


def test_anthropic_static_and_callable(tg, engine):
    w = wrap_anthropic(
        _anthropic_client(), project="p", component="c", tracer=tg, feature_as_of=lambda: AS_OF
    )
    w.messages.create(model="claude-x", messages=[])
    assert _traces(engine)[0].feature_as_of == AS_OF

"""Streaming calls must not emit a silent false-success trace.

A stream=True call returns an iterator, not a materialized response; the
wrappers do not drain it, so text/usage are unknown. Recording parse_status=
'success' with empty text and zero tokens would corrupt the very dataset
TraceGuard exists to make trustworthy. These tests assert the honest 'partial'
behaviour instead, for all three streaming entry points.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.sdk.tracer import Tracer
from traceguard.sdk.wrappers.anthropic import wrap_anthropic
from traceguard.sdk.wrappers.openai import wrap_openai
from traceguard.store.models import Trace


@pytest.fixture
def tg_tracer(engine):
    return Tracer(engine=engine)


class _StreamObj:
    """Stand-in for an SDK Stream/iterator: no .choices/.content/.usage."""

    def __iter__(self):
        return iter([SimpleNamespace(delta="chunk")])


# ---- OpenAI ----------------------------------------------------------------

class _FakeCreate:
    def create(self, **kwargs):
        return _StreamObj() if kwargs.get("stream") else SimpleNamespace(
            id="x", choices=[], usage=None
        )


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCreate()


class _FakeOpenAI:
    def __init__(self):
        self.chat = _FakeChat()
        self.responses = _FakeCreate()


def _one_row(engine) -> Trace:
    with Session(engine) as sess:
        return sess.scalars(select(Trace)).one()


def test_openai_chat_stream_records_partial_not_false_success(tg_tracer, engine):
    wrapped = wrap_openai(_FakeOpenAI(), project="demo", component="x", tracer=tg_tracer)
    result = wrapped.chat.completions.create(
        model="gpt-x", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    assert isinstance(result, _StreamObj)  # returned untouched
    row = _one_row(engine)
    assert row.parse_status == "partial"
    assert row.output_parsed["streaming"] is True
    assert row.tokens_in is None and row.tokens_out is None


def test_openai_responses_stream_records_partial(tg_tracer, engine):
    wrapped = wrap_openai(_FakeOpenAI(), project="demo", component="x", tracer=tg_tracer)
    wrapped.responses.create(model="gpt-x", input="hi", stream=True)
    row = _one_row(engine)
    assert row.parse_status == "partial"
    assert row.output_parsed["streaming"] is True


def test_openai_non_stream_still_success(tg_tracer, engine):
    """Negative control: stream omitted -> normal success path unchanged."""
    wrapped = wrap_openai(_FakeOpenAI(), project="demo", component="x", tracer=tg_tracer)
    wrapped.chat.completions.create(model="gpt-x", messages=[])
    assert _one_row(engine).parse_status == "success"


# ---- Anthropic -------------------------------------------------------------

class _FakeMessages:
    def create(self, **kwargs):
        return _StreamObj() if kwargs.get("stream") else SimpleNamespace(
            id="m", content=[], stop_reason="end_turn", usage=None
        )


class _FakeAnthropic:
    def __init__(self):
        self.messages = _FakeMessages()


def test_anthropic_stream_records_partial_not_false_success(tg_tracer, engine):
    wrapped = wrap_anthropic(_FakeAnthropic(), project="demo", component="x", tracer=tg_tracer)
    result = wrapped.messages.create(
        model="claude-x", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    assert isinstance(result, _StreamObj)
    row = _one_row(engine)
    assert row.parse_status == "partial"
    assert row.output_parsed["streaming"] is True
    assert row.tokens_in is None and row.tokens_out is None


def test_anthropic_non_stream_still_success(tg_tracer, engine):
    wrapped = wrap_anthropic(_FakeAnthropic(), project="demo", component="x", tracer=tg_tracer)
    wrapped.messages.create(model="claude-x", messages=[])
    assert _one_row(engine).parse_status == "success"

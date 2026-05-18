"""Tests for wrap_anthropic — uses a mock client (no real Anthropic SDK)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.sdk.tracer import Tracer
from traceguard.sdk.wrappers.anthropic import wrap_anthropic
from traceguard.store.models import Trace


def _fake_response(text="hello", input_tokens=12, output_tokens=34):
    return SimpleNamespace(
        id="msg_xyz",
        content=[SimpleNamespace(text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self.response


class FakeAnthropicClient:
    def __init__(self, response):
        self.messages = FakeMessages(response)


@pytest.fixture
def tg_tracer(engine):
    return Tracer(engine=engine)


def test_wrap_records_trace_and_returns_original_response(tg_tracer, engine):
    response = _fake_response(text="42")
    client = FakeAnthropicClient(response)
    wrapped = wrap_anthropic(client, project="demo", component="extractor", tracer=tg_tracer)

    result = wrapped.messages.create(
        model="claude-x",
        max_tokens=128,
        messages=[{"role": "user", "content": "hi"}],
    )
    assert result is response  # untouched

    with Session(engine) as sess:
        row = sess.scalars(select(Trace)).one()
    assert row.project == "demo"
    assert row.component == "extractor"
    assert row.operation == "llm_complete"
    assert row.model_id == "claude-x"
    assert row.tokens_in == 12
    assert row.tokens_out == 34
    assert row.parse_status == "success"
    assert row.output_parsed["content_text"] == "42"
    assert row.output_parsed["id"] == "msg_xyz"


def test_wrap_records_error_on_failure(tg_tracer, engine):
    class Boom:
        def create(self, **kwargs):
            raise RuntimeError("api down")

    class BoomClient:
        messages = Boom()

    wrapped = wrap_anthropic(BoomClient(), project="demo", component="x", tracer=tg_tracer)
    with pytest.raises(RuntimeError, match="api down"):
        wrapped.messages.create(model="claude-x", messages=[])

    with Session(engine) as sess:
        row = sess.scalars(select(Trace)).one()
    assert row.error_class == "RuntimeError"
    assert row.parse_status == "failed"
    assert row.model_id == "claude-x"  # recorded before failure

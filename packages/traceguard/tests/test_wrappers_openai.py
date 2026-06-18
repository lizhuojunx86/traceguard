"""Tests for wrap_openai — uses a mock client (no real OpenAI SDK)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.sdk.tracer import Tracer
from traceguard.sdk.wrappers.openai import wrap_openai
from traceguard.store.models import Trace


def _fake_chat_response(text="hello", prompt_tokens=12, completion_tokens=34):
    return SimpleNamespace(
        id="chatcmpl_xyz",
        model="gpt-x",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


def _fake_responses_response(text="hi there", input_tokens=7, output_tokens=9):
    return SimpleNamespace(
        id="resp_abc",
        model="gpt-x",
        output_text=text,
        status="completed",
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
    )


class _FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self.response


class _FakeChat:
    def __init__(self, response):
        self.completions = _FakeCompletions(response)


class _FakeResponsesEndpoint:
    def __init__(self, response):
        self.response = response
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self.response


class FakeOpenAIClient:
    def __init__(self, chat_response, responses_response=None):
        self.chat = _FakeChat(chat_response)
        if responses_response is not None:
            self.responses = _FakeResponsesEndpoint(responses_response)


@pytest.fixture
def tg_tracer(engine):
    return Tracer(engine=engine)


def test_wrap_chat_records_trace_and_returns_original_response(tg_tracer, engine):
    response = _fake_chat_response(text="42")
    client = FakeOpenAIClient(response)
    wrapped = wrap_openai(client, project="demo", component="extractor", tracer=tg_tracer)

    result = wrapped.chat.completions.create(
        model="gpt-x",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert result is response  # untouched

    with Session(engine) as sess:
        row = sess.scalars(select(Trace)).one()
    assert row.project == "demo"
    assert row.component == "extractor"
    assert row.operation == "llm_complete"
    assert row.model_id == "gpt-x"
    assert row.tokens_in == 12
    assert row.tokens_out == 34
    assert row.parse_status == "success"
    assert row.output_parsed["content_text"] == "42"
    assert row.output_parsed["id"] == "chatcmpl_xyz"
    assert row.output_parsed["finish_reason"] == "stop"


def test_wrap_responses_records_trace(tg_tracer, engine):
    chat_response = _fake_chat_response()
    responses_response = _fake_responses_response(text="from responses")
    client = FakeOpenAIClient(chat_response, responses_response=responses_response)
    wrapped = wrap_openai(client, project="demo", component="r", tracer=tg_tracer)

    result = wrapped.responses.create(model="gpt-x", input="hi")
    assert result is responses_response  # untouched

    with Session(engine) as sess:
        row = sess.scalars(select(Trace)).one()
    assert row.operation == "llm_complete"
    assert row.model_id == "gpt-x"
    assert row.tokens_in == 7
    assert row.tokens_out == 9
    assert row.parse_status == "success"
    assert row.output_parsed["content_text"] == "from responses"
    assert row.output_parsed["id"] == "resp_abc"
    assert row.output_parsed["status"] == "completed"


def test_responses_absent_when_client_lacks_it(tg_tracer):
    client = FakeOpenAIClient(_fake_chat_response())  # no responses endpoint
    wrapped = wrap_openai(client, project="demo", component="x", tracer=tg_tracer)
    assert not hasattr(wrapped, "responses")


def test_wrap_records_error_on_failure(tg_tracer, engine):
    class Boom:
        def create(self, **kwargs):
            raise RuntimeError("api down")

    class BoomChat:
        completions = Boom()

    class BoomClient:
        chat = BoomChat()

    wrapped = wrap_openai(BoomClient(), project="demo", component="x", tracer=tg_tracer)
    with pytest.raises(RuntimeError, match="api down"):
        wrapped.chat.completions.create(model="gpt-x", messages=[])

    with Session(engine) as sess:
        row = sess.scalars(select(Trace)).one()
    assert row.error_class == "RuntimeError"
    assert row.parse_status == "failed"
    assert row.model_id == "gpt-x"  # recorded before failure


def test_passthrough_preserves_other_attributes(tg_tracer):
    client = FakeOpenAIClient(_fake_chat_response())
    client.api_key = "sk-test"  # arbitrary passthrough attr
    wrapped = wrap_openai(client, project="demo", component="x", tracer=tg_tracer)
    assert wrapped.api_key == "sk-test"

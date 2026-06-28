"""Regression: a wrapped client must survive copy.deepcopy / copy.copy.

Frameworks such as LangChain/LlamaIndex copy LLM clients. Before the
``_DelegatingWrapper`` guard, copying a ``wrap_openai`` / ``wrap_anthropic``
client crashed: the copy protocol reconstructs an instance via
``cls.__new__(cls)`` (delegate attr unset) and then probes it for
``__setstate__`` / ``__reduce_ex__`` etc.; the old ``__getattr__`` forwarded
those private lookups to the not-yet-set delegate attribute and recursed forever
(``RecursionError``). Even past that, deepcopy reached the engine-backed
``Tracer`` (not deep-copyable) and died with ``TypeError: cannot pickle
'module' object``.

The fix has two halves and these tests pin both:
  * ``__getattr__`` raises ``AttributeError`` for any ``_``-prefixed name, so the
    reconstruct/probe path falls back instead of recursing. Exercised by
    ``copy.copy`` (no custom ``__copy__`` -> goes through the recursion-prone
    reconstruct) and by the half-constructed-instance tests below — both
    ``RecursionError`` on pre-fix code.
  * ``__deepcopy__`` shares the tracer by reference and deep-copies the rest, so
    ``copy.deepcopy`` neither recurses nor chokes on the engine.

Note on pickle: ``pickle.dumps`` runs on a *fully constructed* instance whose
delegate attr is set, so ``__getattr__`` resolves cleanly and never recursed —
there is no pickle-dumps regression to guard. The reconstruct-side recursion
(``pickle.loads`` / ``cls.__new__``) is covered by the half-constructed tests.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.sdk.tracer import Tracer
from traceguard.sdk.wrappers.anthropic import WrappedAnthropicClient, _WrappedMessages, wrap_anthropic
from traceguard.sdk.wrappers.openai import WrappedOpenAIClient, _WrappedChat, wrap_openai
from traceguard.store.models import Trace


# ── fakes (no real SDK; deep-copyable plain objects) ──────────────────────────
def _openai_response():
    return SimpleNamespace(
        id="chatcmpl_xyz",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="hi"),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4, total_tokens=7),
    )


class _FakeCompletions:
    def __init__(self, response):
        self.response = response

    def create(self, **kwargs):
        return self.response


class _FakeChat:
    def __init__(self, response):
        self.completions = _FakeCompletions(response)


class _FakeOpenAIClient:
    def __init__(self, response):
        self.chat = _FakeChat(response)
        self.api_key = "sk-test"  # public passthrough attr


def _anthropic_response():
    return SimpleNamespace(
        id="msg_xyz",
        content=[SimpleNamespace(text="hi")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=3, output_tokens=4),
    )


class _FakeMessages:
    def __init__(self, response):
        self.response = response

    def create(self, **kwargs):
        return self.response


class _FakeAnthropicClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)
        self.api_key = "sk-ant-test"  # public passthrough attr


@pytest.fixture
def tg_tracer(engine):
    return Tracer(engine=engine)


# ── deepcopy: shares the tracer, deep-copies the client, stays functional ─────
def test_deepcopy_openai_wrapper_succeeds_and_stays_functional(tg_tracer, engine):
    wrapped = wrap_openai(
        _FakeOpenAIClient(_openai_response()),
        project="demo",
        component="x",
        tracer=tg_tracer,
    )

    clone = copy.deepcopy(wrapped)  # pre-fix: crashed (RecursionError / engine TypeError)

    # Tracer is shared (a process-level sink, never copied); the client is not.
    assert clone.chat.completions._tracer is wrapped.chat.completions._tracer
    assert clone._client is not wrapped._client
    # Public passthrough survives the copy (goes through the guarded __getattr__).
    assert clone.api_key == "sk-test"

    # The clone is still instrumented: calling it writes a trace to the shared store.
    clone.chat.completions.create(model="gpt-x", messages=[{"role": "user", "content": "hi"}])
    with Session(engine) as sess:
        row = sess.scalars(select(Trace)).one()
    assert row.project == "demo"
    assert row.model_id == "gpt-x"
    assert row.tokens_in == 3


def test_deepcopy_anthropic_wrapper_succeeds_and_stays_functional(tg_tracer, engine):
    wrapped = wrap_anthropic(
        _FakeAnthropicClient(_anthropic_response()),
        project="demo",
        component="x",
        tracer=tg_tracer,
    )

    clone = copy.deepcopy(wrapped)  # pre-fix: crashed (RecursionError / engine TypeError)

    assert clone.messages._tracer is wrapped.messages._tracer
    assert clone._client is not wrapped._client
    assert clone.api_key == "sk-ant-test"

    clone.messages.create(model="claude-x", messages=[{"role": "user", "content": "hi"}])
    with Session(engine) as sess:
        row = sess.scalars(select(Trace)).one()
    assert row.project == "demo"
    assert row.model_id == "claude-x"
    assert row.tokens_in == 3


# ── copy.copy: shallow copy goes through the recursion-prone reconstruct path ──
# (no custom __copy__, so the __getattr__ guard is what keeps it from recursing).
def test_shallow_copy_openai_wrapper_does_not_recurse(tg_tracer):
    wrapped = wrap_openai(
        _FakeOpenAIClient(_openai_response()), project="demo", component="x", tracer=tg_tracer
    )
    clone = copy.copy(wrapped)  # pre-fix: RecursionError
    assert clone is not wrapped
    assert clone.api_key == "sk-test"  # passthrough through the guarded __getattr__


def test_shallow_copy_anthropic_wrapper_does_not_recurse(tg_tracer):
    wrapped = wrap_anthropic(
        _FakeAnthropicClient(_anthropic_response()), project="demo", component="x", tracer=tg_tracer
    )
    clone = copy.copy(wrapped)  # pre-fix: RecursionError
    assert clone is not wrapped
    assert clone.api_key == "sk-ant-test"


# ── the guard itself, exercised on the exact state the copy/pickle protocol hits ─
@pytest.mark.parametrize(
    "cls, delegate_attr",
    [
        (WrappedOpenAIClient, "_client"),
        (WrappedAnthropicClient, "_client"),
        (_WrappedChat, "_original"),
        (_WrappedMessages, "_original"),
    ],
)
def test_getattr_guard_does_not_recurse_on_half_constructed_instance(cls, delegate_attr):
    # cls.__new__ skips __init__, leaving the delegate attr unset — exactly what
    # copy.deepcopy._reconstruct and pickle.loads produce before restoring state.
    # A private/dunder lookup must raise AttributeError, NOT recurse forever
    # resolving the not-yet-set delegate attribute (the pre-fix RecursionError).
    half = cls.__new__(cls)
    with pytest.raises(AttributeError):
        half.__setstate__  # noqa: B018 — the dunder the copy/pickle protocol probes
    with pytest.raises(AttributeError):
        getattr(half, delegate_attr)  # the delegate attr lookup must not recurse either

"""agent_id / session_id (SPEC §3.1 / §4.1, v1.1): SDK plumbing + honest audit boundary.

Forward: explicit argument > TRACEGUARD_AGENT_ID / TRACEGUARD_SESSION_ID > NULL,
through span, the decorator, and both client wrappers.

Reverse (what the new columns do NOT do): they never enter input_hash, and
they are outside the audit algo v1 hash envelope — editing them in the DB
file is invisible to verify_chain. That last test is the documented boundary,
pinned so nobody later "improves" the docs to claim otherwise.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard import audit
from traceguard.audit.canonical import (
    GENESIS_PREV_HASH,
    TRACE_CONTENT_FIELDS,
    compute_row_hash,
    entry_payload,
    trace_content,
)
from traceguard.sdk.tracer import AGENT_ID_ENV, SESSION_ID_ENV, Tracer
from traceguard.sdk.wrappers.anthropic import wrap_anthropic
from traceguard.sdk.wrappers.openai import wrap_openai
from traceguard.store.models import Trace


@pytest.fixture
def tg_tracer(engine):
    return Tracer(engine=engine)


@pytest.fixture(autouse=True)
def _clean_identity_env(monkeypatch):
    monkeypatch.delenv(AGENT_ID_ENV, raising=False)
    monkeypatch.delenv(SESSION_ID_ENV, raising=False)


def _one(engine) -> Trace:
    with Session(engine) as sess:
        return sess.scalars(select(Trace)).one()


def _all(engine) -> list[Trace]:
    with Session(engine) as sess:
        return list(sess.scalars(select(Trace).order_by(Trace.trace_id)))


# ── forward: the values land in the columns ───────────────────────────────


def test_span_explicit_identity_is_stored(tg_tracer, engine):
    with tg_tracer.span("demo", "x", "llm_complete", agent_id="agent-A", session_id="run-1") as sp:
        sp.record_input({"q": 1})
    row = _one(engine)
    assert (row.agent_id, row.session_id) == ("agent-A", "run-1")


def test_span_without_identity_stores_null(tg_tracer, engine):
    with tg_tracer.span("demo", "x", "llm_complete"):
        pass
    row = _one(engine)
    assert row.agent_id is None and row.session_id is None


def test_env_fallback_is_read_per_span(tg_tracer, engine, monkeypatch):
    monkeypatch.setenv(AGENT_ID_ENV, "env-agent")
    monkeypatch.setenv(SESSION_ID_ENV, "env-session")
    with tg_tracer.span("demo", "x", "llm_complete"):
        pass
    monkeypatch.setenv(SESSION_ID_ENV, "env-session-2")  # changed AFTER import/first span
    with tg_tracer.span("demo", "x", "llm_complete"):
        pass
    rows = _all(engine)
    assert [(r.agent_id, r.session_id) for r in rows] == [
        ("env-agent", "env-session"),
        ("env-agent", "env-session-2"),
    ]


def test_explicit_wins_over_env(tg_tracer, engine, monkeypatch):
    monkeypatch.setenv(AGENT_ID_ENV, "env-agent")
    monkeypatch.setenv(SESSION_ID_ENV, "env-session")
    with tg_tracer.span("demo", "x", "llm_complete", agent_id="explicit"):
        pass
    row = _one(engine)
    assert row.agent_id == "explicit"
    assert row.session_id == "env-session"  # only the explicit one is overridden


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_blank_env_reads_as_unset(tg_tracer, engine, monkeypatch, value):
    monkeypatch.setenv(AGENT_ID_ENV, value)
    with tg_tracer.span("demo", "x", "llm_complete"):
        pass
    assert _one(engine).agent_id is None


def test_env_value_is_stripped(tg_tracer, engine, monkeypatch):
    monkeypatch.setenv(AGENT_ID_ENV, "  padded  ")
    with tg_tracer.span("demo", "x", "llm_complete"):
        pass
    assert _one(engine).agent_id == "padded"


def test_decorator_accepts_identity(tg_tracer, engine):
    @tg_tracer.trace("demo", "fn", "parse", agent_id="deco-agent", session_id="deco-run")
    def double(x):
        return 2 * x

    assert double(21) == 42
    row = _one(engine)
    assert (row.agent_id, row.session_id) == ("deco-agent", "deco-run")


def test_decorator_falls_back_to_env(tg_tracer, engine, monkeypatch):
    @tg_tracer.trace("demo", "fn", "parse")
    def ident(x):
        return x

    monkeypatch.setenv(AGENT_ID_ENV, "late-env")  # set after decoration, before the call
    ident(1)
    assert _one(engine).agent_id == "late-env"


def test_identity_does_not_enter_input_hash(tg_tracer, engine):
    for agent in ("A", "B"):
        with tg_tracer.span("demo", "x", "llm_complete", agent_id=agent) as sp:
            sp.record_input({"same": "input"})
    a, b = _all(engine)
    assert a.agent_id != b.agent_id
    assert a.input_hash == b.input_hash


# ── wrappers ──────────────────────────────────────────────────────────────


class _FakeMessages:
    def __init__(self, response):
        self.response = response

    def create(self, **kwargs):
        return self.response


class _FakeAnthropic:
    def __init__(self):
        self.messages = _FakeMessages(
            SimpleNamespace(
                id="msg",
                content=[SimpleNamespace(text="hi")],
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )
        )


def test_wrap_anthropic_passes_identity(tg_tracer, engine):
    wrapped = wrap_anthropic(
        _FakeAnthropic(),
        project="p",
        component="c",
        tracer=tg_tracer,
        agent_id="anth-agent",
        session_id="anth-run",
    )
    wrapped.messages.create(model="claude-x", messages=[])
    row = _one(engine)
    assert (row.agent_id, row.session_id) == ("anth-agent", "anth-run")


def test_wrap_anthropic_env_fallback(tg_tracer, engine, monkeypatch):
    wrapped = wrap_anthropic(_FakeAnthropic(), project="p", component="c", tracer=tg_tracer)
    monkeypatch.setenv(SESSION_ID_ENV, "from-env")
    wrapped.messages.create(model="claude-x", messages=[])
    assert _one(engine).session_id == "from-env"


class _FakeCompletions:
    def create(self, **kwargs):
        return SimpleNamespace(
            id="chat",
            model="gpt-x",
            choices=[SimpleNamespace(message=SimpleNamespace(content="hi"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )


class _FakeResponses:
    def create(self, **kwargs):
        return SimpleNamespace(
            id="resp",
            model="gpt-x",
            output_text="hi",
            status="completed",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


class _FakeOpenAI:
    def __init__(self):
        self.chat = SimpleNamespace(completions=_FakeCompletions())
        self.responses = _FakeResponses()


def test_wrap_openai_passes_identity_on_both_endpoints(tg_tracer, engine):
    wrapped = wrap_openai(
        _FakeOpenAI(),
        project="p",
        component="c",
        tracer=tg_tracer,
        agent_id="oai-agent",
        session_id="oai-run",
    )
    wrapped.chat.completions.create(model="gpt-x", messages=[])
    wrapped.responses.create(model="gpt-x", input="hi")
    rows = _all(engine)
    assert len(rows) == 2
    assert all((r.agent_id, r.session_id) == ("oai-agent", "oai-run") for r in rows)


# ── reverse: the honest audit boundary ────────────────────────────────────


@pytest.fixture
def audited(engine):
    audit.enable(engine, backfill=False)
    yield engine
    audit.detach(engine)
    audit.set_strict(False)


def test_new_columns_are_outside_the_algo_v1_envelope():
    assert "agent_id" not in TRACE_CONTENT_FIELDS
    assert "session_id" not in TRACE_CONTENT_FIELDS


def test_row_hash_is_identical_with_and_without_identity(audited):
    """Physical proof that the envelope ignores the new columns: same preimage."""
    tracer = Tracer(engine=audited)
    with tracer.span("p", "c", "llm_complete") as sp:
        sp.record_input({"x": 1})
    with tracer.span("p", "c", "llm_complete", agent_id="A", session_id="S") as sp:
        sp.record_input({"x": 1})
    plain, tagged = _all(audited)
    assert (plain.agent_id, tagged.agent_id) == (None, "A")

    def preimage_hash(trace: Trace) -> str:
        content = trace_content(trace)
        content["trace_id"] = 0  # normalise the one field that legitimately differs
        content["invoked_at"] = datetime(2026, 1, 1, tzinfo=timezone.utc)
        payload = entry_payload(
            entry_type="write",
            trace_id=0,
            event_id=None,
            cost_at_event=None,
            note=None,
            canon_status="ok",
            canon_error=None,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            content=content,
        )
        return compute_row_hash(GENESIS_PREV_HASH, payload)

    assert preimage_hash(plain) == preimage_hash(tagged)


def test_editing_identity_in_the_db_file_is_invisible_to_verify(audited):
    """Documented boundary (docs/audit.md, contract-status section): agent_id /
    session_id are append-only under the guard but NOT attested by the chain."""
    tracer = Tracer(engine=audited)
    with tracer.span("p", "c", "llm_complete", agent_id="honest", session_id="s") as sp:
        sp.record_input({"x": 1})
    assert audit.verify_chain(audited).ok
    with audited.begin() as conn:
        conn.exec_driver_sql("UPDATE traces SET agent_id='forged', session_id='forged'")
    result = audit.verify_chain(audited)
    assert result.ok, "if this ever fails, the envelope changed — that is algo v2, not v1"
    assert _one(audited).agent_id == "forged"


def test_guard_still_blocks_orm_updates_to_identity(audited):
    tracer = Tracer(engine=audited)
    with tracer.span("p", "c", "llm_complete", agent_id="a1"):
        pass
    with Session(audited) as sess:
        row = sess.scalars(select(Trace)).one()
        row.agent_id = "a2"
        with pytest.raises(audit.AppendOnlyViolationError):
            sess.commit()

"""traces.provider_response_id (SPEC v1.2): capture, migration, and NULL honesty.

The column is a join key for per-request reconciliation, so the interesting
assertions are about when it is NOT set: a fabricated id would reconcile as a
real call.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.audit.canonical import TRACE_CONTENT_FIELDS
from traceguard.sdk.tracer import Tracer
from traceguard.sdk.wrappers.anthropic import wrap_anthropic
from traceguard.sdk.wrappers.openai import wrap_openai
from traceguard.store.models import (
    TRACE_COLUMN_SPEC_VERSION,
    TRACE_COLUMNS_ADDED_SINCE_1_0,
    Trace,
)


def _one(engine) -> Trace:
    with Session(engine) as sess:
        return sess.scalars(select(Trace)).one()


def _rows(engine) -> list[Trace]:
    with Session(engine) as sess:
        return list(sess.scalars(select(Trace).order_by(Trace.trace_id)))


# ── Anthropic ───────────────────────────────────────────────────────────────

class _FakeMessages:
    def create(self, **kwargs):
        if kwargs.get("stream"):
            return SimpleNamespace()  # a Stream: no id until drained
        return SimpleNamespace(id="msg_01ABC", content=[], stop_reason="end_turn", usage=None)


class _FakeAnthropic:
    def __init__(self):
        self.messages = _FakeMessages()


def test_anthropic_records_the_message_id(engine):
    tg = Tracer(engine=engine)
    wrapped = wrap_anthropic(_FakeAnthropic(), project="p", component="c", tracer=tg)
    wrapped.messages.create(model="claude-x", messages=[])
    row = _one(engine)
    assert row.provider_response_id == "msg_01ABC"
    assert row.output_parsed["id"] == "msg_01ABC"  # still in the JSON, not backfilled away


def test_anthropic_streaming_leaves_it_null(engine):
    """No drained stream means no final message, so no id. SPEC §3.1 (v1.2)
    requires NULL over a guess — a synthesized id would reconcile as a real call."""
    tg = Tracer(engine=engine)
    wrapped = wrap_anthropic(_FakeAnthropic(), project="p", component="c", tracer=tg)
    wrapped.messages.create(model="claude-x", messages=[], stream=True)
    row = _one(engine)
    assert row.provider_response_id is None
    assert row.parse_status == "partial"


# ── OpenAI ──────────────────────────────────────────────────────────────────

class _FakeCreate:
    def create(self, **kwargs):
        if kwargs.get("stream"):
            return SimpleNamespace()
        return SimpleNamespace(
            id="resp_01XYZ", choices=[], usage=None, output_text="hi", status="completed"
        )


class _FakeOpenAI:
    def __init__(self):
        self.chat = SimpleNamespace(completions=_FakeCreate())
        self.responses = _FakeCreate()


def test_openai_chat_records_the_response_id(engine):
    tg = Tracer(engine=engine)
    wrapped = wrap_openai(_FakeOpenAI(), project="p", component="c", tracer=tg)
    wrapped.chat.completions.create(model="gpt-x", messages=[])
    assert _one(engine).provider_response_id == "resp_01XYZ"


def test_openai_responses_records_the_response_id(engine):
    tg = Tracer(engine=engine)
    wrapped = wrap_openai(_FakeOpenAI(), project="p", component="c", tracer=tg)
    wrapped.responses.create(model="gpt-x", input="hi")
    assert _one(engine).provider_response_id == "resp_01XYZ"


def test_openai_streaming_leaves_it_null(engine):
    tg = Tracer(engine=engine)
    wrapped = wrap_openai(_FakeOpenAI(), project="p", component="c", tracer=tg)
    wrapped.chat.completions.create(model="gpt-x", messages=[], stream=True)
    wrapped.responses.create(model="gpt-x", input="hi", stream=True)
    assert [r.provider_response_id for r in _rows(engine)] == [None, None]


# ── the recorder refuses to invent one ──────────────────────────────────────

def test_a_non_string_or_empty_id_records_nothing(engine):
    """'the provider gave none' and 'here is an id' are different facts."""
    tg = Tracer(engine=engine)
    for value in (None, "", 12345, object()):
        with tg.span("p", "c", "llm_complete") as span:
            span.record_provider_response_id(value)
    assert all(r.provider_response_id is None for r in _rows(engine))


def test_a_real_id_is_recorded(engine):
    tg = Tracer(engine=engine)
    with tg.span("p", "c", "llm_complete") as span:
        span.record_provider_response_id("msg_real")
    assert _one(engine).provider_response_id == "msg_real"


# ── contract facts ──────────────────────────────────────────────────────────

def test_it_is_outside_the_algo_v1_hash_envelope():
    """Same posture as agent_id/session_id: append-only under the guard, NOT
    attested by the chain. Golden tests pin the envelope byte-for-byte."""
    assert "provider_response_id" not in TRACE_CONTENT_FIELDS


def test_it_is_registered_for_migration_with_its_spec_version():
    assert "provider_response_id" in TRACE_COLUMNS_ADDED_SINCE_1_0
    assert TRACE_COLUMN_SPEC_VERSION["provider_response_id"] == "v1.2"
    # The mapping drives the tuple, so the two can never disagree.
    assert TRACE_COLUMNS_ADDED_SINCE_1_0 == tuple(TRACE_COLUMN_SPEC_VERSION)


def test_it_takes_no_part_in_input_hash(engine):
    """SPEC §3.1: not in input_hash, not in invariants 1-4."""
    tg = Tracer(engine=engine)
    with tg.span("p", "c", "llm_complete") as a:
        a.record_input({"q": "same"})
        a.record_provider_response_id("msg_A")
    with tg.span("p", "c", "llm_complete") as b:
        b.record_input({"q": "same"})
        b.record_provider_response_id("msg_B")
    rows = _rows(engine)
    assert rows[0].input_hash == rows[1].input_hash
    assert rows[0].provider_response_id != rows[1].provider_response_id


def test_the_column_is_indexed():
    """It is a join key over tens of thousands of rows; an unindexed column
    would make per-request reconciliation a table scan per id."""
    assert Trace.__table__.c.provider_response_id.index is True


def test_ingest_fills_it_from_the_transcript_message_id(engine):
    from traceguard.routing_audit.ingest_claude_code import ParsedRecord, _build_trace

    rec = ParsedRecord(
        project="cc",
        component="main",
        model_id="claude-x",
        usage={"input_tokens": 1, "output_tokens": 1},
        parse_status="success",
        invoked_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        is_error=False,
        source_message_id="msg_01FROMTRANSCRIPT",
        source_session_id="sess-1",
        source_uuid="u",
        source_file="main.jsonl",
        agent_id=None,
        meta={"message_id": "msg_01FROMTRANSCRIPT", "session_id": "sess-1"},
    )
    assert _build_trace(rec).provider_response_id == "msg_01FROMTRANSCRIPT"


def test_ingest_leaves_it_null_when_the_line_had_no_api_message_id(engine):
    """Lines that fell back to `uuid:<line uuid>` have no provider id at all.
    Storing the locally-minted fallback would look like a provider's id."""
    from traceguard.routing_audit.ingest_claude_code import ParsedRecord, _build_trace

    rec = ParsedRecord(
        project="cc",
        component="main",
        model_id=None,
        usage=None,
        parse_status="failed",
        invoked_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        is_error=True,
        source_message_id="uuid:abc-123",
        source_session_id="sess-1",
        source_uuid="abc-123",
        source_file="main.jsonl",
        agent_id=None,
        meta={"message_id": None, "session_id": "sess-1"},
    )
    assert _build_trace(rec).provider_response_id is None

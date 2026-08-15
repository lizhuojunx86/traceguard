"""Tests for wrap_anthropic — uses a mock client (no real Anthropic SDK)."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.routing_audit.ingest_claude_code import _usage_tokens
from traceguard.routing_audit.pricing import compute_cost_usd, price_for
from traceguard.sdk.tracer import Tracer
from traceguard.sdk.wrappers.anthropic import wrap_anthropic
from traceguard.store.models import Trace

# A model that exists in the pricing table, so the recompute-from-store
# assertions below exercise a real price sheet rather than a stub.
_PRICED_MODEL = "claude-opus-5"


def _fake_response(text="hello", input_tokens=12, output_tokens=34):
    return SimpleNamespace(
        id="msg_xyz",
        content=[SimpleNamespace(text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _cached_usage(
    input_tokens=100,
    output_tokens=50,
    cache_read=900_000,
    cache_creation=8_000,
    cache_5m=6_000,
    cache_1h=2_000,
    service_tier="standard",
    speed=None,
):
    """A modern Messages API usage block: cache counts are SEPARATE from input_tokens.

    Defaults keep the TTL split self-consistent (5m + 1h == the total) so the
    reconciliation clamp in ``pricing.cache_creation_split`` is a no-op and the
    cost assertions read the split at face value.
    """
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
        cache_creation=SimpleNamespace(
            ephemeral_5m_input_tokens=cache_5m,
            ephemeral_1h_input_tokens=cache_1h,
        ),
        service_tier=service_tier,
        speed=speed,
    )


def _response_with_usage(usage, text="hello"):
    return SimpleNamespace(
        id="msg_xyz",
        content=[SimpleNamespace(text=text)],
        stop_reason="end_turn",
        usage=usage,
    )


def _one_trace(engine):
    with Session(engine) as sess:
        return sess.scalars(select(Trace)).one()


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


def test_tokens_in_is_full_prompt_volume_with_cache(tg_tracer, engine):
    """tokens_in sums the three mutually exclusive input counts, and the split
    survives into output_parsed in the store's flat shape."""
    usage = _cached_usage()
    client = FakeAnthropicClient(_response_with_usage(usage))
    wrapped = wrap_anthropic(client, project="demo", component="agent", tracer=tg_tracer)
    wrapped.messages.create(model=_PRICED_MODEL, messages=[{"role": "user", "content": "hi"}])

    row = _one_trace(engine)
    assert row.tokens_in == 100 + 900_000 + 8_000
    assert row.tokens_out == 50
    assert row.output_parsed["usage"] == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 900_000,
        "cache_creation_input_tokens": 8_000,
        "cache_creation_5m": 6_000,
        "cache_creation_1h": 2_000,
        "service_tier": "standard",
        "speed": None,
    }


def test_tokens_in_matches_ingest_convention(tg_tracer, engine):
    """The wrapper and the Claude Code ingest must agree on tokens_in for the
    same usage block — one convention, two writers (SPEC-adjacent invariant)."""
    usage = _cached_usage()
    client = FakeAnthropicClient(_response_with_usage(usage))
    wrapped = wrap_anthropic(client, project="demo", component="agent", tracer=tg_tracer)
    wrapped.messages.create(model=_PRICED_MODEL, messages=[])

    row = _one_trace(engine)
    ingest_in, ingest_out = _usage_tokens(row.output_parsed["usage"])
    assert row.tokens_in == ingest_in
    assert row.tokens_out == ingest_out


def test_stored_usage_is_repriceable(tg_tracer, engine):
    """output_parsed.usage feeds compute_cost_usd directly, 1-hour bucket included."""
    usage = _cached_usage()
    client = FakeAnthropicClient(_response_with_usage(usage))
    wrapped = wrap_anthropic(client, project="demo", component="agent", tracer=tg_tracer)
    wrapped.messages.create(model=_PRICED_MODEL, messages=[])

    stored = _one_trace(engine).output_parsed["usage"]
    price = price_for(_PRICED_MODEL, None)
    assert price is not None
    expected = (
        100 * price.input_per_mtok
        + 900_000 * price.input_per_mtok * price.cache_read_mult
        + 6_000 * price.input_per_mtok * price.cache_write_5m_mult
        + 2_000 * price.input_per_mtok * price.cache_write_1h_mult
        + 50 * price.output_per_mtok
    ) / Decimal(1_000_000)
    assert compute_cost_usd(_PRICED_MODEL, stored) == expected.quantize(Decimal("0.000001"))

    # The 1-hour bucket bills at its own multiplier rather than collapsing into
    # the 5-minute rate: shifting the whole cache-creation total from 5m to 1h
    # must move the bill by exactly the multiplier delta on those tokens.
    all_5m = dict(stored, cache_creation_5m=8_000, cache_creation_1h=0)
    all_1h = dict(stored, cache_creation_5m=0, cache_creation_1h=8_000)
    delta = compute_cost_usd(_PRICED_MODEL, all_1h) - compute_cost_usd(_PRICED_MODEL, all_5m)
    premium = (
        8_000
        * price.input_per_mtok
        * (price.cache_write_1h_mult - price.cache_write_5m_mult)
        / Decimal(1_000_000)
    )
    assert delta == premium.quantize(Decimal("0.000001"))


def test_usage_without_cache_fields_behaves_as_before(tg_tracer, engine):
    """Old response shape (no cache attributes at all) is unchanged: tokens_in
    is still just input_tokens, and the absent counts record as None."""
    usage = SimpleNamespace(input_tokens=12, output_tokens=34)
    client = FakeAnthropicClient(_response_with_usage(usage))
    wrapped = wrap_anthropic(client, project="demo", component="x", tracer=tg_tracer)
    wrapped.messages.create(model="claude-x", messages=[])

    row = _one_trace(engine)
    assert row.tokens_in == 12
    assert row.tokens_out == 34
    stored = row.output_parsed["usage"]
    assert stored["input_tokens"] == 12
    assert stored["cache_read_input_tokens"] is None
    assert stored["cache_creation_input_tokens"] is None
    assert stored["cache_creation_5m"] is None
    assert stored["cache_creation_1h"] is None


def test_missing_usage_records_no_tokens(tg_tracer, engine):
    """No usage block at all → no perf recorded and no usage key (not zeros)."""
    client = FakeAnthropicClient(_response_with_usage(None))
    wrapped = wrap_anthropic(client, project="demo", component="x", tracer=tg_tracer)
    wrapped.messages.create(model="claude-x", messages=[])

    row = _one_trace(engine)
    assert row.tokens_in is None
    assert row.tokens_out is None
    assert row.parse_status == "success"
    assert "usage" not in row.output_parsed


def test_streaming_still_records_partial_without_tokens(tg_tracer, engine):
    """Streaming is untouched by the usage change: usage is unavailable until
    the caller drains the stream, so 'partial' with no tokens stays correct."""
    client = FakeAnthropicClient(_response_with_usage(_cached_usage()))
    wrapped = wrap_anthropic(client, project="demo", component="x", tracer=tg_tracer)
    wrapped.messages.create(model="claude-x", messages=[], stream=True)

    row = _one_trace(engine)
    assert row.parse_status == "partial"
    assert row.tokens_in is None
    assert row.tokens_out is None
    assert row.output_parsed["streaming"] is True
    assert "usage" not in row.output_parsed

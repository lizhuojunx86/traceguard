"""Tests for the cache-efficiency audit (read-only; no API calls, no writes).

Dollar expectations are recomputed from ``pricing.PRICES`` at run time rather
than frozen as constants, so a legitimate price recalibration flows through
instead of turning into a stale-snapshot failure. Where a check can be made
price-independent (ratios, hit rates, saved %) it is.
"""
from __future__ import annotations

import csv
import io
import itertools
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from traceguard.routing_audit.cache_audit import (
    CC_SOURCE,
    DirectRow,
    MIN_CACHEABLE_TOKENS,
    _read_only_url,
    audit,
    build_sections,
    format_audit,
    main as cache_audit_main,
    parse_bound,
    pings_to_bridge,
)
from traceguard.routing_audit.pricing import (
    PRICES,
    SONNET5_INTRO,
    SONNET5_STANDARD,
    SONNET5_STANDARD_FROM,
    price_for,
)
from traceguard.store.models import Base, Trace, make_engine

_MTOK = Decimal(1_000_000)
T0 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
OPUS = "claude-opus-4-8"
SONNET = "claude-sonnet-5"
UNPRICED = "claude-nosuch-9"

# prompt = 1000 + 9000 + 1000 + 3000 = 14,000; cache read share = 9/14.
FLAT_USAGE = {
    "input_tokens": 1000,
    "output_tokens": 500,
    "cache_read_input_tokens": 9000,
    "cache_creation_input_tokens": 4000,
    "cache_creation_5m": 1000,
    "cache_creation_1h": 3000,
    "speed": "standard",
}
NESTED_USAGE = {
    "input_tokens": 1000,
    "output_tokens": 500,
    "cache_read_input_tokens": 9000,
    "cache_creation_input_tokens": 4000,
    "cache_creation": {
        "ephemeral_5m_input_tokens": 1000,
        "ephemeral_1h_input_tokens": 3000,
    },
    "speed": "standard",
}
# No cache writes at all: prompt = 10,000, 90% served from cache.
READ_ONLY_USAGE = {
    "input_tokens": 1000,
    "output_tokens": 200,
    "cache_read_input_tokens": 9000,
    "cache_creation_input_tokens": 0,
    "speed": "standard",
}

_counter = itertools.count()
_MISSING = object()


def _add(
    sess: Session,
    *,
    model: str | None = OPUS,
    ts: datetime = T0,
    usage: dict | None = None,
    session_id: str | None = "sess-a",
    source: str | None = CC_SOURCE,
    tokens_in: int | None = None,
    parsed: object = _MISSING,
) -> None:
    """Insert one trace. ``parsed`` overrides output_parsed wholesale."""
    if parsed is _MISSING:
        meta: dict | None = {}
        if source is not None:
            meta["source"] = source
        if session_id is not None:
            meta["session_id"] = session_id
        if usage is not None:
            meta["usage"] = usage
    else:
        meta = parsed  # type: ignore[assignment]
    sess.add(
        Trace(
            project="proj",
            component="comp",
            operation="llm_complete",
            input_hash=f"h{next(_counter):08d}",
            parse_status="success",
            model_id=model,
            output_parsed=meta,
            tokens_in=tokens_in,
            invoked_at=ts,
        )
    )


class _Store:
    """A fresh store plus helpers that fill it and audit it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.url = f"sqlite:///{path}"
        make_engine(self.url, create_all=True)

    def fill(self, fn) -> None:
        with Session(make_engine(self.url, create_all=False)) as sess:
            fn(sess)
            sess.commit()

    def run(self, **kwargs):
        return audit(self.url, **kwargs)


@pytest.fixture
def db(tmp_path: Path) -> _Store:
    return _Store(tmp_path / "cache.db")


def _input_price(model: str, ts: datetime = T0) -> Decimal:
    price = price_for(model, ts)
    assert price is not None, f"{model} lost its price entry"
    return price.input_per_mtok


def _by_model(rows, model_id):
    return next(r for r in rows if r.model_id == model_id)


# ── section 1: per-model ────────────────────────────────────────────────────


def test_flat_and_nested_usage_price_identically(db):
    """The store is flat and transcripts are nested; both must reconcile."""

    def fill(sess):
        _add(sess, usage=FLAT_USAGE, session_id="flat", ts=T0)
        _add(sess, usage=NESTED_USAGE, session_id="nested", ts=T0)

    db.fill(fill)
    row = _by_model(db.run().models, OPUS)

    assert row.messages == 2
    # Both shapes must yield the same 5m/1h split, so the totals are exact 2x.
    assert row.cache_5m == 2 * 1000
    assert row.cache_1h == 2 * 3000
    assert row.prompt_tokens == 2 * 14_000
    assert row.priced_messages == 2


def test_hit_rate_is_token_weighted(db):
    def fill(sess):
        _add(sess, usage=FLAT_USAGE, session_id="a", ts=T0)

    db.fill(fill)
    row = _by_model(db.run().models, OPUS)
    # 9,000 read out of a 14,000-token prompt — price-independent.
    assert row.hit_rate == pytest.approx(9_000 / 14_000)


def test_costs_match_the_published_multipliers(db):
    def fill(sess):
        _add(sess, usage=FLAT_USAGE, session_id="a", ts=T0)

    db.fill(fill)
    row = _by_model(db.run().models, OPUS)

    price = PRICES[OPUS]
    p = price.input_per_mtok
    expected_actual = (
        1000 * p
        + 9000 * p * price.cache_read_mult
        + 1000 * p * price.cache_write_5m_mult
        + 3000 * p * price.cache_write_1h_mult
    ) / _MTOK
    expected_counterfactual = 14_000 * p / _MTOK  # every prompt token once, at 1x
    assert row.actual_usd == expected_actual
    assert row.counterfactual_usd == expected_counterfactual
    assert row.saved_usd == expected_counterfactual - expected_actual


def test_saving_ratio_is_price_independent(db):
    """A pure cache-read record saves exactly (1 - 0.1) of its cached share."""

    def fill(sess):
        _add(sess, usage=READ_ONLY_USAGE, session_id="a", ts=T0)

    db.fill(fill)
    row = _by_model(db.run().models, OPUS)
    read_mult = float(PRICES[OPUS].cache_read_mult)
    # 1000 @1x + 9000 @0.1x out of 10,000 @1x.
    expected = 1 - (1000 + 9000 * read_mult) / 10_000
    assert row.saved_pct == pytest.approx(expected)


def test_output_tokens_never_enter_the_cost(db):
    """Caching cannot touch output, so a bigger answer must not move the bill."""

    def fill(sess):
        _add(sess, usage=dict(READ_ONLY_USAGE, output_tokens=999_999), session_id="a")

    db.fill(fill)
    row = _by_model(db.run().models, OPUS)
    p = _input_price(OPUS)
    assert row.counterfactual_usd == 10_000 * p / _MTOK


def test_sonnet5_price_eras_resolve_by_invoked_at(db):
    """The intro/standard boundary must be honoured, not averaged away."""
    before = SONNET5_STANDARD_FROM - timedelta(days=1)
    after = SONNET5_STANDARD_FROM + timedelta(days=1)
    assert SONNET5_INTRO.input_per_mtok != SONNET5_STANDARD.input_per_mtok

    def fill(sess):
        _add(sess, model=SONNET, usage=READ_ONLY_USAGE, session_id="intro", ts=before)
        _add(sess, model=SONNET, usage=READ_ONLY_USAGE, session_id="std", ts=after)

    db.fill(fill)
    row = _by_model(db.run().models, SONNET)

    expected = (
        10_000 * SONNET5_INTRO.input_per_mtok + 10_000 * SONNET5_STANDARD.input_per_mtok
    ) / _MTOK
    assert row.counterfactual_usd == expected
    assert row.priced_messages == 2


def test_unpriced_model_keeps_tokens_and_reports_no_money(db):
    def fill(sess):
        _add(sess, model=UNPRICED, usage=FLAT_USAGE, session_id="a")

    db.fill(fill)
    result = db.run()
    row = _by_model(result.models, UNPRICED)

    assert row.prompt_tokens == 14_000
    assert row.hit_rate == pytest.approx(9_000 / 14_000)
    assert row.priced_messages == 0
    assert row.saved_usd is None and row.saved_pct is None
    # ...and the totals must not silently absorb it.
    assert result.counterfactual_usd == Decimal("0")

    rendered = format_audit(result)
    assert "n/a" in rendered
    assert "money is never guessed" in rendered


def test_error_rows_keep_their_place_in_the_timeline(db):
    """model_id NULL (API errors) carry usage: no money, but a real timestamp."""

    def fill(sess):
        _add(sess, model=None, usage={"input_tokens": None}, session_id="a", ts=T0)
        _add(sess, usage=FLAT_USAGE, session_id="a", ts=T0 + timedelta(hours=3))

    db.fill(fill)
    result = db.run()
    assert _by_model(result.models, "(none)").priced_messages == 0
    # The error row anchors the gap that follows it.
    assert result.gaps.gaps == 1
    assert result.gaps.expired_gaps == 1


# ── section 2: session gaps ─────────────────────────────────────────────────


def _gap_session(sess, gaps: list[timedelta], *, name="sess", usage=FLAT_USAGE):
    ts = T0
    _add(sess, usage=usage, session_id=name, ts=ts)
    for gap in gaps:
        ts = ts + gap
        _add(sess, usage=usage, session_id=name, ts=ts)


def test_gap_buckets(db):
    def fill(sess):
        _gap_session(
            sess,
            [
                timedelta(minutes=1),    # <5m
                timedelta(minutes=30),   # 5m-1h
                timedelta(hours=2),      # 1-4h
                timedelta(hours=10),     # >4h
            ],
        )

    db.fill(fill)
    g = db.run().gaps
    assert g.sessions == 1
    assert g.gaps == 4
    assert g.buckets == {"<5m": 1, "5m-1h": 1, "1-4h": 1, ">4h": 1}
    assert g.expired_gaps == 2


def test_gap_boundaries_are_half_open(db):
    """Exactly 5m and exactly 1h land in the middle bucket, and 1h is not 'expired'."""

    def fill(sess):
        _gap_session(sess, [timedelta(minutes=5), timedelta(hours=1)])

    db.fill(fill)
    g = db.run().gaps
    assert g.buckets["5m-1h"] == 2
    assert g.buckets["<5m"] == 0
    assert g.expired_gaps == 0


def test_sessions_do_not_share_a_timeline(db):
    """Two interleaved sessions must not produce cross-session gaps."""

    def fill(sess):
        for i in range(3):
            _add(sess, usage=FLAT_USAGE, session_id="a", ts=T0 + timedelta(hours=6 * i))
            _add(
                sess,
                usage=FLAT_USAGE,
                session_id="b",
                ts=T0 + timedelta(hours=6 * i, minutes=1),
            )

    db.fill(fill)
    g = db.run().gaps
    assert g.sessions == 2
    assert g.gaps == 4          # 2 per session, not 5 across a merged timeline
    assert g.expired_gaps == 4  # every within-session gap is ~6h


def test_rewrite_bound_prices_only_post_gap_writes(db):
    def fill(sess):
        _gap_session(sess, [timedelta(minutes=1), timedelta(hours=3)])

    db.fill(fill)
    g = db.run().gaps

    price = PRICES[OPUS]
    p = price.input_per_mtok
    expected = (
        1000 * p * price.cache_write_5m_mult + 3000 * p * price.cache_write_1h_mult
    ) / _MTOK
    assert g.expired_gaps == 1
    assert g.rewrite_usd == expected       # one post-gap message, not three
    assert g.rewrite_tokens == 4000

    # The upper-bound caveat must reach the printed output, not just the docs.
    assert "UPPER BOUND" in format_audit(db.run())


def test_unpriced_post_gap_message_is_counted_not_guessed(db):
    def fill(sess):
        _add(sess, model=UNPRICED, usage=FLAT_USAGE, session_id="a", ts=T0)
        _add(
            sess,
            model=UNPRICED,
            usage=FLAT_USAGE,
            session_id="a",
            ts=T0 + timedelta(hours=5),
        )

    db.fill(fill)
    g = db.run().gaps
    assert g.expired_gaps == 1
    assert g.rewrite_unpriced == 1
    assert g.rewrite_usd == Decimal("0")


# ── section 3: keep-alive pings ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "minutes,expected",
    [(10, 0), (55, 0), (56, 1), (60, 1), (110, 1), (111, 2), (240, 4)],
)
def test_pings_to_bridge(minutes, expected):
    assert pings_to_bridge(timedelta(minutes=minutes)) == expected


def test_ping_verdict_not_worth_it(db):
    """Long idle gaps over a large prompt: keep-alive costs more than it saves."""
    big_prompt = {
        "input_tokens": 0,
        "cache_read_input_tokens": 2_000_000,
        "cache_creation_input_tokens": 100,
        "cache_creation_5m": 100,
        "cache_creation_1h": 0,
        "speed": "standard",
    }

    def fill(sess):
        _gap_session(sess, [timedelta(hours=10)], usage=big_prompt)

    db.fill(fill)
    result = db.run()
    g = result.gaps
    assert g.pings == pings_to_bridge(timedelta(hours=10)) == 10
    assert g.ping_usd > g.rewrite_usd
    assert g.ping_worth_it is False

    verdict = _verdict_line(result)
    assert verdict.startswith("NOT WORTH IT")


def test_ping_verdict_worth_it(db):
    """Small prompt, huge re-write on return: the ping pays for itself."""
    tiny_read_big_write = {
        "input_tokens": 0,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 5_000_000,
        "cache_creation_5m": 0,
        "cache_creation_1h": 5_000_000,
        "speed": "standard",
    }

    def fill(sess):
        _gap_session(sess, [timedelta(hours=2)], usage=tiny_read_big_write)

    db.fill(fill)
    result = db.run()
    g = result.gaps
    assert g.ping_usd < g.rewrite_usd
    assert g.ping_worth_it is True

    verdict = _verdict_line(result)
    assert verdict.startswith("WORTH IT")


def test_ping_cost_uses_the_pre_gap_prompt_volume(db):
    """The cache holds what the LAST request left there, not what comes next."""
    small = dict(READ_ONLY_USAGE)                      # prompt 10,000
    large = dict(READ_ONLY_USAGE, cache_read_input_tokens=999_000)  # prompt 1,000,000

    def fill(sess):
        _add(sess, usage=small, session_id="a", ts=T0)
        _add(sess, usage=large, session_id="a", ts=T0 + timedelta(hours=2))

    db.fill(fill)
    g = db.run().gaps

    price = PRICES[OPUS]
    pings = pings_to_bridge(timedelta(hours=2))
    expected = (
        10_000 * price.input_per_mtok * price.cache_read_mult * pings
    ) / _MTOK
    assert g.pings == pings
    assert g.ping_usd == expected


def test_unpriced_pre_gap_message_excluded_from_ping_total(db):
    def fill(sess):
        _add(sess, model=UNPRICED, usage=FLAT_USAGE, session_id="a", ts=T0)
        _add(sess, model=OPUS, usage=FLAT_USAGE, session_id="a", ts=T0 + timedelta(hours=4))

    db.fill(fill)
    g = db.run().gaps
    assert g.ping_unpriced == 1
    assert g.ping_usd == Decimal("0")
    assert g.pings == 0


def _verdict_line(result) -> str:
    section = next(s for s in build_sections(result) if s.key == "ping")
    return next(row[1] for row in section.rows if row[0] == "verdict")


def _capped_verdict_line(result) -> str:
    section = next(s for s in build_sections(result) if s.key == "ping")
    return next(row[1] for row in section.rows if row[0].startswith("verdict (capped"))


def _gap_row(result, bucket: str) -> list[str]:
    section = next(s for s in build_sections(result) if s.key == "gaps")
    return next(row for row in section.rows if row[0] == bucket)


# ── per-bucket money and the capped keep-alive policy ───────────────────────


def test_bucket_costs_sum_to_the_totals(db):
    """Splitting the money by bucket must not create or destroy any of it."""

    def fill(sess):
        _gap_session(
            sess,
            [
                timedelta(minutes=1),
                timedelta(minutes=30),
                timedelta(hours=2),
                timedelta(hours=3),
                timedelta(hours=10),
            ],
        )

    db.fill(fill)
    g = db.run().gaps
    assert sum(b.rewrite_usd for b in g.bucket_costs.values()) == g.rewrite_usd
    assert sum(b.ping_usd for b in g.bucket_costs.values()) == g.ping_usd
    assert sum(b.pings for b in g.bucket_costs.values()) == g.pings
    assert sum(b.rewrite_unpriced for b in g.bucket_costs.values()) == g.rewrite_unpriced
    assert sum(b.ping_unpriced for b in g.bucket_costs.values()) == g.ping_unpriced


def test_buckets_inside_the_ttl_carry_no_money(db):
    """Nothing expires under 1h, so those buckets get no rewrite and no ping."""

    def fill(sess):
        _gap_session(sess, [timedelta(minutes=1), timedelta(minutes=30)])

    db.fill(fill)
    result = db.run()
    g = result.gaps
    for name in ("<5m", "5m-1h"):
        assert g.bucket_costs[name].rewrite_usd == Decimal("0")
        assert g.bucket_costs[name].ping_usd == Decimal("0")
        assert g.bucket_costs[name].pings == 0
        # ...and the report says so in words that are not the price-less "n/a".
        row = _gap_row(result, name)
        assert row[3] == row[4] == row[5] == "no expiry"


def test_bucket_verdicts_can_disagree_with_the_total(db):
    """The point of the split: a 1-4h win hidden inside a >4h loss.

    Same prompt shape in both gaps, so the difference is purely how many pings
    the gap length demands.
    """
    read_heavy = {
        "input_tokens": 0,
        "cache_read_input_tokens": 400_000,
        "cache_creation_input_tokens": 200_000,
        "cache_creation_5m": 0,
        "cache_creation_1h": 200_000,
        "speed": "standard",
    }

    def fill(sess):
        _gap_session(sess, [timedelta(hours=2)], name="short", usage=read_heavy)
        _gap_session(sess, [timedelta(hours=20)], name="long", usage=read_heavy)

    db.fill(fill)
    result = db.run()
    g = result.gaps
    short, long = g.bucket_costs["1-4h"], g.bucket_costs[">4h"]
    assert short.ping_usd < short.rewrite_usd      # one ping, one rewrite avoided
    assert long.ping_usd > long.rewrite_usd        # 20 pings for the same rewrite
    assert _gap_row(result, "1-4h")[5] == "ping wins"
    assert _gap_row(result, ">4h")[5] == "ping loses"
    # The aggregate is dominated by the long gap and refuses; the capped policy
    # keeps the short gap's win because it stops paying at 4h.
    assert g.ping_worth_it is False
    assert g.capped_worth_it is True
    assert _verdict_line(result).startswith("NOT WORTH IT")
    assert _capped_verdict_line(result).startswith("WORTH IT")


def test_capped_policy_pays_for_gaps_it_abandons(db):
    """A prospective pinger cannot see the end of a gap, so it eats the waste."""

    def fill(sess):
        _gap_session(sess, [timedelta(hours=20)], usage=READ_ONLY_USAGE)

    db.fill(fill)
    g = db.run().gaps
    cap_pings = pings_to_bridge(timedelta(hours=4))
    assert g.pings == pings_to_bridge(timedelta(hours=20)) == 21
    assert g.capped_pings == cap_pings == 4        # 55m cadence, gave up at 4h
    assert g.capped_ping_usd > Decimal("0")        # paid...
    assert g.capped_rewrite_usd == Decimal("0")    # ...and bridged nothing
    assert (g.capped_bridged, g.capped_abandoned) == (0, 1)


def test_capped_policy_banks_only_the_gaps_it_bridges(db):
    def fill(sess):
        _gap_session(sess, [timedelta(hours=2)], name="bridged")
        _gap_session(sess, [timedelta(hours=9)], name="abandoned")

    db.fill(fill)
    g = db.run().gaps
    assert (g.capped_bridged, g.capped_abandoned) == (1, 1)
    assert g.capped_rewrite_usd == g.bucket_costs["1-4h"].rewrite_usd
    assert g.capped_rewrite_usd < g.rewrite_usd
    assert g.capped_pings == pings_to_bridge(timedelta(hours=2)) + pings_to_bridge(
        timedelta(hours=4)
    )


# ── section 4: direct API traffic ───────────────────────────────────────────


def test_direct_traffic_without_usage_uses_tokens_in(db):
    def fill(sess):
        # A rerun-harness-shaped row: no source, no session, no usage block.
        _add(sess, session_id=None, source=None, tokens_in=90, parsed={"answer_chars": 12})
        _add(sess, session_id=None, source=None, tokens_in=110, parsed=None)

    db.fill(fill)
    result = db.run()
    assert result.models == []           # nothing lands in the cached population
    row = _by_model(result.direct, OPUS)

    assert row.calls == 2
    assert row.with_usage == 0
    assert row.avg_prompt == pytest.approx(100)
    assert row.hit_rate is None          # unknown, never 0
    assert row.min_cacheable == MIN_CACHEABLE_TOKENS[OPUS]
    assert row.meets_minimum is False
    assert "structural" in row.verdict()


def test_direct_traffic_with_usage_reports_a_hit_rate(db):
    def fill(sess):
        _add(sess, session_id=None, source="wrap_anthropic", usage=READ_ONLY_USAGE)

    db.fill(fill)
    row = _by_model(db.run().direct, OPUS)
    assert row.with_usage == 1
    assert row.hit_rate == pytest.approx(0.9)
    assert row.meets_minimum is True
    assert "cache is engaging" in row.verdict()


def test_direct_verdict_refuses_to_guess_an_unknown_minimum():
    row = DirectRow(model_id=UNPRICED, calls=1, prompt_tokens=5000, with_usage=1)
    assert row.min_cacheable is None
    assert row.meets_minimum is None
    assert "no structural verdict" in row.verdict()


def test_min_cacheable_table_is_not_monotonic():
    """Guards the 'never interpolate a neighbour' rule with the real inversion."""
    assert MIN_CACHEABLE_TOKENS["claude-opus-4-7"] > MIN_CACHEABLE_TOKENS["claude-opus-4-8"]
    assert MIN_CACHEABLE_TOKENS["claude-opus-5"] == 512
    assert MIN_CACHEABLE_TOKENS["claude-fable-5"] == 512
    assert MIN_CACHEABLE_TOKENS["claude-sonnet-5"] == 1024
    assert MIN_CACHEABLE_TOKENS["claude-haiku-4-5-20251001"] == 4096


# ── window, formats, CLI, read-only ─────────────────────────────────────────


def test_empty_db_renders_every_section(db):
    result = db.run()
    assert result.total_traces == 0
    assert result.saved_pct is None
    keys = [s.key for s in build_sections(result)]
    assert keys == ["per_model", "gaps", "ping", "direct"]
    for fmt in ("table", "md", "csv"):
        text_out = format_audit(result, fmt)
        assert "No priced Claude Code traffic" in text_out


def test_window_filters_both_ends(db):
    def fill(sess):
        for day in (1, 15, 30):
            _add(
                sess,
                usage=FLAT_USAGE,
                session_id=f"s{day}",
                ts=datetime(2026, 7, day, 12, 0, tzinfo=timezone.utc),
            )

    db.fill(fill)
    assert db.run().cc_records == 3
    windowed = db.run(
        since=parse_bound("2026-07-10", flag="--since", end_of_day=False),
        until=parse_bound("2026-07-15", flag="--until", end_of_day=True),
    )
    assert windowed.cc_records == 1


def test_parse_bound_day_semantics():
    since = parse_bound("2026-07-15", flag="--since", end_of_day=False)
    until = parse_bound("2026-07-15", flag="--until", end_of_day=True)
    assert since.hour == 0 and since.minute == 0
    assert until.hour == 23 and until.minute == 59
    assert parse_bound(None, flag="--since", end_of_day=False) is None
    with pytest.raises(ValueError, match="--since"):
        parse_bound("not-a-date", flag="--since", end_of_day=False)


def test_csv_is_tidy_and_parseable(db):
    def fill(sess):
        _add(sess, usage=FLAT_USAGE, session_id="a")

    db.fill(fill)
    rows = list(csv.DictReader(io.StringIO(format_audit(db.run(), "csv"))))
    assert {"section", "item", "metric", "value"} == set(rows[0])
    sections = {r["section"] for r in rows}
    assert {"per_model", "gaps", "ping", "direct", "summary"} <= sections
    hit = next(r for r in rows if r["section"] == "per_model" and r["metric"] == "hit rate")
    assert hit["item"] == OPUS


def test_csv_keys_are_unique_within_a_section(db):
    """Tidy form means (section, item, metric) identifies one value.

    The capped keep-alive block repeats every metric name of the unbounded one,
    so without a suffix the CSV would carry two rows called "ping cost" that
    only row order tells apart — invisible to anyone loading it into a frame.
    """

    def fill(sess):
        _gap_session(sess, [timedelta(hours=2), timedelta(hours=9)])

    db.fill(fill)
    rows = [
        r
        for r in csv.DictReader(io.StringIO(format_audit(db.run(), "csv")))
        if r["metric"] != "note"
    ]
    keys = [(r["section"], r["item"], r["metric"]) for r in rows]
    duplicates = {k for k in keys if keys.count(k) > 1}
    assert not duplicates, duplicates


def test_summary_line_is_copyable(db):
    def fill(sess):
        _add(sess, usage=READ_ONLY_USAGE, session_id="a")

    db.fill(fill)
    line = db.run().summary_line()
    assert "Claude Code caching already saves us" in line
    assert "python -m traceguard.routing_audit.cache_audit" in line


@pytest.mark.parametrize("fmt", ["table", "md", "csv"])
def test_main_prints_every_format(db, capsys, fmt):
    def fill(sess):
        _add(sess, usage=FLAT_USAGE, session_id="a")

    db.fill(fill)
    assert cache_audit_main(["--db", db.url, "--format", fmt]) == 0
    out = capsys.readouterr().out
    assert OPUS in out


def test_main_rejects_a_bad_window(db, capsys):
    assert cache_audit_main(["--db", db.url, "--since", "yesterday"]) == 2
    assert "--since" in capsys.readouterr().err


def test_read_only_url_rewrite():
    assert _read_only_url("sqlite:///traces.db") == (
        "sqlite:///file:traces.db?mode=ro&uri=true"
    )
    # already-URI, in-memory and non-SQLite URLs pass through untouched
    assert _read_only_url("sqlite:///file:x?mode=ro&uri=true") == (
        "sqlite:///file:x?mode=ro&uri=true"
    )
    assert _read_only_url("sqlite:///:memory:") == "sqlite:///:memory:"
    assert _read_only_url("postgresql://h/db") == "postgresql://h/db"


def test_audit_engine_physically_cannot_write(db):
    """mode=ro is in effect, not merely intended."""
    ro = create_engine(_read_only_url(db.url), future=True)
    with pytest.raises(OperationalError):
        with ro.begin() as conn:
            conn.execute(text("CREATE TABLE probe (x INTEGER)"))


def test_audit_leaves_the_store_untouched(db):
    def fill(sess):
        _add(sess, usage=FLAT_USAGE, session_id="a")

    db.fill(fill)
    engine = make_engine(db.url, create_all=False)

    def snapshot():
        with engine.connect() as conn:
            tables = sorted(
                r[0]
                for r in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            )
            rows = conn.execute(text("SELECT count(*) FROM traces")).scalar()
        return tables, rows

    before = snapshot()
    format_audit(db.run())
    assert snapshot() == before
    assert Base.metadata.tables  # sanity: we compared against a real schema

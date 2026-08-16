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
    CAP_SWEEP_MAX,
    CAP_SWEEP_MIN,
    CAP_SWEEP_STEP,
    CC_SOURCE,
    DirectRow,
    MIN_CACHEABLE_TOKENS,
    _cap_label,
    _gaps_to_resolve,
    _median,
    _read_only_url,
    _tri_verdict,
    _verdict_sentence,
    _width_label,
    audit,
    build_sections,
    cap_grid,
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
    # cap pinned at 4h: this test predates the solved cap and is about the
    # bucket split, not about which threshold the sweep picks.
    result = db.run(cap=timedelta(hours=4))
    g = result.gaps
    short, long = g.bucket_costs["1-4h"], g.bucket_costs[">4h"]
    assert short.ping_usd < short.rewrite_usd      # one ping, one rewrite avoided
    assert long.ping_usd > long.rewrite_usd        # 20 pings for the same rewrite
    # Column 6, and three-state since the lower bound arrived; "ping wins" used
    # to live at column 5 against the upper bound alone.
    #
    # 1-4h reads UNDECIDED rather than the old "ping wins" and the reason is
    # this fixture, not a regression: each session is two messages writing an
    # identical 200,000 cache-creation tokens, so the median of the non-post-gap
    # messages equals the post-gap message's own write and the lower bound
    # floors to $0. The ping bill then lands inside [$0, upper] by definition.
    # test_capped_verdict_is_worth_it_when_the_baseline_is_small covers the
    # non-degenerate case.
    assert g.bucket_costs["1-4h"].rewrite_usd_lower == Decimal("0")
    assert _gap_row(result, "1-4h")[6] == "UNDECIDED"
    assert _gap_row(result, ">4h")[6] == "NOT WORTH IT"
    # The aggregate is dominated by the long gap and refuses; the capped policy
    # keeps the short gap's win because it stops paying at 4h. Both binary
    # (upper-bound) properties are unchanged.
    assert g.ping_worth_it is False
    assert g.capped_worth_it is True
    assert _verdict_line(result).startswith("NOT WORTH IT")
    assert _capped_verdict_line(result).startswith("UNDECIDED")


def test_capped_policy_pays_for_gaps_it_abandons(db):
    """A prospective pinger cannot see the end of a gap, so it eats the waste."""

    def fill(sess):
        _gap_session(sess, [timedelta(hours=20)], usage=READ_ONLY_USAGE)

    db.fill(fill)
    g = db.run(cap=timedelta(hours=4)).gaps      # pinned: see the 4h note above
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
    g = db.run(cap=timedelta(hours=4)).gaps      # pinned: see the 4h note above
    assert (g.capped_bridged, g.capped_abandoned) == (1, 1)
    assert g.capped_rewrite_usd == g.bucket_costs["1-4h"].rewrite_usd
    assert g.capped_rewrite_usd < g.rewrite_usd
    assert g.capped_pings == pings_to_bridge(timedelta(hours=2)) + pings_to_bridge(
        timedelta(hours=4)
    )


# ── the rewrite LOWER bound ─────────────────────────────────────────────────


def _write_1h(tokens: int, *, prompt_extra: int = 0) -> dict:
    """Usage that writes ``tokens`` at the 1h TTL and nothing else.

    ``cache_creation_input_tokens`` is kept equal to the split because
    pricing.cache_creation_split lets the total win on quantity — a fixture
    that disagreed with itself would be testing the clamp, not the bound.
    """
    return {
        "input_tokens": prompt_extra,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": tokens,
        "cache_creation_5m": 0,
        "cache_creation_1h": tokens,
        "speed": "standard",
    }


def test_median_is_exact_and_stays_decimal():
    """An even-length median is a halved sum; a float round-trip would leak."""
    assert _median([]) == Decimal("0")
    assert _median([7]) == Decimal("7")
    assert _median([1, 2, 3]) == Decimal("2")
    assert _median([1, 2]) == Decimal("1.5")
    assert isinstance(_median([1, 2]), Decimal)
    assert _median([3, 1, 2]) == Decimal("2")          # unsorted input


def test_lower_bound_subtracts_the_session_baseline(db):
    """The post-gap write is credited with an ordinary turn's worth of content."""

    def fill(sess):
        # Three non-post-gap turns writing 1,000 / 2,000 / 3,000 → median 2,000.
        _add(sess, usage=_write_1h(1000), session_id="a", ts=T0)
        _add(sess, usage=_write_1h(2000), session_id="a", ts=T0 + timedelta(minutes=1))
        _add(sess, usage=_write_1h(3000), session_id="a", ts=T0 + timedelta(minutes=2))
        _add(sess, usage=_write_1h(10_000), session_id="a", ts=T0 + timedelta(hours=3))

    db.fill(fill)
    g = db.run().gaps
    price = PRICES[OPUS]
    p = price.input_per_mtok

    upper = 10_000 * p * price.cache_write_1h_mult / _MTOK
    lower = (10_000 - 2000) * p * price.cache_write_1h_mult / _MTOK
    assert g.expired_gaps == 1
    assert g.rewrite_usd == upper
    assert g.rewrite_usd_lower == lower
    assert g.rewrite_tokens == 10_000
    assert g.rewrite_tokens_lower == Decimal(8000)
    assert g.baseline_tokens == Decimal(2000)


def test_lower_bound_floors_at_zero_when_the_baseline_swallows_the_write(db):
    """A post-gap turn that writes less than an ordinary one bounds at $0."""

    def fill(sess):
        _add(sess, usage=_write_1h(50_000), session_id="a", ts=T0)
        _add(sess, usage=_write_1h(1000), session_id="a", ts=T0 + timedelta(hours=2))

    db.fill(fill)
    g = db.run().gaps
    assert g.rewrite_usd > Decimal("0")       # the upper bound still charges it
    assert g.rewrite_usd_lower == Decimal("0")
    assert g.rewrite_tokens_lower == Decimal("0")


def test_lower_bound_keeps_the_messages_own_ttl_mix(db):
    """The residual is scaled, not reassigned to whichever TTL is cheaper."""
    mixed = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 4000,
        "cache_creation_5m": 1000,
        "cache_creation_1h": 3000,
        "speed": "standard",
    }

    def fill(sess):
        _add(sess, usage=_write_1h(2000), session_id="a", ts=T0)
        _add(sess, usage=mixed, session_id="a", ts=T0 + timedelta(hours=2))

    db.fill(fill)
    g = db.run().gaps
    price = PRICES[OPUS]
    p = price.input_per_mtok
    upper = (
        1000 * p * price.cache_write_5m_mult + 3000 * p * price.cache_write_1h_mult
    ) / _MTOK
    # baseline 2,000 of 4,000 written → exactly half survives, both TTLs alike.
    assert g.rewrite_usd == upper
    assert g.rewrite_usd_lower == upper * Decimal(2000) / Decimal(4000)


def test_lower_bound_never_exceeds_the_upper_bound(db):
    """The bracket must be a bracket, per bucket and in total."""

    def fill(sess):
        _gap_session(
            sess,
            [timedelta(hours=2), timedelta(hours=9), timedelta(minutes=3)],
            usage=FLAT_USAGE,
        )
        _gap_session(sess, [timedelta(hours=5)], name="b", usage=READ_ONLY_USAGE)

    db.fill(fill)
    g = db.run().gaps
    assert Decimal("0") <= g.rewrite_usd_lower <= g.rewrite_usd
    assert g.capped_rewrite_usd_lower <= g.capped_rewrite_usd
    for bc in g.bucket_costs.values():
        assert Decimal("0") <= bc.rewrite_usd_lower <= bc.rewrite_usd


def test_bucket_lower_bounds_sum_to_the_total(db):
    def fill(sess):
        _gap_session(sess, [timedelta(hours=2), timedelta(hours=3), timedelta(hours=10)])

    db.fill(fill)
    g = db.run().gaps
    assert sum(b.rewrite_usd_lower for b in g.bucket_costs.values()) == g.rewrite_usd_lower


def test_unpriced_post_gap_message_contributes_to_neither_bound(db):
    def fill(sess):
        _add(sess, model=UNPRICED, usage=FLAT_USAGE, session_id="a", ts=T0)
        _add(
            sess, model=UNPRICED, usage=FLAT_USAGE, session_id="a",
            ts=T0 + timedelta(hours=5),
        )

    db.fill(fill)
    g = db.run().gaps
    assert g.rewrite_unpriced == 1
    assert g.rewrite_usd == g.rewrite_usd_lower == Decimal("0")
    assert "contribute no money to either bound" in format_audit(db.run())


def test_lower_bound_note_reaches_the_output(db):
    """The assumption must be self-exposed in the report, not only the docstring."""

    def fill(sess):
        _gap_session(sess, [timedelta(hours=2)])

    db.fill(fill)
    rendered = format_audit(db.run())
    assert "LOWER BOUND, self-exposed" in rendered
    assert "nothing in usage supports that decomposition" in rendered


# ── the cap sweep ───────────────────────────────────────────────────────────


def _sweep(result):
    return next(s for s in build_sections(result) if s.key == "cap_sweep")


def _sweep_note(result, needle: str) -> str:
    return next(n for n in _sweep(result).notes if needle in n)


def test_cap_grid_is_the_documented_range(db):
    grid = cap_grid()
    assert grid[0] == CAP_SWEEP_MIN == timedelta(hours=1)
    assert grid[-1] == CAP_SWEEP_MAX == timedelta(hours=12)
    assert CAP_SWEEP_STEP == timedelta(minutes=15)
    assert len(grid) == 45                      # 1h..12h inclusive, every 15m
    assert all(b - a == CAP_SWEEP_STEP for a, b in zip(grid, grid[1:]))


def test_sweep_costs_every_cap_plus_the_uncapped_policy(db):
    def fill(sess):
        _gap_session(sess, [timedelta(hours=2), timedelta(hours=20)])

    db.fill(fill)
    sweep = db.run().gaps.sweep
    assert len(sweep.points) == len(cap_grid()) + 1
    assert [p.cap for p in sweep.points[:-1]] == list(cap_grid())
    assert sweep.points[-1].cap is None         # 'no cap' competes on the curve
    assert sweep.unbounded.bridged == 2 and sweep.unbounded.abandoned == 0
    assert len(_sweep(db.run()).rows) == len(cap_grid()) + 1


def test_sweep_matches_the_pinned_policy_it_replaced(db):
    """A point on the curve must equal what session_gaps computes for that cap."""

    def fill(sess):
        _gap_session(sess, [timedelta(hours=2)], name="short")
        _gap_session(sess, [timedelta(hours=9)], name="long")

    db.fill(fill)
    g = db.run(cap=timedelta(hours=4)).gaps
    point = next(p for p in g.sweep.points if p.cap == timedelta(hours=4))
    assert (point.pings, point.ping_usd) == (g.capped_pings, g.capped_ping_usd)
    assert point.saved_upper == g.capped_rewrite_usd
    assert point.saved_lower == g.capped_rewrite_usd_lower
    assert (point.bridged, point.abandoned) == (g.capped_bridged, g.capped_abandoned)


def test_uncapped_point_equals_the_unbounded_totals(db):
    def fill(sess):
        _gap_session(sess, [timedelta(hours=2), timedelta(hours=20)])

    db.fill(fill)
    g = db.run(cap=None).gaps
    assert g.cap is None and g.cap_solved is False
    assert (g.capped_pings, g.capped_ping_usd) == (g.pings, g.ping_usd)
    assert g.capped_rewrite_usd == g.rewrite_usd
    assert g.capped_rewrite_usd_lower == g.rewrite_usd_lower
    assert (g.capped_bridged, g.capped_abandoned) == (g.expired_gaps, 0)


def test_solved_cap_is_the_argmax_and_beats_the_old_hardcoded_4h(db):
    """The cap is now read off the curve; 4h has no privileged status."""

    def fill(sess):
        # Rewrites worth having sit at 6h, well past the retired 4h boundary.
        for i in range(4):
            _gap_session(
                sess, [timedelta(hours=6)], name=f"s{i}",
                usage=_write_1h(5_000_000, prompt_extra=1000),
            )

    db.fill(fill)
    g = db.run().gaps
    assert g.cap_solved is True
    assert g.cap == max(g.sweep.points, key=lambda p: p.net_upper).cap
    assert g.cap >= timedelta(hours=6)          # 4h would have bridged nothing
    four_h = next(p for p in g.sweep.points if p.cap == timedelta(hours=4))
    assert g.sweep.best.net_upper > four_h.net_upper


def test_argmax_ties_go_to_the_smaller_cap(db):
    """Same net, cheaper policy to run — and a deterministic answer."""

    def fill(sess):
        _gap_session(sess, [timedelta(hours=12)], usage=READ_ONLY_USAGE)

    db.fill(fill)
    sweep = db.run().gaps.sweep
    best = sweep.best
    tied = [p for p in sweep.points if p.net_upper == best.net_upper and p.cap]
    assert best.cap == min(tied, key=lambda p: p.cap).cap


def test_plateau_is_contiguous_positive_and_contains_the_argmax(db):
    def fill(sess):
        _gap_session(
            sess, [timedelta(hours=3)], usage=_write_1h(2_000_000, prompt_extra=1000)
        )

    db.fill(fill)
    sweep = db.run().gaps.sweep
    lo, hi = sweep.plateau
    assert lo <= sweep.best.cap <= hi
    inside = [p for p in sweep.points if p.cap and lo <= p.cap <= hi]
    assert all(p.net_upper > 0 for p in inside)
    outside = [p for p in sweep.points if p.cap and not (lo <= p.cap <= hi)]
    assert all(p.net_upper <= 0 for p in outside)
    assert sweep.plateau_width == hi - lo


def test_plateau_width_zero_is_reported_as_a_spike(db):
    """The degenerate case: one grid point wins and its neighbours both lose.

    Session "spike" has a 2h40m gap, so a 2h30m cap bridges nothing while a
    2h45m cap bridges it — and 2h45m is the last cap before pings_to_bridge
    steps from 2 to 3 at 3h. Session "drag" contributes a gap nothing can
    bridge but whose pre-gap prompt is large enough that the extra ping at the
    3h cap outweighs the rewrite the 2h45m cap just banked.
    """

    def fill(sess):
        _add(sess, usage=_write_1h(0, prompt_extra=10_000), session_id="spike", ts=T0)
        _add(
            sess, usage=_write_1h(125_000), session_id="spike",
            ts=T0 + timedelta(minutes=160),
        )
        _drag_session(sess)

    db.fill(fill)
    result = db.run()
    sweep = result.gaps.sweep
    positive = [p for p in sweep.points if p.cap and p.net_upper > 0]
    assert [p.cap for p in positive] == [timedelta(hours=2, minutes=45)]
    assert sweep.plateau == (
        timedelta(hours=2, minutes=45),
        timedelta(hours=2, minutes=45),
    )
    assert sweep.plateau_width == timedelta(0)
    assert result.gaps.cap == timedelta(hours=2, minutes=45)
    # ...and the report must say the optimum is a spike, not quietly recommend it.
    assert "a single grid point" in _sweep_note(result, "plateau")
    assert "2h45m" in _sweep_note(result, "argmax")


def test_uncapped_winner_is_not_read_as_sitting_on_the_plateau(db):
    """The plateau is a grid statistic; the argmax may live off the grid.

    A 20h gap with a big rewrite and a tiny prompt: no cap on the grid bridges
    it, so only 'no cap' banks the saving — while the short gap in the other
    session gives the grid a positive run of its own.
    """

    def fill(sess):
        _add(sess, usage=_write_1h(0, prompt_extra=100), session_id="far", ts=T0)
        _add(
            sess, usage=_write_1h(9_000_000), session_id="far",
            ts=T0 + timedelta(hours=20),
        )
        _add(sess, usage=_write_1h(0, prompt_extra=100), session_id="near", ts=T0)
        _add(
            sess, usage=_write_1h(500_000), session_id="near",
            ts=T0 + timedelta(hours=2),
        )

    db.fill(fill)
    result = db.run()
    sweep = result.gaps.sweep
    assert sweep.best.cap is None                  # uncapped wins outright
    assert result.gaps.cap is None
    assert sweep.plateau is not None               # ...but the grid still has a run
    assert "does not contain the winner" in _sweep_note(result, "not on the grid")


def test_no_positive_cap_reports_no_plateau_at_all(db):
    """Nothing to recommend: say so instead of dressing up the least-bad cap."""

    def fill(sess):
        _gap_session(sess, [timedelta(hours=8)], usage=READ_ONLY_USAGE)

    db.fill(fill)
    result = db.run()
    sweep = result.gaps.sweep
    assert all(p.net_upper <= 0 for p in sweep.points)
    assert sweep.plateau is None and sweep.robust_plateau is None
    assert sweep.plateau_width is None
    assert "no plateau" in _sweep_note(result, "plateau")
    assert "least-bad" in _sweep_note(result, "least-bad")


def test_plateau_censoring_is_flagged_at_the_grid_edge(db):
    """A run that ends where the sweep stops looking is not a measured width."""

    def fill(sess):
        _gap_session(
            sess, [timedelta(hours=2)], usage=_write_1h(9_000_000, prompt_extra=1000)
        )

    db.fill(fill)
    result = db.run()
    sweep = result.gaps.sweep
    assert sweep.plateau[1] == CAP_SWEEP_MAX
    assert sweep.plateau_censored(sweep.plateau) is True
    note = _sweep_note(result, "censored")
    assert "12h ceiling" in note and "floor rather than a measurement" in note


def _drag_session(sess, *, name="drag", prompt=1_000_000):
    """A gap no cap on the grid can bridge, over a prompt worth pinging.

    Its only job is to keep charging one more ping every time the cap crosses a
    55-minute boundary, so a net-benefit curve built on it eventually turns
    over inside the swept range instead of running flat to the ceiling.
    """
    _add(sess, usage=_write_1h(0, prompt_extra=prompt), session_id=name, ts=T0)
    _add(
        sess, usage=_write_1h(0, prompt_extra=1), session_id=name,
        ts=T0 + timedelta(hours=100),
    )


def test_uncensored_plateau_is_not_flagged(db):
    """A curve that turns over inside the grid reports a measured width.

    One bridgeable 2h gap banking 175,000 x 2 x p, against a drag session
    costing 100,000 x p per extra ping: the net survives cap_pings 2 and 3
    (caps 2h..3h30m) and goes negative at 4, which the 55-minute cadence
    reaches at 3h45m — so both plateau edges are sign changes, not grid edges.
    """

    def fill(sess):
        _add(sess, usage=_write_1h(0, prompt_extra=1000), session_id="win", ts=T0)
        _add(
            sess, usage=_write_1h(175_000), session_id="win",
            ts=T0 + timedelta(hours=2),
        )
        _drag_session(sess)

    db.fill(fill)
    result = db.run()
    sweep = result.gaps.sweep
    assert pings_to_bridge(timedelta(hours=3, minutes=30)) == 3
    assert pings_to_bridge(timedelta(hours=3, minutes=45)) == 4
    assert sweep.plateau == (timedelta(hours=2), timedelta(hours=3, minutes=30))
    assert sweep.plateau_width == timedelta(hours=1, minutes=30)
    assert CAP_SWEEP_MIN < sweep.plateau[0] and sweep.plateau[1] < CAP_SWEEP_MAX
    assert sweep.plateau_censored(sweep.plateau) is False
    assert not [n for n in _sweep(result).notes if "censored" in n]
    assert "1h30m wide" in _sweep_note(result, "plateau")


def test_width_and_cap_labels():
    assert _cap_label(None) == "no cap"
    assert _cap_label(timedelta(hours=4)) == "4h"
    assert _cap_label(timedelta(hours=2, minutes=45)) == "2h45m"
    assert _cap_label(timedelta(minutes=15)) == "15m"     # not "0h15m"
    assert _width_label(None) == "n/a"
    assert _width_label(timedelta(0)) == "0 (a single grid point)"
    assert _width_label(timedelta(hours=10, minutes=45)) == "10h45m"


# ── the three-state verdict ─────────────────────────────────────────────────


def test_tri_verdict_boundaries():
    """Both bounds are exclusive on the deciding side, so ties read UNDECIDED."""
    lo, hi = Decimal("10"), Decimal("20")
    assert _tri_verdict(Decimal("9"), lo, hi) == "WORTH IT"
    assert _tri_verdict(Decimal("21"), lo, hi) == "NOT WORTH IT"
    assert _tri_verdict(Decimal("15"), lo, hi) == "UNDECIDED"
    assert _tri_verdict(lo, lo, hi) == "UNDECIDED"
    assert _tri_verdict(hi, lo, hi) == "UNDECIDED"


def test_capped_verdict_is_undecided_between_the_bounds(db):
    """Constructed so the ping bill lands strictly inside the bracket.

    Per session: one ordinary turn writing 1,000,000 tokens with a 1,000,000
    token prompt, then a post-gap turn writing 1,050,000. Baseline 1,000,000
    leaves a 50,000-token residual, so lower = 2 x 50,000 x p and upper =
    2 x 1,050,000 x p, while two pings of the frozen 1,000,000-token prompt
    cost 0.2 x 1,000,000 x p — above the floor, far below the ceiling.
    """

    def fill(sess):
        for name in ("a", "b"):
            _add(sess, usage=_write_1h(1_000_000), session_id=name, ts=T0)
            _add(
                sess, usage=_write_1h(1_050_000), session_id=name,
                ts=T0 + timedelta(hours=2),
            )

    db.fill(fill)
    result = db.run(cap=timedelta(hours=4))
    g = result.gaps
    price = PRICES[OPUS]
    p = price.input_per_mtok
    per_gap_upper = 1_050_000 * p * price.cache_write_1h_mult / _MTOK
    per_gap_lower = 50_000 * p * price.cache_write_1h_mult / _MTOK
    per_gap_ping = 2 * 1_000_000 * p * price.cache_read_mult / _MTOK

    assert g.capped_rewrite_usd == 2 * per_gap_upper
    assert g.capped_rewrite_usd_lower == 2 * per_gap_lower
    assert g.capped_ping_usd == 2 * per_gap_ping
    assert g.capped_rewrite_usd_lower < g.capped_ping_usd < g.capped_rewrite_usd
    assert g.capped_verdict == "UNDECIDED"
    # The binary property still says WORTH IT — that is the flaw, demonstrated.
    assert g.capped_worth_it is True

    line = _capped_verdict_line(result)
    assert line.startswith("UNDECIDED: pings")
    assert "sits inside [" in line
    assert "will not close the band" in line   # 2 identical gaps → sign resolved


def test_capped_verdict_is_worth_it_when_the_baseline_is_small(db):
    """A real win: the ping bill undercuts even the pessimistic bound."""

    def fill(sess):
        _add(sess, usage=_write_1h(1000, prompt_extra=1000), session_id="a", ts=T0)
        _add(
            sess, usage=_write_1h(5_000_000), session_id="a",
            ts=T0 + timedelta(hours=2),
        )

    db.fill(fill)
    result = db.run(cap=timedelta(hours=4))
    g = result.gaps
    assert g.capped_ping_usd < g.capped_rewrite_usd_lower
    assert g.capped_verdict == "WORTH IT"
    assert _capped_verdict_line(result).startswith("WORTH IT")
    assert "(lower bound)" in _capped_verdict_line(result)


def test_capped_verdict_refuses_above_the_upper_bound(db):
    def fill(sess):
        _gap_session(sess, [timedelta(hours=11)], usage=READ_ONLY_USAGE)

    db.fill(fill)
    result = db.run(cap=timedelta(hours=12))
    g = result.gaps
    assert g.capped_ping_usd > g.capped_rewrite_usd
    assert g.capped_verdict == "NOT WORTH IT"
    assert _capped_verdict_line(result).startswith("NOT WORTH IT")
    assert "(upper bound)" in _capped_verdict_line(result)


def test_aggregate_verdict_line_is_untouched(db):
    """The old two-state control must keep its exact wording."""

    def fill(sess):
        _gap_session(sess, [timedelta(hours=20)], usage=READ_ONLY_USAGE)

    db.fill(fill)
    result = db.run()
    g = result.gaps
    assert _verdict_line(result) == (
        f"NOT WORTH IT: pings ${g.ping_usd.quantize(Decimal('0.01')):,} >= "
        f"avoidable rewrites ${g.rewrite_usd.quantize(Decimal('0.01')):,}"
    )


def test_gaps_to_resolve_reports_a_sample_size_not_a_band_width():
    """It answers "is this sign real", which is not "when does the band close"."""
    assert _gaps_to_resolve([]) is None
    assert _gaps_to_resolve([Decimal("1")]) is None          # n < 2
    assert _gaps_to_resolve([Decimal("1"), Decimal("-1")]) is None   # mean 0
    # Zero spread: every gap agreed, so the sign is as settled as it can get.
    assert _gaps_to_resolve([Decimal("5")] * 4) == 4
    # A mean swamped by its scatter needs far more than the sample in hand.
    noisy = [Decimal("100"), Decimal("-100"), Decimal("100"), Decimal("-99")]
    assert _gaps_to_resolve(noisy) > len(noisy)


def test_undecided_sentence_asks_for_more_gaps_when_the_sign_is_unsettled():
    line = _verdict_sentence(
        "UNDECIDED", Decimal("15"), Decimal("10"), Decimal("20"), 900, 12
    )
    assert line.startswith("UNDECIDED: pings $15.00 sits inside [$10.00, $20.00]")
    assert "~900 expired gaps would settle its sign, we have 12" in line


def test_undecided_sentence_says_when_more_data_cannot_help():
    line = _verdict_sentence(
        "UNDECIDED", Decimal("15"), Decimal("10"), Decimal("20"), 5, 40
    )
    assert "more traffic of this shape will not close the band" in line


def test_undecided_sentence_stays_bare_without_an_estimate():
    line = _verdict_sentence(
        "UNDECIDED", Decimal("15"), Decimal("10"), Decimal("20"), None, 1
    )
    assert line == "UNDECIDED: pings $15.00 sits inside [$10.00, $20.00]"


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
    assert keys == ["per_model", "gaps", "ping", "cap_sweep", "direct"]
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


@pytest.mark.parametrize("fmt", ["table", "md", "csv"])
def test_every_renderer_carries_the_cap_sweep(db, fmt):
    """The curve is the evidence for the cap; no format may drop it."""

    def fill(sess):
        _gap_session(sess, [timedelta(hours=2), timedelta(hours=9)])

    db.fill(fill)
    result = db.run()
    out = format_audit(result, fmt)
    # The CSV keys on section.key; the human formats print section.title.
    assert ("cap_sweep" if fmt == "csv" else "3b. Keep-alive cap sweep") in out
    assert "argmax" in out
    # Every swept cap gets a line, the uncapped policy included.
    for label in ("1h", "4h", "2h45m", "12h", "no cap"):
        assert label in out
    if fmt == "csv":
        rows = list(csv.DictReader(io.StringIO(out)))
        caps = {r["item"] for r in rows if r["section"] == "cap_sweep" and r["item"]}
        assert len(caps) == len(cap_grid()) + 1
        assert {"net <=", "net >=", "verdict"} <= {
            r["metric"] for r in rows if r["section"] == "cap_sweep"
        }


def test_md_renderer_pads_short_rows_in_every_section(db):
    """Section 3's two columns and 3b's nine must both survive the md table."""

    def fill(sess):
        _gap_session(sess, [timedelta(hours=2)])

    db.fill(fill)
    out = format_audit(db.run(), "md")
    header = "| cap | bridged / abandoned | pings | ping cost |"
    assert header in out
    for line in out.splitlines():
        if line.startswith("| ") and line.endswith(" |"):
            assert line.count("|") >= 3


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

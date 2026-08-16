"""Cache-efficiency audit — turns "your prompt cache hit rate is low" into numbers.

A READ-ONLY REPORT, not a dashboard and not a gateway. It opens the store with
SQLite ``mode=ro`` and never writes a row, a table, or a file. Privacy follows
the ``rerun`` module's rule: nothing but aggregates, token counts and money
leaves the DB — no prompt bodies, no answers, no summaries.

It does NOT ingest. Point it at a store that ``ingest_claude_code`` already
filled; this module only reads ``traces.output_parsed.usage`` (the flat shape
the ingest writes — see :func:`pricing.cache_creation_split`, which reads both
the flat and nested forms, so a store written by some other path still prices).

Sections, in the order a "why is your hit rate low" question actually gets
answered:

1. **per-model** — token-weighted hit rate, what the input side really cost at
   list price, and what it would have cost with no cache at all.
2. **session gaps** — where the idle time goes, and a BRACKET (lower and upper
   bound) on what cache expiry costs in re-writes.
3. **keep-alive ping counterfactual** — what it would cost to hold the cache
   open across every >1h gap, against that bracket. Three-state: a ping bill
   that lands between the bounds gets UNDECIDED, not a win.
3b. **cap sweep** — the give-up threshold is solved here, as the argmax of net
   benefit over a grid of caps, and reported with the width of the interval
   over which the net stays positive. An argmax without that width is a point
   estimate pretending to be a recommendation.
4. **direct API traffic** — the non-``claude_code_session`` rows (SDK wrappers,
   harnesses), where "low hit rate" is often structural rather than fixable.

TWO BOUNDS, NOT ONE. Every rewrite figure comes as a pair. The upper bound
charges a post-gap message's whole ``cache_creation`` to the expiry; the lower
bound first credits it with the median ``cache_creation`` of the same session's
ordinary turns. Neither is measured — ``usage`` does not separate the prefix a
message had to re-establish from the content it was going to write anyway — so
the honest output is the interval and a verdict that can say "inside it".

WHO IS IN WHICH SECTION. Sections 1–3 read the ``claude_code_session`` rows
that carry both a ``usage`` block and a ``session_id`` — a row without a usage
block has no tokens to weigh, and a row without a session has no timeline to
sit on. Rows whose ``model_id`` is NULL (API-error records) DO carry usage and
DO stay in the timeline, because their timestamps are real gaps in the session;
they simply contribute no money, because there is no model to price. Section 4
takes everything else (``source != "claude_code_session"``, including a NULL
``output_parsed``).

MONEY IS LIST PRICE, AND ONLY WHERE A PRICE EXISTS. Every figure comes from
:mod:`traceguard.routing_audit.pricing` — ``price_for`` (so Sonnet 5's two
price eras resolve by ``invoked_at``) and ``cache_creation_split``. A model
with no price entry, or a speed tier with no published price, is counted in
the token columns and left as ``n/a`` in the money columns. This module does
not invent a price sheet.
"""
from __future__ import annotations

import argparse
import csv
import io
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from typing import Any, Iterable, Sequence
from urllib.parse import quote

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from traceguard.routing_audit.pricing import ModelPrice, cache_creation_split, price_for
from traceguard.store.models import Trace

DEFAULT_DB = "sqlite:///traces_routing_audit.db"
CC_SOURCE = "claude_code_session"

_MTOK = Decimal(1_000_000)
_USD = Decimal("0.01")

# Minimum cacheable prefix, in tokens. Below it the API silently declines to
# cache: no error, just ``cache_creation_input_tokens: 0`` forever. Source:
# the Anthropic prompt-caching reference (platform.claude.com/docs/en/
# build-with-claude/prompt-caching), read 2026-08-16.
#
# NOT MONOTONIC ACROSS GENERATIONS — opus-4-7 needs 2048 while its successor
# opus-4-8 needs 1024, and haiku-4-5 needs 4096. Never interpolate a missing
# model from a neighbouring one; a model absent here reports "unknown" and gets
# no structural verdict, which is the same no-guessing rule pricing.py follows.
MIN_CACHEABLE_TOKENS: dict[str, int] = {
    "claude-opus-5": 512,
    "claude-fable-5": 512,
    "claude-opus-4-8": 1024,
    "claude-sonnet-5": 1024,
    "claude-opus-4-7": 2048,
    "claude-haiku-4-5-20251001": 4096,
}

# Gap buckets. Boundaries are half-open upward so every gap lands in exactly
# one bucket, and "expired" (>1h) is exactly the last two.
_FIVE_MIN = timedelta(minutes=5)
_ONE_HOUR = timedelta(hours=1)
_FOUR_HOURS = timedelta(hours=4)
GAP_BUCKETS: tuple[str, ...] = ("<5m", "5m-1h", "1-4h", ">4h")
_EXPIRED_BUCKETS = frozenset(GAP_BUCKETS[2:])

# Cell for a bucket where the question does not apply, kept distinct from the
# "n/a" that means "no list price, money never guessed".
_NO_EXPIRY = "no expiry"

# Keep-alive cadence for the section-3 counterfactual: one ping just inside the
# 1-hour TTL.
PING_INTERVAL = timedelta(minutes=55)

# Give-up threshold for the capped keep-alive policy: an unbounded pinger pays
# for every gap it cannot see the end of, a capped one stops after this much
# idle and eats the rewrite.
#
# SOLVED, NOT CHOSEN. An earlier version pinned it at 4h because that is the
# 1-4h/>4h bucket boundary — a tidy number, not an answer. It is now the argmax
# of net benefit over this grid, computed on the corpus in front of it (section
# 3b), so a different store gets a different cap. The grid is swept in whole
# CAP_SWEEP_STEP increments from CAP_SWEEP_MIN to CAP_SWEEP_MAX, plus an
# uncapped policy that competes on equal terms — if never giving up really is
# best, the sweep is allowed to say so.
CAP_SWEEP_MIN = _ONE_HOUR
CAP_SWEEP_MAX = timedelta(hours=12)
CAP_SWEEP_STEP = timedelta(minutes=15)

# Two-sided 95% normal quantile, used only to say how much more data an
# UNDECIDED verdict would need. Hardcoded because pulling in scipy to look up a
# constant every stats table already prints would be a poor trade.
_Z95 = Decimal("1.96")

# Sentinel for "solve the cap from the data" — distinct from ``None``, which is
# a real policy (never give up).
_SOLVE: Any = object()


@dataclass(frozen=True)
class Record:
    """One trace, reduced to what a cache audit needs."""

    model_id: str | None
    invoked_at: datetime
    session_id: str | None
    usage: dict[str, Any] | None
    tokens_in: int | None
    source: str | None

    @property
    def prompt_tokens(self) -> int:
        """Full prompt volume: uncached input + cache reads + cache writes.

        The three Messages-API input counts are mutually exclusive, so this sum
        is the real size of the prompt. Without a usage block, fall back to
        ``tokens_in`` — both ``ingest_claude_code`` and (since the wrapper fix)
        ``wrap_anthropic`` / ``rerun`` write exactly that sum there.
        """
        if self.usage is None:
            return int(self.tokens_in or 0)
        m5, h1 = cache_creation_split(self.usage)
        return (
            int(self.usage.get("input_tokens") or 0)
            + int(self.usage.get("cache_read_input_tokens") or 0)
            + m5
            + h1
        )


def _read_only_url(db_url: str) -> str:
    """Rewrite a SQLite URL so the file is opened ``mode=ro``.

    Non-SQLite URLs pass through untouched — read-only is the caller's job on a
    server backend (a role/GRANT, not a connect flag). ``:memory:`` and an
    already-URI form pass through too.
    """
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        return db_url
    path = db_url[len(prefix) :]
    if not path or path.startswith("file:") or path == ":memory:":
        return db_url
    return f"{prefix}file:{quote(path)}?mode=ro&uri=true"


def _read_only_engine(db_url: str | None):
    """Engine that can only read. Never calls ``create_all`` — the DB is ro."""
    return create_engine(_read_only_url(db_url or DEFAULT_DB), future=True)


def parse_bound(value: str | None, *, flag: str, end_of_day: bool) -> datetime | None:
    """Parse a window bound. A bare ISO date opens at 00:00 and closes at 23:59.

    Both bounds are inclusive, so ``--since 2026-07-01 --until 2026-07-31``
    means the whole of July rather than July minus its last day.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{flag} must be an ISO date/datetime, got {value!r}") from exc
    if dt.tzinfo is None:
        if end_of_day and len(value.strip()) <= 10:
            dt = datetime.combine(dt.date(), time.max)
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_records(
    db_url: str | None = None,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Record]:
    """Read every trace in the window, ordered by time. Read-only."""
    engine = _read_only_engine(db_url)
    stmt = select(
        Trace.model_id, Trace.invoked_at, Trace.output_parsed, Trace.tokens_in
    ).order_by(Trace.invoked_at)
    if since is not None:
        stmt = stmt.where(Trace.invoked_at >= since)
    if until is not None:
        stmt = stmt.where(Trace.invoked_at <= until)

    out: list[Record] = []
    with Session(engine) as sess:
        for model_id, invoked_at, parsed, tokens_in in sess.execute(stmt):
            meta = parsed if isinstance(parsed, dict) else {}
            usage = meta.get("usage")
            session_id = meta.get("session_id")
            out.append(
                Record(
                    model_id=model_id,
                    invoked_at=invoked_at,
                    session_id=session_id if isinstance(session_id, str) else None,
                    usage=usage if isinstance(usage, dict) else None,
                    tokens_in=tokens_in,
                    source=meta.get("source"),
                )
            )
    return out


def _tier_multiplier(price: ModelPrice, usage: dict[str, Any] | None) -> Decimal | None:
    """Speed-tier multiplier, or None when the tier has no published price.

    Mirrors ``pricing.compute_cost_usd`` exactly: absent/``standard`` is 1x,
    ``fast`` uses the model's published multiplier, and any other tier refuses
    to guess. ``service_tier`` is ignored here because pricing.py ignores it.
    """
    speed = usage.get("speed") if usage else None
    if speed in (None, "standard"):
        return Decimal(1)
    if speed == "fast":
        return price.fast_multiplier
    return None


def input_side_costs(rec: Record) -> tuple[Decimal, Decimal] | None:
    """``(actual, no_cache_counterfactual)`` for one message's INPUT side.

    ``actual`` bills each token kind at its own multiplier (reads 0.1x, 5m
    writes 1.25x, 1h writes 2x); the counterfactual bills the whole prompt at
    1x, i.e. what the same conversation would have cost with caching switched
    off entirely. Output tokens are excluded from both — caching cannot touch
    them, and including them would dilute the ratio the audit is about.

    Returns None (→ ``n/a``, never a guess) when the model has no price entry,
    the record has no usage, or the speed tier has no published price.
    """
    if rec.usage is None:
        return None
    price = price_for(rec.model_id, rec.invoked_at)
    if price is None:
        return None
    tier = _tier_multiplier(price, rec.usage)
    if tier is None:
        return None

    inp = int(rec.usage.get("input_tokens") or 0)
    read = int(rec.usage.get("cache_read_input_tokens") or 0)
    m5, h1 = cache_creation_split(rec.usage)
    p = price.input_per_mtok
    actual = (
        inp * p
        + read * p * price.cache_read_mult
        + m5 * p * price.cache_write_5m_mult
        + h1 * p * price.cache_write_1h_mult
    ) * tier / _MTOK
    counterfactual = (inp + read + m5 + h1) * p * tier / _MTOK
    return actual, counterfactual


# ── section 1: per-model ────────────────────────────────────────────────────


@dataclass
class ModelRow:
    model_id: str
    messages: int = 0
    input_tokens: int = 0
    cache_read: int = 0
    cache_5m: int = 0
    cache_1h: int = 0
    priced_messages: int = 0
    actual_usd: Decimal = Decimal("0")
    counterfactual_usd: Decimal = Decimal("0")

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read + self.cache_5m + self.cache_1h

    @property
    def hit_rate(self) -> float | None:
        total = self.prompt_tokens
        return self.cache_read / total if total else None

    @property
    def unpriced_messages(self) -> int:
        return self.messages - self.priced_messages

    @property
    def saved_usd(self) -> Decimal | None:
        if not self.priced_messages:
            return None
        return self.counterfactual_usd - self.actual_usd

    @property
    def saved_pct(self) -> float | None:
        if not self.priced_messages or self.counterfactual_usd == 0:
            return None
        return float((self.counterfactual_usd - self.actual_usd) / self.counterfactual_usd)


def per_model(records: Iterable[Record]) -> list[ModelRow]:
    """Aggregate the cached population by model, biggest prompt volume first."""
    rows: dict[str, ModelRow] = {}
    for rec in records:
        key = rec.model_id or "(none)"
        row = rows.setdefault(key, ModelRow(model_id=key))
        row.messages += 1
        usage = rec.usage or {}
        m5, h1 = cache_creation_split(usage)
        row.input_tokens += int(usage.get("input_tokens") or 0)
        row.cache_read += int(usage.get("cache_read_input_tokens") or 0)
        row.cache_5m += m5
        row.cache_1h += h1
        costs = input_side_costs(rec)
        if costs is not None:
            actual, counter = costs
            row.priced_messages += 1
            row.actual_usd += actual
            row.counterfactual_usd += counter
    return sorted(rows.values(), key=lambda r: r.prompt_tokens, reverse=True)


# ── sections 2 & 3: session gaps, expiry rewrites, keep-alive pings ─────────


@dataclass
class BucketCosts:
    """The money side of one gap bucket.

    Only the >1h buckets can carry money: a gap inside the TTL expires nothing,
    so there is no rewrite to avoid and no ping worth sending.
    """

    rewrite_usd: Decimal = Decimal("0")
    rewrite_usd_lower: Decimal = Decimal("0")
    rewrite_unpriced: int = 0
    pings: int = 0
    ping_usd: Decimal = Decimal("0")
    ping_unpriced: int = 0


@dataclass
class GapStats:
    buckets: dict[str, int] = field(
        default_factory=lambda: {name: 0 for name in GAP_BUCKETS}
    )
    bucket_costs: dict[str, BucketCosts] = field(
        default_factory=lambda: {name: BucketCosts() for name in GAP_BUCKETS}
    )
    sessions: int = 0
    gaps: int = 0
    expired_gaps: int = 0           # gaps > 1h
    rewrite_usd: Decimal = Decimal("0")
    rewrite_usd_lower: Decimal = Decimal("0")
    rewrite_tokens: int = 0
    rewrite_tokens_lower: Decimal = Decimal("0")
    rewrite_unpriced: int = 0       # post-gap messages with no price
    baseline_tokens: Decimal = Decimal("0")   # median of the per-session medians
    ping_usd: Decimal = Decimal("0")
    pings: int = 0
    ping_unpriced: int = 0          # pre-gap messages with no price
    # Capped policy: ping until ``cap`` of idle, then give up and let the cache
    # expire. Costs include the pings burned on gaps that outlive the cap;
    # savings count only the gaps it actually bridges.
    cap: timedelta | None = None
    cap_solved: bool = False
    capped_pings: int = 0
    capped_ping_usd: Decimal = Decimal("0")
    capped_rewrite_usd: Decimal = Decimal("0")
    capped_rewrite_usd_lower: Decimal = Decimal("0")
    capped_bridged: int = 0
    capped_abandoned: int = 0
    capped_margins: tuple[Decimal, ...] = ()
    sweep: CapSweep | None = None

    @property
    def ping_worth_it(self) -> bool:
        return self.ping_usd < self.rewrite_usd

    @property
    def capped_worth_it(self) -> bool:
        return self.capped_ping_usd < self.capped_rewrite_usd

    @property
    def capped_verdict(self) -> str:
        """Three-state, because two states cannot express "we do not know".

        The binary ``capped_worth_it`` compares the ping bill against the UPPER
        bound alone, so it answers WORTH IT for everything between the two
        bounds — the region where the answer depends entirely on a modelling
        choice rather than on the data. This one refuses there.
        """
        return _tri_verdict(
            self.capped_ping_usd, self.capped_rewrite_usd_lower, self.capped_rewrite_usd
        )

    @property
    def gaps_to_resolve(self) -> int | None:
        """Expired gaps needed before the pessimistic margin's SIGN is real.

        None when the question does not arise (fewer than two gaps, or a mean
        margin of exactly zero). See :func:`_gaps_to_resolve` for what this
        does and does not settle.
        """
        return _gaps_to_resolve(self.capped_margins)


def _bucket(gap: timedelta) -> str:
    if gap < _FIVE_MIN:
        return GAP_BUCKETS[0]
    if gap <= _ONE_HOUR:
        return GAP_BUCKETS[1]
    if gap <= _FOUR_HOURS:
        return GAP_BUCKETS[2]
    return GAP_BUCKETS[3]


def _creation_tokens(rec: Record) -> int:
    m5, h1 = cache_creation_split(rec.usage or {})
    return m5 + h1


def _median(values: Sequence[int] | Sequence[Decimal]) -> Decimal:
    """Exact median as a Decimal — ``statistics.median`` would return a float.

    Money downstream is Decimal, and an even-length median is a halved sum; a
    float round-trip there is the one place this module could quietly lose a
    cent it never had to lose.
    """
    if not values:
        return Decimal("0")
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return Decimal(ordered[mid])
    return (Decimal(ordered[mid - 1]) + Decimal(ordered[mid])) / 2


def _rewrite_cost(rec: Record) -> Decimal | None:
    """Cache-write cost of one message, at its own TTL multipliers.

    UPPER BOUND on the cost of a cache expiry, by construction: a post-gap
    message writes both the prefix it had to re-establish AND whatever the turn
    genuinely added, and nothing in ``usage`` separates the two.
    """
    if rec.usage is None:
        return None
    price = price_for(rec.model_id, rec.invoked_at)
    if price is None:
        return None
    tier = _tier_multiplier(price, rec.usage)
    if tier is None:
        return None
    m5, h1 = cache_creation_split(rec.usage)
    p = price.input_per_mtok
    return (
        m5 * p * price.cache_write_5m_mult + h1 * p * price.cache_write_1h_mult
    ) * tier / _MTOK


def _ping_cost(rec: Record, pings: int) -> Decimal | None:
    """Cost of ``pings`` keep-alive reads of this message's whole prompt.

    A ping is a no-op request that re-reads the cached prefix, so it bills the
    prompt at the cache-read multiplier (0.1x). Volume and model come from the
    message BEFORE the gap — that is what is sitting in the cache while the
    session is idle.
    """
    if pings <= 0:
        return Decimal("0")
    price = price_for(rec.model_id, rec.invoked_at)
    if price is None:
        return None
    tier = _tier_multiplier(price, rec.usage)
    if tier is None:
        return None
    return (
        rec.prompt_tokens * price.input_per_mtok * price.cache_read_mult * tier * pings
    ) / _MTOK


def _rewrite_floor(rec: Record, baseline: Decimal) -> Decimal | None:
    """LOWER BOUND on what one cache expiry cost, given a per-session baseline.

    THE ASSUMPTION, stated so it can be argued with: a post-gap message's
    ``cache_creation`` is split into "content this turn would have written
    anyway" and "prefix the expiry forced it to re-establish". ``baseline`` —
    the median ``cache_creation`` of the same session's non-post-gap messages —
    stands in for the first part and is subtracted, floored at zero. What is
    left is priced at the message's own 5m/1h TTL mix, i.e. the upper bound
    scaled by the surviving token share, because ``usage`` gives no basis for
    charging the residual to one TTL rather than the other.

    Nothing in the API supports that decomposition. It is a modelling choice
    that produces a number below the truth under one reading of a session's
    behaviour and merely near it under another, so the pair (this,
    :func:`_rewrite_cost`) brackets the answer rather than pinning it.
    """
    upper = _rewrite_cost(rec)
    if upper is None:
        return None
    total = _creation_tokens(rec)
    if total <= 0:
        return Decimal("0")
    residual = max(Decimal("0"), Decimal(total) - baseline)
    return upper * residual / Decimal(total)


def pings_to_bridge(gap: timedelta) -> int:
    """Pings needed to hold a cache open across ``gap`` at ``PING_INTERVAL``.

    Pings land at t+55m, t+110m, … up to (not including) the next real request,
    so a 60-minute gap needs one and a 4-hour gap needs four.
    """
    if gap <= PING_INTERVAL:
        return 0
    return math.ceil(gap / PING_INTERVAL) - 1


@dataclass(frozen=True)
class ExpiredGap:
    """One >1h gap, reduced to what every cap in the sweep needs from it.

    ``prev`` is kept whole rather than pre-costed because ping cost is a
    function of the cap, and re-costing through :func:`_ping_cost` keeps every
    cap's arithmetic identical to the single-cap path it replaced.
    """

    gap: timedelta
    bucket: str
    prev: Record
    rewrite_upper: Decimal | None
    rewrite_lower: Decimal | None
    rewrite_tokens: int
    rewrite_tokens_lower: Decimal


def _scan_sessions(records: Sequence[Record]) -> tuple[list[ExpiredGap], GapStats]:
    """One pass over the timeline: bucket counts, and every expired gap costed.

    The rewrite bounds are settled here, once, because the per-session baseline
    they need is a property of the session rather than of any cap.
    """
    stats = GapStats()
    by_session: dict[str, list[Record]] = {}
    for rec in records:
        if rec.session_id is None:
            continue
        by_session.setdefault(rec.session_id, []).append(rec)

    expired: list[ExpiredGap] = []
    baselines: list[Decimal] = []
    for session in by_session.values():
        session.sort(key=lambda r: r.invoked_at)
        stats.sessions += 1

        # A message is "post-gap" when a >1h gap sits immediately before it; the
        # first message of a session never is. Indices, not the records
        # themselves, because Record carries a dict and so cannot go in a set.
        post_gap = {
            i
            for i in range(1, len(session))
            if session[i].invoked_at - session[i - 1].invoked_at > _ONE_HOUR
        }
        baseline = _median(
            [_creation_tokens(r) for i, r in enumerate(session) if i not in post_gap]
        )
        baselines.append(baseline)

        for i, (prev, cur) in enumerate(zip(session, session[1:]), start=1):
            gap = cur.invoked_at - prev.invoked_at
            stats.gaps += 1
            bucket = _bucket(gap)
            stats.buckets[bucket] += 1
            if i not in post_gap:
                continue
            stats.expired_gaps += 1
            total = _creation_tokens(cur)
            expired.append(
                ExpiredGap(
                    gap=gap,
                    bucket=bucket,
                    prev=prev,
                    rewrite_upper=_rewrite_cost(cur),
                    rewrite_lower=_rewrite_floor(cur, baseline),
                    rewrite_tokens=total,
                    rewrite_tokens_lower=max(Decimal("0"), Decimal(total) - baseline),
                )
            )
    stats.baseline_tokens = _median(baselines)
    return expired, stats


@dataclass(frozen=True)
class CapPoint:
    """What one give-up threshold would have cost and saved.

    ``cap=None`` is the uncapped policy: it bridges everything and pays for
    everything, and it sits in the sweep as a competitor rather than as a
    footnote.
    """

    cap: timedelta | None
    bridged: int
    abandoned: int
    pings: int
    ping_usd: Decimal
    saved_lower: Decimal
    saved_upper: Decimal
    margins: tuple[Decimal, ...] = ()

    @property
    def net_lower(self) -> Decimal:
        return self.saved_lower - self.ping_usd

    @property
    def net_upper(self) -> Decimal:
        return self.saved_upper - self.ping_usd

    @property
    def verdict(self) -> str:
        return _tri_verdict(self.ping_usd, self.saved_lower, self.saved_upper)


def _tri_verdict(ping: Decimal, lower: Decimal, upper: Decimal) -> str:
    """WORTH IT / NOT WORTH IT / UNDECIDED, in that order of confidence.

    UNDECIDED is the honest answer whenever the ping bill lands between the two
    rewrite bounds: inside that band the sign of the decision is set by the
    modelling assumption in :func:`_rewrite_floor`, not by anything measured.
    """
    if ping < lower:
        return "WORTH IT"
    if ping > upper:
        return "NOT WORTH IT"
    return "UNDECIDED"


def _gaps_to_resolve(margins: Sequence[Decimal]) -> int | None:
    """Sample size at which the pessimistic per-gap margin's sign leaves its noise.

    WHAT THIS DOES NOT SAY. It does not say when the UNDECIDED band closes. The
    band is the distance between two modelling assumptions, and more traffic of
    the same shape scales both bounds and the ping bill together, so it does not
    close on its own — no sample size fixes that. What more data CAN settle is
    whether the lower-bound margin is really negative or is itself inside the
    per-gap scatter; this returns the n at which a two-sided 95% interval on the
    mean margin would exclude zero.
    """
    n = len(margins)
    if n < 2:
        return None
    mean = sum(margins, Decimal("0")) / n
    if mean == 0:
        return None
    var = sum(((m - mean) ** 2 for m in margins), Decimal("0")) / (n - 1)
    if var <= 0:
        return n  # every gap agreed exactly; the sign is as resolved as it gets
    needed = (_Z95 * var.sqrt() / abs(mean)) ** 2
    return int(needed.to_integral_value(rounding=ROUND_CEILING))


def cap_grid() -> tuple[timedelta, ...]:
    """Every cap the sweep tries, CAP_SWEEP_MIN..CAP_SWEEP_MAX inclusive."""
    out: list[timedelta] = []
    cap = CAP_SWEEP_MIN
    while cap <= CAP_SWEEP_MAX:
        out.append(cap)
        cap += CAP_SWEEP_STEP
    return tuple(out)


def cap_point(expired: Sequence[ExpiredGap], cap: timedelta | None) -> CapPoint:
    """Cost one give-up threshold against every expired gap in the corpus."""
    cap_pings = None if cap is None else pings_to_bridge(cap)
    pings = bridged = abandoned = 0
    ping_usd = saved_lower = saved_upper = Decimal("0")
    margins: list[Decimal] = []
    for eg in expired:
        n = pings_to_bridge(eg.gap)
        if cap_pings is not None:
            n = min(n, cap_pings)
        cost = _ping_cost(eg.prev, n)
        margin = Decimal("0")
        if cost is not None:
            pings += n
            ping_usd += cost
            margin -= cost
        if cap is None or eg.gap <= cap:
            bridged += 1
            if eg.rewrite_upper is not None and eg.rewrite_lower is not None:
                saved_upper += eg.rewrite_upper
                saved_lower += eg.rewrite_lower
                margin += eg.rewrite_lower
        else:
            abandoned += 1
        margins.append(margin)
    return CapPoint(
        cap=cap,
        bridged=bridged,
        abandoned=abandoned,
        pings=pings,
        ping_usd=ping_usd,
        saved_lower=saved_lower,
        saved_upper=saved_upper,
        margins=tuple(margins),
    )


@dataclass
class CapSweep:
    """The whole net-benefit curve, plus what can honestly be read off it."""

    points: list[CapPoint]
    best: CapPoint
    plateau: tuple[timedelta, timedelta] | None
    robust_plateau: tuple[timedelta, timedelta] | None

    @property
    def grid_points(self) -> list[CapPoint]:
        return [p for p in self.points if p.cap is not None]

    @property
    def unbounded(self) -> CapPoint | None:
        return next((p for p in self.points if p.cap is None), None)

    @staticmethod
    def _width(span: tuple[timedelta, timedelta] | None) -> timedelta | None:
        return None if span is None else span[1] - span[0]

    @property
    def plateau_width(self) -> timedelta | None:
        return self._width(self.plateau)

    @property
    def robust_plateau_width(self) -> timedelta | None:
        return self._width(self.robust_plateau)

    def plateau_censored(self, span: tuple[timedelta, timedelta] | None) -> bool:
        """True when a plateau runs into a grid edge rather than a sign change.

        A censored edge is not a measured one: the run may continue past
        CAP_SWEEP_MAX, and the sweep cannot tell.
        """
        if span is None:
            return False
        return span[0] <= CAP_SWEEP_MIN or span[1] >= CAP_SWEEP_MAX


def _plateau(
    points: Sequence[CapPoint], key
) -> tuple[timedelta, timedelta] | None:
    """Widest run of consecutive grid caps around the argmax where ``key`` > 0.

    Anchored on the argmax rather than on the longest positive run anywhere, so
    the width reported is the width of the run the headline cap sits in. A
    single grid point yields a width of zero — which is the answer that says
    the argmax is a spike, not a shelf.
    """
    if not points:
        return None
    anchor = max(range(len(points)), key=lambda i: key(points[i]))
    if key(points[anchor]) <= 0:
        return None
    lo = hi = anchor
    while lo > 0 and key(points[lo - 1]) > 0:
        lo -= 1
    while hi < len(points) - 1 and key(points[hi + 1]) > 0:
        hi += 1
    return points[lo].cap, points[hi].cap  # type: ignore[return-value]


def sweep_caps(expired: Sequence[ExpiredGap]) -> CapSweep:
    """Cost every cap on the grid, then the uncapped policy, then read the curve.

    The argmax is taken on net benefit against the UPPER rewrite bound — the
    same optimistic measure the rest of section 3 uses — so the cap is chosen
    under the reading most favourable to pinging, and the lower-bound column
    beside it says whether that choice survives the pessimistic one. Ties go to
    the smaller cap: same net, cheaper policy to run.
    """
    grid = [cap_point(expired, cap) for cap in cap_grid()]
    points = grid + [cap_point(expired, None)]
    best = max(points, key=lambda p: p.net_upper)  # first max wins → smallest cap
    return CapSweep(
        points=points,
        best=best,
        plateau=_plateau(grid, lambda p: p.net_upper),
        robust_plateau=_plateau(grid, lambda p: p.net_lower),
    )


def session_gaps(
    records: Sequence[Record], *, cap: timedelta | None | Any = _SOLVE
) -> GapStats:
    """Gap distribution plus the expiry-rewrite and keep-alive counterfactuals.

    ``cap`` defaults to the sentinel ``_SOLVE``, which runs the sweep and takes
    its argmax. Pass a ``timedelta`` to pin a policy, or ``None`` for the
    uncapped one.
    """
    expired, stats = _scan_sessions(records)
    stats.sweep = sweep_caps(expired)
    stats.cap_solved = cap is _SOLVE
    stats.cap = stats.sweep.best.cap if stats.cap_solved else cap

    for eg in expired:
        bucket_costs = stats.bucket_costs[eg.bucket]
        if eg.rewrite_upper is None or eg.rewrite_lower is None:
            stats.rewrite_unpriced += 1
            bucket_costs.rewrite_unpriced += 1
        else:
            stats.rewrite_usd += eg.rewrite_upper
            stats.rewrite_usd_lower += eg.rewrite_lower
            bucket_costs.rewrite_usd += eg.rewrite_upper
            bucket_costs.rewrite_usd_lower += eg.rewrite_lower
            stats.rewrite_tokens += eg.rewrite_tokens
            stats.rewrite_tokens_lower += eg.rewrite_tokens_lower

        n = pings_to_bridge(eg.gap)
        cost = _ping_cost(eg.prev, n)
        if cost is None:
            stats.ping_unpriced += 1
            bucket_costs.ping_unpriced += 1
        else:
            stats.pings += n
            stats.ping_usd += cost
            bucket_costs.pings += n
            bucket_costs.ping_usd += cost

    chosen = cap_point(expired, stats.cap)
    stats.capped_pings = chosen.pings
    stats.capped_ping_usd = chosen.ping_usd
    stats.capped_rewrite_usd = chosen.saved_upper
    stats.capped_rewrite_usd_lower = chosen.saved_lower
    stats.capped_bridged = chosen.bridged
    stats.capped_abandoned = chosen.abandoned
    stats.capped_margins = chosen.margins
    return stats


# ── section 4: direct (non-Claude-Code) API traffic ─────────────────────────


@dataclass
class DirectRow:
    model_id: str
    calls: int = 0
    prompt_tokens: int = 0
    cache_read: int = 0
    with_usage: int = 0

    @property
    def avg_prompt(self) -> float | None:
        return self.prompt_tokens / self.calls if self.calls else None

    @property
    def hit_rate(self) -> float | None:
        """None when NO row carries a usage block — unknown, not zero."""
        if not self.with_usage or not self.prompt_tokens:
            return None
        return self.cache_read / self.prompt_tokens

    @property
    def min_cacheable(self) -> int | None:
        return MIN_CACHEABLE_TOKENS.get(self.model_id)

    @property
    def meets_minimum(self) -> bool | None:
        floor = self.min_cacheable
        avg = self.avg_prompt
        if floor is None or avg is None:
            return None
        return avg >= floor

    def verdict(self) -> str:
        floor, avg = self.min_cacheable, self.avg_prompt
        if avg is None:
            return "no calls in window"
        if floor is None:
            return (
                f"no published minimum cacheable length for {self.model_id} — "
                "no structural verdict"
            )
        if avg < floor:
            return (
                f"average prompt {avg:,.0f} tok is below the {floor:,}-token minimum "
                f"cacheable prefix for {self.model_id}: caching cannot engage at all "
                "on this traffic, so a 0% hit rate is structural, not a misconfiguration"
            )
        if self.hit_rate is None:
            return (
                f"average prompt {avg:,.0f} tok clears the {floor:,}-token minimum, but "
                "no usage block was recorded — hit rate unknown, not zero"
            )
        if self.hit_rate == 0:
            return (
                f"average prompt {avg:,.0f} tok clears the {floor:,}-token minimum yet "
                "zero tokens were served from cache: single-shot unique prompts, or a "
                "prefix that changes every call"
            )
        return f"average prompt {avg:,.0f} tok clears the {floor:,}-token minimum; cache is engaging"


def direct_traffic(records: Iterable[Record]) -> list[DirectRow]:
    rows: dict[str, DirectRow] = {}
    for rec in records:
        key = rec.model_id or "(none)"
        row = rows.setdefault(key, DirectRow(model_id=key))
        row.calls += 1
        row.prompt_tokens += rec.prompt_tokens
        if rec.usage is not None:
            row.with_usage += 1
            row.cache_read += int(rec.usage.get("cache_read_input_tokens") or 0)
    return sorted(rows.values(), key=lambda r: r.calls, reverse=True)


# ── report assembly ─────────────────────────────────────────────────────────


@dataclass
class Section:
    key: str
    title: str
    columns: list[str]
    rows: list[list[str]]
    notes: list[str] = field(default_factory=list)


@dataclass
class CacheAudit:
    db_url: str
    since: datetime | None
    until: datetime | None
    total_traces: int
    cc_records: int
    models: list[ModelRow]
    gaps: GapStats
    direct: list[DirectRow]

    @property
    def actual_usd(self) -> Decimal:
        return sum((m.actual_usd for m in self.models if m.priced_messages), Decimal("0"))

    @property
    def counterfactual_usd(self) -> Decimal:
        return sum(
            (m.counterfactual_usd for m in self.models if m.priced_messages), Decimal("0")
        )

    @property
    def saved_pct(self) -> float | None:
        cf = self.counterfactual_usd
        if cf == 0:
            return None
        return float((cf - self.actual_usd) / cf)

    def summary_line(self) -> str:
        pct = self.saved_pct
        if pct is None:
            return (
                "No priced Claude Code traffic in this window. "
                "Checked with: python -m traceguard.routing_audit.cache_audit"
            )
        return (
            f"Claude Code caching already saves us {pct:.0%} "
            f"({_usd(self.actual_usd)} vs {_usd(self.counterfactual_usd)} list). "
            "Checked with: python -m traceguard.routing_audit.cache_audit"
        )


def _usd(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"${value.quantize(_USD):,}"


def _signed_usd(value: Decimal | None) -> str:
    """Money that can legitimately be negative — a net benefit, not a bill."""
    if value is None:
        return "n/a"
    q = value.quantize(_USD)
    return f"-${-q:,}" if q < 0 else f"${q:,}"


def _cap_label(cap: timedelta | None) -> str:
    """``4h`` / ``2h15m`` / ``15m`` / ``no cap``.

    Whole hours stay bare so a solved cap that lands on one keeps reading the
    way the hardcoded one did, and sub-hour spans drop the empty hour so a
    15-minute step is not written ``0h15m``.
    """
    if cap is None:
        return "no cap"
    minutes = int(cap.total_seconds() // 60)
    hours, rem = divmod(minutes, 60)
    if not hours:
        return f"{rem}m"
    return f"{hours}h" if rem == 0 else f"{hours}h{rem:02d}m"


def _span_label(span: tuple[timedelta, timedelta] | None) -> str:
    if span is None:
        return "none"
    return f"{_cap_label(span[0])}..{_cap_label(span[1])}"


def _width_label(width: timedelta | None) -> str:
    if width is None:
        return "n/a"
    if width == timedelta(0):
        return "0 (a single grid point)"
    return _cap_label(width)


def _verdict_sentence(
    verdict: str,
    ping: Decimal,
    lower: Decimal,
    upper: Decimal,
    needed: int | None,
    have: int,
) -> str:
    """One line that carries the verdict AND the margin that produced it."""
    if verdict == "WORTH IT":
        return f"WORTH IT: pings {_usd(ping)} < avoidable rewrites {_usd(lower)} (lower bound)"
    if verdict == "NOT WORTH IT":
        return (
            f"NOT WORTH IT: pings {_usd(ping)} > avoidable rewrites {_usd(upper)} "
            "(upper bound)"
        )
    tail = ""
    if needed is not None and needed > have:
        tail = (
            f"; the pessimistic side is itself inside its scatter — ~{needed:,} "
            f"expired gaps would settle its sign, we have {have:,}"
        )
    elif needed is not None:
        tail = (
            f"; {have:,} gaps already settle the pessimistic side's sign (~{needed:,} "
            "needed), so more traffic of this shape will not close the band"
        )
    return f"UNDECIDED: pings {_usd(ping)} sits inside [{_usd(lower)}, {_usd(upper)}]{tail}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _tok(value: int | float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.0f}"


def audit(
    db_url: str | None = None,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    cap: timedelta | None | Any = _SOLVE,
) -> CacheAudit:
    """Run the whole audit against a store. Opens read-only; writes nothing.

    ``cap`` is the keep-alive give-up threshold. It defaults to being solved
    from the corpus (section 3b); pass a ``timedelta`` to pin one, or ``None``
    to price the policy that never gives up.
    """
    records = load_records(db_url, since=since, until=until)
    cc = [
        r
        for r in records
        if r.source == CC_SOURCE and r.usage is not None and r.session_id is not None
    ]
    direct = [r for r in records if r.source != CC_SOURCE]
    return CacheAudit(
        db_url=db_url or DEFAULT_DB,
        since=since,
        until=until,
        total_traces=len(records),
        cc_records=len(cc),
        models=per_model(cc),
        gaps=session_gaps(cc, cap=cap),
        direct=direct_traffic(direct),
    )


def build_sections(a: CacheAudit) -> list[Section]:
    sections: list[Section] = []

    # 1 ── per model
    rows = [
        [
            m.model_id,
            _tok(m.messages),
            _tok(m.prompt_tokens),
            _pct(m.hit_rate),
            _usd(m.actual_usd if m.priced_messages else None),
            _usd(m.counterfactual_usd if m.priced_messages else None),
            _usd(m.saved_usd),
            _pct(m.saved_pct),
        ]
        for m in a.models
    ]
    if a.models:
        rows.append(
            [
                "TOTAL",
                _tok(sum(m.messages for m in a.models)),
                _tok(sum(m.prompt_tokens for m in a.models)),
                _pct(_ratio(
                    sum(m.cache_read for m in a.models),
                    sum(m.prompt_tokens for m in a.models),
                )),
                _usd(a.actual_usd),
                _usd(a.counterfactual_usd),
                _usd(a.counterfactual_usd - a.actual_usd),
                _pct(a.saved_pct),
            ]
        )
    notes = [
        "Hit rate is token-weighted: cache_read / (input + cache_read + cache_write_5m "
        "+ cache_write_1h).",
        "Costs are the INPUT side only, at published list price (reads 0.1x, 5m writes "
        "1.25x, 1h writes 2x); output tokens are excluded because caching cannot "
        "affect them. 'no-cache' prices the whole prompt at 1x.",
    ]
    unpriced = [m for m in a.models if m.unpriced_messages]
    if unpriced:
        notes.append(
            "n/a cost = no list price for that model (or speed tier) — "
            + ", ".join(f"{m.model_id}: {m.unpriced_messages} msg" for m in unpriced)
            + ". Tokens still counted; money is never guessed."
        )
    sections.append(
        Section(
            key="per_model",
            title="1. Claude Code traffic, per model",
            columns=[
                "model",
                "messages",
                "prompt tok",
                "hit rate",
                "input cost",
                "no-cache",
                "saved",
                "saved %",
            ],
            rows=rows,
            notes=notes,
        )
    )

    # 2 ── session gaps
    g = a.gaps
    gap_rows = []
    for name in GAP_BUCKETS:
        expires = name in _EXPIRED_BUCKETS
        bc = g.bucket_costs[name]
        gap_rows.append(
            [
                name,
                _tok(g.buckets[name]),
                _pct(_ratio(g.buckets[name], g.gaps)),
                _usd(bc.rewrite_usd_lower) if expires else _NO_EXPIRY,
                _usd(bc.rewrite_usd) if expires else _NO_EXPIRY,
                _usd(bc.ping_usd) if expires else _NO_EXPIRY,
                (
                    _tri_verdict(bc.ping_usd, bc.rewrite_usd_lower, bc.rewrite_usd)
                    if expires and g.buckets[name]
                    else _NO_EXPIRY
                ),
            ]
        )
    narrowing = 0.0 if not g.rewrite_usd else 1 - float(g.rewrite_usd_lower / g.rewrite_usd)
    gap_notes = [
        f"{g.sessions:,} sessions, {g.gaps:,} intervals between consecutive requests "
        f"(grouped by output_parsed.session_id, ordered by invoked_at).",
        "Per-bucket money answers the question the totals hide: a rate averaged over "
        "buckets that behave differently is not a decision. Rewrite is the same pair "
        "of bounds as below, restricted to the bucket; ping is what bridging only "
        f"that bucket's gaps would have cost. All three read '{_NO_EXPIRY}' inside "
        "the 1h TTL, where nothing expires and there is nothing to bridge — distinct "
        "from the 'n/a' elsewhere, which means no list price.",
        f"Cache-expiry rewrite cost, UPPER BOUND: {_usd(g.rewrite_usd)} "
        f"({_tok(g.rewrite_tokens)} cache-creation tokens across the "
        f"{g.expired_gaps:,} first-messages-after-a->1h-gap, each at its own TTL "
        "write multiplier).",
        "UPPER BOUND, and the word is load-bearing: a post-gap message's "
        "cache_creation covers both the prefix it had to re-establish AND whatever "
        "that turn genuinely added. usage does not separate the two, so the true "
        "expiry cost is somewhere at or below this number — never above it.",
        f"LOWER BOUND: {_usd(g.rewrite_usd_lower)} "
        f"({_tok(float(g.rewrite_tokens_lower))} tokens). Built by crediting each "
        "post-gap message with the median cache_creation of the same session's "
        "non-post-gap messages — the content that turn would have written anyway — "
        "and charging only the remainder, floored at zero, at that message's own "
        "5m/1h TTL mix.",
        "LOWER BOUND, self-exposed: nothing in usage supports that decomposition. "
        "It assumes an ordinary turn and a post-gap turn write the same amount of "
        "genuinely new content, which is a guess about behaviour, not a measurement. "
        f"On this corpus the median session baseline is {_tok(float(g.baseline_tokens))} "
        f"tokens against {_tok(_ratio(g.rewrite_tokens, g.expired_gaps))} tokens per "
        f"post-gap write, so the subtraction moves the total by {narrowing:.1%}. A "
        "narrow interval here is a fact about this traffic — post-gap writes dwarf "
        "ordinary ones — and not evidence that either bound is tight.",
    ]
    if g.rewrite_unpriced:
        gap_notes.append(
            f"{g.rewrite_unpriced:,} post-gap messages had no list price and "
            "contribute no money to either bound."
        )
    sections.append(
        Section(
            key="gaps",
            title="2. Session-internal gap distribution",
            columns=[
                "gap",
                "count",
                "share",
                "rewrite >=",
                "rewrite <=",
                "ping cost",
                "verdict",
            ],
            rows=gap_rows,
            notes=gap_notes,
        )
    )

    # 3 ── keep-alive ping counterfactual
    verdict = (
        f"WORTH IT: pings {_usd(g.ping_usd)} < avoidable rewrites {_usd(g.rewrite_usd)}"
        if g.ping_worth_it
        else f"NOT WORTH IT: pings {_usd(g.ping_usd)} >= avoidable rewrites "
        f"{_usd(g.rewrite_usd)}"
    )
    cap_name = _cap_label(g.cap)
    capped_verdict = _verdict_sentence(
        g.capped_verdict,
        g.capped_ping_usd,
        g.capped_rewrite_usd_lower,
        g.capped_rewrite_usd,
        g.gaps_to_resolve,
        g.expired_gaps,
    )
    ping_rows = [
        ["gaps bridged", _tok(g.expired_gaps)],
        ["pings needed", _tok(g.pings)],
        ["ping cost", _usd(g.ping_usd)],
        ["rewrite cost avoided (upper bound)", _usd(g.rewrite_usd)],
        ["rewrite cost avoided (lower bound)", _usd(g.rewrite_usd_lower)],
        ["verdict", verdict],
        [
            "keep-alive cap",
            f"{cap_name} ({'solved' if g.cap_solved else 'pinned'})",
        ],
        # Every capped row carries the suffix rather than sitting under a
        # separator: the CSV renderer keys on this column, so two rows called
        # "ping cost" would be told apart only by their order in the file.
        [
            f"gaps bridged / abandoned (capped {cap_name})",
            f"{g.capped_bridged:,} / {g.capped_abandoned:,}",
        ],
        [f"pings needed (capped {cap_name})", _tok(g.capped_pings)],
        [f"ping cost (capped {cap_name})", _usd(g.capped_ping_usd)],
        [
            f"rewrite cost avoided (capped {cap_name}, upper bound)",
            _usd(g.capped_rewrite_usd),
        ],
        [
            f"rewrite cost avoided (capped {cap_name}, lower bound)",
            _usd(g.capped_rewrite_usd_lower),
        ],
        [f"verdict (capped {cap_name})", capped_verdict],
    ]
    ping_notes = [
        f"Counterfactual: one keep-alive every {int(PING_INTERVAL.total_seconds() // 60)} "
        "minutes across every >1h gap, each billed as a 0.1x cache read of the whole "
        "prompt as it stood before the gap.",
        "Two approximations, both stated rather than hidden: prompt volume is frozen "
        "at the pre-gap message (a real session grows), and caches are model-scoped, "
        "so a mid-session model switch makes the pings that preceded it worthless.",
        "The 'verdict' row above is the unbounded policy against the UPPER bound "
        "alone, kept unchanged as the control: it is the two-state answer this "
        "section used to give everywhere, and it is still the right shape for a "
        "policy nobody would run.",
        f"The capped block is the policy you could actually run: ping until "
        f"{cap_name} of idle, then give up. It pays for the pings burned on the gaps "
        f"that outlive the cap ({g.capped_abandoned:,} of them) and banks savings "
        f"only on the {g.capped_bridged:,} it bridges, so it needs no foreknowledge "
        "of how long a gap will turn out to be. Where the two verdicts disagree, the "
        "unbounded one is not the interesting answer.",
        f"The cap is {'solved on this corpus' if g.cap_solved else 'pinned by the caller'}, "
        "not picked for tidiness — section 3b shows the whole curve it came off.",
        "Its verdict is three-state. WORTH IT means the ping bill undercuts even the "
        "LOWER bound on avoided rewrites; NOT WORTH IT means it exceeds the upper "
        "one; UNDECIDED means it landed between them, where the sign of the answer "
        "is set by the modelling assumption behind the lower bound rather than by "
        "anything in usage. A two-state verdict reports that middle region as a win.",
        "Everything here still inherits the pro-ping tilt: ping cost is charged as a "
        "pure cache read of a frozen prompt, and a real session's prompt grows. That "
        "tilt makes a refusal solid and an endorsement only as wide as its margin — "
        "which is now printed as a margin rather than implied.",
    ]
    if g.ping_unpriced:
        ping_notes.append(
            f"{g.ping_unpriced:,} gaps had an unpriced pre-gap message and are "
            "excluded from the ping total."
        )
    sections.append(
        Section(
            key="ping",
            title="3. Keep-alive ping counterfactual",
            columns=["metric", "value"],
            rows=ping_rows,
            notes=ping_notes,
        )
    )

    # 3b ── the curve the cap came off. Numbered 3b rather than promoted to 4
    # because sections 1 and 4 are quoted verbatim elsewhere; renumbering them
    # to make room would be a cosmetic change with a downstream cost.
    sections.append(_sweep_section(g))

    # 4 ── direct API traffic
    direct_rows = [
        [
            d.model_id,
            _tok(d.calls),
            _pct(d.hit_rate),
            _tok(d.avg_prompt),
            "n/a" if d.min_cacheable is None else _tok(d.min_cacheable),
            {None: "unknown", True: "yes", False: "no"}[d.meets_minimum],
        ]
        for d in a.direct
    ]
    direct_notes = (
        [d.verdict() for d in a.direct]
        if a.direct
        else ["No non-claude_code_session traces in this window."]
    )
    direct_notes.append(
        "Direct traffic = every trace whose output_parsed.source is not "
        f"'{CC_SOURCE}' (SDK wrappers, harnesses); a NULL output_parsed counts here."
    )
    direct_notes.append(
        "Rows with no usage block price their prompt volume from tokens_in and report "
        "hit rate as n/a — unknown, not zero."
    )
    sections.append(
        Section(
            key="direct",
            title="4. Direct API traffic (non-Claude-Code)",
            columns=[
                "model",
                "calls",
                "hit rate",
                "avg prompt tok",
                "min cacheable",
                "reaches min",
            ],
            rows=direct_rows,
            notes=direct_notes,
        )
    )
    return sections


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _sweep_section(g: GapStats) -> Section:
    """Section 3b: net benefit against every cap on the grid, and what it means."""
    sweep = g.sweep
    if sweep is None:  # pragma: no cover — session_gaps always attaches one
        sweep = sweep_caps([])
    rows = [
        [
            _cap_label(p.cap),
            f"{p.bridged:,} / {p.abandoned:,}",
            _tok(p.pings),
            _usd(p.ping_usd),
            _usd(p.saved_lower),
            _usd(p.saved_upper),
            _signed_usd(p.net_lower),
            _signed_usd(p.net_upper),
            p.verdict,
        ]
        for p in sweep.points
    ]
    best = sweep.best
    plateau, robust = sweep.plateau, sweep.robust_plateau
    width, robust_width = sweep.plateau_width, sweep.robust_plateau_width

    notes = [
        f"Every cap from {_cap_label(CAP_SWEEP_MIN)} to {_cap_label(CAP_SWEEP_MAX)} in "
        f"{_cap_label(CAP_SWEEP_STEP)} steps, plus 'no cap', costed against the same "
        f"{g.expired_gaps:,} expired gaps. Net = avoided rewrites - ping bill; the "
        "lower/upper pair is the same bracket section 2 builds.",
        f"argmax: {_cap_label(best.cap)}, net {_signed_usd(best.net_upper)} against "
        f"the upper bound ({_signed_usd(best.net_lower)} against the lower). Taken on "
        "the upper bound because that is the measure most favourable to pinging — if "
        "the cap cannot win there it cannot win anywhere. Ties go to the smaller cap.",
    ]
    if best.cap is None and plateau is not None:
        # The plateau is always read off the grid, so when the uncapped policy
        # wins outright it is not the run the headline sits in. Say that rather
        # than let the two numbers look like one statement.
        notes.append(
            "The argmax is the uncapped policy, which is not on the grid — the "
            "plateau below is the positive run among the capped policies only, and "
            "does not contain the winner."
        )
    if plateau is None:
        notes.append(
            "No cap on the grid reaches a positive net benefit, so there is no "
            "plateau and the argmax above is only the least-bad option. Reporting it "
            "as a recommendation would be a category error."
        )
    else:
        notes.append(
            f"Net stays positive across {_span_label(plateau)} — a plateau "
            f"{_width_label(width)} wide. This is the number that says whether the "
            "argmax means anything: a single optimum on a 15-minute spike is noise "
            "in the gap distribution, while a shelf several hours wide means any cap "
            "in that range buys roughly the same thing and the exact value does not "
            "matter."
        )
        if sweep.plateau_censored(plateau):
            edges = []
            if plateau[0] <= CAP_SWEEP_MIN:
                edges.append(f"the {_cap_label(CAP_SWEEP_MIN)} floor")
            if plateau[1] >= CAP_SWEEP_MAX:
                edges.append(f"the {_cap_label(CAP_SWEEP_MAX)} ceiling")
            notes.append(
                f"That plateau is censored at {' and '.join(edges)} — it ends where "
                "the sweep stops looking, not at a sign change, so its width is a "
                "floor rather than a measurement."
            )
    if robust is None:
        notes.append(
            "Under the LOWER bound no cap turns a profit, so every positive net above "
            "depends on the upper bound's benefit-of-the-doubt."
        )
    elif robust != plateau:
        notes.append(
            f"Under the LOWER bound the profitable range narrows to "
            f"{_span_label(robust)} ({_width_label(robust_width)} wide) — that is the "
            "part of the curve that survives the pessimistic reading."
        )
    else:
        notes.append(
            "The lower bound gives the same range, so the verdict does not turn on "
            "which bound you believe."
        )
    unbounded = sweep.unbounded
    if unbounded is not None:
        notes.append(
            f"'no cap' is a real competitor in the argmax, not a footnote: it bridges "
            f"all {unbounded.bridged:,} gaps for {_usd(unbounded.ping_usd)} and nets "
            f"{_signed_usd(unbounded.net_upper)}. If never giving up were best, this "
            "sweep would have said so."
        )
    return Section(
        key="cap_sweep",
        title="3b. Keep-alive cap sweep",
        columns=[
            "cap",
            "bridged / abandoned",
            "pings",
            "ping cost",
            "rewrite >=",
            "rewrite <=",
            "net >=",
            "net <=",
            "verdict",
        ],
        rows=rows,
        notes=notes,
    )


# ── rendering ───────────────────────────────────────────────────────────────


def _header_lines(a: CacheAudit) -> list[str]:
    window = "all time"
    if a.since or a.until:
        lo = a.since.isoformat() if a.since else "-inf"
        hi = a.until.isoformat() if a.until else "+inf"
        window = f"{lo} .. {hi}"
    return [
        f"db: {a.db_url} (read-only)   window: {window}",
        f"traces: {a.total_traces:,} total, {a.cc_records:,} cached Claude Code "
        f"messages in sections 1-3",
    ]


def render_table(a: CacheAudit, sections: Sequence[Section]) -> str:
    out: list[str] = ["== cache-efficiency audit =="]
    out.extend(_header_lines(a))
    for sec in sections:
        widths = [len(c) for c in sec.columns]
        for row in sec.rows:
            for i, cell in enumerate(row):
                if i < len(widths):
                    widths[i] = max(widths[i], len(cell))
        out.append("")
        out.append(sec.title)
        head = "  ".join(c.ljust(widths[i]) for i, c in enumerate(sec.columns))
        out.append(head)
        out.append("-" * len(head))
        for row in sec.rows:
            out.append(
                "  ".join(
                    cell.ljust(widths[i]) if i < len(widths) else cell
                    for i, cell in enumerate(row)
                ).rstrip()
            )
        for note in sec.notes:
            out.append(f"  * {note}")
    out.append("")
    out.append(a.summary_line())
    return "\n".join(out)


def render_md(a: CacheAudit, sections: Sequence[Section]) -> str:
    out: list[str] = ["# cache-efficiency audit", ""]
    out.extend(f"- {line}" for line in _header_lines(a))
    for sec in sections:
        out.append("")
        out.append(f"## {sec.title}")
        out.append("")
        out.append("| " + " | ".join(sec.columns) + " |")
        out.append("| " + " | ".join("---" for _ in sec.columns) + " |")
        for row in sec.rows:
            cells = list(row) + [""] * (len(sec.columns) - len(row))
            out.append("| " + " | ".join(cells) + " |")
        if sec.notes:
            out.append("")
            out.extend(f"- {note}" for note in sec.notes)
    out.append("")
    out.append(f"> {a.summary_line()}")
    return "\n".join(out)


def render_csv(a: CacheAudit, sections: Sequence[Section]) -> str:
    """Long/tidy form so every section fits one uniform schema."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["section", "item", "metric", "value"])
    for sec in sections:
        for row in sec.rows:
            item = row[0] if row else ""
            for col, cell in zip(sec.columns[1:], row[1:]):
                writer.writerow([sec.key, item, col, cell])
        for note in sec.notes:
            writer.writerow([sec.key, "", "note", note])
    writer.writerow(["summary", "", "line", a.summary_line()])
    return buf.getvalue()


RENDERERS = {"table": render_table, "md": render_md, "csv": render_csv}


def format_audit(a: CacheAudit, fmt: str = "table") -> str:
    return RENDERERS[fmt](a, build_sections(a))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m traceguard.routing_audit.cache_audit",
        description=(
            "Read-only prompt-cache efficiency audit over an existing traces store. "
            "Fill the store first with `python -m traceguard.routing_audit."
            "ingest_claude_code`; this command never writes."
        ),
    )
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--format", choices=tuple(RENDERERS), default="table")
    parser.add_argument("--since", default=None, help="ISO date/datetime (inclusive)")
    parser.add_argument("--until", default=None, help="ISO date/datetime (inclusive)")
    args = parser.parse_args(argv)

    try:
        since = parse_bound(args.since, flag="--since", end_of_day=False)
        until = parse_bound(args.until, flag="--until", end_of_day=True)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    result = audit(args.db, since=since, until=until)
    print(format_audit(result, args.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())

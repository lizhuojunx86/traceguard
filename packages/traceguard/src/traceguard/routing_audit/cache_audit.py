"""Cache-efficiency audit — turns "your prompt cache hit rate is low" into numbers.

A READ-ONLY REPORT, not a dashboard and not a gateway. It opens the store with
SQLite ``mode=ro`` and never writes a row, a table, or a file. Privacy follows
the ``rerun`` module's rule: nothing but aggregates, token counts and money
leaves the DB — no prompt bodies, no answers, no summaries.

It does NOT ingest. Point it at a store that ``ingest_claude_code`` already
filled; this module only reads ``traces.output_parsed.usage`` (the flat shape
the ingest writes — see :func:`pricing.cache_creation_split`, which reads both
the flat and nested forms, so a store written by some other path still prices).

Four sections, in the order a "why is your hit rate low" question actually
gets answered:

1. **per-model** — token-weighted hit rate, what the input side really cost at
   list price, and what it would have cost with no cache at all.
2. **session gaps** — where the idle time goes, and an UPPER BOUND on what
   cache expiry costs in re-writes.
3. **keep-alive ping counterfactual** — what it would cost to hold the cache
   open across every >1h gap, against that upper bound. Usually a refusal.
4. **direct API traffic** — the non-``claude_code_session`` rows (SDK wrappers,
   harnesses), where "low hit rate" is often structural rather than fixable.

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
from decimal import Decimal
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

# Give-up threshold for the capped keep-alive policy, also in section 3. An
# unbounded pinger pays for every gap it cannot see the end of; a capped one
# stops after PING_CAP of idle and eats the rewrite. Set to the 1-4h/>4h bucket
# boundary so the capped policy and the per-bucket columns in section 2 answer
# the same question from two directions.
PING_CAP = _FOUR_HOURS


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
    rewrite_tokens: int = 0
    rewrite_unpriced: int = 0       # post-gap messages with no price
    ping_usd: Decimal = Decimal("0")
    pings: int = 0
    ping_unpriced: int = 0          # pre-gap messages with no price
    # Capped policy: ping until PING_CAP of idle, then give up and let the
    # cache expire. Costs include the pings burned on gaps that outlive the
    # cap; savings count only the gaps it actually bridges.
    capped_pings: int = 0
    capped_ping_usd: Decimal = Decimal("0")
    capped_rewrite_usd: Decimal = Decimal("0")
    capped_bridged: int = 0
    capped_abandoned: int = 0

    @property
    def ping_worth_it(self) -> bool:
        return self.ping_usd < self.rewrite_usd

    @property
    def capped_worth_it(self) -> bool:
        return self.capped_ping_usd < self.capped_rewrite_usd


def _bucket(gap: timedelta) -> str:
    if gap < _FIVE_MIN:
        return GAP_BUCKETS[0]
    if gap <= _ONE_HOUR:
        return GAP_BUCKETS[1]
    if gap <= _FOUR_HOURS:
        return GAP_BUCKETS[2]
    return GAP_BUCKETS[3]


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


def pings_to_bridge(gap: timedelta) -> int:
    """Pings needed to hold a cache open across ``gap`` at ``PING_INTERVAL``.

    Pings land at t+55m, t+110m, … up to (not including) the next real request,
    so a 60-minute gap needs one and a 4-hour gap needs four.
    """
    if gap <= PING_INTERVAL:
        return 0
    return math.ceil(gap / PING_INTERVAL) - 1


def session_gaps(records: Sequence[Record]) -> GapStats:
    """Gap distribution plus the expiry-rewrite and keep-alive counterfactuals."""
    stats = GapStats()
    by_session: dict[str, list[Record]] = {}
    for rec in records:
        if rec.session_id is None:
            continue
        by_session.setdefault(rec.session_id, []).append(rec)

    cap_pings = pings_to_bridge(PING_CAP)
    for session in by_session.values():
        session.sort(key=lambda r: r.invoked_at)
        stats.sessions += 1
        for prev, cur in zip(session, session[1:]):
            gap = cur.invoked_at - prev.invoked_at
            stats.gaps += 1
            bucket = _bucket(gap)
            stats.buckets[bucket] += 1
            if gap <= _ONE_HOUR:
                continue
            stats.expired_gaps += 1
            bucket_costs = stats.bucket_costs[bucket]
            bridged = gap <= PING_CAP

            rewrite = _rewrite_cost(cur)
            if rewrite is None:
                stats.rewrite_unpriced += 1
                bucket_costs.rewrite_unpriced += 1
            else:
                stats.rewrite_usd += rewrite
                bucket_costs.rewrite_usd += rewrite
                m5, h1 = cache_creation_split(cur.usage or {})
                stats.rewrite_tokens += m5 + h1
                if bridged:
                    stats.capped_rewrite_usd += rewrite

            n = pings_to_bridge(gap)
            cost = _ping_cost(prev, n)
            if cost is None:
                stats.ping_unpriced += 1
                bucket_costs.ping_unpriced += 1
            else:
                stats.pings += n
                stats.ping_usd += cost
                bucket_costs.pings += n
                bucket_costs.ping_usd += cost

            capped_n = min(n, cap_pings)
            capped_cost = _ping_cost(prev, capped_n)
            if capped_cost is not None:
                stats.capped_pings += capped_n
                stats.capped_ping_usd += capped_cost
            if bridged:
                stats.capped_bridged += 1
            else:
                stats.capped_abandoned += 1
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
) -> CacheAudit:
    """Run the whole audit against a store. Opens read-only; writes nothing."""
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
        gaps=session_gaps(cc),
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
                _usd(bc.rewrite_usd) if expires else _NO_EXPIRY,
                _usd(bc.ping_usd) if expires else _NO_EXPIRY,
                (
                    ("ping wins" if bc.ping_usd < bc.rewrite_usd else "ping loses")
                    if expires and g.buckets[name]
                    else _NO_EXPIRY
                ),
            ]
        )
    gap_notes = [
        f"{g.sessions:,} sessions, {g.gaps:,} intervals between consecutive requests "
        f"(grouped by output_parsed.session_id, ordered by invoked_at).",
        "Per-bucket money answers the question the totals hide: a rate averaged over "
        "buckets that behave differently is not a decision. Rewrite is the same "
        "UPPER bound as below, restricted to the bucket; ping is what bridging only "
        f"that bucket's gaps would have cost. Both read '{_NO_EXPIRY}' inside the 1h "
        "TTL, where nothing expires and there is nothing to bridge — distinct from "
        "the 'n/a' elsewhere, which means no list price.",
        f"Cache-expiry rewrite cost, UPPER BOUND: {_usd(g.rewrite_usd)} "
        f"({_tok(g.rewrite_tokens)} cache-creation tokens across the "
        f"{g.expired_gaps:,} first-messages-after-a->1h-gap, each at its own TTL "
        "write multiplier).",
        "UPPER BOUND, and the word is load-bearing: a post-gap message's "
        "cache_creation covers both the prefix it had to re-establish AND whatever "
        "that turn genuinely added. usage does not separate the two, so the true "
        "expiry cost is somewhere at or below this number — never above it.",
    ]
    if g.rewrite_unpriced:
        gap_notes.append(
            f"{g.rewrite_unpriced:,} post-gap messages had no list price and "
            "contribute no money to the bound."
        )
    sections.append(
        Section(
            key="gaps",
            title="2. Session-internal gap distribution",
            columns=["gap", "count", "share", "rewrite <=", "ping cost", "verdict"],
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
    cap_hours = int(PING_CAP.total_seconds() // 3600)
    capped_verdict = (
        f"WORTH IT: pings {_usd(g.capped_ping_usd)} < avoidable rewrites "
        f"{_usd(g.capped_rewrite_usd)}"
        if g.capped_worth_it
        else f"NOT WORTH IT: pings {_usd(g.capped_ping_usd)} >= avoidable rewrites "
        f"{_usd(g.capped_rewrite_usd)}"
    )
    ping_rows = [
        ["gaps bridged", _tok(g.expired_gaps)],
        ["pings needed", _tok(g.pings)],
        ["ping cost", _usd(g.ping_usd)],
        ["rewrite cost avoided (upper bound)", _usd(g.rewrite_usd)],
        ["verdict", verdict],
        # Every capped row carries the suffix rather than sitting under a
        # separator: the CSV renderer keys on this column, so two rows called
        # "ping cost" would be told apart only by their order in the file.
        [
            f"gaps bridged / abandoned (capped {cap_hours}h)",
            f"{g.capped_bridged:,} / {g.capped_abandoned:,}",
        ],
        [f"pings needed (capped {cap_hours}h)", _tok(g.capped_pings)],
        [f"ping cost (capped {cap_hours}h)", _usd(g.capped_ping_usd)],
        [
            f"rewrite cost avoided (capped {cap_hours}h, upper bound)",
            _usd(g.capped_rewrite_usd),
        ],
        [f"verdict (capped {cap_hours}h)", capped_verdict],
    ]
    ping_notes = [
        f"Counterfactual: one keep-alive every {int(PING_INTERVAL.total_seconds() // 60)} "
        "minutes across every >1h gap, each billed as a 0.1x cache read of the whole "
        "prompt as it stood before the gap.",
        "Two approximations, both stated rather than hidden: prompt volume is frozen "
        "at the pre-gap message (a real session grows), and caches are model-scoped, "
        "so a mid-session model switch makes the pings that preceded it worthless.",
        "Compared against an UPPER bound on rewrites, so the pro-ping side of this "
        "comparison is already given the benefit of the doubt.",
        f"The capped block is the policy you could actually run: ping until {cap_hours}h "
        "of idle, then give up. It pays for the pings burned on the gaps that outlive "
        f"the cap ({g.capped_abandoned:,} of them) and banks savings only on the "
        f"{g.capped_bridged:,} it bridges, so it needs no foreknowledge of how long a "
        "gap will turn out to be. Where the two verdicts disagree, the unbounded one "
        "is not the interesting answer.",
        "Both verdicts inherit the same pro-ping tilt, which matters more when one of "
        "them says WORTH IT: savings are an upper bound while ping cost is charged as "
        "a pure cache read of a frozen prompt. A refusal under this tilt is solid; an "
        "endorsement is only as wide as its margin.",
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

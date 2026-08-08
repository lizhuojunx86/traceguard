"""List-price table for models observed in local Claude Code session data.

Contract-external data. SPEC §3.1 defines ``traces.cost_usd`` as *list price
at write time*; local Claude Code JSONL carries no cost field anywhere (no
``costUSD`` — verified against all local session files, CC 2.1.150–2.1.198),
so this table is the fallback used to compute it from ``message.usage``.

Price sources — verified against anthropic.com official pages 2026-07-02:
    fable-5 $10/$50, opus-4-8 $5/$25 (fast mode $10/$50 = 2×, per the
    Opus 4.8 announcement), opus-4-7 $5/$25, haiku-4.5 $1/$5 per MTok
    (input/output). Cache: hit/read = 0.1× input price; write 1.25×
    (5-minute TTL) / 2.0× (1-hour TTL) input price.

    Cache multipliers re-verified 2026-08-08 against the platform prompt-caching
    page: "5-minute cache write tokens are 1.25 times the base input tokens
    price / 1-hour cache write tokens are 2 times / Cache read tokens are 0.1
    times". The 2.0 constant was always right; until 2026-08-08 no code path
    could reach it (see cache_creation_split).

    opus-5 added 2026-08-08, verified against anthropic.com/news/claude-opus-5
    (published 2026-07-24): "priced at $5 per million input tokens and $25 per
    million output tokens (the same as Opus 4.8)", and "Fast mode is available
    at twice Opus 5's base price" → fast_multiplier 2. Cross-checked the same
    day against the platform models-overview table ("$5 / input MTok
    $25 / output MTok").

Speed tiers: ``usage.speed == "fast"`` bills at ``fast_multiplier`` × the
standard price across all token kinds (Opus 4.8 and Opus 5 have an official
fast price — both 2×). Models without a published fast price return None for
fast records rather than guessing. Records without a ``speed`` field (older CC
schema) are billed as standard.

Release dates (``KNOWN_RELEASED_AT``):
    opus-4-8 = 2026-05-28 and fable-5 = 2026-06-09 verified against
    anthropic.com/news 2026-07-02; opus-5 = 2026-07-24 verified against
    anthropic.com/news/claude-opus-5 on 2026-08-08 (page published that date,
    "available today on all platforms"). NOT the first local observation,
    which is 2026-07-25 02:06:28 — a day later, and the reason the SPEC §3.2
    ``released_at <= available_to_us_at`` check exists. haiku-4-5 date is
    embedded in the model id. opus-4-7 has no verified date yet
    (announcement page not located) —
    it falls back to first-seen-in-local-data, which satisfies
    ``released_at <= available_to_us_at`` (SPEC §3.2) but overstates it.
    ``available_to_us_at`` always keeps first-local-appearance semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

_MTOK = Decimal(1_000_000)
_COST_QUANTUM = Decimal("0.000001")  # traces.cost_usd is Numeric(12, 6)


@dataclass(frozen=True)
class ModelPrice:
    """USD per MTok list price plus cache multipliers on the input price.

    ``fast_multiplier`` scales the whole price sheet for ``speed == "fast"``
    records; None means the model has no published fast price.
    """

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_mult: Decimal = Decimal("0.1")
    cache_write_5m_mult: Decimal = Decimal("1.25")
    cache_write_1h_mult: Decimal = Decimal("2.0")
    fast_multiplier: Decimal | None = None


# Claude Sonnet 5 has two price eras (verified against the platform pricing
# page 2026-07-02): introductory $2/$10 through 2026-08-31, then standard
# $3/$15 from 2026-09-01. The base ``PRICES`` entry carries the CURRENT era's
# price; :func:`price_for` picks the right era by ``invoked_at`` so historical
# and future traces both reconcile. Cache multipliers are the standard
# 0.1×/1.25×/2× for both eras.
SONNET5_INTRO = ModelPrice(Decimal("2.00"), Decimal("10.00"))
SONNET5_STANDARD = ModelPrice(Decimal("3.00"), Decimal("15.00"))
SONNET5_STANDARD_FROM = datetime(2026, 9, 1, tzinfo=timezone.utc)

PRICES: dict[str, ModelPrice] = {
    "claude-opus-5": ModelPrice(
        Decimal("5.00"), Decimal("25.00"), fast_multiplier=Decimal("2")
    ),
    "claude-opus-4-8": ModelPrice(
        Decimal("5.00"), Decimal("25.00"), fast_multiplier=Decimal("2")
    ),
    "claude-opus-4-7": ModelPrice(Decimal("5.00"), Decimal("25.00")),
    "claude-fable-5": ModelPrice(Decimal("10.00"), Decimal("50.00")),
    "claude-haiku-4-5-20251001": ModelPrice(Decimal("1.00"), Decimal("5.00")),
    # All locally observed sonnet-5 traces are in the introductory era (July);
    # the base entry is intro. Time-aware callers should use price_for().
    "claude-sonnet-5": SONNET5_INTRO,
}

KNOWN_RELEASED_AT: dict[str, datetime] = {
    # Verified against anthropic.com/news on 2026-07-02 (opus-4-8, fable-5);
    # haiku date embedded in the model id. opus-4-7: unverified, resolved to
    # first-seen by the ingest (see module docstring). sonnet-5 announced
    # 2026-06-30 ("Introducing Claude Sonnet 5"). opus-5 announced 2026-07-24
    # (verified 2026-08-08), one day before its first local trace.
    "claude-opus-5": datetime(2026, 7, 24, tzinfo=timezone.utc),
    "claude-opus-4-8": datetime(2026, 5, 28, tzinfo=timezone.utc),
    "claude-fable-5": datetime(2026, 6, 9, tzinfo=timezone.utc),
    "claude-haiku-4-5-20251001": datetime(2025, 10, 1, tzinfo=timezone.utc),
    "claude-sonnet-5": datetime(2026, 6, 30, tzinfo=timezone.utc),
}


def price_for(model_id: str | None, invoked_at: datetime | None = None) -> ModelPrice | None:
    """Price sheet for ``model_id`` at ``invoked_at`` (handles Sonnet 5 eras)."""
    if model_id == "claude-sonnet-5" and invoked_at is not None:
        return SONNET5_STANDARD if invoked_at >= SONNET5_STANDARD_FROM else SONNET5_INTRO
    return PRICES.get(model_id) if model_id is not None else None


def cache_creation_split(usage: Mapping[str, Any]) -> tuple[int, int]:
    """Split cache-creation tokens into (5-minute, 1-hour) TTL buckets.

    TWO UPSTREAM SHAPES, both real, both must be read:

    * nested — ``{"cache_creation": {"ephemeral_5m_input_tokens": N,
      "ephemeral_1h_input_tokens": M}}``. The Messages API response shape.
    * flat — ``{"cache_creation_5m": N, "cache_creation_1h": M}``. What the
      local Claude Code JSONL writes, and what 58,194 of the 58,210 rows in the
      local store actually carry. Zero rows carry the nested form.

    Reading only the nested form is what made ``cache_write_1h_mult`` a dead
    constant: the "no split available" fallback fired on every single record,
    every 1-hour cache write was billed at the 5-minute 1.25x rate, and the
    whole store came out $1,393.49 low. Support both, and prefer nested when a
    record somehow carries both, since that is the API's own wording.

    RECONCILIATION. ``cache_creation_input_tokens`` is the top-level billable
    count; the split describes its composition. On 24 of 58,194 local rows they
    disagree, in BOTH directions (a split under-reporting the total by 69,714
    tokens, and one over-reporting it by 6,977). Taking the split at face value
    silently drops 274,513 tokens from the bill; taking the total alone throws
    away the 1-hour premium. So the total wins on quantity and the split wins on
    composition: 1h is capped at the total, and whatever remains is 5m. No token
    is ever dropped or invented, and the 58,170 consistent rows are unaffected.

    One edge of that rule is a deliberate choice, not a fallout: when a split
    reports MORE 1-hour tokens than the total allows, ``h1`` clamps to the whole
    total and ``m5`` becomes 0 — i.e. an over-reported split is resolved toward
    the more expensive 2x rate. Total spend is still bounded by the total, so
    this can never over-bill in aggregate; it only decides which way to read a
    contradiction, and it reads it conservatively.

    The total is only distrusted when it is absent entirely, in which case the
    split is all there is.
    """
    total = int(usage.get("cache_creation_input_tokens") or 0)

    nested = usage.get("cache_creation") or {}
    split_5m = nested.get("ephemeral_5m_input_tokens")
    split_1h = nested.get("ephemeral_1h_input_tokens")
    if split_5m is None and split_1h is None:
        split_5m = usage.get("cache_creation_5m")
        split_1h = usage.get("cache_creation_1h")

    if split_5m is None and split_1h is None:
        return total, 0  # no split reported: all of it is 5-minute TTL

    split_5m = int(split_5m or 0)
    split_1h = int(split_1h or 0)
    if total <= 0:
        return split_5m, split_1h
    one_hour = min(split_1h, total)
    return total - one_hour, one_hour


def compute_cost_usd(
    model_id: str | None,
    usage: Mapping[str, Any] | None,
    invoked_at: datetime | None = None,
) -> Decimal | None:
    """List-price cost of one API message from its ``usage`` block.

    Returns None when the model has no price entry, usage is missing, or the
    record's speed tier has no published price — never guesses. The 5m/1h
    cache-creation split comes from :func:`cache_creation_split`, which reads
    both the nested and the flat upstream shapes; read its docstring before
    changing anything about cache accounting. ``invoked_at`` selects the price
    era for time-versioned models (Sonnet 5 intro vs standard).
    """
    if model_id is None or usage is None:
        return None
    price = price_for(model_id, invoked_at)
    if price is None:
        return None

    speed = usage.get("speed")
    if speed in (None, "standard"):
        tier_mult = Decimal(1)
    elif speed == "fast":
        if price.fast_multiplier is None:
            return None
        tier_mult = price.fast_multiplier
    else:  # unknown future tier — refuse to guess
        return None

    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_5m, cache_1h = cache_creation_split(usage)

    cost = (
        (
            input_tokens * price.input_per_mtok
            + cache_read * price.input_per_mtok * price.cache_read_mult
            + cache_5m * price.input_per_mtok * price.cache_write_5m_mult
            + cache_1h * price.input_per_mtok * price.cache_write_1h_mult
            + output_tokens * price.output_per_mtok
        )
        * tier_mult
        / _MTOK
    )
    return cost.quantize(_COST_QUANTUM)

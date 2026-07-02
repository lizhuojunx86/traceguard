"""List-price table for models observed in local Claude Code session data.

Contract-external data. SPEC §3.1 defines ``traces.cost_usd`` as *list price
at write time*; local Claude Code JSONL carries no cost field anywhere (no
``costUSD`` — verified against all local session files, CC 2.1.150–2.1.198),
so this table is the fallback used to compute it from ``message.usage``.

Price sources
    Anthropic list prices as cached in the local ``claude-api`` skill
    reference ("Current Models", cached 2026-06-24):
    fable-5 $10/$50, opus-4-8 $5/$25, opus-4-7 $5/$25, haiku-4.5 $1/$5 per
    MTok (input/output). Cache multipliers per the same reference: cache
    read ≈ 0.1× input price; cache write 1.25× (5-minute TTL) / 2.0×
    (1-hour TTL) input price.
    TODO(lizhuojun): 核对上面四个模型的 list price 与官方 pricing 页是否一致;
    token 分量都存在 traces.output_parsed.usage 里，价格改了可以重算。

Release dates (``KNOWN_RELEASED_AT``)
    Only ``claude-haiku-4-5-20251001`` is filled (date embedded in the model
    id). For the rest the ingest falls back to *first-seen timestamp in the
    local data* for both ``released_at`` and ``available_to_us_at`` — that
    satisfies the SPEC §3.2 ``released_at <= available_to_us_at`` constraint
    but overstates ``released_at``.
    TODO(lizhuojun): 核对 opus-4-7 / opus-4-8 / fable-5 的公开发布日期后补进
    KNOWN_RELEASED_AT（model_registry 是 insert-only，改值需要新 DB 重跑）。

Note on speed tiers: fast mode ("speed": "fast") bills at a premium not
captured here; all locally observed records are "standard".
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
    """USD per MTok list price plus cache multipliers on the input price."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_mult: Decimal = Decimal("0.1")
    cache_write_5m_mult: Decimal = Decimal("1.25")
    cache_write_1h_mult: Decimal = Decimal("2.0")


PRICES: dict[str, ModelPrice] = {
    "claude-opus-4-8": ModelPrice(Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-7": ModelPrice(Decimal("5.00"), Decimal("25.00")),
    "claude-fable-5": ModelPrice(Decimal("10.00"), Decimal("50.00")),
    "claude-haiku-4-5-20251001": ModelPrice(Decimal("1.00"), Decimal("5.00")),
}

KNOWN_RELEASED_AT: dict[str, datetime] = {
    # Date embedded in the model id; the other observed models are resolved
    # to first-seen-in-local-data by the ingest (see module docstring TODO).
    "claude-haiku-4-5-20251001": datetime(2025, 10, 1, tzinfo=timezone.utc),
}


def compute_cost_usd(model_id: str | None, usage: Mapping[str, Any] | None) -> Decimal | None:
    """List-price cost of one API message from its ``usage`` block.

    Returns None when the model has no price entry or usage is missing —
    never guesses. Uses the ``cache_creation`` 5m/1h split when present,
    otherwise treats all cache-creation tokens as 5-minute TTL (1.25×).
    """
    if model_id is None or usage is None:
        return None
    price = PRICES.get(model_id)
    if price is None:
        return None

    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_creation_total = int(usage.get("cache_creation_input_tokens") or 0)

    nested = usage.get("cache_creation") or {}
    cache_5m = nested.get("ephemeral_5m_input_tokens")
    cache_1h = nested.get("ephemeral_1h_input_tokens")
    if cache_5m is None and cache_1h is None:
        cache_5m, cache_1h = cache_creation_total, 0
    else:
        cache_5m = int(cache_5m or 0)
        cache_1h = int(cache_1h or 0)

    cost = (
        input_tokens * price.input_per_mtok
        + cache_read * price.input_per_mtok * price.cache_read_mult
        + cache_5m * price.input_per_mtok * price.cache_write_5m_mult
        + cache_1h * price.input_per_mtok * price.cache_write_1h_mult
        + output_tokens * price.output_per_mtok
    ) / _MTOK
    return cost.quantize(_COST_QUANTUM)

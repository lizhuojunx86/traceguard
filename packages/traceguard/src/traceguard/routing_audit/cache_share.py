"""Shareable summary of a cache audit — the file you can hand to someone else.

This exists because comparing cache behaviour across organisations needs a
corpus, and a corpus needs submissions from people who have read what they are
about to send. So the design problem here is TRUST, not serialisation: every
choice below is made so that the person exporting can open the file, read all
of it in one screen-scroll, and satisfy themselves that nothing of theirs is in
it. :func:`render_share` is dumped verbatim by ``--show-share`` for exactly that
reason — a preview that summarised would defeat its own purpose.

WHAT NEVER LEAVES. No prompt or answer text, no file paths, no session ids, no
per-trace timestamps, no free-form strings of any kind. The rule is stated as an
invariant rather than as a list of things that were remembered:

    every string in the payload is one of
      (a) a constant defined in this module or in ``cache_audit``,
      (b) a model id on the published-price whitelist,
      (c) an ISO-8601 form of one of the two declared window bounds,
      (d) the installed distribution version,
      (e) a decimal money literal (see below), which is a number wearing a
          string's clothes rather than text.

Nothing else is a string. Everything else is a number, a bool, or null. A new
field that carries anything from the DB therefore breaks the invariant rather
than quietly widening the export, and ``test_emit_share_leaks_no_sentinel_...``
in ``tests/test_cache_share.py`` is the check that says so out loud.

MODEL IDS ARE WHITELISTED, NOT PASSED THROUGH. An arbitrary ``model_id`` is a
free-form string that reached the store from somewhere — an internal gateway
name, a fine-tune deployment, a vendor alias — and any of those can name an
employer. Ids present in :data:`pricing.PRICES` or
:data:`cache_audit.MIN_CACHEABLE_TOKENS` are public model names and go out as
themselves; every other non-null id is folded into a single
:data:`UNRECOGNIZED` row that keeps the counts and drops the name. NULL stays
:data:`NO_MODEL`, the same label section 1 uses.

THE WINDOW MUST BE CLOSED. An export with an open bound is refused, and that is
the one hard error in this module. A corpus whose members each measured "all
time" over their own history is not a corpus — the expired-gap rate, the
switch rate and every dollar figure scale with how long you looked, so rows from
different windows cannot be put in the same table at all. Refusing at export is
the only place that costs nobody anything; refusing at collection time means a
person already spent the effort.

MONEY IS A STRING WITH TWO DECIMALS. These files are meant to be diffed and
re-read for a long time; a JSON float would put ``2044.7199999999998`` in one of
them eventually. Token counts are integers and rates are floats rounded to six
places.

THE BAND IS THE ANSWER, THE ARGMAX IS A FOOTNOTE. Section 3b's own conclusion is
that a single best cap leads its runner-up by less than the size of corrections
this report has already had to make once. The schema encodes that judgement in
its field names rather than leaving it to a note somebody will not read:
``recommended_cap_band`` is the citable field and ``argmax_reference_only`` is
named so that quoting it reads wrong.

This module writes one file and makes no network calls. Nothing here uploads
anything, and nothing here should ever learn how to.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version as _dist_version
from typing import Any, Sequence

from traceguard.routing_audit.cache_audit import (
    CAP_SWEEP_MAX,
    CAP_SWEEP_MIN,
    CAP_SWEEP_STEP,
    GAP_BUCKETS,
    MIN_CACHEABLE_TOKENS,
    PING_INTERVAL,
    CacheAudit,
    CapPoint,
    CapSweep,
    ExpiredGap,
    ModelRow,
    _EXPIRED_BUCKETS,
    _tri_verdict,
)
from traceguard.routing_audit.pricing import PRICES

# Bumped only for a BREAKING change. Fields may be added to a version; an
# existing field never changes meaning, type or name, because the whole point of
# the corpus is that a row written today still parses next year. A reader that
# does not know a field ignores it.
SCHEMA_VERSION = 1

DISTRIBUTION = "traceguard"

# Labels for the two model rows that are not a model. Constants rather than
# literals so the invariant above can be checked by identity.
NO_MODEL = "(none)"
UNRECOGNIZED = "(unrecognized)"

# Model ids that may go out under their own name: the ones this repo already
# publishes a price or a minimum-cacheable-prefix for, i.e. public product
# names. Anything else is a string of unknown provenance.
PUBLIC_MODEL_IDS: frozenset[str] = frozenset(PRICES) | frozenset(MIN_CACHEABLE_TOKENS)

# Number of gap-length quantile groups in the switch-by-length cut. Ten when
# there are at least ten expired gaps; fewer gaps yield fewer groups rather
# than empty ones.
DECILES = 10

_MONEY = Decimal("0.01")
_RATE_PLACES = 6


class ShareWindowError(ValueError):
    """Raised when an export is asked for over a window that is not closed."""


# ── scalar renderers ────────────────────────────────────────────────────────


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(_MONEY))


def _rate(value: float | None) -> float | None:
    return None if value is None else round(value, _RATE_PLACES)


def _minutes(span: timedelta | None) -> int | None:
    return None if span is None else int(span.total_seconds() // 60)


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


# ── model rows ──────────────────────────────────────────────────────────────


def _model_key(model_id: str) -> str:
    """Public name, ``(none)``, or the single fold-in row. Never a raw id."""
    if model_id == NO_MODEL:
        return NO_MODEL
    return model_id if model_id in PUBLIC_MODEL_IDS else UNRECOGNIZED


def _merge(rows: Sequence[ModelRow]) -> dict[str, Any]:
    messages = sum(r.messages for r in rows)
    prompt = sum(r.prompt_tokens for r in rows)
    read = sum(r.cache_read for r in rows)
    priced = sum(r.priced_messages for r in rows)
    actual = sum((r.actual_usd for r in rows if r.priced_messages), Decimal("0"))
    counter = sum((r.counterfactual_usd for r in rows if r.priced_messages), Decimal("0"))
    return {
        "messages": messages,
        "prompt_tokens": prompt,
        "input_tokens": sum(r.input_tokens for r in rows),
        "cache_read_tokens": read,
        "cache_write_5m_tokens": sum(r.cache_5m for r in rows),
        "cache_write_1h_tokens": sum(r.cache_1h for r in rows),
        "hit_rate": _rate(_ratio(read, prompt)),
        "priced_messages": priced,
        "unpriced_messages": messages - priced,
        "input_usd": _money(actual) if priced else None,
        "no_cache_usd": _money(counter) if priced else None,
    }


def _models(rows: Sequence[ModelRow]) -> list[dict[str, Any]]:
    """Per-model aggregates, biggest prompt volume first, names whitelisted.

    The fold-in row carries ``distinct_model_ids`` so a reader can tell one
    unrecognised model from twelve without being told which they were.
    """
    grouped: dict[str, list[ModelRow]] = {}
    for row in rows:
        grouped.setdefault(_model_key(row.model_id), []).append(row)
    out = []
    for key, group in grouped.items():
        entry = {"model_id": key, **_merge(group)}
        if key == UNRECOGNIZED:
            entry["distinct_model_ids"] = len(group)
        out.append(entry)
    out.sort(key=lambda e: e["prompt_tokens"], reverse=True)
    return out


# ── gap buckets ─────────────────────────────────────────────────────────────


def _buckets(audit_result: CacheAudit) -> list[dict[str, Any]]:
    """One row per gap bucket: how many, what it cost, and what came back.

    Buckets inside the 1h TTL expire nothing, so their money and switch fields
    are ``null`` — the JSON equivalent of the report's ``no expiry``, and
    distinct from a figure that is missing because no price existed.
    """
    gaps = audit_result.gaps
    out = []
    for name in GAP_BUCKETS:
        count = gaps.buckets[name]
        costs = gaps.bucket_costs[name]
        expires = name in _EXPIRED_BUCKETS
        row: dict[str, Any] = {
            "bucket": name,
            "count": count,
            "share": _rate(_ratio(count, gaps.gaps)),
            "expires": expires,
        }
        if expires:
            row.update(
                {
                    "rewrite_usd_lower": _money(costs.rewrite_usd_lower),
                    "rewrite_usd_upper": _money(costs.rewrite_usd),
                    "ping_usd": _money(costs.ping_usd),
                    "rewrite_unpriced_messages": costs.rewrite_unpriced,
                    "verdict": _tri_verdict(
                        costs.ping_usd, costs.rewrite_usd_lower, costs.rewrite_usd
                    )
                    if count
                    else None,
                    "switched": costs.switched,
                    "same_model": costs.same_model,
                    "undecidable": costs.switch_undecidable,
                    "switch_rate": _rate(costs.switch_rate),
                }
            )
        else:
            row.update(
                {
                    "rewrite_usd_lower": None,
                    "rewrite_usd_upper": None,
                    "ping_usd": None,
                    "rewrite_unpriced_messages": None,
                    "verdict": None,
                    "switched": None,
                    "same_model": None,
                    "undecidable": None,
                    "switch_rate": None,
                }
            )
        out.append(row)
    return out


# ── cross-model switching by gap length ─────────────────────────────────────


def _switch_deciles(expired: Sequence[ExpiredGap]) -> list[dict[str, Any]]:
    """Cross-model switch rate cut by gap-length quantile.

    The most distinctive axis in this corpus, and the one a single overall rate
    destroys: on this repo's own store the rate roughly quadruples between the
    ``1-4h`` and ``>4h`` buckets, which is the whole reason a keep-alive policy
    has a finite optimal cap at all. Buckets are coarse and fixed; deciles let
    two submissions with different session rhythms be compared on the same
    ten-point curve.

    Each group carries its own length bounds in minutes. A decile index without
    them is uninterpretable across organisations — "the 3rd decile" is 90
    minutes for one submitter and 9 hours for another.
    """
    ordered = sorted(expired, key=lambda eg: eg.gap)
    n = len(ordered)
    if not n:
        return []
    groups = min(DECILES, n)
    out = []
    for i in range(groups):
        lo, hi = i * n // groups, (i + 1) * n // groups
        chunk = ordered[lo:hi]
        if not chunk:
            continue
        switched = sum(1 for eg in chunk if eg.switched is True)
        same = sum(1 for eg in chunk if eg.switched is False)
        undecidable = sum(1 for eg in chunk if eg.switched is None)
        out.append(
            {
                "decile": i + 1,
                "gaps": len(chunk),
                "gap_minutes_min": _minutes(chunk[0].gap),
                "gap_minutes_max": _minutes(chunk[-1].gap),
                "switched": switched,
                "same_model": same,
                "undecidable": undecidable,
                "switch_rate": _rate(_ratio(switched, switched + same)),
            }
        )
    return out


# ── keep-alive cap ──────────────────────────────────────────────────────────


def _band(sweep: CapSweep, span: tuple[timedelta, timedelta] | None) -> dict[str, Any] | None:
    if span is None:
        return None
    spread = sweep.spread(span)
    return {
        "low_minutes": _minutes(span[0]),
        "high_minutes": _minutes(span[1]),
        "width_minutes": _minutes(span[1] - span[0]),
        "grid_points": sweep.band_points(span),
        "net_usd_min": _money(spread[0]) if spread else None,
        "net_usd_max": _money(spread[1]) if spread else None,
        # A run that stops because it hit the edge of the grid is not a run that
        # was measured to stop there.
        "censored_low": span[0] <= CAP_SWEEP_MIN,
        "censored_high": span[1] >= CAP_SWEEP_MAX,
    }


def _argmax(point: CapPoint) -> dict[str, Any]:
    """The single best cap, under a name that discourages quoting it.

    ``net_usd`` carries both ends of the undecidable-gap assumption because
    neither is the truth: ``measured`` deducts only gaps PROVEN to have changed
    model, ``pessimistic`` treats every undecidable gap as one too.
    ``rewrite_lower_bound`` is the same measured run costed against the
    pessimistic end of the rewrite bracket.
    """
    return {
        "cap_minutes": _minutes(point.cap),
        "uncapped": point.cap is None,
        "bridged": point.bridged,
        "abandoned": point.abandoned,
        "pings": point.pings,
        "ping_usd": _money(point.ping_usd),
        "wasted_pings": point.wasted_pings,
        "wasted_usd": _money(point.wasted_usd),
        "rewrite_avoided_usd_lower": _money(point.saved_lower),
        "rewrite_avoided_usd_upper": _money(point.saved_upper),
        "net_usd": {
            "measured": _money(point.net_upper),
            "pessimistic": _money(point.net_upper_pessimistic),
            "rewrite_lower_bound": _money(point.net_lower),
        },
        "verdict": point.verdict,
    }


def _keep_alive(audit_result: CacheAudit) -> dict[str, Any]:
    sweep = audit_result.gaps.sweep
    if sweep is None:  # pragma: no cover — session_gaps always attaches one
        return {}
    return {
        "cadence_minutes": _minutes(PING_INTERVAL),
        "grid": {
            "min_minutes": _minutes(CAP_SWEEP_MIN),
            "max_minutes": _minutes(CAP_SWEEP_MAX),
            "step_minutes": _minutes(CAP_SWEEP_STEP),
            "tolerance": sweep.tolerance,
        },
        # The citable one: where net stays within `tolerance` of the maximum.
        "recommended_cap_band": _band(sweep, sweep.peak_band),
        # A weaker, wider claim: where net merely stays positive. Says capping is
        # the right shape of policy, NOT that the caps in it are interchangeable.
        "sign_stable_band": _band(sweep, sweep.plateau),
        "sign_stable_band_lower_bound": _band(sweep, sweep.robust_plateau),
        "argmax_reference_only": _argmax(sweep.best),
        "argmax_sits_on_ping_step": sweep.argmax_on_ping_step,
        "uncapped_policy": _argmax(sweep.unbounded) if sweep.unbounded else None,
    }


# ── payload ─────────────────────────────────────────────────────────────────


def tool_version() -> str:
    """Version of the INSTALLED distribution, never a literal in this repo.

    Read from package metadata on purpose. A hand-copied version string is a
    number that goes stale silently, and this repo has already had to fix one of
    those (a README test count). In a file whose whole job is to be compared
    against other files months later, the same mistake is worse than stale: a
    submission labelled 1.3.0 but produced by some other checkout would corrupt
    the corpus in a way nobody could detect afterwards.
    """
    try:
        return _dist_version(DISTRIBUTION)
    except PackageNotFoundError:  # pragma: no cover — running from a non-install
        return "unknown"


def build_share(audit_result: CacheAudit) -> dict[str, Any]:
    """Build the shareable payload, or refuse if the window is not closed.

    :raises ShareWindowError: when either bound is missing.
    """
    since, until = audit_result.since, audit_result.until
    if since is None or until is None:
        raise ShareWindowError(
            "cannot export a share file over an open window: "
            f"--since is {'set' if since else 'MISSING'} and "
            f"--until is {'set' if until else 'MISSING'}. Every rate and every "
            "dollar figure in this file scales with how long you looked, so "
            "submissions measured over different windows cannot go in the same "
            "table — an 'all time' row is not comparable with anything. Pass "
            "--benchmark for the frozen window, or give both --since and --until."
        )

    gaps = audit_result.gaps
    expired = gaps.expired_details
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": tool_version(),
        "window": {
            "since": _iso(since),
            "until": _iso(until),
            "days": round((until - since).total_seconds() / 86400, 2),
        },
        "corpus": {
            "traces_in_window": audit_result.total_traces,
            "analyzed_messages": audit_result.cc_records,
            "sessions": gaps.sessions,
            "gaps": gaps.gaps,
            "expired_gaps": gaps.expired_gaps,
        },
        # Deliberately high in the file. A submission where most gaps cannot be
        # classified is a weak submission, and it should be obvious before
        # anyone reads a dollar figure out of it.
        "data_quality": {
            "undecidable_gaps": gaps.switch_undecidable,
            "undecidable_share": _rate(
                _ratio(gaps.switch_undecidable, gaps.expired_gaps)
            ),
            "unpriced_post_gap_messages": gaps.rewrite_unpriced,
            "unpriced_pre_gap_messages": gaps.ping_unpriced,
            "unpriced_messages": sum(m.unpriced_messages for m in audit_result.models),
        },
        "models": _models(audit_result.models),
        "gap_buckets": _buckets(audit_result),
        "cross_model": {
            "switched_gaps": gaps.switched_gaps,
            "same_model_gaps": gaps.same_model_gaps,
            "undecidable_gaps": gaps.switch_undecidable,
            "switch_rate_of_decidable": _rate(gaps.switch_rate),
            "by_gap_length_decile": _switch_deciles(expired),
        },
        "keep_alive": _keep_alive(audit_result),
    }


def _iso(value: datetime) -> str:
    return value.isoformat()


def render_share(payload: dict[str, Any]) -> str:
    """The exact bytes an export writes. ``--show-share`` prints this and stops."""
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)

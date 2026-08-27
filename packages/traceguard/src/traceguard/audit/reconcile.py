"""Out-of-band capture reconciliation (audit v2, layer L1).

The hash chain guarantees storage integrity: what the SDK saw was not changed
afterwards. It says nothing about capture fidelity: whether what the SDK saw
is what actually happened. Wrapper self-reports are produced inside the
process the agent controls, so a compromised or misbehaving agent can fake
calls, drop calls, or replay old ones, and the chain will faithfully attest
the lie (METR 2026-08-26: roughly 7% of the evaluated transcripts had spoofed
tool calls).

This module is the cheapest cross-check available: compare the traces table's
self-reported token volume, per model and time window, with what the model
provider says it served over the same window (an out-of-band source the agent
does not write to). What it proves and does not prove:

- L0 (chain): the stored self-reports were not altered after the fact.
- **L1 (this module)**: the self-reported totals agree with the provider's
  totals within a tolerance, per model. A disagreement is a ``capture_mismatch``
  (WARN) whose direction matters and is spelled out in the finding:
  traces > provider = self-reports that were never served (spoofed / replayed);
  traces < provider = traffic the SDK never saw (uninstrumented, dropped
  fail-open, or an org-wide report wider than this DB).
- L2 (not built): per-call authenticity. Needs provider-signed request logs;
  outside what an SDK can provide.

Provider sources: ``fetch_anthropic_usage`` pulls the Usage Admin API
(``GET /v1/organizations/usage_report/messages``, Admin API key — it is
org-wide, so narrow it with ``api_key_ids`` / ``workspace_ids`` to the keys
this DB's traffic uses); ``load_usage_report`` reads the same JSON from a
file (a ``curl`` dump), which is also the deterministic test path. Any other
source can be adapted by producing :class:`UsageBucket` values.

Conventions that MUST match or every comparison is a false positive:

- ``tokens_in`` is full prompt volume: ``uncached_input_tokens`` +
  ``cache_read_input_tokens`` + ``cache_creation`` (5m + 1h). This is exactly
  what ``wrap_anthropic`` records (see its module docstring).
- Windows are compared on ``invoked_at`` (physical time) against provider
  buckets snapped to UTC ``bucket_width`` boundaries — use
  :func:`align_window` before fetching or you manufacture edge mismatches.
- The Usage API reports tokens only, not request counts. Call counts from
  the traces side are shown for context and never compared.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib import parse as _urlparse
from urllib import request as _urlrequest

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard.audit.verify import WARN, ChainFinding
from traceguard.store.models import Trace

CAPTURE_MISMATCH = "capture_mismatch"

ANTHROPIC_USAGE_PATH = "/v1/organizations/usage_report/messages"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ADMIN_KEY_ENV = "ANTHROPIC_ADMIN_KEY"

_BUCKET_WIDTHS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
}
# Usage API page limits per bucket width (documented maxima).
_PAGE_LIMIT: dict[str, int] = {"1m": 1440, "1h": 168, "1d": 31}

Opener = Callable[..., Any]


@dataclass(frozen=True)
class UsageBucket:
    """Provider-side usage for one model in one time bucket.

    ``model`` is ``None`` when the source did not group by model (then only
    the total comparison is meaningful).
    """

    starting_at: datetime
    ending_at: datetime
    model: str | None
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True)
class SideTotals:
    calls: int
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True)
class ModelComparison:
    model: str | None
    traces: SideTotals
    provider: SideTotals

    def delta(self, metric: str) -> int:
        return getattr(self.traces, metric) - getattr(self.provider, metric)


@dataclass
class ReconcileResult:
    ok: bool
    starting_at: datetime
    ending_at: datetime
    tolerance: float
    comparisons: dict[str | None, ModelComparison] = field(default_factory=dict)
    total: ModelComparison | None = None
    findings: list[ChainFinding] = field(default_factory=list)
    buckets_outside_window: int = 0

    def summary(self) -> str:
        status = "OK" if self.ok else "CAPTURE MISMATCH"
        t = self.total
        return (
            f"reconcile {status}: window {self.starting_at.isoformat()} → "
            f"{self.ending_at.isoformat()}, {len(self.comparisons)} model(s), "
            f"tolerance {self.tolerance:.1%}; tokens_in traces={t.traces.tokens_in if t else 0} "
            f"provider={t.provider.tokens_in if t else 0}, tokens_out traces="
            f"{t.traces.tokens_out if t else 0} provider={t.provider.tokens_out if t else 0}; "
            f"{len(self.findings)} finding(s)"
        )


def _parse_rfc3339(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError(f"timestamp must carry a timezone: {text!r}")
    return value.astimezone(timezone.utc)


def _floor(ts: datetime, width: timedelta) -> datetime:
    ts = ts.astimezone(timezone.utc)
    if width == timedelta(days=1):
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    if width == timedelta(hours=1):
        return ts.replace(minute=0, second=0, microsecond=0)
    return ts.replace(second=0, microsecond=0)


def align_window(
    starting_at: datetime, ending_at: datetime, bucket_width: str = "1d"
) -> tuple[datetime, datetime]:
    """Snap ``[starting_at, ending_at)`` outward to UTC ``bucket_width`` edges.

    The Usage API snaps every bucket to the start of its minute/hour/day in
    UTC; comparing against traces over an unaligned window would count
    partial-bucket traffic on one side only.
    """
    if bucket_width not in _BUCKET_WIDTHS:
        raise ValueError(f"bucket_width must be one of {sorted(_BUCKET_WIDTHS)}")
    if starting_at.tzinfo is None or ending_at.tzinfo is None:
        raise ValueError("window timestamps must be tz-aware")
    width = _BUCKET_WIDTHS[bucket_width]
    start = _floor(starting_at, width)
    end = _floor(ending_at, width)
    if end < ending_at.astimezone(timezone.utc):
        end += width
    if end <= start:
        raise ValueError("window is empty after alignment")
    return start, end


def _result_tokens_in(result: Mapping[str, Any]) -> int:
    creation = result.get("cache_creation") or {}
    return (
        int(result.get("uncached_input_tokens") or 0)
        + int(result.get("cache_read_input_tokens") or 0)
        + int(creation.get("ephemeral_5m_input_tokens") or 0)
        + int(creation.get("ephemeral_1h_input_tokens") or 0)
    )


def usage_from_report(pages: Iterable[Mapping[str, Any]]) -> list[UsageBucket]:
    """Flatten Usage API page(s) into :class:`UsageBucket` values.

    One bucket per (time bucket, result row); when the report was grouped by
    more than ``model`` (say, also by ``api_key_id``) several rows share a
    model and are simply summed later. Empty buckets contribute nothing.
    """
    out: list[UsageBucket] = []
    for page in pages:
        for bucket in page.get("data") or []:
            start = _parse_rfc3339(bucket["starting_at"])
            end = _parse_rfc3339(bucket["ending_at"])
            for row in bucket.get("results") or []:
                out.append(
                    UsageBucket(
                        starting_at=start,
                        ending_at=end,
                        model=row.get("model"),
                        tokens_in=_result_tokens_in(row),
                        tokens_out=int(row.get("output_tokens") or 0),
                    )
                )
    return out


def load_usage_report(path: str | os.PathLike[str]) -> list[UsageBucket]:
    """Read a Usage API response saved to disk.

    Accepts one page object, a JSON list of page objects, or JSON lines (one
    page per line) — whatever a paginating ``curl`` loop produced.
    """
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        loaded = [json.loads(line) for line in text.splitlines() if line.strip()]
    pages = loaded if isinstance(loaded, list) else [loaded]
    return usage_from_report(pages)


def fetch_anthropic_usage(
    starting_at: datetime,
    ending_at: datetime,
    *,
    admin_key: str,
    bucket_width: str = "1d",
    models: Iterable[str] | None = None,
    api_key_ids: Iterable[str] | None = None,
    workspace_ids: Iterable[str] | None = None,
    base_url: str = DEFAULT_ANTHROPIC_BASE_URL,
    timeout: float = 30.0,
    opener: Opener | None = None,
    user_agent: str = "traceguard-audit",
) -> list[UsageBucket]:
    """Pull the Usage Admin API for ``[starting_at, ending_at)``, grouped by model.

    Requires an **Admin** API key (``sk-ant-admin…``); a regular key is
    rejected by the endpoint. The report is organization-wide — pass the
    ``api_key_ids`` / ``workspace_ids`` this DB's traffic actually uses, or the
    provider side will legitimately exceed the traces side. Follows
    ``has_more`` / ``next_page`` until exhausted. Raw ``urllib`` on purpose:
    the endpoint is not in the SDK, and this package adds no dependencies.
    """
    if bucket_width not in _BUCKET_WIDTHS:
        raise ValueError(f"bucket_width must be one of {sorted(_BUCKET_WIDTHS)}")
    if not admin_key:
        raise ValueError("admin_key is required (Admin API key, sk-ant-admin…)")
    start, end = align_window(starting_at, ending_at, bucket_width)
    open_ = opener or _urlrequest.urlopen

    query: list[tuple[str, str]] = [
        ("starting_at", start.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("ending_at", end.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("bucket_width", bucket_width),
        ("group_by[]", "model"),
        ("limit", str(_PAGE_LIMIT[bucket_width])),
    ]
    query += [("models[]", m) for m in (models or [])]
    query += [("api_key_ids[]", k) for k in (api_key_ids or [])]
    query += [("workspace_ids[]", w) for w in (workspace_ids or [])]

    headers = {
        "x-api-key": admin_key,
        "anthropic-version": "2023-06-01",
        "Accept": "application/json",
        "User-Agent": user_agent,
    }
    pages: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params = list(query)
        if page_token:
            params.append(("page", page_token))
        url = f"{base_url.rstrip('/')}{ANTHROPIC_USAGE_PATH}?{_urlparse.urlencode(params)}"
        req = _urlrequest.Request(url, headers=headers, method="GET")
        with open_(req, timeout=timeout) as resp:
            body = resp.read()
        page = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        pages.append(page)
        if not page.get("has_more"):
            break
        page_token = page.get("next_page")
        if not page_token:
            break
    return usage_from_report(pages)


def traces_usage(
    engine: Engine,
    starting_at: datetime,
    ending_at: datetime,
    *,
    project: str | None = None,
    operation: str | None = "llm_complete",
) -> dict[str | None, SideTotals]:
    """Self-reported totals per ``model_id`` over ``invoked_at`` in the window."""
    stmt = (
        select(
            Trace.model_id,
            func.count(Trace.trace_id),
            func.coalesce(func.sum(Trace.tokens_in), 0),
            func.coalesce(func.sum(Trace.tokens_out), 0),
        )
        .where(Trace.invoked_at >= starting_at)
        .where(Trace.invoked_at < ending_at)
        .group_by(Trace.model_id)
    )
    if project is not None:
        stmt = stmt.where(Trace.project == project)
    if operation is not None:
        stmt = stmt.where(Trace.operation == operation)
    out: dict[str | None, SideTotals] = {}
    with Session(engine) as sess:
        for model_id, calls, tokens_in, tokens_out in sess.execute(stmt):
            out[model_id] = SideTotals(int(calls), int(tokens_in), int(tokens_out))
    return out


def _sum_provider(
    buckets: Iterable[UsageBucket], starting_at: datetime, ending_at: datetime
) -> tuple[dict[str | None, SideTotals], int]:
    per_model: dict[str | None, list[int]] = {}
    outside = 0
    for b in buckets:
        if b.ending_at <= starting_at or b.starting_at >= ending_at:
            outside += 1
            continue
        acc = per_model.setdefault(b.model, [0, 0])
        acc[0] += b.tokens_in
        acc[1] += b.tokens_out
    return {m: SideTotals(0, v[0], v[1]) for m, v in per_model.items()}, outside


def _exceeds(traces_v: int, provider_v: int, tolerance: float, floor: int) -> bool:
    diff = abs(traces_v - provider_v)
    if diff <= floor:
        return False
    denominator = provider_v if provider_v > 0 else traces_v
    if denominator <= 0:
        return False
    return diff / denominator > tolerance


def _direction(traces_v: int, provider_v: int) -> str:
    if traces_v > provider_v:
        return (
            "traces exceed provider — self-reported volume the provider never "
            "served (spoofed or replayed self-reports), or the provider report is "
            "filtered narrower than this DB"
        )
    return (
        "provider exceeds traces — traffic the SDK never recorded (uninstrumented "
        "calls, traces dropped fail-open, or an org-wide report wider than this "
        "DB; narrow it with api_key_ids / workspace_ids)"
    )


def reconcile(
    engine: Engine,
    *,
    starting_at: datetime,
    ending_at: datetime,
    provider: Iterable[UsageBucket],
    tolerance: float = 0.05,
    absolute_floor: int = 0,
    project: str | None = None,
    operation: str | None = "llm_complete",
    model_map: Mapping[str, str] | None = None,
) -> ReconcileResult:
    """Compare self-reported token totals with provider totals, per model.

    A ``capture_mismatch`` (WARN) is raised for each (model, metric) whose
    relative difference exceeds ``tolerance`` (and ``absolute_floor`` tokens),
    for models present on only one side, and for the grand total. Per-model
    mismatches with a clean total usually mean a naming difference: map trace
    ``model_id`` values onto provider names with ``model_map``.

    Provider buckets entirely outside the window are ignored and counted in
    ``buckets_outside_window`` — pass an aligned window (:func:`align_window`).
    """
    if starting_at.tzinfo is None or ending_at.tzinfo is None:
        raise ValueError("window timestamps must be tz-aware")
    if ending_at <= starting_at:
        raise ValueError("ending_at must be after starting_at")

    raw_traces = traces_usage(engine, starting_at, ending_at, project=project, operation=operation)
    traces_side: dict[str | None, SideTotals] = {}
    for model_id, totals in raw_traces.items():
        key = model_map.get(model_id, model_id) if (model_map and model_id) else model_id
        prev = traces_side.get(key)
        traces_side[key] = (
            SideTotals(
                prev.calls + totals.calls,
                prev.tokens_in + totals.tokens_in,
                prev.tokens_out + totals.tokens_out,
            )
            if prev
            else totals
        )
    provider_side, outside = _sum_provider(provider, starting_at, ending_at)

    result = ReconcileResult(
        ok=True,
        starting_at=starting_at,
        ending_at=ending_at,
        tolerance=tolerance,
        buckets_outside_window=outside,
    )
    empty = SideTotals(0, 0, 0)
    models = sorted(set(traces_side) | set(provider_side), key=lambda m: (m is None, m or ""))
    for model in models:
        t = traces_side.get(model, empty)
        p = provider_side.get(model, empty)
        result.comparisons[model] = ModelComparison(model, t, p)
        label = model if model is not None else "<no model_id>"
        if model not in provider_side and (t.tokens_in or t.tokens_out):
            result.findings.append(
                ChainFinding(
                    CAPTURE_MISMATCH,
                    WARN,
                    None,
                    None,
                    f"model {label}: {t.calls} trace(s), tokens_in={t.tokens_in} "
                    f"tokens_out={t.tokens_out}, but the provider reports NO usage "
                    "for this model in the window — self-reported calls that were "
                    "never served, or a model naming mismatch (see model_map)",
                )
            )
            continue
        if model not in traces_side and (p.tokens_in or p.tokens_out):
            result.findings.append(
                ChainFinding(
                    CAPTURE_MISMATCH,
                    WARN,
                    None,
                    None,
                    f"model {label}: provider reports tokens_in={p.tokens_in} "
                    f"tokens_out={p.tokens_out} but the traces table has NO rows for "
                    "it in the window — traffic the SDK never recorded, or a naming "
                    "mismatch (see model_map)",
                )
            )
            continue
        for metric in ("tokens_in", "tokens_out"):
            tv, pv = getattr(t, metric), getattr(p, metric)
            if _exceeds(tv, pv, tolerance, absolute_floor):
                result.findings.append(
                    ChainFinding(
                        CAPTURE_MISMATCH,
                        WARN,
                        None,
                        None,
                        f"model {label} {metric}: traces={tv} provider={pv} "
                        f"(Δ={tv - pv:+d}, {abs(tv - pv) / max(pv if pv else tv, 1):.1%} > "
                        f"{tolerance:.1%}); {_direction(tv, pv)}",
                    )
                )

    total_t = SideTotals(
        sum(v.calls for v in traces_side.values()),
        sum(v.tokens_in for v in traces_side.values()),
        sum(v.tokens_out for v in traces_side.values()),
    )
    total_p = SideTotals(
        0,
        sum(v.tokens_in for v in provider_side.values()),
        sum(v.tokens_out for v in provider_side.values()),
    )
    result.total = ModelComparison("<total>", total_t, total_p)
    for metric in ("tokens_in", "tokens_out"):
        tv, pv = getattr(total_t, metric), getattr(total_p, metric)
        if _exceeds(tv, pv, tolerance, absolute_floor):
            result.findings.append(
                ChainFinding(
                    CAPTURE_MISMATCH,
                    WARN,
                    None,
                    None,
                    f"total {metric}: traces={tv} provider={pv} (Δ={tv - pv:+d}); "
                    f"{_direction(tv, pv)}",
                )
            )
    result.ok = not result.findings
    return result


def parse_window(text: str) -> tuple[datetime, datetime]:
    """CLI helper: ``"<rfc3339>,<rfc3339>"`` → tz-aware UTC pair."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2 or not all(parts):
        raise ValueError(
            "window must be START,END in RFC 3339, e.g. 2026-08-01T00:00:00Z,2026-08-08T00:00:00Z"
        )
    start, end = _parse_rfc3339(parts[0]), _parse_rfc3339(parts[1])
    if end <= start:
        raise ValueError("window END must be after START")
    return start, end


__all__ = [
    "CAPTURE_MISMATCH",
    "UsageBucket",
    "SideTotals",
    "ModelComparison",
    "ReconcileResult",
    "align_window",
    "usage_from_report",
    "load_usage_report",
    "fetch_anthropic_usage",
    "traces_usage",
    "reconcile",
    "parse_window",
    "ADMIN_KEY_ENV",
]

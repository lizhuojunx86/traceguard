"""Per-run usage drift log — the cross-tool six-field record.

Emits the record shape published by clauderank's usage-drift-log spec
(``m1kapp/claude-rank/docs/usage-drift-log.md``), adopted verbatim per the
convergence agreement in sculptdotfun/viberank#83: one JSON object appended
per run to ``~/.usage-report-history.jsonl``, never rewritten.

The six-field contract::

    {"at": "...+08:00", "month": "YYYY-MM", "cost_usd": 0.0,
     "messages": 0, "corpus": {"files": 0, "bytes": 0}}

- ``at`` keeps the local UTC offset (drift correlates with working hours).
- ``month`` uses the local month boundary.
- ``cost_usd`` is month-to-date, summed from the append-only traces store —
  frozen at first sight, not recomputed from live files.
- ``messages`` counts month-to-date *human-authored* messages, matching
  clauderank's semantics (``cli/scripts/sess.py``): ``type == "user"``
  records, excluding ``isSidechain`` (prompts a parent agent sent) and
  excluding pure tool_result turns.
- ``corpus.files``/``corpus.bytes`` count the transcript tree recursively
  (subagent transcripts live below depth 2; a flat glob undercounts badly).

Drop semantics (reader side, also implemented here as a warning): against
the most recent prior record for the same month, warn when
``prior.cost_usd > current.cost_usd * 1.02``, then read ``corpus`` to
classify — files decreased means data was removed; files same or higher
means live files were rewritten in place (or the accounting changed).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.store.models import Trace, make_engine

DEFAULT_HISTORY_PATH = Path.home() / ".usage-report-history.jsonl"


def _is_human_user_record(rec: dict) -> bool:
    """clauderank's human-utterance test: a user record that is not a
    sidechain prompt and not a pure tool_result turn."""
    if rec.get("type") != "user" or rec.get("isSidechain"):
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        has_text = any(
            isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
            for b in content
        )
        only_tool_result = bool(content) and all(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
        return has_text and not only_tool_result
    return False


def _count_human_messages(source: Path, month: str, month_start: datetime) -> int:
    """Count this month's human-authored messages under ``source``.

    Only files with mtime >= the local month start are parsed: a record
    timestamped inside the month must have been written inside the month, so
    its file's mtime cannot predate the month boundary (rewrites only ever
    bump mtime forward).
    """
    cutoff = month_start.timestamp()
    local_tz = month_start.tzinfo
    count = 0
    for path in sorted(source.rglob("*.jsonl")):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            fh = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"user"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not _is_human_user_record(rec):
                    continue
                ts_raw = rec.get("timestamp")
                if not isinstance(ts_raw, str):
                    continue
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts.astimezone(local_tz).strftime("%Y-%m") == month:
                    count += 1
    return count


def _scan_corpus(source: Path) -> dict:
    files = 0
    total_bytes = 0
    for path in source.rglob("*.jsonl"):
        try:
            total_bytes += path.stat().st_size
        except OSError:
            continue
        files += 1
    return {"files": files, "bytes": total_bytes}


def _month_cost_usd(db_url: str, month_start: datetime) -> float:
    engine = make_engine(db_url, create_all=False)
    start_utc = month_start.astimezone(timezone.utc)
    total = Decimal("0")
    with Session(engine) as session:
        stmt = select(Trace.cost_usd).where(Trace.invoked_at >= start_utc)
        for (cost,) in session.execute(stmt):
            if cost is not None:
                total += Decimal(cost)
    return float(round(total, 2))


def build_record(source: Path | str, db_url: str, now: datetime | None = None) -> dict:
    """Assemble one six-field record for the current local month."""
    source = Path(source)
    local_now = (now or datetime.now(timezone.utc)).astimezone()
    month = local_now.strftime("%Y-%m")
    month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return {
        "at": local_now.isoformat(timespec="seconds"),
        "month": month,
        "cost_usd": _month_cost_usd(db_url, month_start),
        "messages": _count_human_messages(source, month, month_start),
        "corpus": _scan_corpus(source),
    }


def read_prior(history_path: Path | str, month: str) -> dict | None:
    """Most recent prior record for ``month``, or None."""
    prior = None
    try:
        with open(history_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if rec.get("month") == month:
                    prior = rec
    except OSError:
        return None
    return prior


def drift_warning(prior: dict | None, current: dict) -> str | None:
    """The spec's drop check plus its corpus discriminator, as one line."""
    if not prior:
        return None
    try:
        prior_cost = float(prior["cost_usd"])
        prior_files = int(prior["corpus"]["files"])
    except (KeyError, TypeError, ValueError):
        return None
    if prior_cost <= current["cost_usd"] * 1.02:
        return None
    files = current["corpus"]["files"]
    if files < prior_files:
        cause = f"transcripts removed ({prior_files - files} files gone)"
    else:
        cause = "files rewritten in place (or the accounting changed)"
    return (
        f"month-to-date cost_usd fell {prior_cost:.2f} -> "
        f"{current['cost_usd']:.2f} against the prior run; {cause}"
    )


def emit(
    source: Path | str,
    db_url: str,
    history_path: Path | str | None = None,
    now: datetime | None = None,
) -> tuple[dict, str | None]:
    """Build this run's record, append it, return (record, warning-or-None).

    The prior record is read before appending, so the warning always compares
    against the previous run rather than the line just written.
    """
    path = Path(history_path) if history_path else DEFAULT_HISTORY_PATH
    record = build_record(source, db_url, now=now)
    warning = drift_warning(read_prior(path, record["month"]), record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return record, warning

"""Backfill local Claude Code session history into the ``traces`` table.

Contract-external opt-in tool (see package docstring). Reads
``~/.claude/projects`` JSONL transcripts, maps each *distinct assistant API
message* to one ``traces`` row, and records provenance in
``routing_audit_ingest_log`` so re-runs are idempotent and every batch can be
rolled back.

Observed source schema (sampled 2026-07-02 across local files written by
Claude Code 2.1.150–2.1.198; the parser targets exactly these fields):

- Layout: ``<root>/<project-slug>/<sessionId>.jsonl`` for main transcripts;
  subagent transcripts in separate files
  ``<project-slug>/<sessionId>/subagents/**/agent-<agentId>.jsonl`` with a
  sibling ``agent-<agentId>.meta.json`` carrying ``agentType`` (e.g.
  ``Explore``, ``general-purpose``, ``workflow-subagent``). Subagent lines
  repeat the *parent* ``sessionId`` and add ``agentId`` + ``isSidechain``.
- Line ``type`` values include ``assistant``, ``user``, ``system``,
  ``attachment``, ``file-history-snapshot``, ``queue-operation``,
  ``ai-title``, ``last-prompt``, ``pr-link``, ``mode``. Only ``assistant``
  lines are ingested.
- ``assistant`` lines carry: ``timestamp`` (ISO-8601 UTC), ``cwd``,
  ``sessionId``, ``uuid``/``parentUuid``, ``requestId``, ``version``,
  ``gitBranch``, ``entrypoint``, and ``message`` with ``id`` (API message
  id), ``model``, ``stop_reason`` and ``usage`` (``input_tokens``,
  ``output_tokens``, ``cache_read_input_tokens``,
  ``cache_creation_input_tokens``, nested ``cache_creation``
  {``ephemeral_5m_input_tokens``, ``ephemeral_1h_input_tokens``},
  ``service_tier``, ``speed``, ``iterations``).
- **No cost field exists anywhere** (no ``costUSD``) → cost_usd is computed
  from usage via :mod:`traceguard.routing_audit.pricing`.
- **Streaming duplication**: one API message is written as MULTIPLE lines
  (one per content block), all sharing ``message.id``. Earlier lines carry a
  PARTIAL usage snapshot — ``output_tokens`` grows across lines and
  ``stop_reason`` is often null until the final line; the LAST line's
  ``output_tokens`` equalled the per-message maximum in 100% of locally
  scanned multi-line messages. Records are therefore deduplicated globally
  by ``message.id`` with the last-parsed line winning; this also collapses
  messages copied across files by session resume. API-error lines
  (``isApiErrorMessage``, model ``<synthetic>``) may lack ``message.id`` →
  fall back to ``uuid:<line uuid>``.
- Not available in the source (left NULL): per-message latency,
  correlation/parent links, prompt template identity.

Field mapping:

- ``project``    ← sanitized basename of the record ``cwd`` (canonical names
  ``huadian`` / ``quant_alpha_v2`` / ``traceguard`` preserved; anything else
  keeps its own lowercase-snake slug); fallback ``unknown``.
- ``component``  ← ``main`` for main transcripts, ``agentType`` from the
  subagent meta file, ``unknown`` if the meta file is missing.
- ``operation``  ← ``llm_complete``.
- ``invoked_at`` ← the record's own ``timestamp`` (backfill: this is the
  original call time, not the ingest time).
- ``parse_status`` ← ``failed`` for API-error records, ``partial`` when
  ``stop_reason == "max_tokens"`` (truncated output), else ``success``.
- ``tokens_in``  ← input + cache_read + cache_creation tokens (full prompt
  volume; the per-kind split is kept in ``output_parsed.usage``);
  ``tokens_out`` ← ``output_tokens``.
- ``input_hash`` ← SDK :func:`traceguard.input_hash` over the stable source
  identity ``{"source", "session_id", "message_id"}`` (SPEC §3.1: the hash
  MUST come from the SDK normalize function).

Privacy: prompt/completion content, tool inputs/outputs and error text are
NEVER read into the database. ``input_summary`` is a short synthetic label;
``output_parsed`` holds only non-sensitive metadata (version, entrypoint,
git branch, stop_reason, token breakdown, agent identity).

Data caveats (read before trusting the numbers):

- **The source is mutable.** Claude Code rewrites a session ``.jsonl`` in
  place on resume/compact: messages can disappear, be re-emitted with a new
  ``uuid``, or move between files. ``routing_audit_ingest_log`` is the
  immutable retention layer — once a ``source_message_id`` is logged its
  trace is never rewritten or double-counted. **On any disagreement between
  the source tree and the DB, the DB wins.** A consequence: a model's
  "first-seen" timestamp (used for ``available_to_us_at``) can drift between
  runs as the source changes; the first successful ingest into a given DB
  fixes it (registry is insert-only).
- **Cross-cutover residue is expected, not a bug.** Long sessions that
  straddle a model-availability change carry the old model on their tail
  messages. Concretely (data as of 2026-07-02): 31 ``claude-fable-5``
  messages land on 2026-06-13, all inside three sessions that opened
  2026-06-10..12 and switched to opus-4-8 afterwards — i.e. the fade-out
  tail of pre-existing sessions during the Fable stand-down window, not a
  mapping error. Such rows are kept as-is.
- **Incremental runs trust mtime.** ``--since`` skips files whose mtime
  predates the cutoff; because resume/compact bumps mtime, a rewritten file
  is always re-scanned and the idempotency layer dedupes. A full scan (no
  ``--since``) remains the backstop for correctness.
- **iCloud sync hazard (this repo lives under a synced Desktop path).** iCloud
  produces ``"<name> 2.<ext>"`` conflict copies; for a live SQLite DB or a log
  being appended, that can surface as corruption/duplication exposure (a sister
  project, quant_alpha_v2, relocated to ``~/apps`` for exactly this reason).
  The ``.gitignore`` masks ``* 2.*`` so copies never reach a commit, but the
  DB/logs themselves are still at risk in place. Whether to move THIS repo off
  the synced path is a separate decision — recorded here, not acted on.
"""
from __future__ import annotations

import json
import re
import uuid as uuid_mod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from traceguard.registry.models import register_model
from traceguard.routing_audit.models import RoutingAuditIngestLog, ensure_tables
from traceguard.routing_audit.pricing import KNOWN_RELEASED_AT, compute_cost_usd
from traceguard.sdk.normalizer import input_hash
from traceguard.store.models import ModelRegistryEntry, Trace, make_engine

DEFAULT_SOURCE = Path.home() / ".claude" / "projects"
OPERATION = "llm_complete"
_SYNTHETIC_MODEL = "<synthetic>"
_CANONICAL_PROJECTS = {"huadian", "quant_alpha_v2", "traceguard"}
_WRITE_CHUNK = 500


@dataclass
class SourceFile:
    path: Path
    agent_id: str | None = None  # None → main transcript
    agent_type: str | None = None


@dataclass
class ParsedRecord:
    source_message_id: str
    source_session_id: str
    source_uuid: str | None
    source_file: str
    agent_id: str | None
    project: str
    component: str
    model_id: str | None  # None for API-error/synthetic records
    usage: dict[str, Any] | None
    parse_status: str
    invoked_at: datetime
    is_error: bool
    meta: dict[str, Any]


@dataclass
class IngestStats:
    files_main: int = 0
    files_subagent: int = 0
    files_skipped_mtime: int = 0  # skipped by --since (mtime before cutoff)
    lines_read: int = 0
    assistant_lines: int = 0
    malformed_lines: int = 0
    skipped_no_identity: int = 0
    skipped_no_timestamp: int = 0
    duplicate_lines: int = 0  # extra streaming lines / resume copies collapsed
    records: int = 0  # distinct records found in source this run
    error_records: int = 0
    already_ingested: int = 0
    written: int = 0
    written_cost: Decimal = Decimal("0")  # list-price cost of rows written this run
    missing_price: int = 0
    batch_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    ts_min: datetime | None = None
    ts_max: datetime | None = None
    models_first_seen: dict[str, datetime] = field(default_factory=dict)
    # (model_id, project, component) -> [count, tokens_in, tokens_out, cost]
    per_key: dict[tuple[str, str, str], list[Any]] = field(default_factory=dict)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _sanitize_project(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "unknown"


def map_project(cwd: str | None) -> str:
    """Map a record's cwd to a project name (SPEC §2 lowercase snake)."""
    if not cwd:
        return "unknown"
    slug = _sanitize_project(Path(cwd).name)
    # `_CANONICAL_PROJECTS` are already in sanitized form; everything else
    # keeps its own slug so per-project cost still shows up in the report.
    return slug if slug in _CANONICAL_PROJECTS or slug else "unknown"


def _load_agent_type(agent_file: Path) -> str | None:
    meta_path = agent_file.with_name(agent_file.stem + ".meta.json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    agent_type = meta.get("agentType")
    return agent_type if isinstance(agent_type, str) and agent_type else None


def discover_session_files(source_root: Path, *, include_subagents: bool = True) -> list[SourceFile]:
    """Find main and subagent transcripts under the Claude Code projects root."""
    found: list[SourceFile] = []
    if not source_root.is_dir():
        return found
    for project_dir in sorted(p for p in source_root.iterdir() if p.is_dir()):
        for f in sorted(project_dir.glob("*.jsonl")):
            found.append(SourceFile(path=f))
        if include_subagents:
            for f in sorted(project_dir.glob("*/subagents/**/agent-*.jsonl")):
                agent_id = f.stem.removeprefix("agent-")
                found.append(
                    SourceFile(path=f, agent_id=agent_id, agent_type=_load_agent_type(f))
                )
    return found


def _usage_tokens(usage: dict[str, Any] | None) -> tuple[int | None, int | None]:
    if usage is None:
        return None, None
    tokens_in = (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )
    tokens_out = int(usage.get("output_tokens") or 0)
    return tokens_in, tokens_out


def parse_session_file(
    source_file: SourceFile, source_root: Path, stats: IngestStats
) -> Iterator[ParsedRecord]:
    """Yield one ParsedRecord per assistant line (dedup happens in the caller)."""
    component = "main" if source_file.agent_id is None else (source_file.agent_type or "unknown")
    try:
        rel_path = str(source_file.path.relative_to(source_root))
    except ValueError:
        rel_path = str(source_file.path)

    with source_file.path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stats.lines_read += 1
            # Cheap prefilter: assistant records always contain the literal
            # value string; false positives are filtered after json.loads.
            if '"assistant"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                stats.malformed_lines += 1
                continue
            if not isinstance(rec, dict) or rec.get("type") != "assistant":
                continue
            stats.assistant_lines += 1

            msg = rec.get("message") or {}
            model_raw = msg.get("model")
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            is_error = bool(rec.get("isApiErrorMessage")) or model_raw == _SYNTHETIC_MODEL

            message_id = msg.get("id")
            line_uuid = rec.get("uuid")
            if isinstance(message_id, str) and message_id:
                source_message_id = message_id
            elif isinstance(line_uuid, str) and line_uuid:
                source_message_id = f"uuid:{line_uuid}"
            else:
                stats.skipped_no_identity += 1
                continue

            invoked_at = _parse_ts(rec.get("timestamp"))
            if invoked_at is None:
                stats.skipped_no_timestamp += 1
                continue

            if is_error:
                parse_status = "failed"
            elif msg.get("stop_reason") == "max_tokens":
                parse_status = "partial"
            else:
                parse_status = "success"

            model_id = (
                model_raw
                if isinstance(model_raw, str) and model_raw and model_raw != _SYNTHETIC_MODEL
                else None
            )

            meta: dict[str, Any] = {
                "source": "claude_code_session",
                "session_id": rec.get("sessionId"),
                "message_id": message_id,
                "cc_version": rec.get("version"),
                "entrypoint": rec.get("entrypoint"),
                "git_branch": rec.get("gitBranch"),
                "stop_reason": msg.get("stop_reason"),
            }
            if source_file.agent_id is not None:
                meta["agent_id"] = source_file.agent_id
                meta["agent_type"] = source_file.agent_type
            if is_error:
                meta["raw_model"] = model_raw
                meta["api_error_status"] = rec.get("apiErrorStatus")
            if usage is not None:
                nested = usage.get("cache_creation") or {}
                meta["usage"] = {
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                    "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
                    "cache_creation_5m": nested.get("ephemeral_5m_input_tokens"),
                    "cache_creation_1h": nested.get("ephemeral_1h_input_tokens"),
                    "service_tier": usage.get("service_tier"),
                    "speed": usage.get("speed"),
                    "iterations": len(usage["iterations"])
                    if isinstance(usage.get("iterations"), list)
                    else None,
                }

            session_id = rec.get("sessionId")
            yield ParsedRecord(
                source_message_id=source_message_id,
                source_session_id=session_id if isinstance(session_id, str) else "unknown",
                source_uuid=line_uuid if isinstance(line_uuid, str) else None,
                source_file=rel_path,
                agent_id=source_file.agent_id,
                project=map_project(rec.get("cwd")),
                component=component,
                model_id=model_id,
                usage=usage,
                parse_status=parse_status,
                invoked_at=invoked_at,
                is_error=is_error,
                meta=meta,
            )


def _file_mtime_utc(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def collect_records(
    source_root: Path,
    *,
    include_subagents: bool = True,
    stats: IngestStats | None = None,
    since: datetime | None = None,
) -> tuple[dict[str, ParsedRecord], IngestStats]:
    """Discover, parse and deduplicate all source records (no DB access).

    ``since`` (incremental mode) skips files whose mtime predates the cutoff.
    resume/compact bumps mtime, so a rewritten file is always re-scanned; the
    idempotency layer then dedupes. Omit ``since`` for a full backstop scan.
    """
    stats = stats or IngestStats()
    records: dict[str, ParsedRecord] = {}
    for source_file in discover_session_files(source_root, include_subagents=include_subagents):
        if since is not None:
            mtime = _file_mtime_utc(source_file.path)
            if mtime is not None and mtime < since:
                stats.files_skipped_mtime += 1
                continue
        if source_file.agent_id is None:
            stats.files_main += 1
        else:
            stats.files_subagent += 1
        for rec in parse_session_file(source_file, source_root, stats):
            if rec.source_message_id in records:
                stats.duplicate_lines += 1
            # Last line wins: earlier lines of a streamed message carry a
            # partial usage snapshot and often a null stop_reason.
            records[rec.source_message_id] = rec
    stats.records = len(records)
    for rec in records.values():
        _accumulate(stats, rec)
    return records, stats


def _accumulate(stats: IngestStats, rec: ParsedRecord) -> None:
    if rec.is_error:
        stats.error_records += 1
    if stats.ts_min is None or rec.invoked_at < stats.ts_min:
        stats.ts_min = rec.invoked_at
    if stats.ts_max is None or rec.invoked_at > stats.ts_max:
        stats.ts_max = rec.invoked_at
    if rec.model_id is not None:
        first = stats.models_first_seen.get(rec.model_id)
        if first is None or rec.invoked_at < first:
            stats.models_first_seen[rec.model_id] = rec.invoked_at

    tokens_in, tokens_out = _usage_tokens(rec.usage)
    cost = compute_cost_usd(rec.model_id, rec.usage)
    if cost is None and rec.model_id is not None:
        stats.missing_price += 1
    key = (rec.model_id or "(none)", rec.project, rec.component)
    agg = stats.per_key.setdefault(key, [0, 0, 0, Decimal("0")])
    agg[0] += 1
    agg[1] += tokens_in or 0
    agg[2] += tokens_out or 0
    agg[3] += cost or Decimal("0")


def register_observed_models(models_first_seen: dict[str, datetime], engine: Any) -> None:
    """Register every observed model before writing traces (SPEC §3.1 MUST).

    ``available_to_us_at`` = first appearance in the local data;
    ``released_at`` = known value from ``KNOWN_RELEASED_AT``, clamped to the
    first-seen timestamp as a fallback (see pricing module TODO).
    ``if_exists="ignore"`` keeps re-runs idempotent (existing rows untouched).
    """
    for model_id, first_seen in sorted(models_first_seen.items()):
        released_at = KNOWN_RELEASED_AT.get(model_id, first_seen)
        if released_at > first_seen:
            released_at = first_seen
        register_model(
            model_id,
            model_family="anthropic",
            capability_class="general-llm",
            released_at=released_at,
            available_to_us_at=first_seen,
            engine=engine,
            if_exists="ignore",
        )


def _build_trace(rec: ParsedRecord) -> Trace:
    tokens_in, tokens_out = _usage_tokens(rec.usage)
    summary = (
        f"claude-code backfill {rec.project}/{rec.component}"
        f" model={rec.model_id or _SYNTHETIC_MODEL}"
        f" session={rec.source_session_id[:8]}"
    )[:200]
    return Trace(
        project=rec.project,
        component=rec.component,
        operation=OPERATION,
        input_hash=input_hash(
            {
                "source": "claude_code_session",
                "session_id": rec.source_session_id,
                "message_id": rec.source_message_id,
            }
        ),
        input_summary=summary,
        model_id=rec.model_id,
        output_parsed=rec.meta,
        parse_status=rec.parse_status,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=compute_cost_usd(rec.model_id, rec.usage),
        invoked_at=rec.invoked_at,
        error_class="api_error" if rec.is_error else None,
    )


def new_batch_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"cc-{stamp}-{uuid_mod.uuid4().hex[:6]}"


def ingest(
    source: Path | str = DEFAULT_SOURCE,
    db_url: str | None = None,
    *,
    write: bool = False,
    include_subagents: bool = True,
    batch_id: str | None = None,
    since: datetime | None = None,
) -> IngestStats:
    """Parse the source tree; optionally write new traces (default dry-run).

    Idempotent: records whose ``source_message_id`` already exists in
    ``routing_audit_ingest_log`` are skipped. Every written row is logged
    under ``stats.batch_id`` for :func:`rollback_batch`. ``since`` enables
    incremental scanning (see :func:`collect_records`).
    """
    source_root = Path(source).expanduser()
    records, stats = collect_records(
        source_root, include_subagents=include_subagents, since=since
    )
    if not write:
        return stats

    engine = make_engine(db_url)
    ensure_tables(engine)
    with Session(engine) as sess:
        existing = set(sess.scalars(select(RoutingAuditIngestLog.source_message_id)).all())
    new_records = [r for r in records.values() if r.source_message_id not in existing]
    stats.already_ingested = stats.records - len(new_records)

    register_observed_models(stats.models_first_seen, engine)

    # model_registry is insert-only (if_exists="ignore" never updates). If an
    # earlier, narrower run (e.g. --no-subagents) registered a later
    # available_to_us_at, traces written now could predate it — the exact
    # shape invariant 2 rejects. Surface it instead of writing silently.
    with Session(engine) as sess:
        for model_id, first_seen in sorted(stats.models_first_seen.items()):
            entry = sess.get(ModelRegistryEntry, model_id)
            if entry is not None and first_seen < entry.available_to_us_at:
                stats.warnings.append(
                    f"model {model_id}: observed invoked_at {first_seen.isoformat()} predates "
                    f"registered available_to_us_at {entry.available_to_us_at.isoformat()}; "
                    "registry is insert-only — re-run into a fresh DB for correct timing"
                )

    stats.batch_id = batch_id or new_batch_id()
    with Session(engine) as sess:
        for start in range(0, len(new_records), _WRITE_CHUNK):
            chunk = new_records[start : start + _WRITE_CHUNK]
            for rec in chunk:
                trace = _build_trace(rec)
                sess.add(trace)
                sess.flush()  # populate trace.trace_id
                if trace.cost_usd is not None:
                    stats.written_cost += trace.cost_usd
                sess.add(
                    RoutingAuditIngestLog(
                        batch_id=stats.batch_id,
                        source_message_id=rec.source_message_id,
                        source_session_id=rec.source_session_id,
                        source_uuid=rec.source_uuid,
                        source_file=rec.source_file,
                        agent_id=rec.agent_id,
                        trace_id=trace.trace_id,
                    )
                )
            sess.commit()
            stats.written += len(chunk)
    return stats


def rollback_batch(
    batch_id: str, db_url: str | None = None, *, dry_run: bool = False
) -> tuple[int, int]:
    """Delete all traces written by ``batch_id``. Returns (traces, log rows).

    With ``dry_run=True`` nothing is deleted — only the counts that a real
    rollback would remove are returned.
    """
    engine = make_engine(db_url)
    ensure_tables(engine)
    with Session(engine) as sess:
        trace_ids = list(
            sess.scalars(
                select(RoutingAuditIngestLog.trace_id).where(
                    RoutingAuditIngestLog.batch_id == batch_id
                )
            )
        )
        if dry_run:
            return len(trace_ids), len(trace_ids)
        n_traces = 0
        for start in range(0, len(trace_ids), _WRITE_CHUNK):
            chunk = trace_ids[start : start + _WRITE_CHUNK]
            n_traces += sess.execute(
                delete(Trace).where(Trace.trace_id.in_(chunk))
            ).rowcount
        n_log = sess.execute(
            delete(RoutingAuditIngestLog).where(RoutingAuditIngestLog.batch_id == batch_id)
        ).rowcount
        sess.commit()
    return n_traces, n_log


def append_run_log(
    log_path: Path | str, stats: IngestStats, *, wrote: bool, error: str | None = None
) -> None:
    """Append one JSON-line record of this run (for the scheduled job trail).

    Fields: timestamp, batch, whether it wrote, distinct/new-row counts, new
    list-price cost, per-kind file counts, and any error/warnings — enough to
    chart daily growth or spot a failed cron run. Never raises: a logging
    failure must not fail the ingest.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "wrote": wrote,
        "batch_id": stats.batch_id,
        "records": stats.records,
        "written": stats.written,
        "new_cost_usd": f"{stats.written_cost:.6f}",
        "already_ingested": stats.already_ingested,
        "files_main": stats.files_main,
        "files_subagent": stats.files_subagent,
        "files_skipped_mtime": stats.files_skipped_mtime,
        "error_records": stats.error_records,
        "warnings": stats.warnings,
        "error": error,
    }
    try:
        path = Path(log_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:  # logging must never break the ingest
        print(f"WARNING: could not write run log to {log_path}: {exc}")


def format_report(stats: IngestStats, *, wrote: bool) -> str:
    """Plain-text summary: totals, model × project × component, time span."""
    mode = f"WROTE batch={stats.batch_id}" if wrote else "DRY-RUN (no writes)"
    files_line = f"files: {stats.files_main} main + {stats.files_subagent} subagent transcripts"
    if stats.files_skipped_mtime:
        files_line += f" ({stats.files_skipped_mtime} skipped by --since)"
    lines = [
        f"== routing_audit: claude-code ingest — {mode} ==",
        files_line,
        (
            f"lines: {stats.lines_read} read, {stats.assistant_lines} assistant, "
            f"{stats.duplicate_lines} duplicate (streaming/resume), "
            f"{stats.malformed_lines} malformed, "
            f"{stats.skipped_no_identity + stats.skipped_no_timestamp} unusable"
        ),
        (
            f"records: {stats.records} distinct messages "
            f"({stats.error_records} api-error) | already ingested: "
            f"{stats.already_ingested} | written: {stats.written} "
            f"(new cost ${stats.written_cost:.4f})"
        ),
    ]
    if stats.missing_price:
        lines.append(f"WARNING: {stats.missing_price} records have a model with no price entry")
    for warning in stats.warnings:
        lines.append(f"WARNING: {warning}")
    if stats.ts_min and stats.ts_max:
        lines.append(f"time span: {stats.ts_min.isoformat()} → {stats.ts_max.isoformat()}")

    header = (
        f"{'model':<28} {'project':<22} {'component':<20} "
        f"{'traces':>7} {'tokens_in':>13} {'tokens_out':>11} {'cost_usd':>11}"
    )
    lines += ["", header, "-" * len(header)]
    total = [0, 0, 0, Decimal("0")]
    for (model, project, component), agg in sorted(
        stats.per_key.items(), key=lambda kv: kv[1][3], reverse=True
    ):
        lines.append(
            f"{model:<28} {project:<22} {component:<20} "
            f"{agg[0]:>7} {agg[1]:>13} {agg[2]:>11} {agg[3]:>11.4f}"
        )
        for i in range(3):
            total[i] += agg[i]
        total[3] += agg[3]
    lines.append("-" * len(header))
    lines.append(
        f"{'TOTAL':<28} {'':<22} {'':<20} "
        f"{total[0]:>7} {total[1]:>13} {total[2]:>11} {total[3]:>11.4f}"
    )
    return "\n".join(lines)

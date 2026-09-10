"""Evidence bundle export and offline verification (``evidence-bundle/v1``).

A bundle is a self-contained JSON document — selected traces, the chain segment
covering them, the chain head, external anchors, source snapshots, findings —
built so that whoever receives it can check what is checkable **without the
database, without the network, and without traceguard**.

Format spec, including the full "proves what / proves nothing" table:
``docs/specs/evidence-bundle.md``; JSON Schema:
``docs/specs/evidence-bundle-v1.schema.json``.

The one thing to get right when reading this module: **a ``hash_only`` bundle
and a ``full`` bundle do not support the same conclusion, and the code refuses
to let them share a word.** algo v1's hash envelope covers content fields
(``input_summary`` / ``output_parsed`` / ``error_message``); strip them and
entry hashes cannot be recomputed at all. A ``hash_only`` verify therefore
checks chain LINKAGE and the head against an anchor, and says so with a
``content_not_recomputed`` INFO finding that is always present in that mode.
Reading such a pass as "content verified" is the most consequential mistake
this format invites, so the code makes it structurally hard.

``content_not_recomputed`` is a BUNDLE-level finding, not an audit finding
kind: it is not in ``FINDING_SEVERITY`` and is not bound by the SPEC §6.6 kind
freeze.

Anchors are structure-checked, never cryptographically verified — see the spec
§4. Zero new runtime dependencies (stdlib ``json`` only).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard.audit.canonical import (
    ALGO_VERSION,
    TRACE_CONTENT_FIELDS,
    CanonicalizationError,
    canon_error_content,
    compute_row_hash,
    entry_payload,
)
from traceguard.audit.models import AuditChainEntry, AuditCostEvent
from traceguard.audit.verify import BREAK, WARN, ChainAnchor, ChainFinding, export_anchor
from traceguard.store.models import Trace

BUNDLE_SCHEMA = "evidence-bundle/v1"

#: Severity for statements about the SCOPE of a verification rather than a
#: defect. Deliberately absent from audit.FINDING_SEVERITY: this is a
#: bundle-level annotation, not a chain finding kind (no §6.6 freeze).
INFO = "INFO"

#: Emitted on every hash_only verification. Its presence is the mechanism that
#: keeps "the linkage holds" from being read as "the content is verified".
CONTENT_NOT_RECOMPUTED = "content_not_recomputed"

#: traces columns carried in a bundle. The content subset is what hash_only
#: strips; everything else is metadata a recipient needs to make sense of the
#: rows (and cost_usd, which was never in the envelope anyway).
_TRACE_CONTENT_ONLY: tuple[str, ...] = ("input_summary", "output_parsed", "error_message")
_TRACE_EXPORT_FIELDS: tuple[str, ...] = TRACE_CONTENT_FIELDS + (
    "agent_id",
    "session_id",
    "provider_response_id",
    "cost_usd",
)

_CHAIN_ENTRY_FIELDS: tuple[str, ...] = (
    "seq",
    "entry_type",
    "trace_id",
    "event_id",
    "cost_at_event",
    "note",
    "canon_status",
    "canon_error",
    "prev_hash",
    "row_hash",
    "algo_version",
    "created_at",
)

VALID_ANCHOR_KINDS = frozenset(
    {"file", "git-note", "webhook", "rfc3161", "ots", "rekor"}
)
_RFC3161_REQUIRED = ("tsa_url", "digest_alg", "message_imprint", "token_b64")


def _jsonable(value: Any) -> Any:
    """Datetimes to UTC isoformat, Decimals to str, everything else as-is."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def anchor_record(anchor: ChainAnchor, *, kind: str = "file", location: str | None = None) -> dict:
    """A :class:`ChainAnchor` as a bundle ``anchors[]`` entry."""
    if kind not in VALID_ANCHOR_KINDS:
        raise ValueError(f"anchor kind must be one of {sorted(VALID_ANCHOR_KINDS)}, got {kind!r}")
    return {
        "kind": kind,
        "seq": anchor.seq,
        "row_hash": anchor.row_hash,
        "algo_version": anchor.algo_version,
        "entry_count": anchor.entry_count,
        "exported_at": anchor.exported_at,
        "location": location,
    }


@dataclass
class BundleVerifyResult:
    """Outcome of :func:`verify_bundle`. ``ok`` is False only on BREAK."""

    ok: bool
    content_mode: str
    entries_checked: int = 0
    traces_included: int = 0
    content_recomputed: int = 0
    anchors_checked: int = 0
    findings: list[ChainFinding] = field(default_factory=list)

    @property
    def breaks(self) -> list[ChainFinding]:
        return [f for f in self.findings if f.severity == BREAK]

    def summary(self) -> str:
        """Deliberately different wording per mode.

        A ``hash_only`` pass has checked linkage and anchors and NOTHING about
        content; saying "verified" for both would be the format's worst
        failure mode in one word.
        """
        if not self.ok:
            return (
                f"bundle FAILED ({self.content_mode}): {len(self.breaks)} break(s) over "
                f"{self.entries_checked} chain entry/entries"
            )
        if self.content_mode == "hash_only":
            return (
                f"bundle LINKAGE OK (hash_only): {self.entries_checked} entry/entries form "
                f"an unbroken chain and {self.anchors_checked} anchor(s) match the head — "
                "content was NOT recomputed and is NOT covered by this result"
            )
        return (
            f"bundle VERIFIED (full): {self.entries_checked} entry/entries recomputed, "
            f"{self.content_recomputed} against included trace content, "
            f"{self.anchors_checked} anchor(s) match the head"
        )


def export_bundle(
    engine: Engine,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    trace_ids: Sequence[int] | None = None,
    content_mode: str = "full",
    include_sources: bool = True,
    anchors: Iterable[dict] | None = None,
) -> dict:
    """Build an ``evidence-bundle/v1`` document.

    Selects traces by ``trace_ids`` if given, else by ``invoked_at`` within
    ``[since, until)``; then carries every chain entry that references one of
    them, plus the current chain head. ``anchors`` are recorded verbatim (use
    :func:`anchor_record` for a :class:`ChainAnchor`).

    ``content_mode='hash_only'`` strips the content fields from every trace.
    That makes entry hashes unrecomputable by construction — which is the
    point, and why :func:`verify_bundle` reports such a bundle differently.
    """
    if content_mode not in ("full", "hash_only"):
        raise ValueError(f"content_mode must be 'full' or 'hash_only', got {content_mode!r}")
    from traceguard import __version__

    drop = set(_TRACE_CONTENT_ONLY) if content_mode == "hash_only" else set()

    with Session(engine) as sess:
        stmt = select(Trace)
        if trace_ids is not None:
            stmt = stmt.where(Trace.trace_id.in_(list(trace_ids)))
        else:
            if since is not None:
                stmt = stmt.where(Trace.invoked_at >= since)
            if until is not None:
                stmt = stmt.where(Trace.invoked_at < until)
        traces = list(sess.scalars(stmt.order_by(Trace.trace_id.asc())))
        selected = [t.trace_id for t in traces]

        entries = list(
            sess.scalars(
                select(AuditChainEntry)
                .where(AuditChainEntry.trace_id.in_(selected))
                .order_by(AuditChainEntry.seq.asc())
            )
        )
        event_ids = [e.event_id for e in entries if e.entry_type == "cost_event" and e.event_id]
        cost_events = {
            ev.event_id: ev
            for ev in sess.scalars(
                select(AuditCostEvent).where(AuditCostEvent.event_id.in_(event_ids))
            )
        }

        snapshots: list[dict] = []
        if include_sources and selected:
            try:
                from traceguard.sources.models import SourceSnapshotRow

                for row in sess.scalars(
                    select(SourceSnapshotRow)
                    .where(SourceSnapshotRow.trace_id.in_(selected))
                    .order_by(SourceSnapshotRow.snapshot_id.asc())
                ):
                    snapshots.append(
                        {
                            name: _jsonable(getattr(row, name))
                            for name in (
                                "snapshot_id", "trace_id", "source_uri", "source_kind",
                                "content_hash", "content_encoding", "normalized_hash",
                                "normalizer_id", "retrieved_at", "published_at",
                                "effective_at", "source_version", "mcp_server_id",
                                "tool_name", "cache_status", "verdict", "strict",
                            )
                        }
                    )
            except Exception:  # noqa: BLE001 - sources not enabled / table absent
                snapshots = []

    head = export_anchor(engine)
    return {
        "schema": BUNDLE_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": f"traceguard {__version__}",
        "content_mode": content_mode,
        "chain": {
            "algo_version": ALGO_VERSION,
            "head": {
                "seq": head.seq,
                "row_hash": head.row_hash,
                "algo_version": head.algo_version,
                "entry_count": head.entry_count,
                "exported_at": head.exported_at,
            },
            "entries": [
                {name: _jsonable(getattr(e, name)) for name in _CHAIN_ENTRY_FIELDS}
                for e in entries
            ],
        },
        "traces": [
            {
                name: _jsonable(getattr(t, name))
                for name in _TRACE_EXPORT_FIELDS
                if name not in drop
            }
            for t in traces
        ],
        "source_snapshots": snapshots,
        "anchors": list(anchors or []),
        "findings": [],
        "approvals": [],  # reserved; traceguard.approval is planned, not implemented
        "_cost_events": [
            {
                "event_id": ev.event_id,
                "trace_id": ev.trace_id,
                "event_type": ev.event_type,
                "old_value": ev.old_value,
                "new_value": ev.new_value,
                "reason": ev.reason,
                "batch_id": ev.batch_id,
                "occurred_at": _jsonable(ev.occurred_at),
            }
            for ev in cost_events.values()
        ],
    }


def _recompute(entry: dict, content: Any) -> str:
    payload = entry_payload(
        entry_type=entry["entry_type"],
        trace_id=entry.get("trace_id"),
        event_id=entry.get("event_id"),
        cost_at_event=entry.get("cost_at_event"),
        note=entry.get("note"),
        canon_status=entry["canon_status"],
        canon_error=entry.get("canon_error"),
        created_at=_parse_dt(entry["created_at"]),
        content=content,
    )
    return compute_row_hash(entry["prev_hash"], payload)


def _check_anchors(bundle: dict, head_hash: str | None) -> tuple[int, list[ChainFinding]]:
    """Structure-check every anchor; compare those that carry a head digest.

    No signature is ever verified (spec §4): a structurally valid RFC 3161
    token is reported as present, not as valid.
    """
    findings: list[ChainFinding] = []
    checked = 0
    for i, anchor in enumerate(bundle.get("anchors") or []):
        kind = anchor.get("kind")
        if kind not in VALID_ANCHOR_KINDS:
            findings.append(
                ChainFinding(
                    "anchor_malformed", BREAK, None, None,
                    f"anchors[{i}] has kind {kind!r}; expected one of {sorted(VALID_ANCHOR_KINDS)}",
                )
            )
            continue
        if kind == "rfc3161":
            missing = [f for f in _RFC3161_REQUIRED if not anchor.get(f)]
            if missing:
                findings.append(
                    ChainFinding(
                        "anchor_malformed", BREAK, None, None,
                        f"anchors[{i}] is kind 'rfc3161' but lacks {missing}; "
                        "traceguard structure-checks this token and never verifies its "
                        "signature — verify it with `openssl ts` and a CA you fetched yourself",
                    )
                )
                continue
        if kind == "ots" and anchor.get("ots_status") == "pending":
            findings.append(
                ChainFinding(
                    "anchor_pending", WARN, None, None,
                    f"anchors[{i}] is an OpenTimestamps proof still PENDING — it carries a "
                    "calendar server's promise, not a bitcoin-attested time. A pending "
                    "proof is not evidence; upgrade it before relying on it",
                )
            )
        row_hash = anchor.get("row_hash")
        if not row_hash:
            continue
        checked += 1
        if head_hash is not None and row_hash != head_hash:
            findings.append(
                ChainFinding(
                    "anchor_mismatch", BREAK, anchor.get("seq"), None,
                    f"anchors[{i}] row_hash {row_hash} != the bundle's chain head {head_hash}; "
                    "the chain was truncated or rewritten relative to this anchor",
                )
            )
    return checked, findings


def verify_bundle(bundle: dict) -> BundleVerifyResult:
    """Verify a bundle offline. No database, no network, no signature checks.

    ``full``: recomputes every chain entry's ``row_hash`` from its metadata and
    the included content, and checks linkage and anchors.
    ``hash_only``: checks linkage and anchors ONLY, and always emits
    ``content_not_recomputed`` (INFO) — content is outside what that result
    covers, and the wording of :meth:`BundleVerifyResult.summary` says so.
    """
    if bundle.get("schema") != BUNDLE_SCHEMA:
        raise ValueError(
            f"not an {BUNDLE_SCHEMA} document (its 'schema' field is "
            f"{bundle.get('schema')!r}); see docs/specs/evidence-bundle.md"
        )
    content_mode = bundle.get("content_mode")
    if content_mode not in ("full", "hash_only"):
        raise ValueError(f"content_mode must be 'full' or 'hash_only', got {content_mode!r}")

    chain = bundle.get("chain") or {}
    entries = list(chain.get("entries") or [])
    traces = {t["trace_id"]: t for t in (bundle.get("traces") or [])}
    cost_events = {e["event_id"]: e for e in (bundle.get("_cost_events") or [])}

    result = BundleVerifyResult(
        ok=True, content_mode=content_mode, entries_checked=len(entries),
        traces_included=len(traces),
    )

    # ── linkage: checked in BOTH modes; it is all hash_only can offer ──
    prev: str | None = None
    for entry in entries:
        if prev is not None and entry["prev_hash"] != prev:
            result.findings.append(
                ChainFinding(
                    "link_broken", BREAK, entry.get("seq"), entry.get("trace_id"),
                    f"prev_hash {entry['prev_hash']} != the previous entry's row_hash {prev}",
                )
            )
        prev = entry["row_hash"]

    if content_mode == "full":
        for entry in entries:
            content: Any
            if entry["canon_status"] == "failed":
                content = canon_error_content(entry.get("canon_error"))
            elif entry["entry_type"] in ("write", "backfill"):
                trace = traces.get(entry.get("trace_id"))
                if trace is None:
                    result.findings.append(
                        ChainFinding(
                            "missing_trace", BREAK, entry.get("seq"), entry.get("trace_id"),
                            "the chain entry references a trace the bundle does not include, "
                            "so its content cannot be checked",
                        )
                    )
                    continue
                content = {
                    name: _parse_dt(trace.get(name))
                    if name in ("feature_as_of", "invoked_at")
                    else trace.get(name)
                    for name in TRACE_CONTENT_FIELDS
                }
            elif entry["entry_type"] == "cost_event":
                event = cost_events.get(entry.get("event_id"))
                if event is None:
                    result.findings.append(
                        ChainFinding(
                            "missing_cost_event", BREAK, entry.get("seq"), entry.get("trace_id"),
                            f"cost event {entry.get('event_id')} is referenced but not included",
                        )
                    )
                    continue
                content = {
                    "event_id": event["event_id"], "trace_id": event["trace_id"],
                    "event_type": event["event_type"], "old_value": event["old_value"],
                    "new_value": event["new_value"], "reason": event["reason"],
                    "batch_id": event["batch_id"],
                    "occurred_at": _parse_dt(event["occurred_at"]),
                }
            else:
                content = None
            try:
                recomputed = _recompute(entry, content)
            except (CanonicalizationError, KeyError, TypeError) as exc:
                result.findings.append(
                    ChainFinding(
                        "hash_mismatch", BREAK, entry.get("seq"), entry.get("trace_id"),
                        f"entry content could not be canonicalized for recomputation ({exc})",
                    )
                )
                continue
            if recomputed != entry["row_hash"]:
                result.findings.append(
                    ChainFinding(
                        "hash_mismatch", BREAK, entry.get("seq"), entry.get("trace_id"),
                        f"recomputed {recomputed} != stored {entry['row_hash']}; the covered "
                        "content or the entry metadata was changed after chaining",
                    )
                )
            elif entry["entry_type"] in ("write", "backfill"):
                result.content_recomputed += 1
    else:
        result.findings.append(
            ChainFinding(
                CONTENT_NOT_RECOMPUTED, INFO, None, None,
                "content_mode is 'hash_only': the trace content fields covered by the algo "
                "v1 hash envelope are absent, so entry hashes were NOT recomputed. This "
                "result covers chain LINKAGE and the head-vs-anchor check only — it says "
                "nothing about whether the content is what was chained. Re-export with "
                "content_mode='full' to check that.",
            )
        )

    head_hash = (chain.get("head") or {}).get("row_hash")
    if entries:
        last = entries[-1]["row_hash"]
        if head_hash and head_hash != last and (chain.get("head") or {}).get("seq") == entries[-1].get("seq"):
            result.findings.append(
                ChainFinding(
                    "hash_mismatch", BREAK, entries[-1].get("seq"), None,
                    f"the bundle's declared head {head_hash} disagrees with its own last "
                    f"entry {last} at the same seq",
                )
            )
    checked, anchor_findings = _check_anchors(bundle, head_hash)
    result.anchors_checked = checked
    result.findings.extend(anchor_findings)

    result.ok = not result.breaks
    return result


def write_bundle(bundle: dict, path: str | Path) -> Path:
    """Write a bundle as indented JSON with stable key order."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(bundle, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return target


def load_bundle(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


__all__ = [
    "BUNDLE_SCHEMA",
    "CONTENT_NOT_RECOMPUTED",
    "INFO",
    "VALID_ANCHOR_KINDS",
    "BundleVerifyResult",
    "anchor_record",
    "export_bundle",
    "verify_bundle",
    "write_bundle",
    "load_bundle",
]

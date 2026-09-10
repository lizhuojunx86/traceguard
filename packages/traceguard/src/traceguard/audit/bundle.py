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
    GENESIS_PREV_HASH,
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
#: Bundle-level (see docs/specs/evidence-bundle.md §3): an anchor is present
#: but covers a chain position this bundle does not carry, so it corroborates
#: nothing about the entries inside.
ANCHOR_UNLINKED = "anchor_unlinked"
#: Bundle-level: an anchor that names a chain position this bundle does not
#: carry and is not the declared head. Nothing is compared — an honest anchor
#: exported before or after this window is NOT evidence of a rewrite.
ANCHOR_OUTSIDE_WINDOW = "anchor_outside_window"
#: Bundle-level: the entries carried are not consecutive in the chain, so
#: linkage cannot connect the runs on either side of the gap.
CHAIN_GAP = "chain_gap"
#: Bundle-level: an entry whose canonicalization failed at write time. The
#: chain hashed an error placeholder, so recomputing it proves the placeholder
#: is intact and says nothing about the trace content.
CONTENT_UNATTESTED = "content_unattested"
#: Bundle-level: fields and tables the bundle carries that no hash covers.
CARRIED_UNATTESTED = "carried_unattested"

#: traces columns carried in a bundle. The content subset is what hash_only
#: strips; everything else is metadata a recipient needs to make sense of the
#: rows (and cost_usd, which was never in the envelope anyway).
_TRACE_CONTENT_ONLY: tuple[str, ...] = ("input_summary", "output_parsed", "error_message")
#: Carried for the recipient's benefit but OUTSIDE the algo v1 hash envelope,
#: so recomputation says nothing about them and they can be edited under a
#: passing verify. Named in the `carried_unattested` finding rather than left
#: for the reader to work out by diffing two tuples.
_TRACE_FIELDS_OUTSIDE_ENVELOPE: tuple[str, ...] = (
    "agent_id",
    "session_id",
    "provider_response_id",
    "cost_usd",
)

_TRACE_EXPORT_FIELDS: tuple[str, ...] = TRACE_CONTENT_FIELDS + _TRACE_FIELDS_OUTSIDE_ENVELOPE

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
    #: anchors compared against an entry CARRIED IN THIS BUNDLE, not merely
    #: against the bundle's own ``chain.head`` field. Only these tie the
    #: included entries to something the bundle's author did not write.
    anchors_binding: int = 0
    #: anchors that name a chain position this bundle can neither bind nor
    #: place against its head. They were NOT compared to anything.
    anchors_outside_window: int = 0
    #: entries whose seq run is unbroken. A sparse selection (``--trace-ids``
    #: picking non-adjacent traces) is legitimate but cannot be linkage-checked
    #: across its gaps.
    contiguous: bool = True
    #: entries chained over a canonicalization error rather than over content.
    content_unattested: int = 0
    findings: list[ChainFinding] = field(default_factory=list)

    @property
    def breaks(self) -> list[ChainFinding]:
        return [f for f in self.findings if f.severity == BREAK]

    def summary(self) -> str:
        """Deliberately different wording per mode.

        A ``hash_only`` pass has checked linkage and anchors and NOTHING about
        content; saying "verified" for both would be the format's worst
        failure mode in one word. The same applies to a `full` bundle whose
        entries no anchor covers — it is self-consistent, which a rewrite is
        too, so it says INTERNALLY CONSISTENT rather than VERIFIED.
        """
        if not self.ok:
            return (
                f"bundle FAILED ({self.content_mode}): {len(self.breaks)} break(s) over "
                f"{self.entries_checked} chain entry/entries"
            )
        if self.content_mode == "hash_only":
            return (
                f"bundle LINKAGE OK (hash_only): {self.entries_checked} entry/entries form "
                f"an unbroken chain and {self._anchor_phrase()} — "
                "content was NOT recomputed and is NOT covered by this result"
            )
        # The same rule that separates `full` from `hash_only` separates an
        # anchor-bound bundle from a merely self-consistent one: a rewrite that
        # re-chains the segment reproduces every internal hash, so "VERIFIED"
        # is a claim only an anchor covering these entries can support.
        verdict = "VERIFIED" if (self.anchors_binding and self.contiguous) else (
            "INTERNALLY CONSISTENT"
        )
        return (
            f"bundle {verdict} (full): {self.entries_checked} entry/entries recomputed, "
            f"{self.content_recomputed} against included trace content, "
            f"{self._anchor_phrase()}"
        )

    def _anchor_phrase(self) -> str:
        """Never let an anchor that binds nothing read as corroboration.

        ``chain.head`` is a field of the bundle, so "the anchor matches the
        head" says only that two numbers the author wrote agree. It becomes
        evidence about the ENTRIES only when the anchor is compared against an
        entry the bundle carries — otherwise a self-consistent rewrite of the
        segment leaves both untouched.
        """
        if self.anchors_binding and not self.contiguous:
            return (
                f"{self.anchors_binding} anchor(s) bind part of this selection, which has "
                "gaps — an anchor covers only the unbroken run it sits in"
            )
        if self.anchors_binding:
            return f"{self.anchors_binding} anchor(s) bind these entries"
        compared_to_head = self.anchors_checked - self.anchors_outside_window
        if compared_to_head > 0:
            return (
                f"{compared_to_head} anchor(s) match the bundle's declared head, but NONE "
                "covers the entries carried here — no external corroboration of this content"
            )
        if self.anchors_outside_window:
            # Never "match": these were compared to nothing at all, and saying
            # otherwise is the same overclaim as the head comparison, one step
            # further from any evidence.
            return (
                f"{self.anchors_outside_window} anchor(s) present, none comparable to this "
                "window — no external corroboration of this content"
            )
        return "no anchor covers these entries — internal consistency only"


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
        # Not private and not optional: a cost_event chain entry hashes the
        # event row, so `full` verification cannot recompute those entries
        # without them. It is part of the published schema for that reason.
        "cost_events": [
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


def _shrank(anchor: dict, head: dict, anchor_count: Any, head_count: Any) -> bool:
    """Did the chain lose entries between this anchor and this export?

    Only when the anchor is known to be at least as old as the export: an
    anchor taken AFTER it legitimately counts more. Missing counts mean no
    signal, not a silent pass to a stricter verdict — unlike a missing
    ``exported_at`` next to a seq that already went backwards, there is nothing
    here to be conservative about.
    """
    if not isinstance(anchor_count, int) or isinstance(anchor_count, bool):
        return False
    if not isinstance(head_count, int) or isinstance(head_count, bool):
        return False
    if anchor_count <= head_count:
        return False
    anchor_at = _parse_dt(anchor.get("exported_at"))
    head_at = _parse_dt(head.get("exported_at"))
    if anchor_at is not None and head_at is not None and anchor_at > head_at:
        return False  # the anchor is newer; counting more is expected
    return True


def _check_anchors(
    bundle: dict, head: dict, entries: list[dict]
) -> tuple[int, int, int, list[ChainFinding]]:
    """Structure-check every anchor and compare it against what it can be placed against.

    Returns ``(checked, binding, outside_window, findings)``. An anchor names a
    chain position (``seq``), and where that position falls decides what — if
    anything — the anchor can be compared to here:

    1. **inside the window** (``seq`` names an entry carried here) → compared
       against THAT entry. The only comparison that constrains the bundle's
       contents, and what ``binding`` counts; a difference is a real
       ``anchor_mismatch`` (BREAK).
    2. **at the declared head** (or an old anchor carrying no ``seq``) →
       compared against ``chain.head``. That constrains nothing — the head is a
       field of the bundle, so a rewrite that re-chains the entries leaves both
       it and the anchor untouched — but a mismatch there is still a real
       BREAK: it says the head does not follow from the anchored position.
    3. **before the window** (or in one of a sparse selection's gaps, or between
       the last entry and the head) → NOT compared. This is the ordinary shape
       of an evidence export: an anchor taken at seq 3 has nothing to say about
       entries 4..6, and comparing its digest to the head produced
       ``anchor_mismatch`` — the tool accusing a truthful anchor of proving a
       rewrite. ``anchor_outside_window`` (WARN) instead.
    4. **after the head** → also not compared, and its own finding: the anchor
       is newer than this export, so the export should be redone.
    5. **no head declared** → nothing to place a non-window anchor against, so
       it falls to case 3 with that named as the reason.

    No signature is ever verified (spec §4): a structurally valid RFC 3161
    token is reported as present, not as valid.
    """
    findings: list[ChainFinding] = []
    by_seq = {e.get("seq"): e.get("row_hash") for e in entries if e.get("seq") is not None}
    entry_seqs = sorted(by_seq)
    head_hash = head.get("row_hash")
    head_seq = head.get("seq")
    if not isinstance(head_seq, int) or isinstance(head_seq, bool):
        head_seq = None
    checked = 0
    binding = 0
    outside_window = 0
    head_compared = 0
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
        seq = anchor.get("seq")
        if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool)):
            # Report a hand-edited file rather than raising out of verify.
            findings.append(
                ChainFinding(
                    "anchor_malformed", BREAK, None, None,
                    f"anchors[{i}] has seq {seq!r}, which is not an integer chain position",
                )
            )
            continue
        checked += 1

        # (0) the chain SHRANK. entry_count only grows on an append-only chain,
        # so an anchor that counted more entries than this export found means
        # rows were removed — and unlike seq, this survives a MID-chain
        # deletion, which leaves both the tip seq and the head row_hash intact.
        # Without it that deletion reads as a benign `chain_gap`: verify_chain
        # fails on the database while verify_bundle passes on its export.
        # Checked before placement because it is true wherever the anchor sits.
        # When the seq ALSO went backwards, branch 4 names the position and
        # gives the better diagnosis; this is the check that catches what seq
        # cannot see.
        anchor_count = anchor.get("entry_count")
        head_count = head.get("entry_count")
        seq_went_backwards = head_seq is not None and seq is not None and seq > head_seq
        if not seq_went_backwards and _shrank(anchor, head, anchor_count, head_count):
            findings.append(
                ChainFinding(
                    "anchor_mismatch", BREAK, seq, None,
                    f"anchors[{i}] counted {anchor_count} chain entries, but this bundle's "
                    f"head declares only {head_count}. Entry count only grows on an "
                    "append-only chain, so entries present when the anchor was taken are "
                    "gone — a deletion, which a mid-chain removal hides from both the tip "
                    "seq and the head hash",
                )
            )
            continue

        # (1) inside the window: the only comparison that constrains the entries.
        if seq in by_seq:
            if row_hash != by_seq[seq]:
                findings.append(
                    ChainFinding(
                        "anchor_mismatch", BREAK, seq, None,
                        f"anchors[{i}] attests row_hash {row_hash} at seq {seq}, but the entry "
                        f"carried here at that seq hashes to {by_seq[seq]}; the content covered "
                        "by that entry was changed after it was anchored",
                    )
                )
            else:
                binding += 1
            continue

        # (2) at the declared head, or an old anchor that carries no seq.
        # `head_hash is not None` is part of the CONDITION, not a check inside
        # it: with no head there is nothing to compare against, and counting the
        # comparison anyway made summary() report a match that never happened.
        if head_hash is not None and (seq is None or (head_seq is not None and seq == head_seq)):
            head_compared += 1
            if row_hash != head_hash:
                findings.append(
                    ChainFinding(
                        "anchor_mismatch", BREAK, seq, None,
                        f"anchors[{i}] row_hash {row_hash} != the bundle's chain head "
                        f"{head_hash}; the chain was truncated or rewritten relative to "
                        "this anchor",
                    )
                )
            continue

        # (4) past the declared head. Which of two opposite things this means
        # is decided by WHEN the anchor was taken, and both timestamps are in
        # the bundle already.
        if head_seq is not None and seq > head_seq:
            anchor_at = _parse_dt(anchor.get("exported_at"))
            head_at = _parse_dt(head.get("exported_at"))
            if anchor_at is None or head_at is None or anchor_at <= head_at:
                # The anchor existed at or before this export, and it attests a
                # HIGHER position than the export found. On an append-only chain
                # seq only grows, so the tip moved BACKWARDS: rows below an
                # anchored position are gone. That is truncation or rollback —
                # the thing the chain exists to catch, and what `verify_chain
                # --anchor-file` reports as a BREAK on the same database and the
                # same anchor. Reporting it as a warning here would leave the
                # two shipped tools contradicting each other on identical input,
                # in the direction SPEC B3.4 calls the dangerous one.
                #
                # Unknown timestamps are treated as the dangerous case on
                # purpose: `anchor_record` and `export_bundle` both always write
                # `exported_at`, so a bundle missing them is hand-edited, and
                # stripping a field must not downgrade a truncation to a warning.
                unknown = anchor_at is None or head_at is None
                findings.append(
                    ChainFinding(
                        "anchor_mismatch", BREAK, seq, None,
                        f"anchors[{i}] attests chain position seq {seq}, but this bundle's "
                        f"head is only seq {head_seq}, and the anchor "
                        + (
                            "carries no exported_at to rule out that it predates the export"
                            if unknown
                            else f"was exported at {anchor_at.isoformat()}, at or before this "
                            f"bundle's head ({head_at.isoformat()})"
                        )
                        + ". The chain reached seq "
                        f"{seq} and this export found a LOWER tip: on an append-only chain "
                        "that means entries below an anchored position were removed — "
                        "truncation or rollback, not a stale anchor",
                    )
                )
                continue
            outside_window += 1
            findings.append(
                ChainFinding(
                    ANCHOR_OUTSIDE_WINDOW, WARN, seq, None,
                    f"anchors[{i}] is at seq {seq}, LATER than this bundle's declared head "
                    f"(seq {head_seq}), and was exported at {anchor_at.isoformat()}, AFTER "
                    f"this bundle's head ({head_at.isoformat()}): the chain simply advanced "
                    "after the export, so nothing here can be compared to it. Re-export the "
                    f"bundle from a database that has reached seq {seq}",
                )
            )
            continue

        outside_window += 1

        # (3)/(5) before the window, inside a gap, between the last entry and
        # the head, or nowhere placeable because no head was declared.
        if head_seq is None:
            where = (
                "and this bundle declares no chain head, so there is nothing to place it "
                "against"
            )
        elif entry_seqs and seq < entry_seqs[0]:
            where = (
                f"which is BEFORE the first entry carried here (seq {entry_seqs[0]}) — the "
                "ordinary shape of an evidence export taken after the anchor"
            )
        elif entry_seqs and seq > entry_seqs[-1]:
            where = (
                f"which falls between the last entry carried here (seq {entry_seqs[-1]}) and "
                f"the declared head (seq {head_seq})"
            )
        elif not entry_seqs:
            where = "and this bundle carries no entries at all for it to cover"
        else:
            where = "which falls in a gap in this bundle's entries"
        at = f"is at seq {seq}" if seq is not None else "carries no seq"
        findings.append(
            ChainFinding(
                ANCHOR_OUTSIDE_WINDOW, WARN, seq, None,
                f"anchors[{i}] {at}, {where}. It was NOT compared to anything: an "
                "anchor for a chain position this bundle does not carry says nothing about "
                "these entries, and comparing its digest to the head would report a truthful "
                "anchor as proof of a rewrite. For corroboration, "
                + (
                    f"export a window that reaches seq {seq}, or "
                    if seq is not None
                    else ""
                )
                + "anchor again while this window is the chain tip",
            )
        )

    if entries and head_compared and not binding:
        seqs = [e.get("seq") for e in entries if e.get("seq") is not None]
        span = f"seq {seqs[0]}..{seqs[-1]}" if seqs else "the entries carried here"
        findings.append(
            ChainFinding(
                ANCHOR_UNLINKED, WARN, None, None,
                f"{head_compared} anchor(s) were compared against this bundle's declared "
                f"head and none names a seq it carries "
                f"({span}); each was compared only against the bundle's own `chain.head` "
                "field. That comparison is between two values the bundle's author wrote, so "
                "it does NOT corroborate the entries here: rewriting their content and "
                "re-chaining the segment leaves both the head and the anchor untouched. To "
                "get corroboration, export a window that reaches the anchored seq, or anchor "
                "again while this window is the chain tip",
            )
        )
    return checked, binding, outside_window, findings


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
    cost_events = {e["event_id"]: e for e in (bundle.get("cost_events") or [])}

    result = BundleVerifyResult(
        ok=True, content_mode=content_mode, entries_checked=len(entries),
        traces_included=len(traces),
    )

    # ── linkage: checked in BOTH modes; it is all hash_only can offer ──
    # A segment that claims to start at the beginning must actually seed from
    # the genesis constant. Without this, `entries[0]["prev_hash"]` is a free
    # value and a whole re-chained history verifies.
    if entries and entries[0].get("seq") == 1 and entries[0].get("prev_hash") != GENESIS_PREV_HASH:
        result.findings.append(
            ChainFinding(
                "link_broken", BREAK, 1, entries[0].get("trace_id"),
                f"the first entry is seq 1 but its prev_hash is {entries[0].get('prev_hash')!r}, "
                f"not the genesis {GENESIS_PREV_HASH}; this chain does not start where it says",
            )
        )

    # Only CHAIN-ADJACENT entries can be linkage-checked. A bundle selected by
    # trace_ids is routinely sparse, and comparing across a gap reported the
    # export itself as tampering — the tool calling its own output evidence of
    # a break. Gaps are named instead, because "these two entries are not
    # linked to each other" is the true statement.
    gaps: list[str] = []
    prev_hash: str | None = None
    prev_seq: int | None = None
    for entry in entries:
        seq = entry.get("seq")
        adjacent = prev_seq is not None and seq is not None and seq == prev_seq + 1
        if prev_hash is not None and adjacent and entry["prev_hash"] != prev_hash:
            result.findings.append(
                ChainFinding(
                    "link_broken", BREAK, seq, entry.get("trace_id"),
                    f"prev_hash {entry['prev_hash']} != the previous entry's row_hash "
                    f"{prev_hash}",
                )
            )
        elif prev_seq is not None and not adjacent:
            gaps.append(f"{prev_seq}->{seq}")
        prev_hash = entry["row_hash"]
        prev_seq = seq

    if gaps:
        result.contiguous = False
        result.findings.append(
            ChainFinding(
                CHAIN_GAP, WARN, None, None,
                f"the entries carried here are not consecutive in the chain ({len(gaps)} "
                f"gap(s): {', '.join(gaps)}), which is what selecting individual traces "
                "produces. Linkage was checked only WITHIN each unbroken run: entries on "
                "either side of a gap are not tied to each other, and an anchor covering "
                "one run says nothing about another. Export a contiguous window if the "
                "recipient needs the runs connected",
            )
        )

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
                if entry["canon_status"] == "failed":
                    # The chain hashed an error placeholder because the content
                    # could not be canonicalized at write time. Recomputing it
                    # proves the placeholder is intact; the trace content it
                    # stands in for was never covered by any hash.
                    result.content_unattested += 1
                else:
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
    unattested: list[str] = [
        f"trace fields outside the algo v1 envelope ({', '.join(_TRACE_FIELDS_OUTSIDE_ENVELOPE)})"
    ]
    if bundle.get("source_snapshots"):
        unattested.append(
            f"{len(bundle['source_snapshots'])} source_snapshots row(s) — "
            "traceguard.sources is not chained at all (SPEC v1.2 D9)"
        )
    if bundle.get("approvals"):
        unattested.append(f"{len(bundle['approvals'])} approvals row(s)")
    result.findings.append(
        ChainFinding(
            CARRIED_UNATTESTED, INFO, None, None,
            "this bundle carries data that NO hash covers, and a passing verify says "
            "nothing about it: " + "; ".join(unattested) + ". Editing any of it leaves "
            "every check in this result green",
        )
    )

    if result.content_unattested:
        result.findings.append(
            ChainFinding(
                CONTENT_UNATTESTED, WARN, None, None,
                f"{result.content_unattested} entry/entries were chained over a "
                "canonicalization ERROR, not over trace content: at write time the content "
                "could not be canonicalized, so the hash covers an error placeholder. Those "
                "entries recompute correctly and still attest nothing about what the trace "
                "said — they are excluded from the recomputed-against-content count",
            )
        )

    checked, binding, outside, anchor_findings = _check_anchors(
        bundle, chain.get("head") or {}, entries
    )
    result.anchors_checked = checked
    result.anchors_binding = binding
    result.anchors_outside_window = outside
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

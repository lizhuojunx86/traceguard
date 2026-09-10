"""Which sources rewrote what they had already served (A5).

The generalization of ``analysis/eps_revision.py``: group snapshots by
``source_uri``, order by ``retrieved_at``, and read off the sequence of
``content_hash`` values. A source whose hash changed between two retrievals
served two different things under one identifier — which is the whole
phenomenon the sources extension exists to make visible.

Counting discipline, inherited from that script and non-negotiable:

- **A snapshot with verdict ``unchecked`` is not an observation.** Nothing was
  compared, so it cannot support or refute anything. Counting it would let
  un-instrumented call sites dilute the rate toward zero — the direction that
  makes the report say "no problem here" precisely where nobody looked.
  ``unverifiable`` IS counted: the retrieval happened and the bytes were
  digested; only the *timing* claim was unprovable, and drift is about the
  bytes.
- **...but "not an observation" is not "did not happen".** An ``unchecked``
  retrieval still digested bytes, and the bytes are what drift is about. It
  therefore stays in the source's DIGEST SEQUENCE while staying out of the
  rate's denominator. Dropping it from the sequence was worse than a miscount:
  deleting an element can only ever LOWER the adjacent-pair change count
  (``[a≠b] ≤ [a≠e] + [e≠b]``), so ``a → b(unchecked) → a`` reported zero
  changes for a source that demonstrably served two different byte-sets and
  changed back. The bias ran in exactly the direction the first bullet exists
  to prevent.
- **A source seen once cannot have drifted.** It contributes to neither the
  numerator nor the denominator — one observation is not a comparison. Mixing
  single-retrieval sources into the denominator is the standard way to
  manufacture a reassuringly small percentage.
- **A change seen without two observations is still reported**, just not in the
  rate: ``sources_changed_uncomparable``. Silence there would re-open the hole
  the second bullet closes, by a different door.
- The proportion is reported with its **n and a Wilson 95% interval**, never
  bare.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard.sources.models import SourceSnapshotRow
from traceguard.sources.stats import wilson_interval
from traceguard.sources.validate import SourceVerdict

#: Verdicts whose snapshots count as observations of the source's CONTENT.
#: `unchecked` is excluded — see the module docstring.
OBSERVED_VERDICTS: frozenset[str] = frozenset(
    {
        SourceVerdict.VERIFIED.value,
        SourceVerdict.ANACHRONISTIC.value,
        SourceVerdict.UNVERIFIABLE.value,
    }
)


@dataclass(frozen=True)
class Retrieval:
    """One observation of a source: when, what digest, under which verdict."""

    retrieved_at: datetime
    content_hash: str
    verdict: str
    trace_id: int


@dataclass
class SourceDrift:
    """The retrieval history of one ``source_uri``, and what changed in it."""

    source_uri: str
    retrievals: list[Retrieval] = field(default_factory=list)

    @property
    def observations(self) -> int:
        """Retrievals that count toward the RATE: everything but ``unchecked``.

        Deliberately not ``len(self.retrievals)`` — the sequence carries the
        unchecked rows too, because they hold real digests (see the module
        docstring), but they are not observations.
        """
        return sum(1 for r in self.retrievals if r.verdict != SourceVerdict.UNCHECKED.value)

    @property
    def unchecked_in_sequence(self) -> int:
        """Rows in the digest sequence that are not observations."""
        return len(self.retrievals) - self.observations

    @property
    def distinct_hashes(self) -> int:
        return len({r.content_hash for r in self.retrievals})

    @property
    def changes(self) -> int:
        """Adjacent-pair digest changes, in ``retrieved_at`` order.

        Counted on ADJACENT pairs rather than as ``distinct_hashes - 1``, so a
        source that flips A → B → A is reported as two changes, not one. That
        flip is a real event (and a more alarming one than a single revision);
        collapsing it would hide it.
        """
        return sum(
            1
            for a, b in zip(self.retrievals, self.retrievals[1:])
            if a.content_hash != b.content_hash
        )

    @property
    def comparable(self) -> bool:
        """True when this source was observed at least twice.

        One retrieval is not a comparison: such a source can neither have
        drifted nor be shown not to have, so it belongs in neither side of the
        rate.
        """
        return self.observations >= 2

    @property
    def drifted(self) -> bool:
        return self.comparable and self.changes > 0

    @property
    def changed_uncomparable(self) -> bool:
        """The digests changed, but there are fewer than two observations.

        Real and reportable, and outside the rate: it cannot go in the
        numerator without a denominator it has not earned.
        """
        return self.changes > 0 and not self.comparable

    @property
    def first_seen(self) -> datetime | None:
        return self.retrievals[0].retrieved_at if self.retrievals else None

    @property
    def last_seen(self) -> datetime | None:
        return self.retrievals[-1].retrieved_at if self.retrievals else None


@dataclass
class DriftReport:
    """Sources grouped and counted, with the excluded populations named.

    ``sources_total`` is every source that produced a snapshot;
    ``sources_comparable`` is the denominator of ``drift_rate``. They differ by
    ``sources_single_observation``, which is reported rather than silently
    folded away — as is ``sources_changed_uncomparable``, the sources that
    changed without earning a place in the denominator.
    """

    sources: list[SourceDrift] = field(default_factory=list)
    snapshots_scanned: int = 0
    snapshots_unchecked: int = 0
    since: datetime | None = None
    source_uri_pattern: str | None = None

    @property
    def sources_total(self) -> int:
        return len(self.sources)

    @property
    def comparable_sources(self) -> list[SourceDrift]:
        return [s for s in self.sources if s.comparable]

    @property
    def sources_comparable(self) -> int:
        return len(self.comparable_sources)

    @property
    def sources_single_observation(self) -> int:
        return self.sources_total - self.sources_comparable

    @property
    def sources_drifted(self) -> int:
        return sum(1 for s in self.sources if s.drifted)

    @property
    def sources_changed_uncomparable(self) -> int:
        """Sources whose digests changed with fewer than two observations.

        Outside the rate and never silent: this is where a source retrieved
        only through un-instrumented call sites shows up, and it is exactly the
        population a rate would otherwise hide.
        """
        return sum(1 for s in self.sources if s.changed_uncomparable)

    @property
    def drift_rate(self) -> float | None:
        """Share of *comparable* sources that changed at least once.

        ``None`` when nothing is comparable — a rate over an empty denominator
        is not 0.0, and printing 0.0 would claim a measurement that was never
        made.
        """
        n = self.sources_comparable
        return self.sources_drifted / n if n else None

    @property
    def drift_rate_ci(self) -> tuple[float, float]:
        return wilson_interval(self.sources_drifted, self.sources_comparable)

    @property
    def total_changes(self) -> int:
        return sum(s.changes for s in self.sources)

    def summary(self) -> str:
        rate = self.drift_rate
        if rate is None:
            return (
                f"no comparable source yet: {self.sources_total} source(s), none "
                f"retrieved twice ({self.snapshots_scanned} snapshot(s) scanned, "
                f"{self.snapshots_unchecked} unchecked and not counted)"
                + self._uncomparable_clause()
                + self._scope_clause()
            )
        low, high = self.drift_rate_ci
        return (
            f"{self.sources_drifted}/{self.sources_comparable} comparable source(s) "
            f"changed content at least once = {rate:.1%} "
            f"(Wilson 95% CI {low:.1%}–{high:.1%}); {self.total_changes} change(s) "
            f"over {self.snapshots_scanned} snapshot(s). Excluded: "
            f"{self.sources_single_observation} source(s) retrieved only once, "
            f"{self.snapshots_unchecked} unchecked snapshot(s)."
            + self._uncomparable_clause()
            + self._scope_clause()
        )

    def _uncomparable_clause(self) -> str:
        n = self.sources_changed_uncomparable
        if not n:
            return ""
        return (
            f" {n} source(s) changed digest with fewer than two observations — real, "
            "and outside the rate."
        )

    def _scope_clause(self) -> str:
        """Every sentence above is about the FILTERED population, so say so.

        Without this the same words describe "no source drifted" and "no source
        drifted in the last six hours", and only one of them is reassuring.
        """
        scope = []
        if self.since is not None:
            scope.append(f"retrieved_at >= {self.since.isoformat()}")
        if self.source_uri_pattern is not None:
            scope.append(f"source_uri LIKE {self.source_uri_pattern!r}")
        if not scope:
            return ""
        return " Scope: " + "; ".join(scope) + "."


def compute_drift(
    engine: Engine,
    *,
    since: datetime | None = None,
    source_uri: str | None = None,
) -> DriftReport:
    """Group snapshots by ``source_uri`` and read off each digest sequence.

    ``since`` filters on ``retrieved_at``; ``source_uri`` is a SQL LIKE
    pattern. Snapshots whose verdict is ``unchecked`` are counted in
    ``snapshots_unchecked`` and kept OUT of the rate's denominator, but stay in
    each source's digest sequence — they carry real digests (module docstring).

    Ordering is ``(retrieved_at, snapshot_id)``: two retrievals can share a
    timestamp at the DB's resolution, and insertion order is then the only
    tiebreak that reflects what actually happened. Without it the change count
    would depend on how SQLite happened to return the rows.
    """
    report = DriftReport(since=since, source_uri_pattern=source_uri)
    stmt = select(SourceSnapshotRow).order_by(
        SourceSnapshotRow.retrieved_at.asc(), SourceSnapshotRow.snapshot_id.asc()
    )
    if since is not None:
        stmt = stmt.where(SourceSnapshotRow.retrieved_at >= since)
    if source_uri is not None:
        stmt = stmt.where(SourceSnapshotRow.source_uri.like(source_uri))

    grouped: dict[str, SourceDrift] = {}
    with Session(engine) as sess:
        for row in sess.scalars(stmt):
            report.snapshots_scanned += 1
            if row.verdict == SourceVerdict.UNCHECKED.value:
                report.snapshots_unchecked += 1
            entry = grouped.setdefault(row.source_uri, SourceDrift(row.source_uri))
            entry.retrievals.append(
                Retrieval(
                    retrieved_at=row.retrieved_at,
                    content_hash=row.content_hash,
                    verdict=row.verdict,
                    trace_id=row.trace_id,
                )
            )

    report.sources = sorted(grouped.values(), key=lambda s: s.source_uri)
    return report


def drift_to_dict(report: DriftReport) -> dict:
    """JSON-ready view with a stable key order (so `--json` output diffs cleanly)."""
    rate = report.drift_rate
    low, high = report.drift_rate_ci
    return {
        "since": report.since.isoformat() if report.since else None,
        "source_uri_pattern": report.source_uri_pattern,
        "snapshots_scanned": report.snapshots_scanned,
        "snapshots_unchecked": report.snapshots_unchecked,
        "sources_total": report.sources_total,
        "sources_comparable": report.sources_comparable,
        "sources_single_observation": report.sources_single_observation,
        "sources_drifted": report.sources_drifted,
        "sources_changed_uncomparable": report.sources_changed_uncomparable,
        "total_changes": report.total_changes,
        "drift_rate": rate,
        "drift_rate_ci": [low, high] if rate is not None else None,
        "sources": [
            {
                "source_uri": s.source_uri,
                "observations": s.observations,
                "distinct_hashes": s.distinct_hashes,
                "changes": s.changes,
                "unchecked_in_sequence": s.unchecked_in_sequence,
                "comparable": s.comparable,
                "drifted": s.drifted,
                "changed_uncomparable": s.changed_uncomparable,
                "first_seen": s.first_seen.isoformat() if s.first_seen else None,
                "last_seen": s.last_seen.isoformat() if s.last_seen else None,
                "sequence": [
                    {
                        "retrieved_at": r.retrieved_at.isoformat(),
                        "content_hash": r.content_hash,
                        "verdict": r.verdict,
                        "trace_id": r.trace_id,
                    }
                    for r in s.retrievals
                ],
            }
            for s in report.sources
        ],
    }

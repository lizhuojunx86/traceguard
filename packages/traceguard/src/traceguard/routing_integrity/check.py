"""Classify how much a trace's invariant-2 result is actually worth."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Iterator, Sequence

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard.store.models import ModelRegistryEntry, Trace


class Verdict(str, Enum):
    """What invariant 2 on this trace is worth.

    Ordered worst to best so ``max``/sorting behave sensibly in reports.
    """

    #: Requested a routing alias and the gateway never said who served it.
    #: The timeline underneath this trace is unknowable, not merely unchecked.
    UNVERIFIABLE = "unverifiable"
    #: The call itself failed, so no model served it and none was ever going to.
    #: Not an integrity problem — an operational one — and kept out of the
    #: actionable count so a bad afternoon of 401s cannot masquerade as a
    #: timeline defect.
    FAILED_CALL = "failed_call"
    #: A concrete model served the call, but it is absent from model_registry,
    #: so invariant 2 has no timestamps to compare against.
    UNREGISTERED = "unregistered"
    #: Requested and served differ. Invariant 2 ran against the requested name;
    #: the served model is the one that needs checking.
    DIVERGED = "diverged"
    #: Requested == served, or the wrapper predates routing capture and the
    #: model is registered. Invariant 2 checked a real model.
    VERIFIED = "verified"


@dataclass(frozen=True)
class Finding:
    """One trace's verdict, with the identifiers needed to act on it."""

    trace_id: int | None
    verdict: Verdict
    requested_model: str | None
    served_model: str | None
    feature_as_of: datetime | None
    detail: str

    @property
    def actionable(self) -> bool:
        """True when this trace should not be trusted for a point-in-time claim.

        A failed call is not in that set: it produced no result, so there is no
        result to distrust. Counting it would let an expired key inflate the
        integrity report, which is the fastest way to teach someone to ignore it.
        """
        return self.verdict not in (Verdict.VERIFIED, Verdict.FAILED_CALL)


def _registered(model_id: str | None, engine: Engine) -> bool:
    if not model_id:
        return False
    with Session(engine) as sess:
        return sess.get(ModelRegistryEntry, model_id) is not None


def classify(
    routing: dict[str, Any] | None,
    *,
    engine: Engine,
    model_id: str | None = None,
) -> tuple[Verdict, str]:
    """Classify one call from its ``output_parsed["routing"]`` blob.

    ``model_id`` is the trace's SPEC §3.1 field, used as the fallback for traces
    written before routing capture existed. Returns the verdict and a sentence
    explaining it, suitable for putting straight into a report or an assertion
    message.
    """
    if not routing:
        # Pre-1.5 trace, or a hand-opened span. We know only the requested id.
        if _registered(model_id, engine):
            return (
                Verdict.VERIFIED,
                f"no routing record; {model_id!r} is registered and was checked as-is",
            )
        return (
            Verdict.UNREGISTERED,
            f"no routing record and {model_id!r} is not in model_registry",
        )

    requested = routing.get("requested_model")
    served = routing.get("served_model")
    is_alias = bool(routing.get("requested_is_alias"))

    if served is None:
        if is_alias:
            return (
                Verdict.UNVERIFIABLE,
                f"asked for alias {requested!r} and the gateway did not report a "
                "served model; nothing here identifies what answered",
            )
        if _registered(requested, engine):
            return (
                Verdict.VERIFIED,
                f"no served model reported, but {requested!r} is a concrete "
                "registered id and was checked as-is",
            )
        return (
            Verdict.UNREGISTERED,
            f"no served model reported and {requested!r} is not in model_registry",
        )

    if not _registered(served, engine):
        return (
            Verdict.UNREGISTERED,
            f"{served!r} served this call but is not in model_registry, so "
            "invariant 2 has no timestamps to compare",
        )

    if served != requested:
        return (
            Verdict.DIVERGED,
            f"requested {requested!r} but {served!r} served it; invariant 2 ran "
            f"against {requested!r} and must be re-run against {served!r}",
        )

    return (Verdict.VERIFIED, f"{served!r} both requested and served")


def classify_trace(trace: Trace, *, engine: Engine) -> Finding:
    """Classify a persisted :class:`~traceguard.store.models.Trace` row."""
    parsed = trace.output_parsed if isinstance(trace.output_parsed, dict) else {}
    routing = parsed.get("routing")
    routing = routing if isinstance(routing, dict) else None

    if trace.error_class:
        # The call never produced a result. Grading its timeline would be
        # grading nothing; say what happened instead.
        return Finding(
            trace_id=trace.trace_id,
            verdict=Verdict.FAILED_CALL,
            requested_model=(routing or {}).get("requested_model") or trace.model_id,
            served_model=None,
            feature_as_of=trace.feature_as_of,
            detail=f"call failed ({trace.error_class}); no model served it",
        )

    verdict, detail = classify(routing, engine=engine, model_id=trace.model_id)
    return Finding(
        trace_id=trace.trace_id,
        verdict=verdict,
        requested_model=(routing or {}).get("requested_model") or trace.model_id,
        served_model=(routing or {}).get("served_model"),
        feature_as_of=trace.feature_as_of,
        detail=detail,
    )


def scan(
    engine: Engine,
    *,
    project: str | None = None,
    only_dated: bool = True,
) -> Iterator[Finding]:
    """Yield a :class:`Finding` per trace, worst cases included.

    ``only_dated`` limits the scan to traces carrying a ``feature_as_of``,
    because a trace with no point-in-time stamp is not making a point-in-time
    claim and invariant 2 never applied to it. Pass ``False`` to audit
    everything.
    """
    stmt = select(Trace)
    if project is not None:
        stmt = stmt.where(Trace.project == project)
    if only_dated:
        stmt = stmt.where(Trace.feature_as_of.is_not(None))
    with Session(engine) as sess:
        for trace in sess.scalars(stmt):
            yield classify_trace(trace, engine=engine)


def summarise(findings: Sequence[Finding]) -> dict[Verdict, int]:
    """Count findings by verdict, including zeroes so reports are stable."""
    counts = {verdict: 0 for verdict in Verdict}
    for finding in findings:
        counts[finding.verdict] += 1
    return counts

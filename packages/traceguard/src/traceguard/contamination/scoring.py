"""Attach contamination scores to a trace without changing the schema (SPEC §6.1)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard.store.models import Trace

# Namespaced key under output_parsed so scores never collide with business output.
CONTAMINATION_KEY = "_traceguard_contamination"


@dataclass(frozen=True)
class ContaminationScore:
    """One contamination estimate attached to a trace."""

    method: str  # e.g. "min_k_prob" | "regime_decay" | "claim_verification"
    value: float
    flagged: bool
    detail: dict[str, Any] | None = None


def attach_contamination_score(
    trace_id: int,
    score: ContaminationScore,
    *,
    engine: Engine,
    key: str = CONTAMINATION_KEY,
) -> dict[str, Any]:
    """Append a contamination score to a trace's ``output_parsed`` JSON.

    Scores live under ``output_parsed[key]`` as a list, so no MUST column is
    added — per SPEC §6.1, contamination scores attach via ``output_parsed``,
    not via new schema. Multiple calls append rather than overwrite.

    Args:
        trace_id: primary key of the trace to annotate.
        score: the contamination estimate to attach.
        engine: SQLAlchemy engine for the trace store.
        key: the ``output_parsed`` key to store scores under.

    Returns:
        The updated ``output_parsed`` dict.

    Raises:
        LookupError: if no trace with ``trace_id`` exists.
    """
    with Session(engine) as sess:
        row = sess.get(Trace, trace_id)
        if row is None:
            raise LookupError(f"no trace with trace_id={trace_id}")
        data = dict(row.output_parsed) if isinstance(row.output_parsed, dict) else {}
        scores = list(data.get(key, []))
        scores.append(asdict(score))
        data[key] = scores
        row.output_parsed = data  # reassign so SQLAlchemy marks the JSON dirty
        sess.commit()
        return data

"""Eval trace writer for persisting Guardian evaluation results.

Provides a simple interface to write evaluation traces to a SQLite
(or any SQLAlchemy-compatible) database.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from guardian.store.models import Base, EvalTrace

logger = logging.getLogger(__name__)


class TraceWriter:
    """Writes Guardian evaluation traces to the database.

    Args:
        database_url: SQLAlchemy database URL (e.g. 'sqlite:///traces.db').
    """

    def __init__(self, database_url: str) -> None:
        self._engine = create_engine(database_url)
        Base.metadata.create_all(self._engine)

    def write(
        self,
        pipeline_name: str,
        step_name: str,
        action: str,
        passed: bool,
        score: float,
        issues: list[str],
        attempt: int,
        output_preview: str | None = None,
    ) -> EvalTrace:
        """Write a single evaluation trace to the database.

        Args:
            pipeline_name: Name of the pipeline.
            step_name: Name of the step evaluated.
            action: Decision taken (pass, retry, abort, etc.).
            passed: Whether all checks passed.
            score: Quality score (0.0 to 1.0).
            issues: List of issue descriptions.
            attempt: Current attempt number.
            output_preview: Optional truncated output preview.

        Returns:
            The persisted EvalTrace instance.
        """
        trace = EvalTrace(
            pipeline_name=pipeline_name,
            step_name=step_name,
            action=action,
            passed=passed,
            score=score,
            issues=json.dumps(issues),
            attempt=attempt,
            output_preview=output_preview,
        )

        with Session(self._engine) as session:
            session.add(trace)
            session.commit()
            session.refresh(trace)
            logger.info(
                "Trace written: pipeline=%s step=%s action=%s score=%.2f",
                pipeline_name,
                step_name,
                action,
                score,
            )

        return trace

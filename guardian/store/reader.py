"""Eval trace reader for querying historical Guardian evaluation results.

Provides query methods for traces, aggregations, and pipeline metadata.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, distinct, func, select
from sqlalchemy.orm import Session

from guardian.store.models import Base, EvalTrace

logger = logging.getLogger(__name__)


class TraceReader:
    """Reads Guardian evaluation traces from the database.

    Args:
        database_url: SQLAlchemy database URL.
    """

    def __init__(self, database_url: str) -> None:
        self._engine = create_engine(database_url)
        Base.metadata.create_all(self._engine)

    def list_pipelines(self) -> list[dict]:
        """List all pipelines that have recorded traces.

        Returns:
            List of dicts with pipeline_name, step_count, trace_count,
            latest_trace timestamp.
        """
        with Session(self._engine) as session:
            rows = session.execute(
                select(
                    EvalTrace.pipeline_name,
                    func.count(distinct(EvalTrace.step_name)).label("step_count"),
                    func.count(EvalTrace.id).label("trace_count"),
                    func.max(EvalTrace.created_at).label("latest_trace"),
                ).group_by(EvalTrace.pipeline_name)
            ).all()

            return [
                {
                    "pipeline_name": r.pipeline_name,
                    "step_count": r.step_count,
                    "trace_count": r.trace_count,
                    "latest_trace": r.latest_trace.isoformat() if r.latest_trace else None,
                }
                for r in rows
            ]

    def query_traces(
        self,
        pipeline_name: str | None = None,
        step_name: str | None = None,
        days: int = 7,
        limit: int = 100,
    ) -> list[dict]:
        """Query traces with optional filters.

        Args:
            pipeline_name: Filter by pipeline name.
            step_name: Filter by step name.
            days: Number of days to look back.
            limit: Maximum number of traces to return.

        Returns:
            List of trace dicts ordered by created_at descending.
        """
        since = datetime.now(timezone.utc) - timedelta(days=days)

        with Session(self._engine) as session:
            stmt = select(EvalTrace).where(EvalTrace.created_at >= since)

            if pipeline_name:
                stmt = stmt.where(EvalTrace.pipeline_name == pipeline_name)
            if step_name:
                stmt = stmt.where(EvalTrace.step_name == step_name)

            stmt = stmt.order_by(EvalTrace.created_at.desc()).limit(limit)
            traces = session.execute(stmt).scalars().all()

            return [self._trace_to_dict(t) for t in traces]

    def get_step_stats(
        self,
        pipeline_name: str,
        step_name: str,
        days: int = 7,
    ) -> dict:
        """Get aggregated statistics for a specific step.

        Args:
            pipeline_name: Pipeline name.
            step_name: Step name.
            days: Number of days to look back.

        Returns:
            Dict with count, pass_rate, avg_score, action breakdown.
        """
        since = datetime.now(timezone.utc) - timedelta(days=days)

        with Session(self._engine) as session:
            base_filter = [
                EvalTrace.pipeline_name == pipeline_name,
                EvalTrace.step_name == step_name,
                EvalTrace.created_at >= since,
            ]

            total = session.execute(
                select(func.count(EvalTrace.id)).where(*base_filter)
            ).scalar() or 0

            if total == 0:
                return {
                    "pipeline_name": pipeline_name,
                    "step_name": step_name,
                    "days": days,
                    "total": 0,
                    "pass_rate": None,
                    "avg_score": None,
                    "action_counts": {},
                }

            passed = session.execute(
                select(func.count(EvalTrace.id)).where(
                    *base_filter, EvalTrace.passed == True  # noqa: E712
                )
            ).scalar() or 0

            avg_score = session.execute(
                select(func.avg(EvalTrace.score)).where(*base_filter)
            ).scalar()

            action_rows = session.execute(
                select(
                    EvalTrace.action,
                    func.count(EvalTrace.id).label("cnt"),
                ).where(*base_filter).group_by(EvalTrace.action)
            ).all()

            return {
                "pipeline_name": pipeline_name,
                "step_name": step_name,
                "days": days,
                "total": total,
                "pass_rate": round(passed / total, 4),
                "avg_score": round(float(avg_score), 4) if avg_score else None,
                "action_counts": {r.action: r.cnt for r in action_rows},
            }

    def get_daily_scores(
        self,
        pipeline_name: str,
        step_name: str,
        days: int = 30,
    ) -> list[dict]:
        """Get daily average scores for drift detection.

        Args:
            pipeline_name: Pipeline name.
            step_name: Step name.
            days: Number of days to look back.

        Returns:
            List of dicts with date, avg_score, count, pass_rate.
        """
        since = datetime.now(timezone.utc) - timedelta(days=days)

        with Session(self._engine) as session:
            # SQLite date function
            date_expr = func.date(EvalTrace.created_at)

            rows = session.execute(
                select(
                    date_expr.label("day"),
                    func.avg(EvalTrace.score).label("avg_score"),
                    func.count(EvalTrace.id).label("count"),
                    func.sum(
                        func.cast(EvalTrace.passed, type_=EvalTrace.passed.type)
                    ).label("pass_count"),
                ).where(
                    EvalTrace.pipeline_name == pipeline_name,
                    EvalTrace.step_name == step_name,
                    EvalTrace.created_at >= since,
                ).group_by(date_expr).order_by(date_expr)
            ).all()

            return [
                {
                    "date": r.day,
                    "avg_score": round(float(r.avg_score), 4),
                    "count": r.count,
                    "pass_rate": round(r.pass_count / r.count, 4) if r.count else 0,
                }
                for r in rows
            ]

    @staticmethod
    def _trace_to_dict(trace: EvalTrace) -> dict:
        """Convert an EvalTrace to a JSON-serializable dict."""
        return {
            "id": trace.id,
            "pipeline_name": trace.pipeline_name,
            "step_name": trace.step_name,
            "action": trace.action,
            "passed": trace.passed,
            "score": trace.score,
            "issues": json.loads(trace.issues) if trace.issues else [],
            "attempt": trace.attempt,
            "output_preview": trace.output_preview,
            "created_at": trace.created_at.isoformat() if trace.created_at else None,
        }

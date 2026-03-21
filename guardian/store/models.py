"""SQLAlchemy models for eval trace storage.

Defines the schema for persisting Guardian evaluation results.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


class EvalTrace(Base):
    """A single Guardian evaluation trace record.

    Attributes:
        id: Auto-incrementing primary key.
        pipeline_name: Name of the pipeline being evaluated.
        step_name: Name of the step within the pipeline.
        action: Decision taken (pass, retry, abort, alert, passthrough).
        passed: Whether all checks passed.
        score: Quality score from 0.0 to 1.0.
        issues: JSON-serialized list of issue descriptions.
        attempt: Attempt number (1-based).
        output_preview: Optional truncated preview of the step output.
        created_at: Timestamp when this trace was recorded.
    """

    __tablename__ = "eval_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    step_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    issues: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    output_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

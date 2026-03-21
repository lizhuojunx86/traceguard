"""FastAPI server for Guardian dashboard API.

Provides REST endpoints for querying traces, pipeline metadata,
and drift detection results.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from guardian.optimizer.drift_detector import detect_drift
from guardian.store.reader import TraceReader

app = FastAPI(
    title="Pipeline Guardian API",
    description="Dashboard API for monitoring multi-agent LLM pipeline quality",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_reader() -> TraceReader:
    """Create a TraceReader from the configured database URL."""
    db_url = os.environ.get("GUARDIAN_DB_URL", "sqlite:///traces.db")
    return TraceReader(db_url)


@app.get("/api/pipelines")
def list_pipelines() -> list[dict]:
    """List all pipelines with trace metadata.

    Returns a list of pipelines that have recorded evaluation traces,
    including step count, total trace count, and latest trace timestamp.
    """
    reader = _get_reader()
    return reader.list_pipelines()


@app.get("/api/traces")
def query_traces(
    pipeline: str | None = Query(default=None, description="Filter by pipeline name"),
    step: str | None = Query(default=None, description="Filter by step name"),
    days: int = Query(default=7, ge=1, le=365, description="Days to look back"),
    limit: int = Query(default=100, ge=1, le=1000, description="Max results"),
) -> list[dict]:
    """Query historical evaluation traces.

    Supports filtering by pipeline name, step name, and time range.
    Results are ordered by created_at descending.
    """
    reader = _get_reader()
    return reader.query_traces(
        pipeline_name=pipeline,
        step_name=step,
        days=days,
        limit=limit,
    )


@app.get("/api/stats")
def step_stats(
    pipeline: str = Query(description="Pipeline name"),
    step: str = Query(description="Step name"),
    days: int = Query(default=7, ge=1, le=365, description="Days to look back"),
) -> dict:
    """Get aggregated statistics for a specific step.

    Returns pass rate, average score, and action count breakdown.
    """
    reader = _get_reader()
    return reader.get_step_stats(pipeline, step, days=days)


@app.get("/api/drift")
def drift_report(
    pipeline: str = Query(description="Pipeline name"),
    recent_days: int = Query(default=3, ge=1, le=30, description="Recent period in days"),
    baseline_days: int = Query(default=14, ge=2, le=90, description="Total baseline period in days"),
) -> dict:
    """Run drift detection for a pipeline.

    Compares recent evaluation metrics against a baseline period.
    Returns per-step drift analysis and overall pipeline status.
    """
    reader = _get_reader()
    report = detect_drift(
        reader=reader,
        pipeline_name=pipeline,
        recent_days=recent_days,
        baseline_days=baseline_days,
    )
    return {
        "pipeline_name": report.pipeline_name,
        "has_drift": report.has_drift,
        "summary": report.summary,
        "steps": [
            {
                "step_name": r.step_name,
                "drifted": r.drifted,
                "trend": r.trend,
                "signals": r.signals,
                "baseline_avg_score": r.baseline_avg_score,
                "recent_avg_score": r.recent_avg_score,
                "baseline_pass_rate": r.baseline_pass_rate,
                "recent_pass_rate": r.recent_pass_rate,
            }
            for r in report.step_results
        ],
    }


@app.get("/api/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    """Serve the built-in dashboard UI."""
    html_path = Path(__file__).parent / "dashboard.html"
    return html_path.read_text(encoding="utf-8")

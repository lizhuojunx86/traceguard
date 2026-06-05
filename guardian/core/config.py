"""Pipeline configuration models and YAML loader.

Defines Pydantic models for the entire pipeline configuration hierarchy
and provides a loader to parse YAML config files into validated Python objects.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class SpecEdge(BaseModel):
    """A boundary value that a reported metric may be reverse-calculated toward.

    ``type`` documents which side of a specification the edge represents
    (a lower interval limit, an upper interval limit, or a benchmark target).
    Detection currently keys off ``value``; ``type`` is retained for directional
    refinement and reporting.
    """

    type: Literal["interval_low", "interval_high", "benchmark"]
    value: float


class ReverseCalcConfig(BaseModel):
    """Generic statistical reverse-calculation check.

    Flags a metric that is *too stable and hugging a boundary* across a
    rolling window — the signature of values being back-solved to just clear a
    threshold rather than measured. Domain-agnostic: it operates only on the
    configured field name, boundary values, and a ground-truth ``sigma_floor``
    (the natural cross-sample spread, typically established out-of-band, e.g.
    via interview). Nothing here is specific to any single domain.
    """

    mode: Literal["sigma_floor"] = Field(
        default="sigma_floor",
        description="Detection mode. 'sigma_floor': flattened sigma near an edge.",
    )
    target_field: str = Field(
        description="Name of the numeric output field to analyze",
    )
    window_batches: int = Field(
        default=6,
        ge=2,
        description="Number of recent samples (incl. current) to aggregate",
    )
    sigma_floor: float = Field(
        gt=0,
        description=(
            "Ground-truth natural spread of the metric (NOT a constant default; "
            "established out-of-band, e.g. via interview). Observed sigma far "
            "below this is suspicious."
        ),
    )
    sigma_ratio: float = Field(
        default=20.0,
        gt=0,
        description="Observed sigma must be < sigma_floor / sigma_ratio to flag",
    )
    edge_band: float = Field(
        gt=0,
        description="Mean within this distance of a spec edge counts as 'hugging'",
    )
    group_field: str | None = Field(
        default=None,
        description="Output field whose value selects the applicable spec edge",
    )
    spec_edges: dict[str, SpecEdge] = Field(
        default_factory=dict,
        description="Map of group value -> boundary the metric may hug",
    )
    lookback_days: int = Field(
        default=3650,
        ge=1,
        description="History window (days) to pull prior samples from eval_store",
    )


class StructuralCheckConfig(BaseModel):
    """Configuration for structural validation of a step's output."""

    schema_path: str | None = Field(
        default=None,
        alias="schema",
        description="Path to JSON Schema file for output validation",
    )
    required_fields: list[str] = Field(
        default_factory=list,
        description="Fields that must be present in the output",
    )
    max_length: int | None = Field(
        default=None,
        description="Maximum allowed output length in characters",
    )
    min_length: int | None = Field(
        default=None,
        description="Minimum expected output length in characters",
    )
    language: str | None = Field(
        default=None,
        description="Expected primary language code (e.g. 'en', 'zh')",
    )
    reverse_calc: ReverseCalcConfig | None = Field(
        default=None,
        description="Optional reverse-calculation (audit-flag) check settings",
    )

    model_config = {"populate_by_name": True}


class SemanticCheckConfig(BaseModel):
    """Configuration for LLM-as-Judge semantic evaluation."""

    enabled: bool = Field(
        default=False,
        description="Whether semantic evaluation is active",
    )
    model: str | None = Field(
        default=None,
        description="LLM model to use for semantic evaluation",
    )
    api_base: str | None = Field(
        default=None,
        description="Base URL for OpenAI-compatible API endpoint",
    )
    api_key_env: str = Field(
        default="GUARDIAN_LLM_API_KEY",
        description="Environment variable name holding the API key",
    )
    criteria: list[str] = Field(
        default_factory=list,
        description="Evaluation criteria for the LLM judge",
    )
    min_score: int = Field(
        default=3,
        ge=1,
        le=5,
        description="Minimum acceptable score (1-5)",
    )


class ActionConfig(BaseModel):
    """Configuration for actions taken based on validation results."""

    on_structural_fail: Literal["retry", "abort", "alert", "passthrough"] = Field(
        default="abort",
        description="Action when structural validation fails",
    )
    on_semantic_low: Literal["retry", "abort", "alert", "passthrough"] = Field(
        default="alert",
        description="Action when semantic score is below threshold",
    )
    max_retries: int = Field(
        default=2,
        ge=0,
        description="Maximum number of retry attempts",
    )
    retry_hint: str | None = Field(
        default=None,
        description="Hint message to include when retrying a step",
    )
    alert_channel: str | None = Field(
        default=None,
        description="Channel for sending alerts (e.g. 'telegram', 'webhook')",
    )


class GuardianConfig(BaseModel):
    """Configuration for a Guardian checkpoint attached to a pipeline step."""

    structural: StructuralCheckConfig = Field(
        default_factory=StructuralCheckConfig,
        description="Structural validation settings",
    )
    semantic: SemanticCheckConfig = Field(
        default_factory=SemanticCheckConfig,
        description="Semantic evaluation settings",
    )
    actions: ActionConfig = Field(
        default_factory=ActionConfig,
        description="Action settings for validation outcomes",
    )


class StepConfig(BaseModel):
    """Configuration for a single pipeline step."""

    name: str = Field(description="Unique name identifying this step")
    container: str = Field(description="Docker image for this step")
    input_source: str = Field(
        description="Input source: 'trigger' or a previous step name",
    )
    guardian: GuardianConfig | None = Field(
        default=None,
        description="Optional Guardian checkpoint configuration",
    )


class PipelineConfig(BaseModel):
    """Top-level pipeline configuration."""

    name: str = Field(description="Pipeline name")
    description: str | None = Field(
        default=None,
        description="Human-readable pipeline description",
    )
    trigger: Literal["cron", "webhook", "manual"] = Field(
        default="manual",
        description="How the pipeline is triggered",
    )
    steps: list[StepConfig] = Field(
        description="Ordered list of pipeline steps",
        min_length=1,
    )

    @field_validator("steps")
    @classmethod
    def step_names_must_be_unique(cls, steps: list[StepConfig]) -> list[StepConfig]:
        """Validate that all step names are unique within the pipeline."""
        names = [s.name for s in steps]
        if len(names) != len(set(names)):
            duplicates = [n for n in names if names.count(n) > 1]
            raise ValueError(
                f"Step names must be unique, found duplicates: {set(duplicates)}"
            )
        return steps


def load_pipeline(path: str) -> PipelineConfig:
    """Load and validate a pipeline configuration from a YAML file.

    Args:
        path: Filesystem path to the YAML configuration file.

    Returns:
        A validated PipelineConfig instance.

    Raises:
        FileNotFoundError: If the config file does not exist.
        KeyError: If the YAML is missing the top-level 'pipeline' key.
        ValidationError: If the config data fails Pydantic validation.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict) or "pipeline" not in raw:
        raise KeyError("YAML config must contain a top-level 'pipeline' key")

    return PipelineConfig(**raw["pipeline"])

"""Tests for pipeline configuration loading and validation."""
import os
import tempfile

import pytest
import yaml

from guardian.core.config import (
    ActionConfig,
    GuardianConfig,
    PipelineConfig,
    SemanticCheckConfig,
    StepConfig,
    StructuralCheckConfig,
    load_pipeline,
)


MINIMAL_PIPELINE = {
    "pipeline": {
        "name": "test-pipeline",
        "steps": [
            {
                "name": "step_01",
                "container": "collector:latest",
                "input_source": "trigger",
            }
        ],
    }
}

FULL_PIPELINE = {
    "pipeline": {
        "name": "full-pipeline",
        "description": "A fully configured pipeline",
        "trigger": "cron",
        "steps": [
            {
                "name": "step_01_collect",
                "container": "collector:latest",
                "input_source": "trigger",
                "guardian": {
                    "structural": {
                        "schema": "schemas/step_01_output.json",
                        "required_fields": ["data", "timestamp", "source"],
                        "max_length": 50000,
                        "min_length": 10,
                        "language": "en",
                    },
                    "semantic": {
                        "enabled": True,
                        "model": "gpt-4o-mini",
                        "criteria": [
                            "Output contains structured market data",
                            "Data is from the correct date range",
                        ],
                        "min_score": 3,
                    },
                    "actions": {
                        "on_structural_fail": "retry",
                        "on_semantic_low": "alert",
                        "max_retries": 3,
                        "retry_hint": "Fix the output format",
                        "alert_channel": "telegram",
                    },
                },
            },
            {
                "name": "step_02_analyze",
                "container": "analyzer:latest",
                "input_source": "step_01_collect",
            },
        ],
    }
}


class TestStructuralCheckConfig:
    """Tests for StructuralCheckConfig model."""

    def test_defaults(self):
        config = StructuralCheckConfig()
        assert config.schema_path is None
        assert config.required_fields == []
        assert config.max_length is None
        assert config.min_length is None
        assert config.language is None

    def test_full_config(self):
        config = StructuralCheckConfig(
            schema_path="schemas/out.json",
            required_fields=["a", "b"],
            max_length=1000,
            min_length=10,
            language="en",
        )
        assert config.schema_path == "schemas/out.json"
        assert config.required_fields == ["a", "b"]
        assert config.max_length == 1000
        assert config.min_length == 10
        assert config.language == "en"


class TestSemanticCheckConfig:
    """Tests for SemanticCheckConfig model."""

    def test_defaults(self):
        config = SemanticCheckConfig()
        assert config.enabled is False
        assert config.criteria == []
        assert config.min_score == 3

    def test_custom(self):
        config = SemanticCheckConfig(
            enabled=True,
            model="gpt-4o",
            criteria=["Is coherent"],
            min_score=4,
        )
        assert config.enabled is True
        assert config.model == "gpt-4o"
        assert config.min_score == 4


class TestActionConfig:
    """Tests for ActionConfig model."""

    def test_defaults(self):
        config = ActionConfig()
        assert config.on_structural_fail == "abort"
        assert config.on_semantic_low == "alert"
        assert config.max_retries == 2
        assert config.retry_hint is None
        assert config.alert_channel is None

    def test_custom(self):
        config = ActionConfig(
            on_structural_fail="retry",
            max_retries=5,
            retry_hint="Fix it",
            alert_channel="telegram",
        )
        assert config.on_structural_fail == "retry"
        assert config.max_retries == 5

    def test_invalid_action_rejected(self):
        with pytest.raises(ValueError):
            ActionConfig(on_structural_fail="explode")


class TestGuardianConfig:
    """Tests for GuardianConfig model."""

    def test_defaults(self):
        config = GuardianConfig()
        assert config.structural is not None
        assert config.semantic is not None
        assert config.actions is not None

    def test_nested_config(self):
        config = GuardianConfig(
            structural=StructuralCheckConfig(required_fields=["x"]),
            actions=ActionConfig(on_structural_fail="retry"),
        )
        assert config.structural.required_fields == ["x"]
        assert config.actions.on_structural_fail == "retry"


class TestStepConfig:
    """Tests for StepConfig model."""

    def test_minimal(self):
        step = StepConfig(
            name="step_01",
            container="img:latest",
            input_source="trigger",
        )
        assert step.name == "step_01"
        assert step.guardian is None

    def test_with_guardian(self):
        step = StepConfig(
            name="step_01",
            container="img:latest",
            input_source="trigger",
            guardian=GuardianConfig(),
        )
        assert step.guardian is not None


class TestPipelineConfig:
    """Tests for PipelineConfig model."""

    def test_minimal(self):
        pipeline = PipelineConfig(
            name="test",
            steps=[
                StepConfig(name="s1", container="c:1", input_source="trigger")
            ],
        )
        assert pipeline.name == "test"
        assert pipeline.trigger == "manual"
        assert pipeline.description is None
        assert len(pipeline.steps) == 1

    def test_step_name_uniqueness(self):
        with pytest.raises(ValueError, match="unique"):
            PipelineConfig(
                name="test",
                steps=[
                    StepConfig(name="dup", container="c:1", input_source="trigger"),
                    StepConfig(name="dup", container="c:2", input_source="trigger"),
                ],
            )


class TestLoadPipeline:
    """Tests for load_pipeline YAML loader."""

    def _write_yaml(self, data: dict) -> str:
        """Write data to a temp YAML file and return its path."""
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.dump(data, f)
        return path

    def test_load_minimal(self):
        path = self._write_yaml(MINIMAL_PIPELINE)
        try:
            config = load_pipeline(path)
            assert config.name == "test-pipeline"
            assert len(config.steps) == 1
        finally:
            os.unlink(path)

    def test_load_full(self):
        path = self._write_yaml(FULL_PIPELINE)
        try:
            config = load_pipeline(path)
            assert config.name == "full-pipeline"
            assert config.trigger == "cron"
            assert len(config.steps) == 2

            step = config.steps[0]
            assert step.guardian is not None
            assert step.guardian.structural.required_fields == [
                "data",
                "timestamp",
                "source",
            ]
            assert step.guardian.structural.max_length == 50000
            assert step.guardian.actions.on_structural_fail == "retry"
            assert step.guardian.actions.max_retries == 3
            assert step.guardian.semantic.enabled is True
            assert step.guardian.semantic.min_score == 3
        finally:
            os.unlink(path)

    def test_load_nonexistent_file(self):
        with pytest.raises(FileNotFoundError):
            load_pipeline("/nonexistent/path.yaml")

    def test_load_invalid_yaml(self):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write("not: valid: yaml: [[[")
        try:
            with pytest.raises(Exception):
                load_pipeline(path)
        finally:
            os.unlink(path)

    def test_load_missing_pipeline_key(self):
        path = self._write_yaml({"wrong_key": {}})
        try:
            with pytest.raises((KeyError, ValueError)):
                load_pipeline(path)
        finally:
            os.unlink(path)

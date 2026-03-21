"""Tests for CLI entry point."""
import json
import os
import tempfile

import pytest
import yaml
from click.testing import CliRunner

from guardian.cli import cli


def _write_file(content: str, suffix: str = ".json") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def _make_pipeline_config(
    step_name: str = "step_01",
    required_fields: list[str] | None = None,
    on_structural_fail: str = "abort",
    min_length: int | None = None,
    max_retries: int = 2,
) -> str:
    config = {
        "pipeline": {
            "name": "test-pipeline",
            "steps": [
                {
                    "name": step_name,
                    "container": "test:latest",
                    "input_source": "trigger",
                    "guardian": {
                        "structural": {
                            "required_fields": required_fields or [],
                        },
                        "actions": {
                            "on_structural_fail": on_structural_fail,
                            "max_retries": max_retries,
                        },
                    },
                }
            ],
        }
    }
    if min_length is not None:
        config["pipeline"]["steps"][0]["guardian"]["structural"]["min_length"] = min_length
    return _write_file(yaml.dump(config), suffix=".yaml")


class TestCheckCommand:
    """Tests for the 'check' CLI command."""

    def test_pass_scenario(self):
        pipeline_path = _make_pipeline_config(required_fields=["name", "value"])
        input_path = _write_file(json.dumps({"name": "x", "value": 42}))
        runner = CliRunner()

        try:
            result = runner.invoke(cli, [
                "check",
                "--pipeline", pipeline_path,
                "--step", "step_01",
                "--input", input_path,
            ])
            assert result.exit_code == 0
            output = json.loads(result.output)
            assert output["action"] == "pass"
            assert output["score"] == 1.0
            assert output["issues"] == []
        finally:
            os.unlink(pipeline_path)
            os.unlink(input_path)

    def test_abort_scenario(self):
        pipeline_path = _make_pipeline_config(
            required_fields=["missing_field"],
            on_structural_fail="abort",
        )
        input_path = _write_file(json.dumps({"other": "data"}))
        runner = CliRunner()

        try:
            result = runner.invoke(cli, [
                "check",
                "--pipeline", pipeline_path,
                "--step", "step_01",
                "--input", input_path,
            ])
            assert result.exit_code == 2  # abort exits with code 2
            output = json.loads(result.output)
            assert output["action"] == "abort"
            assert len(output["issues"]) > 0
        finally:
            os.unlink(pipeline_path)
            os.unlink(input_path)

    def test_retry_scenario(self):
        pipeline_path = _make_pipeline_config(
            required_fields=["data"],
            on_structural_fail="retry",
            max_retries=3,
        )
        input_path = _write_file(json.dumps({"wrong": "field"}))
        runner = CliRunner()

        try:
            result = runner.invoke(cli, [
                "check",
                "--pipeline", pipeline_path,
                "--step", "step_01",
                "--input", input_path,
                "--attempt", "1",
            ])
            assert result.exit_code == 0
            output = json.loads(result.output)
            assert output["action"] == "retry"
        finally:
            os.unlink(pipeline_path)
            os.unlink(input_path)

    def test_retry_exhausted_becomes_abort(self):
        pipeline_path = _make_pipeline_config(
            required_fields=["data"],
            on_structural_fail="retry",
            max_retries=2,
        )
        input_path = _write_file(json.dumps({"wrong": "field"}))
        runner = CliRunner()

        try:
            result = runner.invoke(cli, [
                "check",
                "--pipeline", pipeline_path,
                "--step", "step_01",
                "--input", input_path,
                "--attempt", "2",
            ])
            assert result.exit_code == 2
            output = json.loads(result.output)
            assert output["action"] == "abort"
        finally:
            os.unlink(pipeline_path)
            os.unlink(input_path)

    def test_step_not_found(self):
        pipeline_path = _make_pipeline_config(step_name="step_01")
        input_path = _write_file("{}")
        runner = CliRunner()

        try:
            result = runner.invoke(cli, [
                "check",
                "--pipeline", pipeline_path,
                "--step", "nonexistent_step",
                "--input", input_path,
            ])
            assert result.exit_code == 1
        finally:
            os.unlink(pipeline_path)
            os.unlink(input_path)

    def test_with_db_writes_trace(self):
        pipeline_path = _make_pipeline_config(required_fields=["name"])
        input_path = _write_file(json.dumps({"name": "ok"}))
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        runner = CliRunner()

        try:
            result = runner.invoke(cli, [
                "check",
                "--pipeline", pipeline_path,
                "--step", "step_01",
                "--input", input_path,
                "--db", f"sqlite:///{db_path}",
            ])
            assert result.exit_code == 0
            output = json.loads(result.output)
            assert output["action"] == "pass"

            # Verify trace was written
            from sqlalchemy import create_engine, select
            from sqlalchemy.orm import Session
            from guardian.store.models import EvalTrace

            engine = create_engine(f"sqlite:///{db_path}")
            with Session(engine) as s:
                traces = s.execute(select(EvalTrace)).scalars().all()
                assert len(traces) == 1
                assert traces[0].pipeline_name == "test-pipeline"
                assert traces[0].step_name == "step_01"
        finally:
            os.unlink(pipeline_path)
            os.unlink(input_path)
            os.unlink(db_path)


class TestCheckWithExampleConfig:
    """End-to-end test using the example market_intel config."""

    def test_valid_output_passes(self):
        valid_output = {
            "data": [
                {"symbol": "AAPL", "price": 185.50, "volume": 1000000},
                {"symbol": "GOOGL", "price": 142.30, "volume": 500000},
            ],
            "timestamp": "2026-03-21T10:00:00Z",
            "source": "market-api",
        }
        input_path = _write_file(json.dumps(valid_output))
        runner = CliRunner()

        try:
            result = runner.invoke(cli, [
                "check",
                "--pipeline", "configs/examples/market_intel.yaml",
                "--step", "step_01_collect",
                "--input", input_path,
            ])
            assert result.exit_code == 0
            output = json.loads(result.output)
            assert output["action"] == "pass"
            assert output["score"] == 1.0
        finally:
            os.unlink(input_path)

    def test_invalid_output_retries(self):
        invalid_output = {"wrong_format": True}
        input_path = _write_file(json.dumps(invalid_output))
        runner = CliRunner()

        try:
            result = runner.invoke(cli, [
                "check",
                "--pipeline", "configs/examples/market_intel.yaml",
                "--step", "step_01_collect",
                "--input", input_path,
            ])
            assert result.exit_code == 0  # retry, not abort
            output = json.loads(result.output)
            assert output["action"] == "retry"
            assert len(output["issues"]) > 0
            assert "retry_hint" in output
        finally:
            os.unlink(input_path)

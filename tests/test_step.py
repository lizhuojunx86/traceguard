"""Tests for step output abstraction."""
import json
import os
import tempfile
from datetime import datetime, timezone

import pytest

from guardian.core.step import StepOutput, load_step_output


class TestStepOutput:
    """Tests for StepOutput dataclass."""

    def test_create_with_string(self):
        output = StepOutput(
            step_name="step_01",
            output_data="raw text output",
        )
        assert output.step_name == "step_01"
        assert output.output_data == "raw text output"
        assert isinstance(output.timestamp, datetime)
        assert output.metadata == {}

    def test_create_with_dict(self):
        data = {"key": "value", "count": 42}
        output = StepOutput(
            step_name="step_02",
            output_data=data,
        )
        assert output.output_data == data

    def test_custom_timestamp_and_metadata(self):
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        output = StepOutput(
            step_name="s1",
            output_data="x",
            timestamp=ts,
            metadata={"source": "test"},
        )
        assert output.timestamp == ts
        assert output.metadata["source"] == "test"

    def test_output_as_string_from_str(self):
        output = StepOutput(step_name="s", output_data="hello")
        assert output.output_as_string() == "hello"

    def test_output_as_string_from_dict(self):
        data = {"a": 1}
        output = StepOutput(step_name="s", output_data=data)
        result = output.output_as_string()
        assert json.loads(result) == data

    def test_output_as_dict_from_dict(self):
        data = {"a": 1}
        output = StepOutput(step_name="s", output_data=data)
        assert output.output_as_dict() == data

    def test_output_as_dict_from_json_string(self):
        data = {"b": 2}
        output = StepOutput(step_name="s", output_data=json.dumps(data))
        assert output.output_as_dict() == data

    def test_output_as_dict_from_non_json_string(self):
        output = StepOutput(step_name="s", output_data="not json")
        assert output.output_as_dict() is None


class TestLoadStepOutput:
    """Tests for load_step_output from file."""

    def _write_tmp(self, content: str, suffix: str = ".json") -> str:
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        return path

    def test_load_json_file(self):
        data = {"result": "ok", "items": [1, 2, 3]}
        path = self._write_tmp(json.dumps(data))
        try:
            output = load_step_output("step_01", path)
            assert output.step_name == "step_01"
            assert output.output_as_dict() == data
        finally:
            os.unlink(path)

    def test_load_plain_text_file(self):
        path = self._write_tmp("plain text content", suffix=".txt")
        try:
            output = load_step_output("step_01", path)
            assert output.output_data == "plain text content"
        finally:
            os.unlink(path)

    def test_load_nonexistent_file(self):
        with pytest.raises(FileNotFoundError):
            load_step_output("step_01", "/nonexistent/file.json")

    def test_load_with_metadata(self):
        path = self._write_tmp('{"x": 1}')
        try:
            output = load_step_output("s", path, metadata={"env": "test"})
            assert output.metadata["env"] == "test"
        finally:
            os.unlink(path)

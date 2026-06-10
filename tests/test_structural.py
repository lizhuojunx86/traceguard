"""Tests for structural validator."""
import json
import os
import tempfile


from guardian.core.config import StructuralCheckConfig
from guardian.core.step import StepOutput
from guardian.validators.structural import StructuralResult, validate_structural


class TestStructuralResult:
    """Tests for StructuralResult."""

    def test_passed(self):
        r = StructuralResult(passed=True, issues=[])
        assert r.passed is True
        assert r.issues == []

    def test_failed(self):
        r = StructuralResult(passed=False, issues=["missing field: x"])
        assert r.passed is False
        assert len(r.issues) == 1


class TestRequiredFields:
    """Tests for required_fields checking."""

    def test_all_fields_present(self):
        output = StepOutput(
            step_name="s",
            output_data={"data": 1, "timestamp": "now", "source": "api"},
        )
        config = StructuralCheckConfig(required_fields=["data", "timestamp", "source"])
        result = validate_structural(output, config)
        assert result.passed is True

    def test_missing_fields(self):
        output = StepOutput(step_name="s", output_data={"data": 1})
        config = StructuralCheckConfig(required_fields=["data", "timestamp", "source"])
        result = validate_structural(output, config)
        assert result.passed is False
        assert any("timestamp" in i for i in result.issues)
        assert any("source" in i for i in result.issues)

    def test_non_dict_output_with_required_fields(self):
        output = StepOutput(step_name="s", output_data="plain text")
        config = StructuralCheckConfig(required_fields=["data"])
        result = validate_structural(output, config)
        assert result.passed is False
        assert any("not a JSON object" in i for i in result.issues)

    def test_no_required_fields_configured(self):
        output = StepOutput(step_name="s", output_data="anything")
        config = StructuralCheckConfig(required_fields=[])
        result = validate_structural(output, config)
        assert result.passed is True


class TestLengthChecks:
    """Tests for min/max length checking."""

    def test_within_bounds(self):
        output = StepOutput(step_name="s", output_data="hello world")
        config = StructuralCheckConfig(min_length=5, max_length=100)
        result = validate_structural(output, config)
        assert result.passed is True

    def test_too_short(self):
        output = StepOutput(step_name="s", output_data="hi")
        config = StructuralCheckConfig(min_length=10)
        result = validate_structural(output, config)
        assert result.passed is False
        assert any("too short" in i.lower() for i in result.issues)

    def test_too_long(self):
        output = StepOutput(step_name="s", output_data="x" * 200)
        config = StructuralCheckConfig(max_length=100)
        result = validate_structural(output, config)
        assert result.passed is False
        assert any("too long" in i.lower() for i in result.issues)

    def test_dict_length_uses_serialized(self):
        data = {"key": "a" * 200}
        output = StepOutput(step_name="s", output_data=data)
        config = StructuralCheckConfig(max_length=50)
        result = validate_structural(output, config)
        assert result.passed is False


class TestJsonSchemaValidation:
    """Tests for JSON Schema validation."""

    def _write_schema(self, schema: dict) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(schema, f)
        return path

    def test_valid_against_schema(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "score": {"type": "number"},
            },
            "required": ["name", "score"],
        }
        path = self._write_schema(schema)
        try:
            output = StepOutput(
                step_name="s", output_data={"name": "test", "score": 95}
            )
            config = StructuralCheckConfig(schema_path=path)
            result = validate_structural(output, config)
            assert result.passed is True
        finally:
            os.unlink(path)

    def test_invalid_against_schema(self):
        schema = {
            "type": "object",
            "properties": {"score": {"type": "number"}},
            "required": ["score"],
        }
        path = self._write_schema(schema)
        try:
            output = StepOutput(
                step_name="s", output_data={"score": "not_a_number"}
            )
            config = StructuralCheckConfig(schema_path=path)
            result = validate_structural(output, config)
            assert result.passed is False
            assert any("schema" in i.lower() for i in result.issues)
        finally:
            os.unlink(path)

    def test_non_dict_with_schema(self):
        schema = {"type": "object"}
        path = self._write_schema(schema)
        try:
            output = StepOutput(step_name="s", output_data="just text")
            config = StructuralCheckConfig(schema_path=path)
            result = validate_structural(output, config)
            assert result.passed is False
        finally:
            os.unlink(path)

    def test_schema_file_not_found(self):
        output = StepOutput(step_name="s", output_data={"a": 1})
        config = StructuralCheckConfig(schema_path="/nonexistent/schema.json")
        result = validate_structural(output, config)
        assert result.passed is False
        assert any("schema file" in i.lower() for i in result.issues)


class TestLanguageCheck:
    """Tests for language consistency checking."""

    def test_english_text_passes_en(self):
        output = StepOutput(
            step_name="s",
            output_data="This is a normal English sentence with standard words.",
        )
        config = StructuralCheckConfig(language="en")
        result = validate_structural(output, config)
        assert result.passed is True

    def test_chinese_text_fails_en(self):
        output = StepOutput(
            step_name="s",
            output_data="这是一段完全由中文字符组成的文本内容，用于测试语言检测功能",
        )
        config = StructuralCheckConfig(language="en")
        result = validate_structural(output, config)
        assert result.passed is False
        assert any("language" in i.lower() for i in result.issues)

    def test_chinese_text_passes_zh(self):
        output = StepOutput(
            step_name="s",
            output_data="这是一段中文文本",
        )
        config = StructuralCheckConfig(language="zh")
        result = validate_structural(output, config)
        assert result.passed is True

    def test_no_language_check(self):
        output = StepOutput(step_name="s", output_data="任意文本 any text")
        config = StructuralCheckConfig(language=None)
        result = validate_structural(output, config)
        assert result.passed is True


class TestMultipleChecks:
    """Tests combining multiple checks."""

    def test_multiple_failures(self):
        output = StepOutput(step_name="s", output_data="hi")
        config = StructuralCheckConfig(
            required_fields=["data"],
            min_length=100,
        )
        result = validate_structural(output, config)
        assert result.passed is False
        assert len(result.issues) >= 2

    def test_all_checks_pass(self):
        output = StepOutput(
            step_name="s",
            output_data={"data": "value", "ts": "2026-01-01"},
        )
        config = StructuralCheckConfig(
            required_fields=["data", "ts"],
            min_length=5,
            max_length=10000,
        )
        result = validate_structural(output, config)
        assert result.passed is True

    def test_empty_config_always_passes(self):
        output = StepOutput(step_name="s", output_data="anything")
        config = StructuralCheckConfig()
        result = validate_structural(output, config)
        assert result.passed is True

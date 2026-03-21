"""Structural validator for pipeline step outputs.

Performs four types of checks:
1. JSON Schema validation (if schema_path is configured)
2. Required fields presence (if required_fields is configured)
3. Output length bounds (min_length / max_length)
4. Language consistency (basic character-set detection)
"""
from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

from guardian.core.config import StructuralCheckConfig
from guardian.core.step import StepOutput


@dataclass
class StructuralResult:
    """Result of structural validation.

    Attributes:
        passed: Whether all structural checks passed.
        issues: List of human-readable issue descriptions.
    """

    passed: bool
    issues: list[str] = field(default_factory=list)


def validate_structural(
    output: StepOutput,
    config: StructuralCheckConfig,
) -> StructuralResult:
    """Run all configured structural checks against a step output.

    Args:
        output: The step output to validate.
        config: Structural check configuration.

    Returns:
        StructuralResult indicating pass/fail and any issues found.
    """
    issues: list[str] = []

    _check_json_schema(output, config, issues)
    _check_required_fields(output, config, issues)
    _check_length(output, config, issues)
    _check_language(output, config, issues)

    return StructuralResult(passed=len(issues) == 0, issues=issues)


def _check_json_schema(
    output: StepOutput,
    config: StructuralCheckConfig,
    issues: list[str],
) -> None:
    """Validate output against a JSON Schema file."""
    if config.schema_path is None:
        return

    schema_path = Path(config.schema_path)
    if not schema_path.exists():
        issues.append(f"Schema file not found: {config.schema_path}")
        return

    data = output.output_as_dict()
    if data is None:
        issues.append("Output is not valid JSON; cannot validate against schema")
        return

    with open(schema_path) as f:
        schema = json.load(f)

    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        issues.append(f"JSON Schema validation failed: {e.message}")


def _check_required_fields(
    output: StepOutput,
    config: StructuralCheckConfig,
    issues: list[str],
) -> None:
    """Check that all required fields are present in the output."""
    if not config.required_fields:
        return

    data = output.output_as_dict()
    if data is None:
        issues.append(
            "Output is not a JSON object; cannot check required fields"
        )
        return

    missing = [f for f in config.required_fields if f not in data]
    for f in missing:
        issues.append(f"Missing required field: {f}")


def _check_length(
    output: StepOutput,
    config: StructuralCheckConfig,
    issues: list[str],
) -> None:
    """Check output length against min/max bounds."""
    if config.min_length is None and config.max_length is None:
        return

    text = output.output_as_string()
    length = len(text)

    if config.min_length is not None and length < config.min_length:
        issues.append(
            f"Output too short: {length} chars (minimum: {config.min_length})"
        )

    if config.max_length is not None and length > config.max_length:
        issues.append(
            f"Output too long: {length} chars (maximum: {config.max_length})"
        )


def _check_language(
    output: StepOutput,
    config: StructuralCheckConfig,
    issues: list[str],
) -> None:
    """Check language consistency using Unicode script detection.

    Uses a simple heuristic: count the proportion of characters belonging
    to expected vs unexpected scripts. If the unexpected ratio exceeds
    a threshold, the check fails.
    """
    if config.language is None:
        return

    text = output.output_as_string()
    # Strip whitespace, digits, punctuation for script analysis
    alpha_chars = [c for c in text if unicodedata.category(c).startswith("L")]
    if not alpha_chars:
        return  # No alphabetic chars to check

    lang = config.language.lower()
    expected_count = 0
    total = len(alpha_chars)

    for c in alpha_chars:
        script = _char_script(c)
        if _script_matches_language(script, lang):
            expected_count += 1

    ratio = expected_count / total
    # If less than 50% of alphabetic chars match the expected language, flag it
    if ratio < 0.5:
        issues.append(
            f"Language mismatch: expected '{config.language}', "
            f"but only {ratio:.0%} of text matches"
        )


def _char_script(c: str) -> str:
    """Determine the rough script category of a character."""
    cp = ord(c)
    # CJK Unified Ideographs and extensions
    if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
        return "cjk"
    # Hiragana
    if 0x3040 <= cp <= 0x309F:
        return "hiragana"
    # Katakana
    if 0x30A0 <= cp <= 0x30FF:
        return "katakana"
    # Hangul
    if 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:
        return "hangul"
    # Cyrillic
    if 0x0400 <= cp <= 0x04FF:
        return "cyrillic"
    # Arabic
    if 0x0600 <= cp <= 0x06FF:
        return "arabic"
    # Latin
    if cp < 0x0250 or (0x1E00 <= cp <= 0x1EFF):
        return "latin"
    return "other"


def _script_matches_language(script: str, lang: str) -> bool:
    """Check if a script is expected for a given language code."""
    mapping: dict[str, set[str]] = {
        "en": {"latin"},
        "zh": {"cjk"},
        "ja": {"cjk", "hiragana", "katakana"},
        "ko": {"hangul", "cjk"},
        "ru": {"cyrillic"},
        "ar": {"arabic"},
    }
    # Default: accept latin for unknown languages
    expected_scripts = mapping.get(lang, {"latin"})
    return script in expected_scripts

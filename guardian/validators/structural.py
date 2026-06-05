"""Structural validator for pipeline step outputs.

Checks are registered in a name-keyed registry and dispatched by
``validate_structural``. Built-in checks:

1. JSON Schema validation (if schema_path is configured)
2. Required fields presence (if required_fields is configured)
3. Output length bounds (min_length / max_length)
4. Language consistency (basic character-set detection)

New check types register themselves via ``@register_structural_check(name)``
and self-skip when their configuration slice is absent. This keeps the
dispatcher generic: adding a check requires no edit to ``validate_structural``.
Each check returns a :class:`CheckOutcome` carrying issues plus a ``flag_type``
(``"standard"`` for hard failures, ``"suspicion"`` for advisory audit flags
that should not be treated as data errors).
"""
from __future__ import annotations

import json
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

import jsonschema

from guardian.core.config import ReverseCalcConfig, SpecEdge, StructuralCheckConfig
from guardian.core.step import StepOutput

# Audit-flag taxonomy (see E3 / advisory checks).
FLAG_STANDARD = "standard"
FLAG_SUSPICION = "suspicion"


@dataclass
class StructuralResult:
    """Result of structural validation.

    Attributes:
        passed: Whether all structural checks passed.
        issues: List of human-readable issue descriptions.
        flag_type: ``"standard"`` for ordinary checks, or ``"suspicion"`` when
            the only failures are advisory audit flags (data is flagged for
            human review but is not necessarily wrong).
    """

    passed: bool
    issues: list[str] = field(default_factory=list)
    flag_type: str = FLAG_STANDARD


@dataclass
class CheckContext:
    """Inputs handed to a structural check.

    Most checks only need ``output`` and ``config``. History-aware checks
    (e.g. cross-batch statistics) also use ``reader`` to read prior traces
    from eval_store while remaining stateless themselves.
    """

    output: StepOutput
    config: StructuralCheckConfig
    reader: Any | None = None
    pipeline_name: str | None = None
    step_name: str | None = None


@dataclass
class CheckOutcome:
    """What a single structural check returns."""

    issues: list[str] = field(default_factory=list)
    flag_type: str = FLAG_STANDARD


CheckFn = Callable[[CheckContext], CheckOutcome]

# Name-keyed registry. Insertion order defines dispatch order.
_STRUCTURAL_CHECKS: dict[str, CheckFn] = {}


def register_structural_check(name: str) -> Callable[[CheckFn], CheckFn]:
    """Register a structural check under ``name`` (decorator)."""

    def decorator(fn: CheckFn) -> CheckFn:
        _STRUCTURAL_CHECKS[name] = fn
        return fn

    return decorator


def list_structural_checks() -> list[str]:
    """Return registered check names in dispatch order."""
    return list(_STRUCTURAL_CHECKS)


def validate_structural(
    output: StepOutput,
    config: StructuralCheckConfig,
    *,
    reader: Any | None = None,
    pipeline_name: str | None = None,
    step_name: str | None = None,
) -> StructuralResult:
    """Run all registered structural checks against a step output.

    Args:
        output: The step output to validate.
        config: Structural check configuration.
        reader: Optional TraceReader for history-aware checks (eval_store).
        pipeline_name: Pipeline name (for history-aware checks).
        step_name: Step name (for history-aware checks).

    Returns:
        StructuralResult indicating pass/fail, any issues found, and the
        aggregate flag_type.
    """
    ctx = CheckContext(
        output=output,
        config=config,
        reader=reader,
        pipeline_name=pipeline_name,
        step_name=step_name,
    )

    issues: list[str] = []
    flags: set[str] = set()
    for check in _STRUCTURAL_CHECKS.values():
        outcome = check(ctx)
        if outcome.issues:
            issues.extend(outcome.issues)
            flags.add(outcome.flag_type)

    # Aggregate flag_type: only "suspicion" when every failing check was a
    # suspicion. A real hard failure (e.g. schema) must never be masked into
    # an advisory flag.
    if issues and flags == {FLAG_SUSPICION}:
        flag_type = FLAG_SUSPICION
    else:
        flag_type = FLAG_STANDARD

    return StructuralResult(
        passed=len(issues) == 0,
        issues=issues,
        flag_type=flag_type,
    )


@register_structural_check("json_schema")
def _check_json_schema(ctx: CheckContext) -> CheckOutcome:
    """Validate output against a JSON Schema file."""
    config = ctx.config
    if config.schema_path is None:
        return CheckOutcome()

    issues: list[str] = []
    schema_path = Path(config.schema_path)
    if not schema_path.exists():
        issues.append(f"Schema file not found: {config.schema_path}")
        return CheckOutcome(issues=issues)

    data = ctx.output.output_as_dict()
    if data is None:
        issues.append("Output is not valid JSON; cannot validate against schema")
        return CheckOutcome(issues=issues)

    with open(schema_path) as f:
        schema = json.load(f)

    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        issues.append(f"JSON Schema validation failed: {e.message}")

    return CheckOutcome(issues=issues)


@register_structural_check("required_fields")
def _check_required_fields(ctx: CheckContext) -> CheckOutcome:
    """Check that all required fields are present in the output."""
    config = ctx.config
    if not config.required_fields:
        return CheckOutcome()

    issues: list[str] = []
    data = ctx.output.output_as_dict()
    if data is None:
        issues.append(
            "Output is not a JSON object; cannot check required fields"
        )
        return CheckOutcome(issues=issues)

    missing = [f for f in config.required_fields if f not in data]
    for f in missing:
        issues.append(f"Missing required field: {f}")

    return CheckOutcome(issues=issues)


@register_structural_check("length")
def _check_length(ctx: CheckContext) -> CheckOutcome:
    """Check output length against min/max bounds."""
    config = ctx.config
    if config.min_length is None and config.max_length is None:
        return CheckOutcome()

    issues: list[str] = []
    text = ctx.output.output_as_string()
    length = len(text)

    if config.min_length is not None and length < config.min_length:
        issues.append(
            f"Output too short: {length} chars (minimum: {config.min_length})"
        )

    if config.max_length is not None and length > config.max_length:
        issues.append(
            f"Output too long: {length} chars (maximum: {config.max_length})"
        )

    return CheckOutcome(issues=issues)


@register_structural_check("language")
def _check_language(ctx: CheckContext) -> CheckOutcome:
    """Check language consistency using Unicode script detection.

    Uses a simple heuristic: count the proportion of characters belonging
    to expected vs unexpected scripts. If the unexpected ratio exceeds
    a threshold, the check fails.
    """
    config = ctx.config
    if config.language is None:
        return CheckOutcome()

    text = ctx.output.output_as_string()
    # Strip whitespace, digits, punctuation for script analysis
    alpha_chars = [c for c in text if unicodedata.category(c).startswith("L")]
    if not alpha_chars:
        return CheckOutcome()  # No alphabetic chars to check

    lang = config.language.lower()
    expected_count = 0
    total = len(alpha_chars)

    for c in alpha_chars:
        script = _char_script(c)
        if _script_matches_language(script, lang):
            expected_count += 1

    ratio = expected_count / total
    issues: list[str] = []
    # If less than 50% of alphabetic chars match the expected language, flag it
    if ratio < 0.5:
        issues.append(
            f"Language mismatch: expected '{config.language}', "
            f"but only {ratio:.0%} of text matches"
        )

    return CheckOutcome(issues=issues)


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


@register_structural_check("reverse_calc")
def _check_reverse_calc(ctx: CheckContext) -> CheckOutcome:
    """Advisory check: flag a metric that is suspiciously flat near a boundary.

    The signature of a reverse-calculated metric is a cross-sample spread far
    below its natural floor while the mean hugs a specification edge — values
    back-solved to just clear a threshold rather than independently measured.

    History-aware but stateless: prior samples are read from eval_store via
    ``ctx.reader``; this check holds no state of its own. It emits a SUSPICION
    flag (data flagged for human review, not declared wrong) so it can be
    quarantined from pass-rate statistics downstream.
    """
    rc = ctx.config.reverse_calc
    if rc is None:
        return CheckOutcome()

    data = ctx.output.output_as_dict()
    if data is None or rc.target_field not in data:
        return CheckOutcome()

    current = _as_float(data.get(rc.target_field))
    if current is None:
        return CheckOutcome()

    edge = _resolve_spec_edge(rc, data)
    if edge is None:
        return CheckOutcome()

    # Recover the prior sample series from eval_store (read-only / stateless).
    series = [current]
    if ctx.reader is not None and ctx.pipeline_name and ctx.step_name:
        prior = ctx.reader.query_traces(
            pipeline_name=ctx.pipeline_name,
            step_name=ctx.step_name,
            days=rc.lookback_days,
            limit=rc.window_batches,
        )
        for trace in prior:
            val = _extract_field(trace.get("output_preview"), rc.target_field)
            if val is not None:
                series.append(val)

    if len(series) < rc.window_batches:
        return CheckOutcome()  # insufficient history; do not conclude

    series = series[: rc.window_batches]
    sigma = pstdev(series)
    mu = mean(series)
    threshold = rc.sigma_floor / rc.sigma_ratio
    hugging = abs(mu - edge.value) < rc.edge_band

    if sigma < threshold and hugging:
        return CheckOutcome(
            issues=[
                f"reverse-calc suspicion ({rc.mode}) on '{rc.target_field}': "
                f"sigma={sigma:.4f} < floor/{rc.sigma_ratio:.0f}={threshold:.4f}, "
                f"mean={mu:.3f} within {rc.edge_band} of {edge.type} edge "
                f"{edge.value} (n={len(series)})"
            ],
            flag_type=FLAG_SUSPICION,
        )
    return CheckOutcome()


def _as_float(value: Any) -> float | None:
    """Coerce a value to float, or None if not numeric (bool excluded)."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_field(preview: Any, field_name: str) -> float | None:
    """Parse a numeric field out of a stored ``output_preview`` JSON string.

    Returns None when the preview is missing, not JSON, not an object, the
    field is absent, or the value is non-numeric. A truncated or non-JSON
    preview yielding None is exactly the eval_store limitation that motivates
    structured output persistence (PoC E4 candidate).
    """
    if not isinstance(preview, str):
        return None
    try:
        parsed = json.loads(preview)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or field_name not in parsed:
        return None
    return _as_float(parsed.get(field_name))


def _resolve_spec_edge(rc: ReverseCalcConfig, data: dict) -> SpecEdge | None:
    """Pick the spec edge applicable to this output, or None.

    With ``group_field`` set, the edge is selected by that field's value;
    without it, exactly one configured edge is required.
    """
    if rc.group_field is not None:
        key = data.get(rc.group_field)
        if key is None:
            return None
        return rc.spec_edges.get(str(key))
    if len(rc.spec_edges) == 1:
        return next(iter(rc.spec_edges.values()))
    return None

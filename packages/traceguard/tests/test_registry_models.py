"""Tests for select_model + register_model (SPEC §4.2, §3.2)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from traceguard.registry import NoEligibleModelError, register_model, select_model


UTC = timezone.utc


def _register(engine, model_id, capability, released, available, deprecated=None):
    register_model(
        model_id,
        model_family="anthropic",
        capability_class=capability,
        released_at=released,
        available_to_us_at=available,
        deprecated_at=deprecated,
        engine=engine,
    )


def test_strict_returns_latest_eligible(engine):
    _register(
        engine,
        "claude-3",
        "general-llm",
        datetime(2024, 3, 1, tzinfo=UTC),
        datetime(2024, 3, 5, tzinfo=UTC),
    )
    _register(
        engine,
        "claude-4",
        "general-llm",
        datetime(2025, 6, 1, tzinfo=UTC),
        datetime(2025, 6, 5, tzinfo=UTC),
    )
    chosen = select_model(
        "general-llm",
        available_at=datetime(2025, 1, 1, tzinfo=UTC),
        strict=True,
        engine=engine,
    )
    assert chosen == "claude-3"  # claude-4 not yet available at 2025-01-01

    chosen = select_model(
        "general-llm",
        available_at=datetime(2025, 12, 1, tzinfo=UTC),
        strict=True,
        engine=engine,
    )
    assert chosen == "claude-4"


def test_strict_raises_when_nothing_eligible(engine):
    _register(
        engine,
        "claude-future",
        "general-llm",
        datetime(2030, 1, 1, tzinfo=UTC),
        datetime(2030, 1, 5, tzinfo=UTC),
    )
    with pytest.raises(NoEligibleModelError):
        select_model(
            "general-llm",
            available_at=datetime(2025, 1, 1, tzinfo=UTC),
            strict=True,
            engine=engine,
        )


def test_loose_returns_latest_with_anachronism_flag(engine):
    _register(
        engine,
        "claude-old",
        "general-llm",
        datetime(2024, 3, 1, tzinfo=UTC),
        datetime(2024, 3, 5, tzinfo=UTC),
    )
    _register(
        engine,
        "claude-new",
        "general-llm",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 5, tzinfo=UTC),
    )
    # Looking back at 2025: claude-new is anachronistic
    model_id, is_anachronistic = select_model(
        "general-llm",
        available_at=datetime(2025, 6, 1, tzinfo=UTC),
        strict=False,
        engine=engine,
    )
    assert model_id == "claude-new"
    assert is_anachronistic is True


def test_loose_no_anachronism_when_latest_was_available(engine):
    _register(
        engine,
        "claude-only",
        "general-llm",
        datetime(2024, 3, 1, tzinfo=UTC),
        datetime(2024, 3, 5, tzinfo=UTC),
    )
    model_id, is_anachronistic = select_model(
        "general-llm",
        available_at=datetime(2025, 6, 1, tzinfo=UTC),
        strict=False,
        engine=engine,
    )
    assert model_id == "claude-only"
    assert is_anachronistic is False


def test_register_rejects_duplicate(engine):
    _register(
        engine,
        "claude-x",
        "general-llm",
        datetime(2024, 3, 1, tzinfo=UTC),
        datetime(2024, 3, 5, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="already registered"):
        _register(
            engine,
            "claude-x",
            "general-llm",
            datetime(2024, 3, 1, tzinfo=UTC),
            datetime(2024, 3, 5, tzinfo=UTC),
        )


def test_register_rejects_released_after_available(engine):
    with pytest.raises(ValueError, match="released_at"):
        _register(
            engine,
            "claude-bad",
            "general-llm",
            released=datetime(2024, 3, 10, tzinfo=UTC),
            available=datetime(2024, 3, 5, tzinfo=UTC),
        )


def test_strict_excludes_deprecated_models(engine):
    _register(
        engine,
        "claude-deprecated",
        "general-llm",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 5, tzinfo=UTC),
        deprecated=datetime(2024, 6, 1, tzinfo=UTC),
    )
    # At 2024-08, deprecated should be excluded
    with pytest.raises(NoEligibleModelError):
        select_model(
            "general-llm",
            available_at=datetime(2024, 8, 1, tzinfo=UTC),
            strict=True,
            engine=engine,
        )
    # At 2024-04 (before deprecation), still eligible
    chosen = select_model(
        "general-llm",
        available_at=datetime(2024, 4, 1, tzinfo=UTC),
        strict=True,
        engine=engine,
    )
    assert chosen == "claude-deprecated"

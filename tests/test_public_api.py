"""Frozen public API surface for v0.1.0-huadian-baseline.

DO NOT modify these assertions in 0.1.x. Removing/renaming any symbol
listed here is a breaking change requiring a major version bump.
"""


def test_public_api_surface():
    import guardian

    expected = {"evaluate_async", "StepOutput", "GuardianConfig", "GuardianDecision"}
    actual = set(guardian.__all__)
    assert actual == expected, f"Public API drift: {actual ^ expected}"


def test_public_api_importable():
    from guardian import GuardianConfig, GuardianDecision, StepOutput, evaluate_async

    assert all([evaluate_async, StepOutput, GuardianConfig, GuardianDecision])

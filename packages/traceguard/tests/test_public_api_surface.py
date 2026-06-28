"""Freeze the public import surface (the SemVer commitment from 1.0).

``traceguard.__all__`` is the stable API. This test fails if a symbol is added,
removed, or renamed without a deliberate update here — turning the "no breaking
changes" promise into something CI enforces mechanically rather than by review
discipline. Adding a symbol is a SemVer-minor and should update EXPECTED in the
same change; removing/renaming one is a major bump.
"""
from __future__ import annotations

import importlib

import traceguard

EXPECTED_PUBLIC_API = {
    "__version__",
    # Instrumentation
    "Tracer",
    "Span",
    "tracer",
    "normalize_input",
    "input_hash",
    "wrap_anthropic",
    "wrap_openai",
    # Registries
    "select_model",
    "register_model",
    "NoEligibleModelError",
    "load_prompt",
    "PromptTemplate",
    # Replay sets (invariant 4)
    "create_replay_set",
    "add_replay_item",
    "lock_replay_set",
    "build_locked_replay_set",
    # Validators
    "validate_feature_as_of",
    "validate_model_timing",
    "validate_reference_timing",
    "assert_replay_set_locked",
    "InvariantViolation",
    # Store / ORM
    "make_engine",
    "Trace",
    "ModelRegistryEntry",
    "ReplaySet",
    "ReplaySetItem",
    "ReplaySetLockedError",
}


def test_public_api_surface_is_frozen():
    assert set(traceguard.__all__) == EXPECTED_PUBLIC_API


def test_all_has_no_duplicates():
    assert len(traceguard.__all__) == len(set(traceguard.__all__))


def test_every_public_name_is_importable():
    for name in traceguard.__all__:
        assert hasattr(traceguard, name), f"{name} listed in __all__ but not importable"


def test_version_is_a_string():
    assert isinstance(traceguard.__version__, str)
    assert traceguard.__version__.count(".") >= 2


def test_deep_paths_still_work_as_aliases():
    """Pinned consumers import deep paths; top-level re-export must not break them."""
    from traceguard.registry.models import select_model
    from traceguard.sdk.tracer import tracer as deep_tracer
    from traceguard.validators.lookahead import assert_replay_set_locked

    assert traceguard.select_model is select_model
    assert traceguard.tracer is deep_tracer
    assert traceguard.assert_replay_set_locked is assert_replay_set_locked


def test_py_typed_marker_is_packaged():
    """PEP 561: the marker must sit inside the installed package directory."""
    import os

    pkg_dir = os.path.dirname(importlib.import_module("traceguard").__file__)
    assert os.path.exists(os.path.join(pkg_dir, "py.typed"))

"""TraceGuard SDK — point-in-time correct LLM instrumentation.

See ../TRACEGUARD_SPEC.md for the binding interface contract.

The names re-exported here (and enumerated in ``__all__``) are the **stable
public API**. From 1.0 they follow SemVer per docs/SPEC.md §6, and the deep
submodule paths they come from remain importable as aliases so pinned consumers
do not break.

Opt-in, non-contract extensions are **experimental** and deliberately kept off
this frozen surface — import them from their submodule paths:

- ``traceguard.exporters.otel`` (extra ``traceguard[otel]``) — OpenTelemetry /
  OpenInference export.
- ``traceguard.contamination`` (extra ``traceguard[contamination]`` /
  ``[contamination-hf]``) — training-contamination estimators.
- ``traceguard.loop`` — self-improvement-loop evidence gating.
"""
from __future__ import annotations

__version__ = "0.9.0"

from traceguard.registry.models import NoEligibleModelError, register_model, select_model
from traceguard.registry.prompts import PromptTemplate, load_prompt
from traceguard.registry.replay import (
    add_replay_item,
    build_locked_replay_set,
    create_replay_set,
    lock_replay_set,
)
from traceguard.sdk.normalizer import input_hash, normalize_input
from traceguard.sdk.tracer import Span, Tracer, tracer
from traceguard.sdk.wrappers._base import resolve_feature_as_of
from traceguard.sdk.wrappers.anthropic import wrap_anthropic
from traceguard.sdk.wrappers.openai import wrap_openai
from traceguard.store.models import (
    ModelRegistryEntry,
    ReplaySet,
    ReplaySetItem,
    ReplaySetLockedError,
    Trace,
    make_engine,
)
from traceguard.validators.lookahead import (
    InvariantViolation,
    assert_replay_set_locked,
    validate_feature_as_of,
    validate_model_timing,
    validate_reference_timing,
)

__all__ = [
    "__version__",
    # Instrumentation (SPEC §4.1, §4.4)
    "Tracer",
    "Span",
    "tracer",
    "normalize_input",
    "input_hash",
    "wrap_anthropic",
    "wrap_openai",
    "resolve_feature_as_of",
    # Model + prompt registries (SPEC §4.2, §4.3)
    "select_model",
    "register_model",
    "NoEligibleModelError",
    "load_prompt",
    "PromptTemplate",
    # Replay sets — invariant 4 write-path (SPEC §3.4)
    "create_replay_set",
    "add_replay_item",
    "lock_replay_set",
    "build_locked_replay_set",
    # Invariant validators 1–4 (SPEC §4.5, §5)
    "validate_feature_as_of",
    "validate_model_timing",
    "validate_reference_timing",
    "assert_replay_set_locked",
    "InvariantViolation",
    # Store / ORM (SPEC §3)
    "make_engine",
    "Trace",
    "ModelRegistryEntry",
    "ReplaySet",
    "ReplaySetItem",
    "ReplaySetLockedError",
]

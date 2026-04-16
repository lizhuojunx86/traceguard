"""Pipeline Guardian — public API surface.

This module re-exports the symbols that constitute the **frozen** public API
for the v0.1.0-huadian-baseline release. Anything not listed in ``__all__``
(including all submodules under ``guardian.core.*``, ``guardian.validators.*``,
``guardian.store.*``, etc.) is **internal** and may change without notice in
0.2.0+.

Downstream integrators MUST pin a specific git SHA or tag (not ``main``) and
treat removal/rename of any name in ``__all__`` as a breaking change requiring
a major version bump.
"""
from guardian.core.config import GuardianConfig
from guardian.core.guardian_node import GuardianDecision, evaluate_async
from guardian.core.step import StepOutput

__all__ = [
    "evaluate_async",
    "StepOutput",
    "GuardianConfig",
    "GuardianDecision",
]

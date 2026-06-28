"""Shared plumbing for the delegating OpenAI/Anthropic SDK wrappers.

The ``wrap_*`` wrappers are thin delegating shells: each holds the wrapped
object under a private attribute (``_client`` on the outer client wrappers,
``_original`` on the inner endpoint wrappers) and forwards every other
attribute through ``__getattr__``. Two cross-cutting concerns are factored
here so all six wrapper classes inherit them identically.

1. ``__getattr__`` must NOT delegate private/dunder names. ``copy.deepcopy``
   and ``pickle`` reconstruct an object via ``cls.__new__(cls)`` (no
   ``__init__``) and then probe it for ``__deepcopy__`` / ``__setstate__`` /
   ``__reduce_ex__`` etc. On that half-constructed instance the delegate
   attribute is not set yet, so a naive ``__getattr__`` that forwards to
   ``self._client`` recurses forever resolving ``_client`` itself
   (``RecursionError``). Raising ``AttributeError`` for any ``_``-prefixed
   name lets the copy/pickle protocol fall back to its default behaviour.
   Frameworks such as LangChain/LlamaIndex deepcopy LLM clients, so without
   this guard a wrapped client crashes on adoption.

2. ``__deepcopy__`` deep-copies the wrapped client but *shares* the
   :class:`~traceguard.sdk.tracer.Tracer`. A tracer is a process-level sink
   (like a logger): copying it is both impossible — its SQLAlchemy ``Engine``
   holds module references that are not deep-copyable — and semantically
   wrong, since two deep-copied clients should keep writing to the same trace
   store. Without this, the ``__getattr__`` guard alone only converts the
   ``RecursionError`` into ``TypeError: cannot pickle 'module' object`` when
   deepcopy reaches the engine.
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime
from typing import Any, Callable, Optional, Union

_log = logging.getLogger("traceguard.wrappers")

# Point-in-time stamp for instrumented calls. A fixed ``datetime`` is stamped on
# every call; a zero-arg callable is resolved at *each* call (e.g. it reads a
# contextvar a backtest loop sets, so successive calls can simulate different
# moments without changing the ``create()`` call site); ``None`` records no
# ``feature_as_of`` (the default — fully backward compatible). Once stamped, the
# resulting trace becomes checkable by the look-ahead invariants (SPEC §3).
FeatureAsOf = Union[datetime, Callable[[], Optional[datetime]], None]


def _resolve_feature_as_of(value: FeatureAsOf) -> Optional[datetime]:
    """Resolve a :data:`FeatureAsOf` to a tz-aware datetime (or ``None``) per call.

    Fail-open (SPEC §4.1): instrumentation must never break the host LLM call. So
    a callable that raises, or a naive (tz-less) datetime — which the store would
    reject (``UTCDateTime`` requires tz-aware), silently dropping the whole trace
    under the default fail-open tracer — is downgraded to ``feature_as_of=None``
    with a warning. That keeps the trace (an honest missing stamp the consumer's
    invariant check will surface) instead of a wrong timestamp or a lost row.
    """
    if callable(value):
        try:
            value = value()
        except Exception:  # noqa: BLE001 - fail-open: never break the host call
            _log.warning(
                "feature_as_of callable raised; recording trace with feature_as_of=None",
                exc_info=True,
            )
            return None
    if value is not None and getattr(value, "tzinfo", None) is None:
        _log.warning(
            "feature_as_of is a naive datetime (no tzinfo); recording feature_as_of=None "
            "— pass a tz-aware datetime, e.g. datetime.now(timezone.utc)",
        )
        return None
    return value


class _DelegatingWrapper:
    """Mixin providing copy-safe attribute delegation for the SDK wrappers."""

    # Name of the instance attribute holding the wrapped object. Overridden to
    # ``"_client"`` on the outer client wrappers; the inner endpoint wrappers
    # use the default.
    _delegate_attr: str = "_original"

    def __getattr__(self, name: str) -> Any:
        # Never delegate private/dunder lookups: that is what makes copy/pickle
        # protocol probing recurse on a half-constructed instance (see module
        # docstring). Real public attributes are forwarded to the wrapped object.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(getattr(self, self._delegate_attr), name)

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        cls = self.__class__
        new = cls.__new__(cls)
        memo[id(self)] = new
        for key, value in self.__dict__.items():
            # Share the engine-backed tracer; deep-copy everything else
            # (including the wrapped client) so the copy is independent.
            new.__dict__[key] = value if key == "_tracer" else copy.deepcopy(value, memo)
        return new

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
from typing import Any


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

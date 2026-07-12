"""traceguard.resolve_feature_as_of — public point-in-time helper.

Exposed (0.9.0) so consumers that instrument by hand (their own Tracer.span,
e.g. a no-SDK / bare-httpx client wrap_openai can't attach to) get the same
fail-open feature_as_of semantics as the wrappers instead of re-implementing
them. Surfaced by the quant_alpha_v2 pilot.
"""
from __future__ import annotations

from datetime import datetime, timezone

import traceguard
from traceguard import resolve_feature_as_of

AS_OF = datetime(2025, 1, 1, tzinfo=timezone.utc)


def test_exported_at_top_level():
    assert traceguard.resolve_feature_as_of is resolve_feature_as_of


def test_datetime_passthrough():
    assert resolve_feature_as_of(AS_OF) == AS_OF


def test_none_passthrough():
    assert resolve_feature_as_of(None) is None


def test_callable_is_resolved():
    assert resolve_feature_as_of(lambda: AS_OF) == AS_OF


def test_naive_datetime_downgraded_to_none():
    assert resolve_feature_as_of(datetime(2025, 1, 1)) is None  # no tzinfo


def test_callable_raising_is_fail_open():
    def boom():
        raise RuntimeError("as-of source down")

    assert resolve_feature_as_of(boom) is None


def test_callable_returning_naive_downgraded():
    assert resolve_feature_as_of(lambda: datetime(2025, 1, 1)) is None

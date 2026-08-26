"""Contract-external opt-in extension: was invariant 2 actually checked?

Purely additive, like ``routing_audit`` / ``contamination`` / ``loop``: nothing
here is re-exported from the top-level ``traceguard`` package, no MUST field is
added or changed, and the normalize algorithm is untouched. Import by submodule
path only::

    from traceguard.routing_integrity import classify, scan

The problem it exists for
-------------------------
``validate_model_timing`` checks the model id on the trace, and SPEC §3.1 fixes
that field as the model the caller *requested*. Direct against a provider,
requested and served are the same string and the check means what it looks like.
Behind an OpenAI-compatible gateway they can differ per request — ask for
``orcarouter/auto`` and DeepSeek or Claude answers, chosen by the router's
policy and upstream health on the day.

So the invariant can pass while checking a name that was never a model. Nothing
raises. The backtest comes out green. This module names that state instead of
letting it hide: it reads ``output_parsed["routing"]`` (written by the SDK
wrappers) and tells you, per trace, whether invariant 2 was checked against
something real. Traces written before that capture existed fall back to
``model_id`` and are classified on what is knowable from it.

It does not weaken or replace invariant 2 — a trace that fails
``validate_model_timing`` still fails. It answers the prior question: *was there
anything meaningful to check?*

CLI::

    python -m traceguard.routing_integrity --db sqlite:///traces.db
"""
from __future__ import annotations

from traceguard.routing_integrity.check import (
    Verdict,
    classify,
    classify_trace,
    scan,
)

__all__ = ["Verdict", "classify", "classify_trace", "scan"]

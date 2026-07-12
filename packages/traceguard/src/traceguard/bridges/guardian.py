"""Bridge: write a traceguard ``Trace`` from a pipeline-guardian checkpoint.

Experimental — API may change, not under the frozen 1.0 surface (SPEC §6.6).

Lets a project already running ``guardian`` adopt traceguard with a ~5-line,
opt-in adapter at its existing ``GuardianDecision`` seam — *without* changing its
pinned guardian dependency. Each Guardian checkpoint becomes one point-in-time
trace row (input hash, the decision, optional ``feature_as_of``) as a
fire-and-forget side effect.

Design constraints (mirroring ``exporters``):
- **Never imports guardian.** The guardian objects are duck-typed via ``getattr``
  / their documented methods, so traceguard keeps a zero dependency on guardian
  (the two-package firewall holds) and the bridge works with guardian
  uninstalled — only the caller, which already has guardian, supplies the
  objects.
- **Fully fail-open** (SPEC §4.1): any error — a malformed object, a persistence
  failure — is swallowed and the call returns ``None``. Bridging a checkpoint
  must never break or mask the host guardian pipeline.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from traceguard.sdk.tracer import Tracer
from traceguard.sdk.tracer import tracer as default_tracer
from traceguard.sdk.wrappers._base import FeatureAsOf, resolve_feature_as_of

_log = logging.getLogger("traceguard.bridges.guardian")

_OPERATION = "guardian_check"


def _output_payload(output: Any) -> Any:
    """Best-effort, duck-typed extraction of a guardian ``StepOutput``'s body.

    Prefer the parsed dict, fall back to the string form, then the raw attribute
    — whichever the object exposes. The result is hashed via ``normalize_input``
    inside the span, so it just needs to be a stable representation of the output.
    """
    as_dict = getattr(output, "output_as_dict", None)
    if callable(as_dict):
        try:
            parsed = as_dict()
        except Exception:  # noqa: BLE001 - duck-typed; fall through to other forms
            parsed = None
        if parsed is not None:
            return parsed
    as_str = getattr(output, "output_as_string", None)
    if callable(as_str):
        return as_str()
    return getattr(output, "output_data", None)


def _decision_payload(decision: Any) -> dict[str, Any]:
    """Duck-typed snapshot of a guardian ``GuardianDecision`` for ``output_parsed``."""
    issues = getattr(decision, "issues", None)
    return {
        "action": getattr(decision, "action", None),
        "score": getattr(decision, "score", None),
        "issues": list(issues) if issues is not None else [],
        "semantic_score": getattr(decision, "semantic_score", None),
        "semantic_status": getattr(decision, "semantic_status", None),
        "flag_type": getattr(decision, "flag_type", None),
        "retry_hint": getattr(decision, "retry_hint", None),
    }


def write_trace_from_guardian(
    output: Any,
    decision: Any,
    *,
    project: str,
    component: str | None = None,
    tracer: Tracer | None = None,
    feature_as_of: FeatureAsOf = None,
) -> Optional[int]:
    """Persist one traceguard trace from a guardian checkpoint; return its id.

    Args:
        output: A guardian ``StepOutput`` (duck-typed: ``step_name``,
            ``output_as_dict`` / ``output_as_string`` / ``output_data``,
            ``metadata``).
        decision: A guardian ``GuardianDecision`` (duck-typed: ``action``,
            ``score``, ``issues``, ``semantic_score``, ``semantic_status``,
            ``flag_type``, ``retry_hint``).
        project: Project label recorded on the trace.
        component: Component label; defaults to the output's ``step_name``.
        tracer: Tracer to persist into; defaults to the module-level tracer.
        feature_as_of: Point-in-time stamp (``datetime`` | zero-arg callable |
            ``None``). If ``None``, falls back to ``output.metadata['feature_as_of']``.
            Stamping it makes the bridged trace checkable by the look-ahead
            invariants (SPEC §3).

    Returns:
        The new trace's ``trace_id``, or ``None`` if anything went wrong
        (fail-open — the host pipeline is never affected).
    """
    try:
        tr = tracer or default_tracer
        comp = component or getattr(output, "step_name", None) or "guardian"

        as_of: FeatureAsOf = feature_as_of
        if as_of is None:
            metadata = getattr(output, "metadata", None)
            if isinstance(metadata, dict):
                as_of = metadata.get("feature_as_of")

        # Extract (duck-typed) BEFORE opening the span: if a malformed object
        # raises here, no span opens and nothing is written — a span that failed
        # mid-body would otherwise flush a spurious error trace.
        input_payload = _output_payload(output)
        decision_payload = _decision_payload(decision)
        resolved_as_of = resolve_feature_as_of(as_of)

        with tr.span(
            project,
            str(comp),
            operation=_OPERATION,
            feature_as_of=resolved_as_of,
        ) as span:
            span.record_input(input_payload)
            span.record_output(parsed=decision_payload, parse_status="success")
        return span.trace_id
    except Exception:  # noqa: BLE001 - fail-open: never break the host guardian pipeline
        _log.warning("write_trace_from_guardian failed; no trace written", exc_info=True)
        return None

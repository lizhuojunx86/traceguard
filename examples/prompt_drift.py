"""Example: a prompt rewrite is caught by the content hash in every trace.

Silently editing a prompt is a classic source of irreproducible results — the
backtest you ran last month used a prompt that no longer exists. TraceGuard
pins the SHA-256 of the prompt body into every trace, so two prompt versions
produce two different hashes that are visible in the traces table forever.

It also treats a prompt template as *time-versioned reference data*: a prompt's
introduced_at must be <= the feature_as_of it is used to compute (invariant 3,
validate_reference_timing). Using v2 (introduced 2026-06-01) to compute a
feature dated 2026-05-20 is a look-ahead violation.

Synthetic — no API keys, no network, in-memory SQLite.

Run (from the repo root)::

    uv run python examples/prompt_drift.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "traceguard" / "src"
if _PKG_SRC.is_dir() and str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from traceguard.registry.prompts import load_prompt  # noqa: E402
from traceguard.sdk.tracer import Tracer  # noqa: E402
from traceguard.store.models import Trace, make_engine  # noqa: E402
from traceguard.validators.lookahead import (  # noqa: E402
    InvariantViolation,
    validate_reference_timing,
)

HERE = Path(__file__).parent
PROMPTS_ROOT = HERE / "prompts"
UTC = timezone.utc


def _trace_call(tracer: Tracer, prompt, text: str, feature_as_of: datetime) -> None:
    """Trace one (simulated) extraction call with a given prompt version."""
    with tracer.span(
        "prompt_drift",
        "extractor",
        "llm_complete",
        correlation_id="doc-001",
        feature_as_of=feature_as_of,
    ) as span:
        span.record_input({"text": text})
        span.record_model_prompt(
            prompt_template_id=prompt.prompt_template_id,
            prompt_template_hash=prompt.prompt_template_hash,
        )
        span.record_output(parsed={"entities": []}, parse_status="success")


def main() -> int:
    engine = make_engine("sqlite:///:memory:")
    tracer = Tracer(engine)
    text = "Acme Corp acquired Widget Inc in 2024."

    # 1) Load two versions of the same prompt id family.
    v1 = load_prompt("demo/extractor/v1", prompts_root=PROMPTS_ROOT)
    v2 = load_prompt("demo/extractor/v2", prompts_root=PROMPTS_ROOT)
    print(f"[1] {v1.prompt_template_id} hash={v1.prompt_template_hash[:12]}…")
    print(f"[1] {v2.prompt_template_id} hash={v2.prompt_template_hash[:12]}…")
    assert v1.prompt_template_hash != v2.prompt_template_hash, "drift went undetected!"

    # 2) Trace one call per version; the differing hashes land in the table.
    as_of = datetime(2026, 6, 15, tzinfo=UTC)  # after both prompts exist
    _trace_call(tracer, v1, v1.render(text=text), as_of)
    _trace_call(tracer, v2, v2.render(text=text), as_of)

    with Session(engine) as sess:
        rows = sess.execute(select(Trace).order_by(Trace.trace_id)).scalars().all()
        hashes = {r.prompt_template_id: r.prompt_template_hash for r in rows}
        for r in rows:
            print(
                f"[2] trace #{r.trace_id}: prompt={r.prompt_template_id} "
                f"hash={r.prompt_template_hash[:12]}… input_hash={r.input_hash[:12]}…"
            )
        assert len(set(hashes.values())) == 2, "tracer failed to record the drift"
        print("[2] two distinct prompt hashes recorded — drift is visible in traces ✓")

    # 3) A prompt is time-versioned reference data: introduced_at <= feature_as_of.
    #    v1 (introduced 2026-05-18) is valid for a 2026-05-20 feature...
    early_feature = datetime(2026, 5, 20, tzinfo=UTC)
    validate_reference_timing(
        valid_from=v1.introduced_at, feature_as_of=early_feature, kind="prompt_template"
    )
    print(f"[3] v1 valid for feature as-of {early_feature.date()} ✓")

    # ...but v2 (introduced 2026-06-01) did not exist yet on 2026-05-20.
    try:
        validate_reference_timing(
            valid_from=v2.introduced_at,
            feature_as_of=early_feature,
            kind="prompt_template",
        )
        raise SystemExit("BUG: anachronistic prompt was not blocked")
    except InvariantViolation as exc:
        print(f"[3] v2 used too early -> blocked: {exc}")

    print("\nprompt_drift OK — every prompt rewrite is hashed, traced, and time-checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

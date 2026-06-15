"""TraceGuard quickstart — entirely synthetic, no API keys, no network.

Demonstrates the core promise: when you run LLM pipelines over historical
data, TraceGuard makes it structurally hard to accidentally use a model or
prompt that did not yet exist at the point in time you are simulating.

Run:
    uv run python examples/quickstart/run_quickstart.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# Workaround: some Python builds (e.g. Homebrew 3.14) skip _-prefixed .pth files,
# breaking uv's editable install. Add the SDK source dir so the tour runs
# regardless of how the env was set up. (pytest uses pythonpath=["src"].)
_PKG_SRC = Path(__file__).resolve().parent.parent.parent / "packages" / "traceguard" / "src"
if _PKG_SRC.is_dir() and str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from traceguard.registry.models import (  # noqa: E402
    NoEligibleModelError,
    register_model,
    select_model,
)
from traceguard.registry.prompts import load_prompt  # noqa: E402
from traceguard.sdk.tracer import Tracer  # noqa: E402
from traceguard.store.models import Trace, make_engine  # noqa: E402
from traceguard.validators.lookahead import (  # noqa: E402
    InvariantViolation,
    validate_feature_as_of,
    validate_model_timing,
)

HERE = Path(__file__).parent
UTC = timezone.utc


def main() -> None:
    engine = make_engine("sqlite:///:memory:")

    # ------------------------------------------------------------------
    # 1. Model registry: two timestamps per model.
    #    released_at        = when the model existed in the world
    #    available_to_us_at = when YOUR system could actually call it
    # ------------------------------------------------------------------
    register_model(
        "demo-llm-2024",
        model_family="internal-ml",
        capability_class="general-llm",
        released_at=datetime(2024, 1, 10, tzinfo=UTC),
        available_to_us_at=datetime(2024, 2, 1, tzinfo=UTC),
        engine=engine,
    )
    register_model(
        "demo-llm-2026",
        model_family="internal-ml",
        capability_class="general-llm",
        released_at=datetime(2026, 1, 5, tzinfo=UTC),
        available_to_us_at=datetime(2026, 1, 15, tzinfo=UTC),
        engine=engine,
    )

    # ------------------------------------------------------------------
    # 2. Point-in-time model selection. Suppose we are backtesting a
    #    signal as of 2025-06-30: the 2026 model must be invisible.
    # ------------------------------------------------------------------
    backtest_date = datetime(2025, 6, 30, tzinfo=UTC)
    model_id = select_model(
        "general-llm", available_at=backtest_date, strict=True, engine=engine
    )
    print(f"[2] strict select at {backtest_date.date()} -> {model_id}")
    assert model_id == "demo-llm-2024"

    # At a 2023 point in time NO model existed yet: strict mode refuses.
    try:
        select_model(
            "general-llm",
            available_at=datetime(2023, 6, 1, tzinfo=UTC),
            strict=True,
            engine=engine,
        )
        raise SystemExit("BUG: anachronistic selection was not blocked")
    except NoEligibleModelError as exc:
        print(f"[2] strict select at 2023-06-01 -> blocked: {exc}")

    # ------------------------------------------------------------------
    # 3. Invariant 2 as a pure function you can call in CI: using the
    #    2026 model for a 2025 feature is a look-ahead violation.
    # ------------------------------------------------------------------
    try:
        validate_model_timing(
            "demo-llm-2026", backtest_date, strict=True, engine=engine
        )
        raise SystemExit("BUG: invariant 2 violation was not raised")
    except InvariantViolation as exc:
        print(f"[3] validate_model_timing -> {exc}")

    # ------------------------------------------------------------------
    # 4. Versioned prompt: loaded from a git-tracked YAML, hash pinned.
    # ------------------------------------------------------------------
    prompt = load_prompt("demo/extractor/v1", prompts_root=HERE / "prompts")
    rendered = prompt.render(text="Acme Corp acquired Widget Inc in 2024.")
    print(f"[4] prompt {prompt.prompt_template_id} hash={prompt.prompt_template_hash[:12]}…")

    # ------------------------------------------------------------------
    # 5. Trace the (simulated) call: reproducible input hash, model and
    #    prompt versions, output, perf — one row in the traces table.
    # ------------------------------------------------------------------
    tracer = Tracer(engine)
    with tracer.span(
        "quickstart",
        "extractor",
        "llm_complete",
        correlation_id="doc-001",
        feature_as_of=backtest_date,
    ) as span:
        span.record_input({"text": rendered})
        span.record_model_prompt(
            model_id=model_id,
            prompt_template_id=prompt.prompt_template_id,
            prompt_template_hash=prompt.prompt_template_hash,
        )
        # ... here you would actually call the model ...
        span.record_output(
            parsed={"entities": [["Acme Corp", "ORG"], ["Widget Inc", "ORG"]]},
            parse_status="success",
        )
        span.record_perf(latency_ms=42, tokens_in=120, tokens_out=18, cost_usd=0.0004)

    with Session(engine) as sess:
        trace = sess.execute(select(Trace)).scalar_one()
        print(
            f"[5] trace #{trace.trace_id}: {trace.project}/{trace.component} "
            f"model={trace.model_id} prompt={trace.prompt_template_id} "
            f"input_hash={trace.input_hash[:12]}…"
        )

        # --------------------------------------------------------------
        # 6. Invariant 1: a downstream feature derived from this trace
        #    may not claim a feature_as_of EARLIER inputs don't support.
        # --------------------------------------------------------------
        validate_feature_as_of([trace], backtest_date)
        print(f"[6] feature_as_of={backtest_date.date()} consistent with inputs ✓")
        try:
            validate_feature_as_of([trace], datetime(2025, 12, 1, tzinfo=UTC))
            raise SystemExit("BUG: invariant 1 violation was not raised")
        except InvariantViolation as exc:
            print(f"[6] forward-dated feature -> {exc}")

    print("\nquickstart OK — every guard fired exactly where it should.")


if __name__ == "__main__":
    main()

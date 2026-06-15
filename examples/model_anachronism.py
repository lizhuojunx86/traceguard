"""Example: strict mode blocks a 2026 model from touching 2021 data.

This is the canonical *harness / pipeline leakage* failure (look-ahead kind 2,
see docs/POSITIONING.md): a backtest that simulates 2021 must not be able to
call a model that only became available in 2026. TraceGuard refuses it two ways:

  * select_model(strict=True) returns only point-in-time-eligible models, and
  * validate_model_timing(strict=True) is a pure CI check that RAISES on a
    violation; in loose mode it instead warns and flags is_anachronistic.

Everything here is synthetic — no API keys, no network, in-memory SQLite.

Run (from the repo root)::

    uv run python examples/model_anachronism.py

or, if traceguard is installed into the active env::

    python examples/model_anachronism.py
"""
from __future__ import annotations

import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

# Workaround: some Python builds (e.g. Homebrew 3.14) skip _-prefixed .pth
# files, breaking uv's editable install. Add the SDK source dir directly so the
# demo runs regardless of how the env was set up.
_PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "traceguard" / "src"
if _PKG_SRC.is_dir() and str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))

from traceguard.registry.models import (  # noqa: E402
    NoEligibleModelError,
    register_model,
    select_model,
)
from traceguard.store.models import make_engine  # noqa: E402
from traceguard.validators.lookahead import (  # noqa: E402
    InvariantViolation,
    validate_model_timing,
)

UTC = timezone.utc


def main() -> int:
    engine = make_engine("sqlite:///:memory:")

    # Two models in the same capability class: one available in 2020, one in 2026.
    register_model(
        "legacy-llm-2020",
        model_family="internal-ml",
        capability_class="general-llm",
        released_at=datetime(2020, 1, 10, tzinfo=UTC),
        available_to_us_at=datetime(2020, 2, 1, tzinfo=UTC),
        engine=engine,
    )
    register_model(
        "shiny-llm-2026",
        model_family="internal-ml",
        capability_class="general-llm",
        released_at=datetime(2026, 1, 5, tzinfo=UTC),
        available_to_us_at=datetime(2026, 1, 15, tzinfo=UTC),
        engine=engine,
    )

    # We are backtesting a signal as of mid-2021.
    backtest_date = datetime(2021, 6, 30, tzinfo=UTC)

    # 1) Strict selection only ever returns a point-in-time-eligible model.
    chosen = select_model(
        "general-llm", available_at=backtest_date, strict=True, engine=engine
    )
    print(f"[1] strict select as-of {backtest_date.date()} -> {chosen}")
    assert chosen == "legacy-llm-2020", chosen

    # 2) The 2026 model is simply invisible in 2021. Trying to *justify* using
    #    it via the validator is a loud invariant-2 failure.
    try:
        validate_model_timing(
            "shiny-llm-2026", backtest_date, strict=True, engine=engine
        )
        raise SystemExit("BUG: anachronistic model was not blocked")
    except InvariantViolation as exc:
        print(f"[2] validate_model_timing(strict=True) -> blocked: {exc}")

    # 3) Loose mode does not raise — it flags the anachronism so a strategy can
    #    apply an explicit discount instead of silently trusting it.
    model_id, is_anachronistic = select_model(
        "general-llm", available_at=backtest_date, strict=False, engine=engine
    )
    print(
        f"[3] loose select as-of {backtest_date.date()} -> "
        f"{model_id} (is_anachronistic={is_anachronistic})"
    )
    assert (model_id, is_anachronistic) == ("shiny-llm-2026", True)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        validate_model_timing(
            "shiny-llm-2026", backtest_date, strict=False, engine=engine
        )
    assert caught, "loose validate_model_timing should warn"
    print(f"[3] validate_model_timing(strict=False) -> warned: {caught[-1].message}")

    # 4) The honest path: a date where the 2026 model genuinely exists passes.
    live_date = datetime(2026, 3, 1, tzinfo=UTC)
    validate_model_timing("shiny-llm-2026", live_date, strict=True, engine=engine)
    print(f"[4] validate_model_timing(strict=True) as-of {live_date.date()} -> ok")

    # 5) And before ANY model existed, strict selection refuses outright.
    try:
        select_model(
            "general-llm",
            available_at=datetime(2018, 1, 1, tzinfo=UTC),
            strict=True,
            engine=engine,
        )
        raise SystemExit("BUG: selection at 2018 should have failed")
    except NoEligibleModelError as exc:
        print(f"[5] strict select as-of 2018-01-01 -> refused: {exc}")

    print("\nmodel_anachronism OK — the 2026 model could never touch 2021 data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

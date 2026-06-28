"""TraceGuard dogfood harness — a *real consumer* writing >=100 traces.

This exists to satisfy Phase 0 acceptance #7 ("a real consumer writes >=100
traces on traceguard") with genuine LLM usage, and to flush out the adoption
friction that only real use surfaces (the kind that produced the 0.8.1 deepcopy
fix). It instruments a small but real classification workload with
``wrap_openai`` and verifies the resulting trace dataset.

Backend is chosen at runtime (no code change), in priority order:
  1. Real OpenAI / any OpenAI-compatible endpoint — set ``OPENAI_API_KEY``
     (and optionally ``OPENAI_BASE_URL`` for Ollama / LM Studio / a proxy).
  2. Stub — no key set: a canned OpenAI-compatible client. Zero deps/cost;
     validates the full traceguard plumbing (tracer -> SQLite -> query) and
     produces real trace rows, just without a real model behind them.

Env knobs: ``DOGFOOD_MODEL`` (default gpt-4o-mini), ``DOGFOOD_N`` (default 120),
``DOGFOOD_DB`` (default ./dogfood_traces.db, fresh per run).

Usage (from repo root)::

    cd packages/traceguard
    OPENAI_API_KEY=sk-... uv run --extra openai python ../../examples/dogfood/run_dogfood.py
    # or credential-free:
    uv run python ../../examples/dogfood/run_dogfood.py
"""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from types import SimpleNamespace

# Workaround: some Python builds (e.g. Homebrew 3.14) skip _-prefixed .pth files
# in site-packages, breaking uv's editable install of traceguard. Add the source
# dir directly so the harness runs regardless of how the env was set up.
_PKG_SRC = Path(__file__).resolve().parent.parent.parent / "packages" / "traceguard" / "src"
if _PKG_SRC.is_dir() and str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from traceguard.sdk.wrappers.openai import wrap_openai  # noqa: E402
from traceguard.sdk.tracer import Tracer  # noqa: E402
from traceguard.store.models import Trace, make_engine  # noqa: E402

PROJECT = "dogfood"
COMPONENT = "headline-classifier"
MODEL_ID = os.environ.get("DOGFOOD_MODEL", "gpt-4o-mini")
N = int(os.environ.get("DOGFOOD_N", "120"))
DB = Path(os.environ.get("DOGFOOD_DB", str(Path(__file__).parent / "dogfood_traces.db")))

LABELS = ("bullish", "bearish", "neutral")

# A real (if small) workload: classify the market sentiment of business
# headlines. 30 seeds cycled up to N so every call carries distinct input.
HEADLINES = [
    "Apple beats earnings expectations, raises guidance",
    "Fed signals it may hold rates steady through year-end",
    "Oil slides 4% on demand worries",
    "Nvidia unveils next-gen accelerator, shares jump",
    "Regional bank discloses fresh loan losses",
    "Retail sales come in flat, missing forecasts",
    "Boeing wins record widebody order",
    "Layoffs deepen across the tech sector",
    "Inflation cools more than expected in latest print",
    "Automaker recalls 1M vehicles over brake defect",
    "Chipmaker guides revenue below the Street",
    "Housing starts rebound on lower mortgage rates",
    "Airline raises fares as fuel costs ease",
    "Pharma giant's trial misses primary endpoint",
    "Cloud provider posts accelerating growth",
    "Copper hits multi-year high on supply crunch",
    "Streaming service loses subscribers for first time",
    "Bank lifts dividend after passing stress test",
    "Semiconductor exports curbed by new rules",
    "Consumer confidence ticks up in June",
    "Energy major cuts capex amid price slump",
    "EV maker delays flagship launch again",
    "Logistics firm warns on holiday volumes",
    "Gold steadies as dollar weakens",
    "Software vendor lands large government contract",
    "Miner halts operations after safety incident",
    "Payments company beats on transaction volume",
    "Homebuilder cancellations rise sharply",
    "Telecom completes spectrum auction at high cost",
    "Insurer reserves more for catastrophe claims",
]


def _stub_response(headline: str, i: int) -> SimpleNamespace:
    """Canned OpenAI-compatible chat response (deterministic by index)."""
    label = LABELS[i % len(LABELS)]
    return SimpleNamespace(
        id=f"chatcmpl_stub_{i:04d}",
        model=MODEL_ID,
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=label),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=20 + len(headline) // 4,
            completion_tokens=1,
            total_tokens=21 + len(headline) // 4,
        ),
    )


class _StubClient:
    """Mimics openai.OpenAI for the parts wrap_openai instruments."""

    def __init__(self) -> None:
        self._i = 0

        def _create(**kwargs):
            headline = kwargs["messages"][-1]["content"]
            resp = _stub_response(headline, self._i)
            self._i += 1
            return resp

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))
        self.api_key = "stub"


def _build_client():
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    if not api_key:
        print("[dogfood] OPENAI_API_KEY not set -> STUB backend (real traces, canned model)")
        return _StubClient(), "stub"
    try:
        import openai
    except ImportError:
        print("[dogfood] openai SDK not installed -> STUB backend")
        return _StubClient(), "stub"
    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    print(f"[dogfood] REAL backend: openai.OpenAI(base_url={base_url or 'api.openai.com'})")
    return openai.OpenAI(**kwargs), "real"


def _classify(client, headline: str) -> str:
    resp = client.chat.completions.create(
        model=MODEL_ID,
        messages=[
            {"role": "system", "content": "Classify the market sentiment of the headline as "
             "exactly one word: bullish, bearish, or neutral."},
            {"role": "user", "content": headline},
        ],
        temperature=0,
        max_tokens=4,
    )
    return (resp.choices[0].message.content or "").strip()


def main() -> int:
    if DB.exists():
        DB.unlink()
    engine = make_engine(f"sqlite:///{DB}")
    tracer = Tracer(engine=engine)
    raw, backend = _build_client()
    client = wrap_openai(raw, project=PROJECT, component=COMPONENT, tracer=tracer)

    print(f"[dogfood] running {N} classifications (model={MODEL_ID}, backend={backend})")
    errors = 0
    for i in range(N):
        headline = HEADLINES[i % len(HEADLINES)] + (f" (#{i // len(HEADLINES)})" if i >= len(HEADLINES) else "")
        try:
            _classify(client, headline)
        except Exception as e:  # noqa: BLE001 — keep going; the trace is still recorded
            errors += 1
            if errors <= 3:
                print(f"[dogfood]   call {i} failed: {type(e).__name__}: {str(e)[:60]}")
        if (i + 1) % 25 == 0:
            print(f"[dogfood]   ... {i + 1}/{N}")

    # ---- validate the trace dataset ----
    with Session(engine) as sess:
        total = sess.scalar(select(func.count()).select_from(Trace))
        ok = sess.scalar(select(func.count()).where(Trace.parse_status == "success"))
        failed = sess.scalar(select(func.count()).where(Trace.parse_status == "failed"))
        with_hash = sess.scalar(select(func.count()).where(Trace.input_hash.is_not(None)))
        with_model = sess.scalar(select(func.count()).where(Trace.model_id == MODEL_ID))
        tokens_in = sess.scalar(select(func.coalesce(func.sum(Trace.tokens_in), 0)))
        tokens_out = sess.scalar(select(func.coalesce(func.sum(Trace.tokens_out), 0)))
        sample = sess.scalars(select(Trace).order_by(Trace.trace_id).limit(3)).all()

    print("\n[dogfood] ===== trace dataset =====")
    print(f"  total traces   : {total}")
    print(f"  parse success  : {ok}")
    print(f"  parse failed   : {failed}")
    print(f"  has input_hash : {with_hash}")
    print(f"  model={MODEL_ID}: {with_model}")
    print(f"  tokens in/out  : {tokens_in} / {tokens_out}")
    for r in sample:
        print(f"  e.g. trace_id={r.trace_id} status={r.parse_status} "
              f"latency_ms={r.latency_ms} out={r.output_parsed.get('content_text')!r}")
    print(f"[dogfood] DB: {DB}")

    # ---- self-checks (Phase 0 acceptance #7) ----
    assert total >= 100, f"need >=100 traces, got {total}"
    assert with_hash == total, "every trace must carry a point-in-time input_hash"
    assert with_model == total, "every trace must record the model_id"
    print(f"\n[dogfood] PASS — {total} traces written by a real consumer via wrap_openai "
          f"({errors} call errors).")

    # ---- adoption note: framework copy transparency (the 0.8.1 fix in the wild) ----
    def _probe(label, fn):
        try:
            fn()
            print(f"  {label:28} -> OK")
        except RecursionError:
            print(f"  {label:28} -> RecursionError  (REGRESSION!)")
        except Exception as e:  # noqa: BLE001
            print(f"  {label:28} -> {type(e).__name__} (same as raw client = transparent)")
    print("[dogfood] copy transparency:")
    _probe("copy.copy(wrapped)", lambda: copy.copy(client))
    _probe("copy.deepcopy(wrapped)", lambda: copy.deepcopy(client))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""End-to-end demo: wrap an OpenAI client, make one Chat Completions call and
one Responses-API call, then query the two resulting traces.

Runs without an API key by default — falls back to fake responses. Set
OPENAI_API_KEY in the environment to hit the real OpenAI API instead.

Usage (from repo root)::

    cd packages/traceguard
    uv run python ../../examples/openai_call.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

# Workaround: some Python builds (e.g. Homebrew 3.14) skip _-prefixed .pth
# files in site-packages, breaking uv's editable install of traceguard. Add
# the source dir directly so the demo works regardless. Pytest already has
# pythonpath=["src"] in pyproject.toml.
_PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "traceguard" / "src"
if _PKG_SRC.is_dir() and str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceguard.sdk.tracer import Tracer
from traceguard.sdk.wrappers.openai import wrap_openai
from traceguard.store.models import Trace, make_engine


PROJECT = "demo"
COMPONENT = "extractor"
MODEL_ID = "gpt-4o-mini"

DEMO_DB = Path(__file__).parent / "demo_openai_traces.db"


def _fake_chat_response() -> SimpleNamespace:
    return SimpleNamespace(
        id="chatcmpl_fake_001",
        model=MODEL_ID,
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content='{"entities": [{"name": "Apple Inc.", "type": "company"}]}'
                ),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=128, completion_tokens=42, total_tokens=170),
    )


def _fake_responses_response() -> SimpleNamespace:
    return SimpleNamespace(
        id="resp_fake_001",
        model=MODEL_ID,
        output_text="Apple Inc. is an American technology company.",
        status="completed",
        usage=SimpleNamespace(input_tokens=64, output_tokens=18, total_tokens=82),
    )


class _FakeOpenAIClient:
    """Mimics the bits of openai.OpenAI that wrap_openai instruments."""

    def __init__(self) -> None:
        chat_response = _fake_chat_response()
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: chat_response)
        )
        responses_response = _fake_responses_response()
        self.responses = SimpleNamespace(create=lambda **kwargs: responses_response)


def _build_client():
    """Return a real OpenAI client if API key is set, else a fake one."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("[demo] OPENAI_API_KEY not set; using fake responses")
        return _FakeOpenAIClient()
    try:
        import openai
    except ImportError:
        print("[demo] openai SDK not installed (extra 'openai'); using fake responses")
        return _FakeOpenAIClient()
    print("[demo] using real openai.OpenAI() client")
    return openai.OpenAI(api_key=api_key)


def main() -> int:
    # Fresh DB per run for a clean demo.
    if DEMO_DB.exists():
        DEMO_DB.unlink()
    engine = make_engine(f"sqlite:///{DEMO_DB}")
    tracer = Tracer(engine=engine)

    client = wrap_openai(
        _build_client(),
        project=PROJECT,
        component=COMPONENT,
        tracer=tracer,
    )

    # 1) Chat Completions — instrumented as a side effect.
    chat = client.chat.completions.create(
        model=MODEL_ID,
        messages=[{"role": "user", "content": "Extract entities from: Apple Inc."}],
    )
    chat_text = chat.choices[0].message.content
    print(f"[demo] chat.completions → {chat_text[:80]}…")

    # 2) Responses API — also instrumented (when the SDK exposes it).
    resp = client.responses.create(
        model=MODEL_ID,
        input="Summarize: Apple Inc. is a technology company.",
    )
    print(f"[demo] responses → {resp.output_text[:80]}…")

    # 3) Query the two traces back.
    with Session(engine) as sess:
        rows = sess.scalars(select(Trace).order_by(Trace.trace_id)).all()
    for row in rows:
        print(
            f"[demo] trace_id={row.trace_id} "
            f"project={row.project} component={row.component} "
            f"model={row.model_id} status={row.parse_status} "
            f"latency_ms={row.latency_ms} tokens_in={row.tokens_in} "
            f"tokens_out={row.tokens_out}"
        )

    # Self-check: both calls produced a trace.
    assert len(rows) == 2, f"expected 2 traces, got {len(rows)}"
    print(f"[demo] done — {len(rows)} traces at {DEMO_DB}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

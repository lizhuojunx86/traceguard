"""End-to-end demo: register a model, load a prompt, wrap an Anthropic
client, make one call, query the resulting trace, and check invariant 2.

Runs without an API key by default — falls back to a fake response. Set
ANTHROPIC_API_KEY in the environment to hit the real Anthropic API instead.

Usage (from repo root)::

    cd packages/traceguard
    uv run python ../../examples/anthropic_call.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
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

from traceguard.registry import load_prompt, register_model, select_model
from traceguard.sdk.tracer import Tracer
from traceguard.sdk.wrappers.anthropic import wrap_anthropic
from traceguard.store.models import Trace, make_engine
from traceguard.validators import validate_model_timing


PROJECT = "demo"
COMPONENT = "extractor"
MODEL_ID = "claude-sonnet-4-5-20260101"
PROMPT_ID = "demo/extractor/v1"

DEMO_DB = Path(__file__).parent / "demo_traces.db"
PROMPTS_ROOT = Path(__file__).parent / "prompts"


def _fake_anthropic_response() -> SimpleNamespace:
    return SimpleNamespace(
        id="msg_fake_001",
        content=[
            SimpleNamespace(
                text='{"entities": [{"name": "Apple Inc.", "type": "company"}]}'
            )
        ],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=128, output_tokens=42),
    )


class _FakeAnthropicClient:
    def __init__(self) -> None:
        response = _fake_anthropic_response()
        self.messages = SimpleNamespace(create=lambda **kwargs: response)


def _build_client():
    """Return a real Anthropic client if API key is set, else a fake one."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[demo] ANTHROPIC_API_KEY not set; using a fake response")
        return _FakeAnthropicClient()
    try:
        import anthropic
    except ImportError:
        print(
            "[demo] anthropic SDK not installed (extra 'anthropic'); "
            "using a fake response"
        )
        return _FakeAnthropicClient()
    print("[demo] using real anthropic.Anthropic() client")
    return anthropic.Anthropic(api_key=api_key)


def main() -> int:
    # Fresh DB per run for a clean demo.
    if DEMO_DB.exists():
        DEMO_DB.unlink()
    engine = make_engine(f"sqlite:///{DEMO_DB}")

    # 1) Register the model (would fail noisily if already there).
    register_model(
        MODEL_ID,
        model_family="anthropic",
        capability_class="general-llm",
        released_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        available_to_us_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        engine=engine,
    )
    print(f"[demo] registered model {MODEL_ID}")

    # 2) Look up the model via SPEC-compliant API (strict required).
    chosen = select_model(
        "general-llm",
        available_at=datetime.now(timezone.utc),
        strict=True,
        engine=engine,
    )
    print(f"[demo] select_model(strict=True) → {chosen}")

    # 3) Load the prompt template from YAML on disk.
    prompt = load_prompt(PROMPT_ID, prompts_root=PROMPTS_ROOT)
    rendered = prompt.render(text="Apple Inc. is an American technology company.")
    print(f"[demo] prompt hash {prompt.prompt_template_hash[:12]}…")

    # 4) Wrap an Anthropic client (real or fake) and make one call.
    tracer = Tracer(engine=engine)
    client = wrap_anthropic(
        _build_client(),
        project=PROJECT,
        component=COMPONENT,
        tracer=tracer,
    )
    response = client.messages.create(
        model=MODEL_ID,
        max_tokens=512,
        messages=[{"role": "user", "content": rendered}],
    )
    response_text = "".join(
        getattr(b, "text", "") for b in getattr(response, "content", [])
    )
    print(f"[demo] response: {response_text[:120]}…")

    # 5) Query the trace back.
    with Session(engine) as sess:
        row = sess.scalars(select(Trace)).one()
        print(
            f"[demo] trace_id={row.trace_id} "
            f"project={row.project} component={row.component} "
            f"model={row.model_id} status={row.parse_status} "
            f"latency_ms={row.latency_ms} tokens_in={row.tokens_in} "
            f"tokens_out={row.tokens_out}"
        )

    # 6) Demonstrate invariant 2: validate the model timing in strict mode.
    validate_model_timing(
        MODEL_ID,
        feature_as_of=datetime.now(timezone.utc),
        strict=True,
        engine=engine,
    )
    print("[demo] invariant 2 (model timing, strict) passed")

    print(f"[demo] done — DB at {DEMO_DB}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

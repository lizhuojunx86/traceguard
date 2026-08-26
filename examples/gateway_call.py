"""Trace a call made through an OpenAI-compatible gateway.

A gateway (OrcaRouter, OpenRouter, ...) is just a ``base_url``: TraceGuard
instruments whatever client you hand it, so the same tracer, registry and
invariants apply unchanged. TraceGuard recommends no gateway over another —
``traceguard.gateways`` is a convenience table, not a ranking.

The point of this example is the part people get wrong: **pin a concrete model
id**. Every gateway offers a routing alias (``orcarouter/auto``,
``openrouter/auto``) that means "you pick". Behind an alias the wrappers record
the alias, not the model that actually served the call, and an alias has no
``available_to_us_at`` — so look-ahead invariant 2 passes without checking
anything. Green run, meaningless timeline. See docs/integrations/gateways.md.

Run (needs a key for whichever gateway you choose):
    ORCAROUTER_API_KEY=sk-... uv run python examples/gateway_call.py orcarouter
    OPENROUTER_API_KEY=sk-... uv run python examples/gateway_call.py openrouter

Without a key it prints the client config it *would* use and exits, so the
example stays runnable offline.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# Same workaround as examples/quickstart: some builds skip _-prefixed .pth
# files, breaking uv's editable install.
_PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "traceguard" / "src"
if _PKG_SRC.is_dir() and str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))

from traceguard.gateways import GATEWAYS, client_kwargs, is_alias_model  # noqa: E402

UTC = timezone.utc


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "orcarouter"
    if name not in GATEWAYS:
        print(f"unknown gateway {name!r}; known: {', '.join(sorted(GATEWAYS))}")
        return 2

    entry = GATEWAYS[name]
    print(f"gateway   {entry.name}")
    print(f"base_url  {entry.base_url}")
    print(f"env key   ${entry.env_key}")
    if entry.headers:
        print(f"headers   {entry.headers}")

    # Refuse the alias before spending a token on an unverifiable trace.
    model = "deepseek/deepseek-v4-pro-0813"
    if is_alias_model(model):
        print(f"\nrefusing {model!r}: a routing alias cannot be point-in-time verified")
        return 1

    try:
        kwargs = client_kwargs(name)
    except RuntimeError as exc:
        print(f"\nno key configured, stopping before the network call: {exc}")
        return 0

    try:
        from openai import OpenAI
    except ImportError:
        print('\ninstall the extra to run the call: pip install "traceguard[openai]"')
        return 0

    from traceguard import Tracer, wrap_openai
    from traceguard.store.models import make_engine

    engine = make_engine("sqlite:///gateway_demo.db")
    client = wrap_openai(OpenAI(**kwargs), tracer=Tracer(engine))

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
    )
    print(f"\nresponse  {response.choices[0].message.content!r}")

    # What the gateway actually served. TraceGuard does not record this yet —
    # printing it here is the honest version of the gap the docs describe.
    served = getattr(response, "model", None)
    print(f"requested {model}")
    print(f"served    {served}")
    if served and served != model:
        print("  ^ these differ: the trace stored the requested id, not this one")

    print(f"\ntrace written to gateway_demo.db at {datetime.now(UTC).isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

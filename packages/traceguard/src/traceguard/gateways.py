"""Contract-external opt-in extension: OpenAI-compatible gateway presets.

Purely additive, like ``exporters`` / ``contamination`` / ``loop`` /
``routing_audit``: nothing here is re-exported from the top-level ``traceguard``
package, no MUST field is added or changed, and the normalize algorithm is
untouched. Import by submodule path only::

    from traceguard.gateways import client_kwargs

A "gateway" here is any OpenAI-compatible endpoint that fronts several upstream
model providers. TraceGuard does not pick one for you and does not endorse any
of them — it records the one *you* picked. These presets exist only so that
pointing a client at a gateway is a one-liner instead of a base-URL lookup.

Read ``docs/integrations/gateways.md`` before using this with historical data.
There is a real and currently unfixed interaction between adaptive routing and
look-ahead invariant 2, summarised in ``ROUTING_CAVEAT`` below.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

__all__ = [
    "GATEWAYS",
    "Gateway",
    "ROUTING_CAVEAT",
    "client_kwargs",
    "is_alias_model",
]

#: Why you cannot trust invariant 2 when a gateway routes for you.
#:
#: ``wrap_openai`` / ``wrap_anthropic`` record ``model_id`` from the *request*
#: kwargs, not from the response body. Against a direct provider those agree.
#: Behind a router asked for an alias such as ``orcarouter/auto``, they do not:
#: the trace stores the alias, while some other model actually served the call.
#: An alias has no ``released_at`` / ``available_to_us_at``, so
#: ``validate_model_timing`` ends up checking a name that never existed as a
#: model — it does not raise, and the run looks clean.
ROUTING_CAVEAT = (
    "Adaptive routing aliases (e.g. 'orcarouter/auto', 'openrouter/auto') are "
    "recorded as-is in model_id, so look-ahead invariant 2 checks the alias "
    "rather than the model that answered. The wrappers also record the "
    "served model under output_parsed['routing'] — audit a store with "
    "`python -m traceguard.routing_integrity`. Still pin a concrete model id "
    "for any run over historical data."
)

#: Model names that mean "you pick" rather than naming a model. Traces carrying
#: one of these are not point-in-time verifiable — see ``ROUTING_CAVEAT``.
_ALIAS_SUFFIXES = ("/auto", ":auto", "/router", "-auto")


@dataclass(frozen=True)
class Gateway:
    """One OpenAI-compatible gateway endpoint."""

    name: str
    base_url: str
    env_key: str
    #: Attribution / identification headers this gateway reads, if any.
    headers: dict[str, str] = field(default_factory=dict)
    #: The routing alias this gateway documents, if it offers one.
    auto_alias: str | None = None
    docs: str = ""


#: Adding a gateway is a dict entry, not code. Order is alphabetical, which is
#: also the order they are documented in — TraceGuard ranks none of them.
GATEWAYS: dict[str, Gateway] = {
    "openrouter": Gateway(
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        env_key="OPENROUTER_API_KEY",
        # HTTP-Referer is what creates the app entry in OpenRouter's public
        # rankings; the title alone does nothing. Both are attribution only and
        # carry no request semantics.
        headers={
            "HTTP-Referer": "https://github.com/lizhuojunx86/traceguard",
            "X-OpenRouter-Title": "TraceGuard",
        },
        auto_alias="openrouter/auto",
        docs="https://openrouter.ai/docs/app-attribution",
    ),
    "orcarouter": Gateway(
        name="OrcaRouter",
        base_url="https://api.orcarouter.ai/v1",
        env_key="ORCAROUTER_API_KEY",
        headers={},
        auto_alias="orcarouter/auto",
        docs="https://docs.orcarouter.ai/",
    ),
}


def is_alias_model(model: str | None) -> bool:
    """True if ``model`` is a routing alias rather than a concrete model id.

    Cheap guard for CI: a locked replay set or a backtest should never carry
    one, because nothing downstream can establish when it "existed".

        >>> is_alias_model("orcarouter/auto")
        True
        >>> is_alias_model("anthropic/claude-opus-4.7")
        False
    """
    if not model:
        return False
    lowered = model.lower()
    return any(lowered.endswith(suffix) for suffix in _ALIAS_SUFFIXES)


def client_kwargs(
    gateway: str,
    *,
    api_key: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build the kwargs for an OpenAI-SDK client pointed at ``gateway``.

    Does not import or construct the client itself, so this module stays
    dependency-free and safe to import without the ``[openai]`` extra::

        from openai import OpenAI
        from traceguard import wrap_openai
        from traceguard.gateways import client_kwargs

        client = wrap_openai(OpenAI(**client_kwargs("orcarouter")))

    ``api_key`` falls back to the gateway's environment variable. Raises
    ``KeyError`` for an unknown gateway and ``RuntimeError`` when no key is
    resolvable, rather than letting the SDK fail later with a 401.
    """
    try:
        entry = GATEWAYS[gateway]
    except KeyError:
        known = ", ".join(sorted(GATEWAYS))
        raise KeyError(f"unknown gateway {gateway!r}; known: {known}") from None

    # Stripped because keys arrive via `$(cat key.txt)` more often than not, and
    # a trailing newline rides into the Authorization header as a 401 that says
    # nothing about its own cause.
    key = (api_key or os.environ.get(entry.env_key) or "").strip()
    if not key:
        raise RuntimeError(
            f"no API key for {entry.name}: pass api_key= or set ${entry.env_key}"
        )

    headers = dict(entry.headers)
    if extra_headers:
        headers.update(extra_headers)

    kwargs: dict[str, object] = {"base_url": entry.base_url, "api_key": key}
    if headers:
        kwargs["default_headers"] = headers
    return kwargs

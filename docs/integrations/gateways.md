# Gateways (OpenAI-compatible routers)

A *gateway* is an OpenAI-compatible endpoint that fronts several upstream model
providers behind one key. TraceGuard works with any of them, because the
wrappers instrument whatever client you hand them — the gateway is just a
`base_url`.

**TraceGuard does not pick a provider for you and does not endorse any of the
ones listed here.** It records the one you picked. Listing order is
alphabetical. If a gateway you use is missing, it almost certainly still works;
the presets below are a convenience, not a compatibility list.

Read [the routing caveat](#routing-aliases-defeat-invariant-2) before pointing
any of this at historical data. It is the part that actually matters.

## Usage

```python
from openai import OpenAI

from traceguard import wrap_openai
from traceguard.gateways import client_kwargs

client = wrap_openai(OpenAI(**client_kwargs("orcarouter")))

response = client.chat.completions.create(
    model="deepseek/deepseek-v4-pro-0813",   # a concrete id, not an alias
    messages=[{"role": "user", "content": "..."}],
)
```

`client_kwargs` reads the key from the gateway's environment variable unless you
pass `api_key=`, and raises rather than letting the call fail later with a 401.
It builds kwargs only — it never imports or constructs the SDK client, so
`traceguard.gateways` is importable without the `[openai]` extra.

The module is contract-external, like `contamination` / `loop` /
`routing_audit`: import it by submodule path, never from the top-level package,
and nothing in it is covered by the frozen SemVer surface.

## Presets

| Key | Endpoint | Env var | Notes |
|---|---|---|---|
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | Sends app-attribution headers, see below |
| `orcarouter` | `https://api.orcarouter.ai/v1` | `ORCAROUTER_API_KEY` | — |

Adding one is a dict entry in `traceguard/gateways.py`, not code.

### App attribution (OpenRouter)

OpenRouter builds a public app entry from two headers, and the preset sends
both:

```
HTTP-Referer: https://github.com/lizhuojunx86/traceguard
X-OpenRouter-Title: TraceGuard
```

`HTTP-Referer` is what creates the entry — the title alone does nothing. Both
are attribution only and change nothing about how a request is served.

If you would rather your traffic be attributed to **your** project instead of
TraceGuard's, override them:

```python
client_kwargs("openrouter", extra_headers={
    "HTTP-Referer": "https://github.com/you/your-project",
    "X-OpenRouter-Title": "Your Project",
})
```

That is the honest default for anyone building a product on top of TraceGuard,
and it is why the override exists.

## Routing aliases defeat invariant 2

Every one of these gateways offers a "just pick for me" alias —
`orcarouter/auto`, `openrouter/auto`. **Do not use one in any run over
historical data.** Here is the precise failure:

`wrap_openai` and `wrap_anthropic` record `model_id` from the *request* kwargs
(`openai.py:125` → `openai.py:136`), not from the response body. Against a
direct provider the two agree, so this has never mattered. Behind a router
asked for an alias, they diverge:

- the trace stores `model_id="orcarouter/auto"`
- some other model — chosen per request, varying with the router's policy and
  upstream health — actually served the call
- an alias has no `released_at` and no `available_to_us_at`
- so `validate_model_timing` checks a name that was never a model

It does not raise. The run comes out green. That is the whole problem: this is
a **silent** failure of look-ahead invariant 2, and a "2023 backtest" can be
served by a 2026 model without anything in the trace saying so.

Until the wrappers capture the served model, the only safe practice is to pin a
concrete model id. A cheap CI guard:

```python
from traceguard.gateways import is_alias_model

assert not is_alias_model(trace.model_id), (
    f"{trace.model_id} is a routing alias; this run is not point-in-time verifiable"
)
```

### Status

Capturing the served model (`response.model` in the OpenAI-compatible shape)
alongside the requested one is the fix, plus running the registry check against
the served id. It is not implemented yet. `is_alias_model` is a guard against
the dangerous case, not a solution.

This affects every adaptive router, including ones TraceGuard has a listing or
a partnership with. If that ever stops being true of this document, the
document is wrong.

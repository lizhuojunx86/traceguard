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

The best practice is still to pin a concrete model id. But the wrappers now
also record who actually answered, so the failure is no longer silent.

## What the wrappers now record

Every instrumented call whose kwargs name a model attaches a routing block to
`output_parsed`:

```json
{
  "routing": {
    "requested_model": "orcarouter/auto",
    "served_model": "deepseek/deepseek-v4-pro-0813",
    "requested_is_alias": true,
    "diverged": true
  }
}
```

`model_id` on the trace is unchanged — SPEC §3.1 fixes it as the *requested*
model, and this rides in `output_parsed`, the same contract-external route
§6.6 gives the contamination scores. No MUST column is added.

`diverged` is `null`, never `false`, when the gateway reported no model.
"We don't know who served this" and "they agree" are different states and
collapsing them is how you get a false clean bill.

## Auditing a trace store

`traceguard.routing_integrity` answers the prior question to invariant 2:
*was there anything real to check?*

```
python -m traceguard.routing_integrity --db sqlite:///traces.db
```

```
scanned 4 trace(s) carrying a feature_as_of

       1  unverifiable  cannot be verified at all
       1  unregistered  served by a model missing from the registry
       1  diverged      checked the requested model, not the one that answered
       1  verified      checked a real, registered model
```

Exit code is 1 when anything is actionable, so it drops into CI as-is. Traces
with no `feature_as_of` are skipped by default — they make no point-in-time
claim, so invariant 2 never applied to them (`--all` includes them).

The four verdicts:

| Verdict | Meaning |
|---|---|
| `verified` | Requested and served agree, and the model is registered |
| `diverged` | Something else answered; re-run invariant 2 against the served id |
| `unregistered` | The served model has no `available_to_us_at` to compare |
| `unverifiable` | An alias was requested and the gateway named no model |

In code, for one call or one row:

```python
from traceguard.routing_integrity import Verdict, scan

bad = [f for f in scan(engine) if f.actionable]
assert not bad, "\n".join(f"trace {f.trace_id}: {f.detail}" for f in bad)
```

This does not weaken invariant 2 — a trace that fails `validate_model_timing`
still fails. It catches the case where the check passed without meaning
anything.

### Still open

The scan reports that a diverged trace needs re-checking; it does not re-run
invariant 2 against the served model and fail the build on the result. That is
the next step, and until it lands `diverged` means "go look", not "this is
fine".

This affects every adaptive router, including ones TraceGuard has a listing or
a partnership with. If that ever stops being true of this document, the
document is wrong.

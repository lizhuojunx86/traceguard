"""Probe a gateway's routing alias and record who actually answers.

The question this answers is narrow and empirical: **if you send the same
request to ``<gateway>/auto`` more than once, do you get the same model?** If
not, no run using that alias is reproducible, and any point-in-time claim built
on it is unverifiable — not because the tooling is weak, but because the
identity of the thing that produced the number was never fixed.

Adaptive routers grade prompt difficulty, so a probe suite that is all trivial
prompts will only ever see the cheap tier and prove nothing. The suite below
spans three tiers deliberately.

Spending guards, because this costs real money
----------------------------------------------
Three independent limits, all enforced in code rather than by being careful:

1. ``Budget.max_calls`` — a hard ceiling on requests per run.
2. ``Budget.max_tokens`` — passed to the API, capping the expensive half.
3. ``Budget.max_estimated_usd`` — a pre-flight worst-case estimate that
   *refuses to start* when the plan could exceed it. The estimate assumes every
   single call is served by the most expensive model on the platform, which is
   not what routers do — so the real spend lands far below it.

The run also stops at the first authentication or quota error rather than
retrying, because a retry loop against a paid endpoint is how a budget
evaporates.

Usage — needs a key, so run it where your key lives::

    cd packages/traceguard
    PYTHONPATH=src ORCAROUTER_API_KEY=sk-... \\
        uv run python -m traceguard.routing_integrity.conformance \\
        --gateway orcarouter --db sqlite:///routing_probe.db --repeats 2

``PYTHONPATH=src`` is not optional on every machine: some Python builds skip
``_``-prefixed ``.pth`` files, which breaks uv's editable install and makes
``python -m traceguard...`` fail with ``ModuleNotFoundError`` even after a
successful ``uv sync``. pytest works because it sets ``pythonpath=["src"]``
itself; the same workaround appears in ``examples/``.

Start with ``--dry-run`` to see the plan and the cost ceiling without spending.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

UTC = timezone.utc


@dataclass(frozen=True)
class Probe:
    """One prompt, tagged with the difficulty tier it is meant to trigger."""

    tier: str
    prompt: str


#: Deliberately spans the router's grading range. Trivial prompts should reach
#: the cheap tier; the hard ones exist to pull the router toward frontier
#: models, which is where both the interesting routing and the cost live.
DEFAULT_PROBES: tuple[Probe, ...] = (
    Probe("trivial", "Reply with exactly one word: ok"),
    Probe("trivial", "What is 2 + 2? Answer with the digit only."),
    Probe("moderate", "In two sentences, explain what a look-ahead bias is in backtesting."),
    Probe(
        "moderate",
        "A CSV has columns date,ticker,close. Write one line of pandas that "
        "computes a 20-day rolling mean per ticker. Code only.",
    ),
    Probe(
        "hard",
        "A backtest calls an LLM through a router alias that picks a model per "
        "request. Argue, in under 150 words, whether the resulting Sharpe ratio "
        "is reproducible, and name the specific property that fails.",
    ),
    Probe(
        "hard",
        "Prove or disprove: if f is continuous on [0,1] and f(0)=f(1), there "
        "exists x in [0,1/2] with f(x)=f(x+1/2). Be rigorous and brief.",
    ),
)


@dataclass(frozen=True)
class Budget:
    """Hard spending limits. All three are enforced; none is advisory."""

    max_calls: int = 30
    max_tokens: int = 256
    max_estimated_usd: float = 1.00
    #: Worst-case list price ($/M tokens) used only for the pre-flight ceiling.
    #: Default is the priciest model observed on OrcaRouter's catalogue on
    #: 2026-08-26 (GPT-5.5 Pro, $30 in / $180 out). It is an assumption for
    #: budgeting, not a live quote — raise it if a pricier model appears.
    price_ceiling_in: float = 30.0
    price_ceiling_out: float = 180.0
    #: Rough input size per probe; the suite's prompts are all well under this.
    assumed_tokens_in: int = 400


@dataclass
class Observation:
    """What one call revealed about the router's choice."""

    tier: str
    prompt: str
    requested: str
    served: str | None
    error: str | None = None


@dataclass
class Report:
    """Aggregate of a probe run — the evidence, not an interpretation of it."""

    gateway: str
    alias: str
    started_at: datetime
    observations: list[Observation] = field(default_factory=list)

    @property
    def served_models(self) -> set[str]:
        return {o.served for o in self.observations if o.served}

    @property
    def silent_calls(self) -> int:
        """Calls that succeeded but named no model — the unverifiable case."""
        return sum(1 for o in self.observations if o.error is None and not o.served)

    def by_prompt(self) -> dict[str, set[str]]:
        """Distinct models per prompt. More than one means the alias is unstable."""
        out: dict[str, set[str]] = defaultdict(set)
        for obs in self.observations:
            if obs.served:
                out[obs.prompt].add(obs.served)
        return dict(out)

    @property
    def unstable_prompts(self) -> dict[str, set[str]]:
        """Prompts that were served by more than one model across repeats."""
        return {p: m for p, m in self.by_prompt().items() if len(m) > 1}

    def render(self) -> str:
        lines = [
            f"gateway   {self.gateway}",
            f"alias     {self.alias}",
            f"started   {self.started_at.isoformat()}",
            f"calls     {len(self.observations)}",
            "",
        ]
        errors = [o for o in self.observations if o.error]
        if errors:
            lines.append(f"{len(errors)} call(s) failed; first: {errors[0].error}")
            lines.append("")

        lines.append(f"distinct models served: {len(self.served_models)}")
        for model in sorted(self.served_models):
            n = sum(1 for o in self.observations if o.served == model)
            tiers = sorted({o.tier for o in self.observations if o.served == model})
            lines.append(f"  {n:>3}x  {model}  ({', '.join(tiers)})")

        if self.silent_calls:
            lines.append(
                f"\n{self.silent_calls} call(s) named no model at all — "
                "unverifiable by construction"
            )

        unstable = self.unstable_prompts
        if unstable:
            lines.append(f"\n{len(unstable)} prompt(s) got different models across repeats:")
            for prompt, models in unstable.items():
                lines.append(f"  {prompt[:60]!r}...")
                lines.append(f"    -> {', '.join(sorted(models))}")
            lines.append(
                "\nThat is the finding: the same request, the same alias, a "
                "different model. Nothing in a trace recording only the alias "
                "distinguishes these runs."
            )
        elif len(self.served_models) <= 1:
            lines.append(
                "\nNo instability observed in this sample. One run is a snapshot: "
                "routing varies with upstream health, so repeat over days before "
                "concluding the alias is stable."
            )
        return "\n".join(lines)


def estimate_worst_case_usd(n_calls: int, budget: Budget) -> float:
    """Ceiling cost if every call were served by the priciest model."""
    per_call = (
        budget.assumed_tokens_in / 1e6 * budget.price_ceiling_in
        + budget.max_tokens / 1e6 * budget.price_ceiling_out
    )
    return n_calls * per_call


class BudgetExceeded(RuntimeError):
    """Raised before any network call when the plan could overspend."""


def plan(probes: Sequence[Probe], repeats: int, budget: Budget) -> tuple[int, float]:
    """Validate a run against every guard. Returns (n_calls, worst_case_usd).

    Raises :class:`BudgetExceeded` rather than trimming the plan silently — a
    run that quietly does less than asked produces evidence you cannot compare
    against the next run.
    """
    n_calls = len(probes) * repeats
    if n_calls > budget.max_calls:
        raise BudgetExceeded(
            f"{n_calls} calls exceeds max_calls={budget.max_calls}; "
            "lower --repeats or raise the budget deliberately"
        )
    worst = estimate_worst_case_usd(n_calls, budget)
    if worst > budget.max_estimated_usd:
        raise BudgetExceeded(
            f"worst case ${worst:.2f} exceeds max_estimated_usd="
            f"${budget.max_estimated_usd:.2f} for {n_calls} calls"
        )
    return n_calls, worst


_FATAL_MARKERS = ("authentication", "api key", "unauthorized", "quota", "insufficient",
                  "billing", "payment", "402", "401")


def _is_fatal(exc: Exception) -> bool:
    """Auth/quota problems must stop the run, not be retried against a meter."""
    text = str(exc).lower()
    return any(marker in text for marker in _FATAL_MARKERS)


def run_probes(
    call: Callable[[str, str, int], Any],
    *,
    gateway: str,
    alias: str,
    probes: Iterable[Probe] = DEFAULT_PROBES,
    repeats: int = 1,
    budget: Budget | None = None,
) -> Report:
    """Send each probe ``repeats`` times through ``call`` and collect what served.

    ``call(model, prompt, max_tokens)`` returns the raw response object; the
    served model is read from its ``.model`` attribute. Injecting the caller
    keeps this function free of any SDK import, which is what lets it be tested
    without a network or a key.
    """
    budget = budget or Budget()
    probe_list = list(probes)
    plan(probe_list, repeats, budget)

    report = Report(gateway=gateway, alias=alias, started_at=datetime.now(UTC))
    for _ in range(repeats):
        for probe in probe_list:
            try:
                response = call(alias, probe.prompt, budget.max_tokens)
            except Exception as exc:  # noqa: BLE001 - recorded, and fatal ones stop us
                report.observations.append(
                    Observation(probe.tier, probe.prompt, alias, None, error=repr(exc))
                )
                if _is_fatal(exc):
                    return report
                continue
            served = getattr(response, "model", None)
            report.observations.append(
                Observation(
                    probe.tier, probe.prompt, alias,
                    str(served) if served is not None else None,
                )
            )
    return report


class MissingExtra(RuntimeError):
    """The optional SDK this harness needs is not installed."""


def _build_caller(gateway: str, db: str):
    """Wire a traced OpenAI client for ``gateway``. Imported lazily on purpose."""
    try:
        from openai import OpenAI
    except ImportError as exc:  # the extra is optional; say so in one line
        raise MissingExtra(
            "the openai SDK is not installed (it is an optional extra).\n"
            "  install it:  uv sync --extra openai\n"
            "  or:          pip install 'traceguard[openai]'"
        ) from exc

    from traceguard import Tracer, wrap_openai
    from traceguard.gateways import client_kwargs
    from traceguard.store.models import make_engine

    # project/component land on every trace, so probe rows stay separable from
    # real work sharing the same store.
    client = wrap_openai(
        OpenAI(**client_kwargs(gateway)),
        project="routing-conformance",
        component=gateway,
        tracer=Tracer(make_engine(db)),
        # Stamped now, so the probe traces are themselves checkable by
        # invariant 2 and the routing_integrity scan has something to grade.
        feature_as_of=datetime.now(UTC),
    )

    def call(model: str, prompt: str, max_tokens: int) -> Any:
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )

    return call


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m traceguard.routing_integrity.conformance",
        description="Probe a gateway's routing alias and record which models answer.",
    )
    parser.add_argument("--gateway", default="orcarouter")
    parser.add_argument("--db", default="sqlite:///routing_probe.db")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-calls", type=int, default=Budget.max_calls)
    parser.add_argument("--max-tokens", type=int, default=Budget.max_tokens)
    parser.add_argument("--max-usd", type=float, default=Budget.max_estimated_usd)
    parser.add_argument(
        "--dry-run", action="store_true", help="show the plan and cost ceiling, spend nothing"
    )
    args = parser.parse_args(argv)

    from traceguard.gateways import GATEWAYS

    if args.gateway not in GATEWAYS:
        print(f"unknown gateway {args.gateway!r}; known: {', '.join(sorted(GATEWAYS))}")
        return 2
    entry = GATEWAYS[args.gateway]
    alias = entry.auto_alias
    if alias is None:
        print(f"{entry.name} documents no routing alias; nothing to probe")
        return 2

    budget = Budget(
        max_calls=args.max_calls,
        max_tokens=args.max_tokens,
        max_estimated_usd=args.max_usd,
    )
    try:
        n_calls, worst = plan(list(DEFAULT_PROBES), args.repeats, budget)
    except BudgetExceeded as exc:
        print(f"refusing to start: {exc}")
        return 2

    print(f"plan: {n_calls} calls to {alias} via {entry.base_url}")
    print(f"      max_tokens={budget.max_tokens}, worst case ${worst:.2f}")
    print("      (worst case assumes every call hits the priciest model; it will not)")

    if args.dry_run:
        print("\ndry run: nothing sent.")
        return 0
    if not os.environ.get(entry.env_key):
        print(f"\n${entry.env_key} is not set; nothing sent.")
        return 2

    try:
        caller = _build_caller(args.gateway, args.db)
    except MissingExtra as exc:
        print(f"\n{exc}")
        return 2

    report = run_probes(
        caller,
        gateway=entry.name,
        alias=alias,
        repeats=args.repeats,
        budget=budget,
    )
    print("\n" + report.render())
    print(f"\ntraces written to {args.db}")
    print(f"audit them: python -m traceguard.routing_integrity --db {args.db} --all")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Turn an accumulating probe store into the table the argument needs.

One probe run answers "is this alias stable right now". A fortnight of them
answers the question that matters: **does the model behind the alias change
over time, without anything in the request changing?** This module reads the
store the daily job writes and reports run by run.

Runs are identified by ``feature_as_of``. Every trace from one probe run shares
a single stamp, because the harness resolves ``datetime.now(UTC)`` once when it
builds the client — which makes the stamp a natural run id as well as the
point-in-time marker it exists to be.

    python -m traceguard.routing_integrity.timeline --db sqlite:///routing_probe.db

Token totals are reported because they are recorded per trace and are the only
spend figure derivable from the store itself. They are **not** a cost: pricing
depends on which upstream served each call, and the served names a gateway
returns do not necessarily match the ids on its price list. Read money off the
provider's console; read volume off this.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard.store.models import Trace


@dataclass
class Run:
    """One probe run: every trace sharing a ``feature_as_of`` stamp.

    Two gateways probed in the same minute are still two runs, because each
    resolves its own stamp when its client is built. ``gateway`` comes from the
    trace's ``component``, so a store holding several gateways stays legible.
    """

    stamp: datetime
    gateway: str = ""
    models: Counter[str] = field(default_factory=Counter)
    failures: int = 0
    silent: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def calls(self) -> int:
        return sum(self.models.values()) + self.failures + self.silent

    @property
    def dominant(self) -> str | None:
        return self.models.most_common(1)[0][0] if self.models else None


def collect_runs(
    engine: Engine,
    *,
    project: str = "routing-conformance",
    gateway: str | None = None,
) -> list[Run]:
    """Group the store's traces into runs, oldest first.

    ``gateway`` filters to one gateway (the trace's ``component``). Leave it
    unset to see every gateway interleaved by time, which is the view that
    shows whether two routers moved together or independently.
    """
    runs: dict[tuple[datetime, str], Run] = {}
    stmt = select(Trace).where(Trace.feature_as_of.is_not(None))
    if project:
        stmt = stmt.where(Trace.project == project)
    if gateway:
        stmt = stmt.where(Trace.component == gateway)
    with Session(engine) as sess:
        for trace in sess.scalars(stmt):
            assert trace.feature_as_of is not None
            key = (trace.feature_as_of, trace.component or "")
            run = runs.setdefault(
                key, Run(stamp=trace.feature_as_of, gateway=trace.component or "")
            )
            run.tokens_in += trace.tokens_in or 0
            run.tokens_out += trace.tokens_out or 0
            if trace.error_class:
                run.failures += 1
                continue
            parsed = trace.output_parsed if isinstance(trace.output_parsed, dict) else {}
            routing = parsed.get("routing") if isinstance(parsed, dict) else None
            served = routing.get("served_model") if isinstance(routing, dict) else None
            if served:
                run.models[str(served)] += 1
            else:
                run.silent += 1
    return [runs[k] for k in sorted(runs)]


def regime_changes(runs: Sequence[Run]) -> list[tuple[Run, Run]]:
    """Consecutive runs of the SAME gateway whose dominant model differs.

    The blunt version of the finding: not "one prompt drifted" but "the model
    most requests landed on is a different model than it was last time".

    Compared within a gateway, never across. Two routers naturally pick
    different models; that is a product difference, not instability, and
    reporting it as a shift would be the kind of overclaim this whole exercise
    exists to avoid.
    """
    out = []
    by_gateway: dict[str, list[Run]] = {}
    for run in runs:
        by_gateway.setdefault(run.gateway, []).append(run)
    for series in by_gateway.values():
        for earlier, later in zip(series, series[1:]):
            if earlier.dominant and later.dominant and earlier.dominant != later.dominant:
                out.append((earlier, later))
    return sorted(out, key=lambda pair: pair[1].stamp)


def render(runs: Sequence[Run]) -> str:
    if not runs:
        return "no probe runs found in this store."

    lines = [f"{len(runs)} probe run(s), {sum(r.calls for r in runs)} call(s)", ""]
    width = max(len(m) for r in runs for m in r.models) if any(r.models for r in runs) else 8

    gateways = sorted({r.gateway for r in runs if r.gateway})
    if len(gateways) > 1:
        lines.insert(1, f"gateways: {', '.join(gateways)}")

    for run in runs:
        parts = ", ".join(
            f"{model:<{width}} {n}x" for model, n in run.models.most_common()
        )
        label = f" [{run.gateway}]" if len(gateways) > 1 and run.gateway else ""
        lines.append(
            f"{run.stamp.isoformat(timespec='seconds')}{label}  {run.calls:>3} calls"
        )
        lines.append(f"    {parts or '(no model reported)'}")
        flags = []
        if run.failures:
            flags.append(f"{run.failures} failed")
        if run.silent:
            flags.append(f"{run.silent} named no model")
        if flags:
            lines.append(f"    [{'; '.join(flags)}]")

    all_models = Counter()
    for run in runs:
        all_models.update(run.models)
    lines.append("")
    lines.append(f"distinct models across all runs: {len(all_models)}")
    for model, n in all_models.most_common():
        lines.append(f"  {n:>4}x  {model}")

    shifts = regime_changes(runs)
    lines.append("")
    if shifts:
        lines.append(f"{len(shifts)} shift(s) in the dominant model between consecutive runs:")
        for earlier, later in shifts:
            tag = f"[{later.gateway}] " if len(gateways) > 1 and later.gateway else ""
            lines.append(
                f"  {tag}{earlier.stamp.isoformat(timespec='seconds')} {earlier.dominant}"
                f"  ->  {later.stamp.isoformat(timespec='seconds')} {later.dominant}"
            )
        lines.append(
            "\nThe request did not change between these runs. The model did. A trace"
            "\nrecording only the alias cannot tell them apart."
        )
    elif len(runs) > 1:
        lines.append(
            "The dominant model held across every run so far. Keep sampling: a stable"
            "\nweek is evidence about that week, not about the alias."
        )

    tin = sum(r.tokens_in for r in runs)
    tout = sum(r.tokens_out for r in runs)
    lines.append("")
    lines.append(f"tokens: {tin:,} in, {tout:,} out (volume, not cost — see module docstring)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m traceguard.routing_integrity.timeline",
        description="Report how a gateway's routing alias behaved over time.",
    )
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--project", default="routing-conformance",
        help="empty string to include every project in the store",
    )
    parser.add_argument(
        "--gateway", default=None,
        help="restrict to one gateway (default: all, interleaved by time)",
    )
    args = parser.parse_args(argv)

    from traceguard.store.models import make_engine

    runs = collect_runs(
        make_engine(args.db), project=args.project, gateway=args.gateway
    )
    print(render(runs))
    return 0


if __name__ == "__main__":
    sys.exit(main())

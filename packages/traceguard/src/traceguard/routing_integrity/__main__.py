"""CLI: report which traces have an invariant-2 result worth trusting.

    python -m traceguard.routing_integrity --db sqlite:///traces.db

Exit code 1 when anything actionable is found, so it drops into CI unchanged.
"""
from __future__ import annotations

import argparse
import sys
from typing import Sequence

from traceguard.routing_integrity.check import Finding, Verdict, scan, summarise
from traceguard.store.models import make_engine

_HEADLINE = {
    Verdict.UNVERIFIABLE: "cannot be verified at all",
    Verdict.FAILED_CALL: "the call failed; nothing to verify",
    Verdict.UNREGISTERED: "served by a model missing from the registry",
    Verdict.DIVERGED: "checked the requested model, not the one that answered",
    Verdict.VERIFIED: "checked a real, registered model",
}


def _render(findings: Sequence[Finding], *, limit: int) -> str:
    counts = summarise(findings)
    total = len(findings)
    lines = [f"scanned {total} trace(s) carrying a feature_as_of", ""]
    for verdict in Verdict:
        lines.append(f"  {counts[verdict]:>6}  {verdict.value:<13} {_HEADLINE[verdict]}")

    failed = counts[Verdict.FAILED_CALL]
    actionable = [f for f in findings if f.actionable]
    if not actionable:
        lines.append("\nevery dated trace that produced a result checked a registered model.")
        if failed:
            lines.append(
                f"({failed} failed call(s) ignored — an operational problem, not a "
                "timeline one.)"
            )
        return "\n".join(lines)

    lines.append(f"\n{len(actionable)} trace(s) need attention:")
    for finding in actionable[:limit]:
        stamp = finding.feature_as_of.isoformat() if finding.feature_as_of else "—"
        lines.append(
            f"\n  trace {finding.trace_id} [{finding.verdict.value}] as of {stamp}"
        )
        lines.append(f"    {finding.detail}")
    if len(actionable) > limit:
        lines.append(f"\n  ... and {len(actionable) - limit} more (raise --limit)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m traceguard.routing_integrity",
        description="Report whether each trace's invariant-2 check meant anything.",
    )
    parser.add_argument("--db", required=True, help="SQLAlchemy URL of the trace store")
    parser.add_argument("--project", default=None, help="restrict to one project")
    parser.add_argument(
        "--all",
        action="store_true",
        help="include traces with no feature_as_of (they make no point-in-time claim)",
    )
    parser.add_argument("--limit", type=int, default=20, help="max rows to print")
    args = parser.parse_args(argv)

    engine = make_engine(args.db)
    findings = list(scan(engine, project=args.project, only_dated=not args.all))
    print(_render(findings, limit=args.limit))
    return 1 if any(f.actionable for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())

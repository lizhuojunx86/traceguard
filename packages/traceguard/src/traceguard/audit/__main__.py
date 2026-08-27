"""CLI for the audit evidence layer (mirrors the routing_audit CLI pattern).

::

    python -m traceguard.audit enable    --db sqlite:///traces.db [--chain-only] [--no-backfill] [--strict]
    python -m traceguard.audit disable   --db sqlite:///traces.db
    python -m traceguard.audit verify    --db sqlite:///traces.db [--anchor '<json>' | --anchor-file PATH]
    python -m traceguard.audit anchor    --db sqlite:///traces.db [--sink SPEC ...] [--every SECONDS [--rounds N]]
    python -m traceguard.audit reconcile --db sqlite:///traces.db --source anthropic-usage|json:PATH
                                         --window START,END [--bucket-width 1d] [--tolerance 0.05]
                                         [--project P] [--api-key-id ID ...] [--workspace-id ID ...]
                                         [--model-map TRACE=PROVIDER ...]

``verify`` exits 1 on BREAK findings (tamper evidence), 0 otherwise.
``anchor`` exits 1 when a sink refused the anchor (an anchor that did not land
protects nothing). ``reconcile`` exits 1 on any ``capture_mismatch``.
``--db`` falls back to ``TRACEGUARD_DB_URL`` then the make_engine default.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from traceguard.audit.anchors import (
    AnchorScheduler,
    AnchorSinkError,
    FileAnchorSink,
    anchor_to,
    parse_sink_spec,
)
from traceguard.audit.chain import disable, enable
from traceguard.audit.reconcile import (
    ADMIN_KEY_ENV,
    align_window,
    fetch_anthropic_usage,
    load_usage_report,
    parse_window,
    reconcile,
)
from traceguard.audit.verify import ChainAnchor, export_anchor, verify_chain
from traceguard.store.models import make_engine


def _print_findings(findings) -> None:
    for finding in findings:
        loc = f"seq={finding.seq}" if finding.seq is not None else ""
        tid = f"trace_id={finding.trace_id}" if finding.trace_id is not None else ""
        where = " ".join(x for x in (loc, tid) if x)
        print(f"  [{finding.severity}] {finding.kind} {where}: {finding.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m traceguard.audit",
        description="Opt-in tamper-evident audit trail for traceguard traces (stable since SPEC v1.1).",
    )
    parser.add_argument(
        "--db", default=None, help="SQLAlchemy DB URL (default: TRACEGUARD_DB_URL / traceguard.db)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_enable = sub.add_parser("enable", help="enable audit (tables + settings + backfill + attach)")
    p_enable.add_argument(
        "--chain-only",
        action="store_true",
        help="enable the hash chain WITHOUT the ORM append-only guard",
    )
    p_enable.add_argument(
        "--no-backfill",
        action="store_true",
        help="do not chain pre-existing traces (they will show as coverage gaps)",
    )
    p_enable.add_argument(
        "--strict",
        action="store_true",
        help="fail closed on chain failures in this process (TRACEGUARD_AUDIT_STRICT=1)",
    )

    sub.add_parser("disable", help="flip the DB flag off (guard lifts, chaining stops)")

    p_verify = sub.add_parser("verify", help="recompute the whole chain; exit 1 on BREAKs")
    anchor_src = p_verify.add_mutually_exclusive_group()
    anchor_src.add_argument(
        "--anchor",
        default=None,
        metavar="JSON",
        help="previously exported anchor JSON: ADDS an anchor-consistency check "
        "(truncation/rewrite since the export) on top of the full walk",
    )
    anchor_src.add_argument(
        "--anchor-file",
        default=None,
        metavar="PATH",
        help="a file sink written by `anchor --sink file:PATH`; its newest anchor is used",
    )

    p_anchor = sub.add_parser(
        "anchor",
        help="print the chain head digest; with --sink also store it OUTSIDE the DB",
    )
    p_anchor.add_argument(
        "--sink",
        action="append",
        default=[],
        metavar="SPEC",
        help="where to store the anchor: file:PATH | git-note[:REPO] | webhook:URL (repeatable)",
    )
    p_anchor.add_argument(
        "--every",
        type=float,
        default=None,
        metavar="SECONDS",
        help="keep anchoring on this interval (the interval IS the exposure window); Ctrl-C to stop",
    )
    p_anchor.add_argument(
        "--rounds",
        type=int,
        default=0,
        help="with --every: stop after N rounds (0 = until interrupted)",
    )

    p_rec = sub.add_parser(
        "reconcile",
        help="compare self-reported token volume with the provider's usage report (capture_mismatch)",
    )
    p_rec.add_argument(
        "--source",
        required=True,
        help="anthropic-usage (Usage Admin API; needs $ANTHROPIC_ADMIN_KEY) | json:PATH (saved report)",
    )
    p_rec.add_argument(
        "--window",
        required=True,
        metavar="START,END",
        help="RFC 3339 pair, e.g. 2026-08-01T00:00:00Z,2026-08-08T00:00:00Z",
    )
    p_rec.add_argument("--bucket-width", default="1d", choices=["1m", "1h", "1d"])
    p_rec.add_argument(
        "--tolerance", type=float, default=0.05, help="relative tolerance (default 0.05)"
    )
    p_rec.add_argument(
        "--absolute-floor",
        type=int,
        default=0,
        help="ignore differences of at most this many tokens",
    )
    p_rec.add_argument("--project", default=None, help="restrict the traces side to one project")
    p_rec.add_argument(
        "--operation",
        default="llm_complete",
        help="restrict the traces side to one operation (default llm_complete)",
    )
    p_rec.add_argument(
        "--api-key-id", action="append", default=[], help="Usage API filter (repeatable)"
    )
    p_rec.add_argument(
        "--workspace-id", action="append", default=[], help="Usage API filter (repeatable)"
    )
    p_rec.add_argument(
        "--model-map",
        action="append",
        default=[],
        metavar="TRACE=PROVIDER",
        help="map a trace model_id onto the provider's model name (repeatable)",
    )
    p_rec.add_argument(
        "--admin-key-env",
        default=ADMIN_KEY_ENV,
        help=f"environment variable holding the Admin API key (default {ADMIN_KEY_ENV})",
    )

    args = parser.parse_args(argv)
    engine = make_engine(args.db)

    if args.command == "enable":
        backfilled = enable(
            engine,
            append_only=not args.chain_only,
            backfill=not args.no_backfill,
            strict=args.strict,
        )
        print(
            f"audit enabled (append_only={not args.chain_only}); "
            f"backfilled {backfilled} pre-existing trace(s)"
        )
        return 0

    if args.command == "disable":
        disable(engine)
        print(
            "audit disabled: guard lifted, new writes are no longer chained "
            "(the existing chain stays verifiable)"
        )
        return 0

    if args.command == "verify":
        anchor = None
        if args.anchor:
            anchor = ChainAnchor.from_json(args.anchor)
        elif args.anchor_file:
            anchor = FileAnchorSink(args.anchor_file).latest()
            if anchor is None:
                print(f"no anchor found in {args.anchor_file}", file=sys.stderr)
                return 2
        result = verify_chain(engine, from_anchor=anchor)
        print(result.summary())
        _print_findings(result.findings)
        return 0 if result.ok else 1

    if args.command == "anchor":
        try:
            sinks = [parse_sink_spec(s) for s in args.sink]
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.every is not None:
            if args.every <= 0:
                print("--every must be > 0", file=sys.stderr)
                return 2
            scheduler = AnchorScheduler(engine, sinks, args.every)
            rounds = 0
            try:
                while True:
                    anchor = scheduler.run_once()
                    if anchor is not None:
                        print(anchor.to_json())
                    rounds += 1
                    if args.rounds and rounds >= args.rounds:
                        break
                    time.sleep(args.every)
            except KeyboardInterrupt:
                pass
            print(
                f"anchored {scheduler.anchors_stored} time(s) to {len(sinks)} sink(s), "
                f"{scheduler.failures} failure(s)",
                file=sys.stderr,
            )
            return 0 if scheduler.failures == 0 else 1
        if not sinks:
            print(export_anchor(engine).to_json())
            return 0
        try:
            anchor = anchor_to(engine, sinks)
        except AnchorSinkError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(anchor.to_json())
        print(
            f"stored to {len(sinks)} sink(s): " + ", ".join(s.name for s in sinks), file=sys.stderr
        )
        return 0

    if args.command == "reconcile":
        try:
            start, end = parse_window(args.window)
            start, end = align_window(start, end, args.bucket_width)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        model_map: dict[str, str] = {}
        for item in args.model_map:
            key, sep, value = item.partition("=")
            if not sep or not key or not value:
                print(f"--model-map expects TRACE=PROVIDER, got {item!r}", file=sys.stderr)
                return 2
            model_map[key] = value
        source = args.source
        if source == "anthropic-usage":
            admin_key = os.environ.get(args.admin_key_env, "")
            if not admin_key:
                print(
                    f"--source anthropic-usage needs an Admin API key in ${args.admin_key_env}",
                    file=sys.stderr,
                )
                return 2
            provider = fetch_anthropic_usage(
                start,
                end,
                admin_key=admin_key,
                bucket_width=args.bucket_width,
                api_key_ids=args.api_key_id or None,
                workspace_ids=args.workspace_id or None,
            )
        elif source.startswith("json:"):
            provider = load_usage_report(source[len("json:") :])
        else:
            print(
                f"unknown --source {source!r}; expected anthropic-usage | json:PATH",
                file=sys.stderr,
            )
            return 2
        result = reconcile(
            engine,
            starting_at=start,
            ending_at=end,
            provider=provider,
            tolerance=args.tolerance,
            absolute_floor=args.absolute_floor,
            project=args.project,
            operation=args.operation,
            model_map=model_map or None,
        )
        print(result.summary())
        for model, cmp in result.comparisons.items():
            label = model if model is not None else "<no model_id>"
            print(
                f"  {label}: calls={cmp.traces.calls} tokens_in traces={cmp.traces.tokens_in} "
                f"provider={cmp.provider.tokens_in} | tokens_out traces={cmp.traces.tokens_out} "
                f"provider={cmp.provider.tokens_out}"
            )
        if result.buckets_outside_window:
            print(
                f"  (ignored {result.buckets_outside_window} provider bucket(s) outside the window)"
            )
        _print_findings(result.findings)
        return 0 if result.ok else 1

    return 2  # pragma: no cover - argparse enforces the subcommand set


if __name__ == "__main__":
    sys.exit(main())

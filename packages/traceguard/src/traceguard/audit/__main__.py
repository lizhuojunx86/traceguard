"""CLI for the audit evidence layer (mirrors the routing_audit CLI pattern).

::

    python -m traceguard.audit --db sqlite:///traces.db enable    [--chain-only] [--no-backfill] [--strict]
    python -m traceguard.audit --db sqlite:///traces.db disable
    python -m traceguard.audit --db sqlite:///traces.db verify    [--anchor '<json>' | --anchor-file PATH]
                                                                 [--ots-proof PATH [--ots-upgrade]]
    python -m traceguard.audit --db sqlite:///traces.db anchor    [--sink SPEC ...] [--every SECONDS [--rounds N]]
    python -m traceguard.audit --db sqlite:///traces.db reconcile
                                   --source anthropic-usage|json:PATH|requests-json:PATH
                                   --window START,END [--bucket-width 1d] [--tolerance 0.05]
                                   [--project P] [--api-key-id ID ...] [--workspace-id ID ...]
                                   [--model-map TRACE=PROVIDER ...]
    python -m traceguard.audit --db sqlite:///traces.db bundle --out PATH [--hash-only] ...
    python -m traceguard.audit verify-bundle PATH        # offline: takes no --db

**``--db`` is a top-level option: it goes BEFORE the subcommand**, as every
line above now shows. Putting it after is rejected by argparse with
``unrecognized arguments: --db ...``; this docstring and docs/audit.md both had
it the wrong way round, so the documented invocations did not run.

``verify`` exits 1 on BREAK findings (tamper evidence), 0 otherwise.
``anchor`` exits 1 when a sink refused the anchor (an anchor that did not land
protects nothing). ``reconcile`` exits 1 on any ``capture_mismatch`` (or, with
``--source requests-json:``, any ``capture_unmatched``).
``bundle`` writes an ``evidence-bundle/v1`` document; ``verify-bundle`` checks
one offline (no DB, no network, no signature verification) and exits 1 on a
BREAK. ``--db`` falls back to ``TRACEGUARD_DB_URL`` then the make_engine default;
``verify-bundle`` opens no database at all.
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
from traceguard.audit.bundle import (
    anchor_record,
    export_bundle,
    load_bundle,
    verify_bundle,
    write_bundle,
)
from traceguard.audit.chain import disable, enable
from traceguard.audit.reconcile import (
    ADMIN_KEY_ENV,
    load_request_ledger,
    reconcile_requests,
    align_window,
    fetch_anthropic_usage,
    load_usage_report,
    parse_window,
    reconcile,
)
from traceguard.audit.verify import ChainAnchor, export_anchor, verify_chain
from traceguard.store.models import make_engine


def _iso_or_none(text):
    """Parse an ISO 8601 CLI value, defaulting a bare date to UTC."""
    if not text:
        return None
    from datetime import datetime, timezone

    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _print_findings(findings) -> None:
    for finding in findings:
        loc = f"seq={finding.seq}" if finding.seq is not None else ""
        tid = f"trace_id={finding.trace_id}" if finding.trace_id is not None else ""
        direction = getattr(finding, "direction", None)
        dirn = f"direction={direction}" if direction else ""
        where = " ".join(x for x in (loc, tid, dirn) if x)
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
    p_verify.add_argument(
        "--ots-proof",
        default=None,
        metavar="PATH",
        help="an .ots proof written by `anchor --sink ots:DIR`: reports whether it commits to the anchor being used, and whether it is pending or complete",
    )
    p_verify.add_argument(
        "--ots-upgrade",
        action="store_true",
        help="with --ots-proof: first ask the calendars for the completed proof (a pending proof is not evidence)",
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
        help="where to store the anchor: file:PATH | git-note[:REPO] | webhook:URL | "
        "ots:DIR (OpenTimestamps, needs the anchors extra) (repeatable)",
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
        help="anthropic-usage (Usage Admin API; needs $ANTHROPIC_ADMIN_KEY) | "
        "json:PATH (saved usage report, aggregate) | requests-json:PATH "
        "(a request-ledger/v1 document — per-request existence check, L1.5)",
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

    p_bundle = sub.add_parser(
        "bundle",
        help="export an evidence-bundle/v1 JSON document (traces + chain + anchors)",
    )
    p_bundle.add_argument("--out", required=True, metavar="PATH", help="where to write it")
    p_bundle.add_argument("--since", default=None, metavar="ISO", help="invoked_at >= this")
    p_bundle.add_argument("--until", default=None, metavar="ISO", help="invoked_at < this")
    p_bundle.add_argument(
        "--trace-ids",
        default=None,
        metavar="A,B,C",
        help="explicit trace_id list (overrides --since/--until)",
    )
    p_bundle.add_argument(
        "--hash-only",
        action="store_true",
        help="strip the hash-covered content fields. The bundle then proves chain "
        "LINKAGE and the head-vs-anchor check ONLY — entry hashes cannot be "
        "recomputed without the content",
    )
    p_bundle.add_argument(
        "--no-sources",
        action="store_true",
        help="omit source_snapshots",
    )
    p_bundle.add_argument(
        "--anchor-file",
        default=None,
        metavar="PATH",
        help="a file sink written by `anchor --sink file:PATH`; its newest anchor "
        "is recorded in the bundle",
    )

    p_vb = sub.add_parser(
        "verify-bundle",
        help="verify an evidence bundle offline; exit 1 on BREAK findings",
    )
    p_vb.add_argument("path", help="path to the bundle JSON")

    args = parser.parse_args(argv)
    if args.command == "verify-bundle":
        # Offline by construction: never opens the DB, so it works on a machine
        # that has only the file (which is the whole point of the format).
        try:
            result = verify_bundle(load_bundle(args.path))
        except (ValueError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(result.summary())
        _print_findings(result.findings)
        return 0 if result.ok else 1

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
        ots_ok = True
        if args.ots_proof:
            # Lazy import: only an --ots-proof run needs the anchors extra.
            try:
                from traceguard.audit.ots import (
                    load_anchor_beside,
                    parse_ots_proof,
                    upgrade_proof,
                    verify_ots_proof,
                )
            except ImportError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            try:
                proof = (
                    upgrade_proof(args.ots_proof)
                    if args.ots_upgrade
                    else parse_ots_proof(args.ots_proof)
                )
            except Exception as exc:  # noqa: BLE001 - a malformed proof is a usage error
                print(f"could not read the OTS proof {args.ots_proof}: {exc}", file=sys.stderr)
                return 2
            # The anchor the proof commits to: the sidecar beside it, else the
            # one already selected. A proof without its anchor cannot be tied
            # to anything, and saying otherwise would be the overclaim.
            subject = load_anchor_beside(args.ots_proof) or anchor
            if subject is None:
                print(
                    "  [INFO] ots: " + proof.describe(),
                )
                print(
                    "  [WARN] the OTS proof has no anchor beside it and none was "
                    "given, so it cannot be tied to this chain",
                    file=sys.stderr,
                )
            else:
                matched, explanation = verify_ots_proof(proof, subject)
                print(f"  [{'INFO' if matched else 'BREAK'}] ots: {explanation}")
                if not matched:
                    ots_ok = False
                elif anchor is None:
                    anchor = subject  # verify against what the proof attests
        result = verify_chain(engine, from_anchor=anchor)
        print(result.summary())
        _print_findings(result.findings)
        return 0 if (result.ok and ots_ok) else 1

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
            # Bucket alignment is a Usage-API concern: that API snaps every
            # bucket to a UTC minute/hour/day edge, so the totals path has to
            # ask for whole buckets. A request ledger has no buckets, and
            # widening its window to the --bucket-width default of 1d both
            # refused windows the ledger does cover and pulled in traces the
            # ledger never claimed to vouch for — reporting them as fabricated.
            if not args.source.startswith("requests-json:"):
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
        if source.startswith("requests-json:"):
            # L1.5: per-request existence, not totals. Different question,
            # different finding kind (capture_unmatched), different output.
            try:
                ledger = load_request_ledger(source[len("requests-json:") :])
                per_request = reconcile_requests(
                    engine,
                    ledger=ledger,
                    starting_at=start,
                    ending_at=end,
                    project=args.project,
                    operation=args.operation,
                )
            except (ValueError, OSError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(per_request.summary())
            _print_findings(per_request.findings)
            return 0 if per_request.ok else 1
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
                f"unknown --source {source!r}; expected anthropic-usage | json:PATH "
                "| requests-json:PATH",
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

    if args.command == "bundle":
        trace_ids = None
        if args.trace_ids:
            try:
                trace_ids = [int(x) for x in args.trace_ids.split(",") if x.strip()]
            except ValueError:
                print("--trace-ids must be a comma-separated list of integers", file=sys.stderr)
                return 2
        anchors = []
        if args.anchor_file:
            stored = FileAnchorSink(args.anchor_file).latest()
            if stored is None:
                print(f"no anchor found in {args.anchor_file}", file=sys.stderr)
                return 2
            anchors.append(anchor_record(stored, kind="file", location=args.anchor_file))
        try:
            since = _iso_or_none(args.since)
            until = _iso_or_none(args.until)
        except ValueError as exc:
            print(f"--since/--until must be ISO 8601: {exc}", file=sys.stderr)
            return 2
        bundle = export_bundle(
            engine,
            since=since,
            until=until,
            trace_ids=trace_ids,
            content_mode="hash_only" if args.hash_only else "full",
            include_sources=not args.no_sources,
            anchors=anchors,
        )
        path = write_bundle(bundle, args.out)
        print(
            f"wrote {path} ({bundle['content_mode']}): "
            f"{len(bundle['traces'])} trace(s), "
            f"{len(bundle['chain']['entries'])} chain entry/entries, "
            f"{len(bundle['source_snapshots'])} source snapshot(s), "
            f"{len(bundle['anchors'])} anchor(s)"
        )
        if args.hash_only:
            print(
                "  hash_only: content fields were stripped, so a later verify checks "
                "chain linkage and anchors ONLY — not content",
                file=sys.stderr,
            )
        return 0

    return 2  # pragma: no cover - argparse enforces the subcommand set


if __name__ == "__main__":
    sys.exit(main())

"""Evidence bundle export + offline verify (evidence-bundle/v1, SPEC v1.2).

The load-bearing property is that `full` and `hash_only` support DIFFERENT
conclusions and never share a word. Most of these tests exist to keep the
weaker mode from being read as the stronger one.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from traceguard import audit, sources
from traceguard.audit.bundle import (
    BUNDLE_SCHEMA,
    CONTENT_NOT_RECOMPUTED,
    INFO,
    anchor_record,
    export_bundle,
    load_bundle,
    verify_bundle,
    write_bundle,
)
from traceguard.sdk.tracer import Tracer
from traceguard.store.models import Trace

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "docs" / "specs" / "evidence-bundle-v1.schema.json"
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def chained(engine):
    """An audited DB with three chained traces, one carrying a source snapshot."""
    audit.enable(engine)
    sources.enable(engine)
    tg = Tracer(engine=engine)
    for i in range(3):
        with tg.span("proj", "comp", "llm_complete", feature_as_of=NOW) as span:
            span.record_input({"q": f"question {i}"})
            span.record_output(parsed={"answer": f"answer {i}"})
            if i == 0:
                span.record_source(
                    sources.SourceSnapshot(
                        source_uri="https://v.example/eps",
                        source_kind="vendor_api",
                        content_hash="a" * 64,
                        retrieved_at=NOW - timedelta(days=1),
                        published_at=NOW - timedelta(days=2),
                    ),
                    strict=False,
                )
    yield engine
    audit.detach(engine)


# ── round trip, both modes ──────────────────────────────────────────────────

def test_full_round_trip_verifies(chained):
    bundle = export_bundle(chained)
    assert bundle["schema"] == BUNDLE_SCHEMA
    assert bundle["content_mode"] == "full"
    assert len(bundle["traces"]) == 3
    assert len(bundle["chain"]["entries"]) == 3
    assert len(bundle["source_snapshots"]) == 1
    assert bundle["approvals"] == []  # reserved, always empty today

    result = verify_bundle(bundle)
    assert result.ok
    assert result.content_recomputed == 3
    # Unanchored, so it is self-consistent and says exactly that.
    assert "INTERNALLY CONSISTENT (full)" in result.summary()

    # With an anchor covering an entry it carries, it can say VERIFIED.
    bundle["anchors"] = [anchor_record(audit.export_anchor(chained), location="/mnt/a.jsonl")]
    anchored = verify_bundle(bundle)
    assert anchored.ok and anchored.anchors_binding == 1
    assert "VERIFIED (full)" in anchored.summary()


def test_hash_only_round_trip_verifies_linkage_only(chained):
    bundle = export_bundle(chained, content_mode="hash_only")
    for trace in bundle["traces"]:
        for stripped in ("input_summary", "output_parsed", "error_message"):
            assert stripped not in trace
        assert "input_hash" in trace  # the digest stays; the content does not

    result = verify_bundle(bundle)
    assert result.ok
    assert result.content_recomputed == 0
    info = [f for f in result.findings if f.kind == CONTENT_NOT_RECOMPUTED]
    assert len(info) == 1 and info[0].severity == INFO


def test_the_two_modes_never_share_a_word(chained):
    """A hash_only pass says nothing about content; saying 'verified' for both
    would be this format's worst failure mode in one word."""
    anchor = [anchor_record(audit.export_anchor(chained), location="/mnt/a.jsonl")]
    full = verify_bundle(export_bundle(chained, anchors=anchor)).summary()
    hash_only = verify_bundle(
        export_bundle(chained, content_mode="hash_only", anchors=anchor)
    ).summary()
    assert "VERIFIED (full)" in full
    assert "VERIFIED" not in hash_only
    assert "LINKAGE OK (hash_only)" in hash_only
    assert "content was NOT recomputed" in hash_only


def test_hash_only_always_carries_the_scope_finding_even_when_clean(chained):
    result = verify_bundle(export_bundle(chained, content_mode="hash_only"))
    assert result.ok  # a clean result...
    assert any(f.kind == CONTENT_NOT_RECOMPUTED for f in result.findings)  # ...still annotated


# ── the anchor must constrain the entries, not just the head field ──────────

def _rechain(bundle):
    """Rewrite every trace's content and re-chain the segment in place.

    This is the attacker with write access to the exported file: they change
    what the record says and recompute every hash the bundle contains, which
    is exactly what internal consistency cannot survive.
    """
    from traceguard.audit import TRACE_CONTENT_FIELDS
    from traceguard.audit.bundle import _parse_dt, _recompute

    by_id = {tr["trace_id"]: tr for tr in bundle["traces"]}
    for entry in bundle["chain"]["entries"]:
        tr = by_id[entry["trace_id"]]
        tr["output_parsed"] = json.dumps({"answer": "the model approved the trade"})
    prev = bundle["chain"]["entries"][0]["prev_hash"]
    for entry in bundle["chain"]["entries"]:
        entry["prev_hash"] = prev
        content = {
            name: _parse_dt(by_id[entry["trace_id"]].get(name))
            if name in ("feature_as_of", "invoked_at")
            else by_id[entry["trace_id"]].get(name)
            for name in TRACE_CONTENT_FIELDS
        }
        entry["row_hash"] = _recompute(entry, content)
        prev = entry["row_hash"]
    return bundle


def test_an_anchor_covering_a_carried_entry_catches_a_rechained_rewrite(chained):
    """The whole point of shipping an anchor: it is the one value in the file
    the exporter cannot recompute."""
    bundle = export_bundle(chained)
    bundle["anchors"] = [anchor_record(audit.export_anchor(chained), location="/mnt/a.jsonl")]
    assert verify_bundle(bundle).anchors_binding == 1

    result = verify_bundle(_rechain(bundle))
    assert not result.ok
    kinds = [f.kind for f in result.findings]
    assert "anchor_mismatch" in kinds
    assert "FAILED" in result.summary()


def test_an_anchor_outside_the_exported_window_is_reported_as_binding_nothing(chained):
    """A partial export whose window stops short of the anchored seq.

    The anchor still 'matches the head', because the head is a field of the
    bundle — so a rewrite of the segment leaves that comparison intact. It must
    not be reported as corroboration, and the verdict must not say VERIFIED.
    """
    tg = Tracer(engine=chained)
    for i in range(3):  # push the chain head past the traces we export
        with tg.span("p", "c", "llm_complete", feature_as_of=NOW) as span:
            span.record_input({"q": f"later {i}"})

    with Session(chained) as s:
        early = list(s.execute(select(Trace.trace_id).order_by(Trace.trace_id)).scalars())[:3]
    bundle = export_bundle(chained, trace_ids=early)
    bundle["anchors"] = [anchor_record(audit.export_anchor(chained), location="/mnt/a.jsonl")]

    clean = verify_bundle(bundle)
    assert clean.ok
    assert clean.anchors_checked == 1 and clean.anchors_binding == 0
    unlinked = [f for f in clean.findings if f.kind == "anchor_unlinked"]
    assert len(unlinked) == 1
    assert "does NOT corroborate" in unlinked[0].detail
    assert "VERIFIED" not in clean.summary()
    assert "no external corroboration" in clean.summary()

    # ...and the rewrite it cannot catch is not called verified either.
    tampered = verify_bundle(_rechain(bundle))
    assert "VERIFIED" not in tampered.summary()
    assert any(f.kind == "anchor_unlinked" for f in tampered.findings)


def _extend(engine, n, tag="later"):
    """Push the chain past the traces already written."""
    tg = Tracer(engine=engine)
    for i in range(n):
        with tg.span("p", "c", "llm_complete", feature_as_of=NOW) as span:
            span.record_input({"q": f"{tag} {i}"})


def test_an_anchor_older_than_the_window_is_a_warning_not_a_break(chained):
    """The ordinary shape of an evidence export: you anchored on Monday and
    export Tuesday's traces on Wednesday.

    That anchor is TRUTHFUL and simply has nothing to say about this window.
    Comparing its digest to the chain head reported `anchor_mismatch` (BREAK)
    — "the chain was truncated or rewritten relative to this anchor" — so the
    tool accused an honest anchor of proving a rewrite, and the bundle read
    FAILED.
    """
    older = audit.export_anchor(chained)  # taken at the current tip
    _extend(chained, 3)

    with Session(chained) as s:
        ids = list(s.execute(select(Trace.trace_id).order_by(Trace.trace_id)).scalars())
    bundle = export_bundle(chained, trace_ids=ids[3:])
    bundle["anchors"] = [anchor_record(older, location="/mnt/monday.jsonl")]

    result = verify_bundle(bundle)

    assert result.ok, [f"{f.kind}: {f.detail}" for f in result.findings]
    assert not [f for f in result.findings if f.kind == "anchor_mismatch"]
    outside = [f for f in result.findings if f.kind == "anchor_outside_window"]
    assert len(outside) == 1 and outside[0].severity == "WARN"
    assert "BEFORE the first entry carried here" in outside[0].detail
    assert "was NOT compared to anything" in outside[0].detail

    # Exactly one finding per outside anchor: anchor_unlinked is about anchors
    # compared to the head, and nothing was compared here.
    assert not [f for f in result.findings if f.kind == "anchor_unlinked"]
    assert result.anchors_outside_window == 1 and result.anchors_binding == 0
    # ...and an uncompared anchor never reads as "match".
    assert "match" not in result.summary()
    assert "none comparable to this window" in result.summary()


def test_an_anchor_later_than_the_head_says_to_re_export(chained):
    """A bundle exported before the anchor was taken. Also truthful, also not
    evidence of anything about these entries — but the remedy is the opposite
    one, so it gets its own wording."""
    bundle = export_bundle(chained)
    head_seq = bundle["chain"]["head"]["seq"]
    _extend(chained, 2)
    newer = audit.export_anchor(chained)
    assert newer.seq > head_seq
    bundle["anchors"] = [anchor_record(newer, location="/mnt/later.jsonl")]

    result = verify_bundle(bundle)

    assert result.ok
    assert not [f for f in result.findings if f.kind == "anchor_mismatch"]
    outside = [f for f in result.findings if f.kind == "anchor_outside_window"]
    assert len(outside) == 1
    assert "LATER than this bundle's declared head" in outside[0].detail
    assert "Re-export" in outside[0].detail


def test_a_mid_chain_deletion_is_caught_by_entry_count(chained):
    """A deletion in the MIDDLE leaves the tip seq and the head row_hash intact.

    Every seq-based check therefore passes, the anchor still binds the head
    entry, and the missing row shows up only as a `chain_gap` WARN — while
    `verify_chain` fails outright on the same database. `entry_count` only
    grows on an append-only chain, so an anchor that counted more entries than
    the export found is the one signal that survives this.
    """
    from sqlalchemy import delete

    from traceguard.audit.models import AuditChainEntry

    anchor = audit.export_anchor(chained)

    audit.detach(chained)
    with Session(chained) as s:
        ids = list(s.execute(select(Trace.trace_id).order_by(Trace.trace_id)).scalars())
        victim = ids[1]  # a middle row, not the tail
        s.execute(delete(AuditChainEntry).where(AuditChainEntry.trace_id == victim))
        s.execute(delete(Trace).where(Trace.trace_id == victim))
        s.commit()
    audit.attach(chained)

    head = audit.export_anchor(chained)
    assert head.seq == anchor.seq, "the tip seq must be untouched for this to be the case"
    assert head.row_hash == anchor.row_hash, "and so must the head hash"
    assert head.entry_count < anchor.entry_count  # the only thing that moved

    assert not audit.verify_chain(chained).ok  # the database verifier's verdict

    bundle = export_bundle(chained)
    bundle["anchors"] = [anchor_record(anchor, location="/mnt/a")]
    result = verify_bundle(bundle)

    assert not result.ok, "the offline verifier must not pass what the DB verifier fails"
    breaks = [f for f in result.findings if f.kind == "anchor_mismatch"]
    assert len(breaks) == 1
    assert "only grows on an" in breaks[0].detail


def test_ordinary_chain_growth_is_not_a_shrink(chained):
    """The count check must not fire on the normal case: an older anchor with
    fewer entries than a later export."""
    older = audit.export_anchor(chained)
    tg = Tracer(engine=chained)
    for i in range(3):
        with tg.span("p", "c", "llm_complete", feature_as_of=NOW) as span:
            span.record_input({"q": f"grow {i}"})

    bundle = export_bundle(chained)
    assert bundle["chain"]["head"]["entry_count"] > older.entry_count
    bundle["anchors"] = [anchor_record(older, location="/mnt/a")]

    result = verify_bundle(bundle)
    assert result.ok, [f"{f.kind}: {f.detail}" for f in result.findings]
    assert not [f for f in result.findings if f.kind == "anchor_mismatch"]


def test_an_anchor_taken_after_the_export_may_count_more(chained):
    """A newer anchor legitimately counts more entries; that is not a shrink."""
    bundle = export_bundle(chained)
    tg = Tracer(engine=chained)
    for i in range(2):
        with tg.span("p", "c", "llm_complete", feature_as_of=NOW) as span:
            span.record_input({"q": f"after {i}"})
    newer = audit.export_anchor(chained)
    assert newer.entry_count > bundle["chain"]["head"]["entry_count"]
    bundle["anchors"] = [anchor_record(newer, location="/mnt/a")]

    result = verify_bundle(bundle)
    assert result.ok
    assert not [f for f in result.findings if f.kind == "anchor_mismatch"]


def test_an_anchor_past_the_head_that_predates_the_export_is_truncation(chained):
    """seq going BACKWARDS is the thing the chain exists to catch.

    An anchor taken at seq N, and an export that then finds a lower tip, means
    entries below an anchored position are gone. `verify_chain --anchor-file`
    calls that a BREAK on the same database and the same anchor; verify_bundle
    treating it as a stale-anchor warning would leave the two shipped tools
    contradicting each other on identical input, in the direction SPEC B3.4
    names as the dangerous one.
    """
    from sqlalchemy import delete

    from traceguard.audit.models import AuditChainEntry

    anchor = audit.export_anchor(chained)  # taken at the true tip

    audit.detach(chained)
    with Session(chained) as s:
        ids = list(s.execute(select(Trace.trace_id).order_by(Trace.trace_id)).scalars())
        for tid in ids[-2:]:
            s.execute(delete(AuditChainEntry).where(AuditChainEntry.trace_id == tid))
            s.execute(delete(Trace).where(Trace.trace_id == tid))
        s.commit()
    audit.attach(chained)

    # The other tool's verdict on this database, for the record.
    assert not audit.verify_chain(chained, from_anchor=anchor).ok

    bundle = export_bundle(chained)
    assert bundle["chain"]["head"]["seq"] < anchor.seq
    bundle["anchors"] = [anchor_record(anchor, location="/mnt/anchors.jsonl")]

    result = verify_bundle(bundle)
    assert not result.ok, "a truncation below an anchored position must not pass"
    breaks = [f for f in result.findings if f.kind == "anchor_mismatch"]
    assert len(breaks) == 1
    assert "truncation or rollback" in breaks[0].detail
    assert "not a stale anchor" in breaks[0].detail


def test_an_anchor_past_the_head_that_postdates_the_export_is_only_stale(chained):
    """The benign half of the same branch: the chain simply advanced after the
    export. Told apart by exported_at, which both sides already carry."""
    bundle = export_bundle(chained)
    tg = Tracer(engine=chained)
    for i in range(2):
        with tg.span("p", "c", "llm_complete", feature_as_of=NOW) as span:
            span.record_input({"q": f"after {i}"})
    newer = audit.export_anchor(chained)
    assert newer.seq > bundle["chain"]["head"]["seq"]
    bundle["anchors"] = [anchor_record(newer, location="/mnt/a")]

    result = verify_bundle(bundle)
    assert result.ok
    outside = [f for f in result.findings if f.kind == "anchor_outside_window"]
    assert len(outside) == 1
    assert "AFTER this bundle's head" in outside[0].detail
    assert "Re-export" in outside[0].detail


def test_a_stripped_exported_at_cannot_downgrade_a_truncation(chained):
    """Both sides always write exported_at, so a bundle missing it is
    hand-edited — and removing a field must not turn a BREAK into a warning."""
    anchor = audit.export_anchor(chained)
    bundle = export_bundle(chained, trace_ids=[])
    bundle["chain"]["head"] = {**bundle["chain"]["head"], "seq": anchor.seq - 1}
    record = anchor_record(anchor, location="/mnt/a")
    del record["exported_at"]
    bundle["anchors"] = [record]

    result = verify_bundle(bundle)
    assert not result.ok
    breaks = [f for f in result.findings if f.kind == "anchor_mismatch"]
    assert len(breaks) == 1 and "no exported_at" in breaks[0].detail


def test_a_no_seq_anchor_with_no_head_reports_no_comparison(chained):
    """Both halves are schema-valid — `chain.head` is optional and `seq` is
    nullable — and each had a passing test, but no test combined them.

    With no head there is nothing to compare against, yet the anchor was
    counted as head-compared: summary() claimed a match that never happened and
    anchor_unlinked said it "was compared against this bundle's declared head".
    """
    bundle = export_bundle(chained, content_mode="hash_only")
    record = anchor_record(audit.export_anchor(chained), location="/mnt/a")
    bundle["anchors"] = [{k: v for k, v in record.items() if k != "seq"}]
    del bundle["chain"]["head"]

    result = verify_bundle(bundle)
    assert result.ok
    assert result.anchors_outside_window == 1
    assert result.anchors_binding == 0
    assert "match" not in result.summary()
    assert not [f for f in result.findings if f.kind == "anchor_unlinked"]
    outside = [f for f in result.findings if f.kind == "anchor_outside_window"]
    assert len(outside) == 1
    assert "carries no seq" in outside[0].detail
    assert "declares no chain head" in outside[0].detail


def test_an_empty_export_does_not_blame_a_gap_that_cannot_exist(chained):
    """A window matching no traces exports zero entries, so an anchor below the
    head falls between nothing."""
    older = audit.export_anchor(chained)
    tg = Tracer(engine=chained)
    with tg.span("p", "c", "llm_complete", feature_as_of=NOW) as span:
        span.record_input({"q": "later"})

    empty = export_bundle(
        chained,
        since=NOW - timedelta(days=900),
        until=NOW - timedelta(days=899),
        anchors=[anchor_record(older, location="/mnt/a")],
    )
    assert empty["chain"]["entries"] == []

    result = verify_bundle(empty)
    outside = [f for f in result.findings if f.kind == "anchor_outside_window"]
    assert len(outside) == 1
    assert "no entries at all" in outside[0].detail
    assert "gap" not in outside[0].detail


def test_an_anchor_at_the_head_is_still_compared_to_it(chained):
    """Branch 2 is unchanged: a head-seq anchor that disagrees is a real BREAK,
    because the head does not follow from the anchored position."""
    bundle = export_bundle(chained)
    genuine = anchor_record(audit.export_anchor(chained), location="/mnt/a")
    bundle["anchors"] = [dict(genuine, row_hash="0" * 64)]

    result = verify_bundle(bundle)
    assert not result.ok
    assert any(f.kind == "anchor_mismatch" for f in result.findings)


def test_an_anchor_without_a_seq_still_falls_back_to_the_head(chained):
    """Anchors written before seq was recorded must keep working."""
    bundle = export_bundle(chained)
    genuine = anchor_record(audit.export_anchor(chained), location="/mnt/a")
    legacy = {k: v for k, v in genuine.items() if k != "seq"}

    assert verify_bundle({**bundle, "anchors": [legacy]}).ok
    broken = verify_bundle({**bundle, "anchors": [dict(legacy, row_hash="0" * 64)]})
    assert not broken.ok
    assert any(f.kind == "anchor_mismatch" for f in broken.findings)


def test_an_anchor_in_a_sparse_selections_gap_is_not_a_break(chained):
    """Entries [1,3,5] with an anchor at 4: not carried, not the head, and the
    old code sent it to the head comparison — the same false BREAK."""
    _extend(chained, 3)
    with Session(chained) as s:
        ids = list(s.execute(select(Trace.trace_id).order_by(Trace.trace_id)).scalars())
    bundle = export_bundle(chained, trace_ids=[ids[0], ids[2], ids[4]])
    seqs = [e["seq"] for e in bundle["chain"]["entries"]]
    gap_seq = seqs[0] + 1
    assert gap_seq not in seqs and gap_seq < bundle["chain"]["head"]["seq"]

    genuine = anchor_record(audit.export_anchor(chained), location="/mnt/a")
    bundle["anchors"] = [dict(genuine, seq=gap_seq)]

    result = verify_bundle(bundle)
    assert result.ok
    outside = [f for f in result.findings if f.kind == "anchor_outside_window"]
    assert len(outside) == 1 and "gap in this bundle's entries" in outside[0].detail


def test_an_outside_anchor_survives_a_bundle_that_declares_no_head(chained):
    """`chain.head` is optional per the schema, so seq comparison must not
    assume it exists (`seq > None` is a TypeError)."""
    older = audit.export_anchor(chained)
    _extend(chained, 2)
    with Session(chained) as s:
        ids = list(s.execute(select(Trace.trace_id).order_by(Trace.trace_id)).scalars())
    bundle = export_bundle(chained, trace_ids=ids[3:])
    bundle["anchors"] = [anchor_record(older, location="/mnt/a")]
    del bundle["chain"]["head"]

    result = verify_bundle(bundle)
    assert result.ok
    outside = [f for f in result.findings if f.kind == "anchor_outside_window"]
    assert len(outside) == 1 and "declares no chain head" in outside[0].detail


def test_a_non_integer_anchor_seq_is_reported_not_raised(chained):
    bundle = export_bundle(chained)
    genuine = anchor_record(audit.export_anchor(chained), location="/mnt/a")
    bundle["anchors"] = [dict(genuine, seq="seven")]

    result = verify_bundle(bundle)
    assert not result.ok
    malformed = [f for f in result.findings if f.kind == "anchor_malformed"]
    assert len(malformed) == 1 and "not an integer chain position" in malformed[0].detail


def test_a_rechained_full_history_is_caught_by_the_genesis_seed(chained):
    """A segment starting at seq 1 must seed from the genesis constant, or the
    attacker picks their own starting point and the whole history re-chains."""
    from traceguard.audit import GENESIS_PREV_HASH

    bundle = export_bundle(chained)
    assert bundle["chain"]["entries"][0]["seq"] == 1
    assert bundle["chain"]["entries"][0]["prev_hash"] == GENESIS_PREV_HASH

    tampered = _rechain(bundle)
    tampered["chain"]["entries"][0]["prev_hash"] = "f" * 64
    prev = "f" * 64
    from traceguard.audit import TRACE_CONTENT_FIELDS
    from traceguard.audit.bundle import _parse_dt, _recompute

    by_id = {tr["trace_id"]: tr for tr in tampered["traces"]}
    for entry in tampered["chain"]["entries"]:
        entry["prev_hash"] = prev
        content = {
            name: _parse_dt(by_id[entry["trace_id"]].get(name))
            if name in ("feature_as_of", "invoked_at")
            else by_id[entry["trace_id"]].get(name)
            for name in TRACE_CONTENT_FIELDS
        }
        entry["row_hash"] = _recompute(entry, content)
        prev = entry["row_hash"]

    result = verify_bundle(tampered)
    assert not result.ok
    assert any(f.kind == "link_broken" and f.seq == 1 for f in result.findings)


def test_a_sparse_trace_id_selection_reports_gaps_not_breaks(chained):
    """`--trace-ids` on non-adjacent traces is a documented, ordinary use.

    It used to produce a bundle that failed its own verify with link_broken —
    the tool reporting its own output as evidence of tampering, which trains a
    recipient to ignore exactly the finding that matters.
    """
    tg = Tracer(engine=chained)
    for i in range(3):
        with tg.span("p", "c", "llm_complete", feature_as_of=NOW) as span:
            span.record_input({"q": f"more {i}"})

    with Session(chained) as s:
        ids = list(s.execute(select(Trace.trace_id).order_by(Trace.trace_id)).scalars())
    sparse = [ids[0], ids[2], ids[4]]

    result = verify_bundle(export_bundle(chained, trace_ids=sparse))
    assert result.ok, [f"{f.kind}: {f.detail}" for f in result.findings]
    assert not result.contiguous
    assert not [f for f in result.findings if f.kind == "link_broken"]
    gap = [f for f in result.findings if f.kind == "chain_gap"]
    assert len(gap) == 1 and "not consecutive" in gap[0].detail
    # ...and a gapped selection is never VERIFIED, even with an anchor.
    bundle = export_bundle(chained, trace_ids=sparse)
    bundle["anchors"] = [anchor_record(audit.export_anchor(chained), location="/mnt/a")]
    assert "VERIFIED" not in verify_bundle(bundle).summary()


def test_a_contiguous_selection_still_linkage_checks(chained):
    """The gap rule must not switch linkage off for an ordinary window."""
    result = verify_bundle(export_bundle(chained))
    assert result.contiguous
    assert not [f for f in result.findings if f.kind == "chain_gap"]

    bundle = export_bundle(chained)
    bundle["chain"]["entries"][1]["prev_hash"] = "0" * 64
    broken = verify_bundle(bundle)
    assert not broken.ok
    assert any(f.kind == "link_broken" for f in broken.findings)


def test_the_bundle_names_what_it_carries_without_attesting(chained):
    result = verify_bundle(export_bundle(chained))
    carried = [f for f in result.findings if f.kind == "carried_unattested"]
    assert len(carried) == 1 and carried[0].severity == INFO
    detail = carried[0].detail
    for outside in ("agent_id", "session_id", "provider_response_id", "cost_usd"):
        assert outside in detail
    assert "source_snapshots" in detail  # the fixture records one
    assert "NO hash covers" in detail


def test_a_canon_failed_entry_is_not_counted_as_content_evidence(chained):
    """Its hash covers an error placeholder, so recomputing it proves the
    placeholder is intact and nothing about what the trace said."""
    bundle = export_bundle(chained)
    entry = bundle["chain"]["entries"][0]
    assert entry["canon_status"] != "failed"  # baseline

    result = verify_bundle(bundle)
    assert result.content_recomputed == 3 and result.content_unattested == 0

    # Re-chain one entry over a canonicalization error, the way the writer does.
    from traceguard.audit.bundle import _recompute
    from traceguard.audit.canonical import canon_error_content

    entry["canon_status"] = "failed"
    entry["canon_error"] = "TypeError: not serializable"
    entry["row_hash"] = _recompute(entry, canon_error_content(entry["canon_error"]))
    prev = entry["row_hash"]
    for nxt in bundle["chain"]["entries"][1:]:
        nxt["prev_hash"] = prev
        tr = {t["trace_id"]: t for t in bundle["traces"]}[nxt["trace_id"]]
        from traceguard.audit import TRACE_CONTENT_FIELDS
        from traceguard.audit.bundle import _parse_dt

        nxt["row_hash"] = _recompute(
            nxt,
            {
                n: _parse_dt(tr.get(n)) if n in ("feature_as_of", "invoked_at") else tr.get(n)
                for n in TRACE_CONTENT_FIELDS
            },
        )
        prev = nxt["row_hash"]

    after = verify_bundle(bundle)
    assert after.content_unattested == 1
    assert after.content_recomputed == 2  # not 3
    unattested = [f for f in after.findings if f.kind == "content_unattested"]
    assert len(unattested) == 1 and "error placeholder" in unattested[0].detail


# ── tamper detection: what each mode catches ────────────────────────────────

def test_full_mode_detects_changed_trace_content(chained):
    bundle = export_bundle(chained)
    bundle["traces"][1]["output_parsed"] = {"answer": "tampered"}
    result = verify_bundle(bundle)
    assert not result.ok
    assert any(f.kind == "hash_mismatch" for f in result.breaks)


def test_hash_only_mode_CANNOT_detect_changed_content(chained):
    """Stated as a test because it is the documented limit, not an oversight:
    with the content stripped there is nothing left to recompute against."""
    bundle = export_bundle(chained, content_mode="hash_only")
    bundle["traces"][1]["input_hash"] = "0" * 64  # a content-ish field, changed
    result = verify_bundle(bundle)
    assert result.ok  # undetected — and the summary says why
    assert "content was NOT recomputed" in result.summary()


@pytest.mark.parametrize("mode", ["full", "hash_only"])
def test_both_modes_detect_a_broken_prev_hash(chained, mode):
    bundle = export_bundle(chained, content_mode=mode)
    bundle["chain"]["entries"][2]["prev_hash"] = "0" * 64
    result = verify_bundle(bundle)
    assert not result.ok
    assert any(f.kind == "link_broken" for f in result.breaks)


def test_full_mode_detects_forged_entry_metadata(chained):
    """Entry metadata is inside the preimage, so re-pointing trace_id shows up."""
    bundle = export_bundle(chained)
    bundle["chain"]["entries"][0]["entry_type"] = "backfill"
    assert any(f.kind == "hash_mismatch" for f in verify_bundle(bundle).breaks)


def test_a_referenced_trace_missing_from_the_bundle_is_a_break(chained):
    bundle = export_bundle(chained)
    bundle["traces"] = bundle["traces"][:1]
    assert any(f.kind == "missing_trace" for f in verify_bundle(bundle).breaks)


# ── anchors ─────────────────────────────────────────────────────────────────

def test_matching_anchor_is_checked(chained):
    head = audit.export_anchor(chained)
    bundle = export_bundle(chained, anchors=[anchor_record(head, location="/mnt/x/a.jsonl")])
    result = verify_bundle(bundle)
    assert result.ok
    assert result.anchors_checked == 1


def test_anchor_disagreeing_with_the_head_is_a_break(chained):
    head = audit.export_anchor(chained)
    record = anchor_record(head)
    record["row_hash"] = "9" * 64
    result = verify_bundle(export_bundle(chained, anchors=[record]))
    assert not result.ok
    assert any(f.kind == "anchor_mismatch" for f in result.breaks)


def test_unknown_anchor_kind_is_a_break(chained):
    bundle = export_bundle(chained, anchors=[{"kind": "carrier-pigeon"}])
    assert any(f.kind == "anchor_malformed" for f in verify_bundle(bundle).breaks)


def test_rfc3161_anchor_is_structure_checked_not_verified(chained):
    """traceguard reports a well-formed token as PRESENT, never as valid: it
    does no cryptography (zero runtime deps) — that is `openssl ts`'s job."""
    good = {
        "kind": "rfc3161",
        "tsa_url": "https://freetsa.org/tsr",
        "digest_alg": "sha256",
        "message_imprint": "b" * 64,
        "token_b64": "MIIBog==",
    }
    assert verify_bundle(export_bundle(chained, anchors=[good])).ok

    incomplete = dict(good)
    del incomplete["token_b64"]
    result = verify_bundle(export_bundle(chained, anchors=[incomplete]))
    assert any(f.kind == "anchor_malformed" for f in result.breaks)
    assert "never verifies its signature" in result.breaks[0].detail


def test_a_pending_ots_anchor_warns_that_it_is_not_evidence(chained):
    bundle = export_bundle(
        chained, anchors=[{"kind": "ots", "ots_status": "pending", "ots_proof_path": "/x.ots"}]
    )
    result = verify_bundle(bundle)
    assert result.ok  # a warning, not a break
    pending = [f for f in result.findings if f.kind == "anchor_pending"]
    assert len(pending) == 1
    assert "not evidence" in pending[0].detail


def test_a_complete_ots_anchor_does_not_warn(chained):
    bundle = export_bundle(chained, anchors=[{"kind": "ots", "ots_status": "complete"}])
    assert not [f for f in verify_bundle(bundle).findings if f.kind == "anchor_pending"]


# ── selection, schema, IO ───────────────────────────────────────────────────

def test_selection_by_trace_ids(chained):
    with Session(chained) as sess:
        ids = [t.trace_id for t in sess.scalars(select(Trace))][:2]
    bundle = export_bundle(chained, trace_ids=ids)
    assert [t["trace_id"] for t in bundle["traces"]] == ids
    assert verify_bundle(bundle).ok


def test_selection_by_window(chained):
    bundle = export_bundle(chained, since=NOW + timedelta(days=365))
    assert bundle["traces"] == []
    assert verify_bundle(bundle).ok


def test_include_sources_false_omits_snapshots(chained):
    assert export_bundle(chained, include_sources=False)["source_snapshots"] == []


def test_wrong_schema_tag_is_refused():
    with pytest.raises(ValueError, match="not an evidence-bundle/v1 document"):
        verify_bundle({"schema": "something/v9", "content_mode": "full"})


def test_bad_content_mode_is_refused(chained):
    with pytest.raises(ValueError, match="content_mode"):
        export_bundle(chained, content_mode="partial")
    with pytest.raises(ValueError, match="content_mode"):
        verify_bundle({"schema": BUNDLE_SCHEMA, "content_mode": "partial"})


def test_write_and_load_round_trip(chained, tmp_path):
    path = write_bundle(export_bundle(chained), tmp_path / "sub" / "evidence.json")
    assert path.exists()
    assert verify_bundle(load_bundle(path)).ok


@pytest.mark.parametrize("mode", ["full", "hash_only"])
def test_bundle_conforms_to_the_published_schema(chained, mode):
    """The schema is the published contract; this keeps the emitter honest."""
    jsonschema = pytest.importorskip("jsonschema")
    if not SCHEMA_PATH.is_file():
        pytest.skip(f"schema not reachable from the package tree: {SCHEMA_PATH}")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    head = audit.export_anchor(chained)
    bundle = export_bundle(chained, content_mode=mode, anchors=[anchor_record(head)])
    # The RAW document, with nothing stripped. Filtering keys here once hid a
    # real defect: the emitter was writing a private `_cost_events` key that
    # the published schema rejects, and the test passed anyway.
    jsonschema.validate(bundle, schema)


def test_schema_rejects_an_rfc3161_anchor_without_its_token():
    jsonschema = pytest.importorskip("jsonschema")
    if not SCHEMA_PATH.is_file():
        pytest.skip("schema not reachable")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    payload = {
        "schema": BUNDLE_SCHEMA,
        "generated_at": NOW.isoformat(),
        "generator": "traceguard test",
        "content_mode": "full",
        "chain": {"algo_version": 1, "entries": []},
        "traces": [],
        "anchors": [{"kind": "rfc3161", "tsa_url": "https://x", "digest_alg": "sha256"}],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


def test_empty_approvals_is_valid(chained):
    jsonschema = pytest.importorskip("jsonschema")
    if not SCHEMA_PATH.is_file():
        pytest.skip("schema not reachable")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    bundle = export_bundle(chained)
    assert bundle["approvals"] == []
    jsonschema.validate(bundle, schema)


# ── cost events travel with the bundle ──────────────────────────────────────

def test_a_bundle_with_a_cost_event_still_matches_the_schema(chained):
    """The regression this pair of fixes exists for: cost_events is a real,
    declared field, and a bundle carrying one validates as emitted."""
    jsonschema = pytest.importorskip("jsonschema")
    if not SCHEMA_PATH.is_file():
        pytest.skip("schema not reachable")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    with Session(chained) as sess:
        trace_id = sess.scalars(select(Trace)).first().trace_id
    audit.record_cost_event(
        chained, trace_id=trace_id, event_type="correction",
        old_value=None, new_value="0.25", reason="test",
    )
    bundle = export_bundle(chained)
    assert bundle["cost_events"], "the cost event must travel with the bundle"
    jsonschema.validate(bundle, schema)


def test_a_cost_event_entry_verifies(chained):
    with Session(chained) as sess:
        trace_id = sess.scalars(select(Trace)).first().trace_id
    audit.record_cost_event(
        chained, trace_id=trace_id, event_type="correction",
        old_value=None, new_value="0.25", reason="test",
    )
    with chained.begin() as conn:
        conn.execute(update(Trace).where(Trace.trace_id == trace_id).values(cost_usd="0.25"))
    result = verify_bundle(export_bundle(chained))
    assert result.ok, [f.detail for f in result.breaks]


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_bundle_then_verify_bundle(tmp_path, capsys):
    import traceguard
    from traceguard.audit.__main__ import main

    url = f"sqlite:///{tmp_path/'t.db'}"
    eng = traceguard.make_engine(url)
    audit.enable(eng)
    tg = Tracer(engine=eng)
    with tg.span("p", "c", "llm_complete") as span:
        span.record_input({"q": "hi"})
    audit.detach(eng)

    out = tmp_path / "evidence.json"
    assert main(["--db", url, "bundle", "--out", str(out)]) == 0
    assert out.exists()
    assert main(["verify-bundle", str(out)]) == 0
    # No --sink was given, so the bundle carries no anchor and the verdict says so.
    assert "INTERNALLY CONSISTENT (full)" in capsys.readouterr().out


def test_cli_verify_bundle_exits_1_on_tamper(tmp_path, capsys):
    import traceguard
    from traceguard.audit.__main__ import main

    url = f"sqlite:///{tmp_path/'t.db'}"
    eng = traceguard.make_engine(url)
    audit.enable(eng)
    tg = Tracer(engine=eng)
    with tg.span("p", "c", "llm_complete") as span:
        span.record_input({"q": "hi"})
    audit.detach(eng)

    out = tmp_path / "evidence.json"
    main(["--db", url, "bundle", "--out", str(out)])
    data = json.loads(out.read_text())
    data["traces"][0]["input_summary"] = "tampered"
    out.write_text(json.dumps(data))

    assert main(["verify-bundle", str(out)]) == 1
    assert "hash_mismatch" in capsys.readouterr().out


def test_cli_hash_only_says_what_it_did_not_check(tmp_path, capsys):
    import traceguard
    from traceguard.audit.__main__ import main

    url = f"sqlite:///{tmp_path/'t.db'}"
    eng = traceguard.make_engine(url)
    audit.enable(eng)
    audit.detach(eng)

    out = tmp_path / "evidence.json"
    assert main(["--db", url, "bundle", "--out", str(out), "--hash-only"]) == 0
    assert "not content" in capsys.readouterr().err
    assert main(["verify-bundle", str(out)]) == 0
    assert "LINKAGE OK (hash_only)" in capsys.readouterr().out


def test_cli_verify_bundle_needs_no_database(tmp_path, capsys, monkeypatch):
    """The format's whole point: a recipient with only the file can check it."""
    import traceguard
    from traceguard.audit.__main__ import main

    url = f"sqlite:///{tmp_path/'t.db'}"
    eng = traceguard.make_engine(url)
    audit.enable(eng)
    audit.detach(eng)
    out = tmp_path / "evidence.json"
    main(["--db", url, "bundle", "--out", str(out)])

    def _no_db(*args, **kwargs):
        raise AssertionError("verify-bundle must not open a database")

    monkeypatch.setattr("traceguard.audit.__main__.make_engine", _no_db)
    assert main(["verify-bundle", str(out)]) == 0

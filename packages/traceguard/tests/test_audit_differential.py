"""Differential guard for SPEC appendix B3.6: the export may never out-claim the DB.

B3.6 (non-normative): on the same chain data, `verify_bundle`'s conclusion must
not be STRONGER than `verify_chain`'s. Weaker is expected and legitimate —
damage outside the exported window is invisible, and a bundle whose anchor binds
nothing can only be INTERNALLY CONSISTENT. Stronger is a bug: the database FAILs
and the export says VERIFIED.

Both audit false negatives found in the v1.2 review rounds were this principle
being violated, reached by two different routes:

1. **Tail truncation.** `af74f23` fixed a real false positive — a truthful
   anchor taken before the exported window was reported as an `anchor_mismatch`
   BREAK — by not comparing out-of-window anchors at all. That turned every
   anchor sitting PAST the head into a WARN, including the one case where an
   anchor past the head is proof: the tail was cut. `verify_chain(from_anchor=)`
   said FAIL, `verify_bundle` said "re-export me". `c655d53` split the two on
   `exported_at`.

2. **Mid-chain row deletion.** `dfda9d6` downgraded the linkage check between
   non-adjacent entries from `link_broken` (BREAK) to `chain_gap` (WARN),
   which is right for the sparse `--trace-ids` export it was written for and
   wrong for a chain with a row cut out of its middle: the tip seq does not
   move, the head row_hash does not change, and every seq-based check passes.
   `verify_chain` FAILED on the database while `verify_bundle` passed on its
   export. `b81cb4b` closed it with `entry_count`, which only grows on an
   append-only chain.

Fixing the first false positive is what produced the second false negative. That
is the whole reason this file exists: the rules are tuned against each other,
and only running BOTH tools on ONE database catches a rule that was tuned too
far.

The matrix is 6 mutations x 3 export shapes x 4 anchor choices = 72
combinations, all deterministic, no hypothesis. Every mutation goes in through
raw SQL: the ORM append-only guard is anti-footgun, not a threat model, and a
tamperer does not use the ORM.

**Scoping, and why it is not a loophole.** "Stronger" is compared against what
the bundle's window can see. A bundle carrying entries 8..10 cannot know that
entry 4 was edited, and `docs/audit.md` says so. So the assertion is: if
`verify_chain` reports a BREAK on an entry or trace THIS BUNDLE CARRIES (or on
the anchor relationship the bundle also carries), the bundle must not say
VERIFIED. The combinations where the damage lies wholly outside the window are
not left implicit — they are enumerated and frozen in
:data:`WINDOW_BLIND_SPOTS`, so a NEW blind spot is a test failure rather than a
silence.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from traceguard import audit, sources
from traceguard.audit.bundle import anchor_record, export_bundle, verify_bundle
from traceguard.audit.canonical import compute_row_hash, entry_payload, trace_content
from traceguard.audit.models import AuditChainEntry
from traceguard.audit.verify import BREAK, ChainAnchor
from traceguard.sdk.tracer import Tracer
from traceguard.store.models import Base, Trace, make_engine

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

#: Traces that also record a source snapshot. Present so the matrix runs against
#: a bundle that carries the unattested `traceguard.sources` rows too — those sit
#: outside the algo v1 envelope and must never move any verdict.
SOURCE_TRACES = (2, 7)


# ── the baseline chain ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Baseline:
    """Ten chained traces and the three anchors taken along the way.

    ``anchor_seq3`` and ``anchor_tip`` are both taken BEFORE any mutation runs;
    ``anchor_seq3`` is deliberately too old to constrain the tail, which is the
    ordinary shape of a partial evidence export.
    """

    engine: Engine
    anchor_seq3: ChainAnchor
    anchor_seq8: ChainAnchor
    anchor_tip: ChainAnchor


def _write_trace(tg: Tracer, i: int) -> None:
    with tg.span("proj", "comp", "llm_complete", feature_as_of=NOW) as span:
        span.record_input({"q": f"question {i}"})
        span.record_output(parsed={"answer": f"answer {i}"})
        if i in SOURCE_TRACES:
            span.record_source(
                sources.SourceSnapshot(
                    source_uri=f"https://vendor.example/eps/{i}",
                    source_kind="vendor_api",
                    content_hash=f"{i}" * 64,
                    retrieved_at=NOW - timedelta(days=1),
                    published_at=NOW - timedelta(days=2),
                ),
                strict=False,
            )


@contextmanager
def _baseline() -> Iterator[Baseline]:
    """A fresh audited DB per use — a mutation is not undoable, so it is not shared."""
    engine = make_engine("sqlite:///:memory:", create_all=True)
    try:
        audit.enable(engine)
        sources.enable(engine)
        tg = Tracer(engine=engine)
        for i in range(1, 4):
            _write_trace(tg, i)
        anchor_seq3 = audit.export_anchor(engine)
        for i in range(4, 9):
            _write_trace(tg, i)
        anchor_seq8 = audit.export_anchor(engine)
        for i in range(9, 11):
            _write_trace(tg, i)
        anchor_tip = audit.export_anchor(engine)
        assert (anchor_seq3.seq, anchor_seq8.seq, anchor_tip.seq) == (3, 8, 10)
        yield Baseline(engine, anchor_seq3, anchor_seq8, anchor_tip)
    finally:
        audit.detach(engine)
        Base.metadata.drop_all(engine)
        engine.dispose()


# ── mutations, all via raw SQL ──────────────────────────────────────────────

def _rechain_from(engine: Engine, seq_from: int) -> None:
    """Re-chain seq_from..tail with the module's OWN hash function.

    This is the attack the whole anchor mechanism exists for: edit the content
    and recompute every downstream hash, so the chain is internally perfect and
    only an externally stored anchor from before the edit disagrees. Using
    `compute_row_hash` rather than a hand-rolled copy is the point — a forger
    would use the real algorithm too.
    """
    with Session(engine) as sess:
        entries = list(sess.scalars(select(AuditChainEntry).order_by(AuditChainEntry.seq)))
        traces = {t.trace_id: t for t in sess.scalars(select(Trace))}
        prev: str | None = None
        updates: list[tuple[int, str, str]] = []
        for entry in entries:
            if entry.seq < seq_from:
                prev = entry.row_hash
                continue
            new_prev = prev if prev is not None else entry.prev_hash
            content: Any = None
            if entry.entry_type in ("write", "backfill"):
                content = trace_content(traces[entry.trace_id])
            payload = entry_payload(
                entry_type=entry.entry_type,
                trace_id=entry.trace_id,
                event_id=entry.event_id,
                cost_at_event=entry.cost_at_event,
                note=entry.note,
                canon_status=entry.canon_status,
                canon_error=entry.canon_error,
                created_at=entry.created_at,
                content=content,
            )
            new_hash = compute_row_hash(new_prev, payload)
            updates.append((entry.seq, new_prev, new_hash))
            prev = new_hash
    # Ascending order matters: prev_hash carries a UNIQUE index, and each new
    # value is the row_hash written one step earlier.
    with engine.begin() as conn:
        for seq, new_prev, new_hash in updates:
            conn.exec_driver_sql(
                "UPDATE audit_chain_entries SET prev_hash=?, row_hash=? WHERE seq=?",
                (new_prev, new_hash, seq),
            )


def _delete_middle(engine: Engine) -> None:
    """One entry and its trace cut out of the middle. Tip seq and head hash do not move."""
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM audit_chain_entries WHERE seq = 5")
        conn.exec_driver_sql("DELETE FROM traces WHERE trace_id = 5")


def _delete_tail(engine: Engine) -> None:
    """The last two entries and traces. What remains is a valid SHORTER chain."""
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM audit_chain_entries WHERE seq IN (9, 10)")
        conn.exec_driver_sql("DELETE FROM traces WHERE trace_id IN (9, 10)")


def _edit_without_rechaining(engine: Engine) -> None:
    """Content edited, hashes left alone — the naive tamper."""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE traces SET output_parsed = ? WHERE trace_id = 4",
            ('{"answer": "tampered"}',),
        )


def _edit_and_rechain(engine: Engine) -> None:
    """Content edited and every downstream hash recomputed — the competent tamper."""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE traces SET output_parsed = ? WHERE trace_id = 6",
            ('{"answer": "tampered"}',),
        )
    _rechain_from(engine, 6)


def _edit_prev_hash(engine: Engine) -> None:
    """One entry's stored prev_hash replaced. A constant, not a substring edit:
    flipping the first nibble is a no-op whenever the hash already starts with it."""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE audit_chain_entries SET prev_hash = ? WHERE seq = 7", ("0" * 64,)
        )


def _no_mutation(engine: Engine) -> None:
    """The control. Every assertion about a clean chain is measured against this."""


MUTATIONS: dict[str, Callable[[Engine], None]] = {
    "delete_middle_entry": _delete_middle,
    "delete_last_two": _delete_tail,
    "edit_without_rechaining": _edit_without_rechaining,
    "edit_and_rechain": _edit_and_rechain,
    "edit_prev_hash": _edit_prev_hash,
    "control": _no_mutation,
}
REAL_MUTATIONS = tuple(name for name in MUTATIONS if name != "control")

#: Export shapes. `sparse` deliberately spans every mutated position (4, 5, 6, 7)
#: while skipping traces on both sides, because a sparse selection is the shape
#: dfda9d6 relaxed the linkage check for.
EXPORTS: dict[str, dict[str, Any]] = {
    "full_window": {},
    "sparse": {"trace_ids": [2, 4, 5, 6, 7, 9]},
    "tail_three": {"trace_ids": [8, 9, 10]},
}

#: Anchor choices. `post_mutation` is taken AFTER the mutation on purpose: an
#: anchor the exporter minted from the state it is exporting corroborates
#: nothing, and the differential must hold there too — `verify_chain` given that
#: same anchor is equally content, so the two tools still agree.
ANCHORS = ("none", "post_mutation", "old_seq3", "pre_mutation_tip")

MATRIX = [(m, e, a) for m in MUTATIONS for e in EXPORTS for a in ANCHORS]


# ── running one cell of the matrix ──────────────────────────────────────────

@dataclass(frozen=True)
class Cell:
    chain_ok: bool
    bundle_ok: bool
    summary: str
    bundle_breaks: int
    #: chain BREAKs naming an entry or trace this bundle carries, or the anchor
    #: relationship it carries. These — and only these — are what the bundle
    #: could possibly have seen.
    visible_chain_breaks: tuple[str, ...]

    @property
    def says_verified(self) -> bool:
        return "VERIFIED" in self.summary


def _anchor_of(base: Baseline, name: str) -> ChainAnchor | None:
    if name == "none":
        return None
    if name == "post_mutation":
        return audit.export_anchor(base.engine)
    if name == "old_seq3":
        return base.anchor_seq3
    return base.anchor_tip


def _run_cell(base: Baseline, export: str, anchor_name: str) -> Cell:
    anchor = _anchor_of(base, anchor_name)
    records = [anchor_record(anchor, location="/mnt/other-host/anchors.jsonl")] if anchor else None
    bundle = export_bundle(base.engine, anchors=records, **EXPORTS[export])
    result = verify_bundle(bundle)

    # The DB side of the differential gets the SAME anchor. Comparing a bundle
    # that carries anchor X against a chain check that used a different anchor
    # (or none) is not a differential — it is two different questions.
    chain = audit.verify_chain(base.engine, from_anchor=anchor)

    carried_seqs = {e["seq"] for e in bundle["chain"]["entries"]}
    carried_traces = {t["trace_id"] for t in bundle["traces"]}
    visible = tuple(
        f.kind
        for f in chain.findings
        if f.severity == BREAK
        and (f.seq in carried_seqs or f.trace_id in carried_traces or f.kind == "anchor_mismatch")
    )
    return Cell(
        chain_ok=chain.ok,
        bundle_ok=result.ok,
        summary=result.summary(),
        bundle_breaks=len(result.breaks),
        visible_chain_breaks=visible,
    )


# ── the guard itself ────────────────────────────────────────────────────────

@pytest.mark.parametrize("mutation", list(MUTATIONS))
def test_the_export_never_out_claims_the_database(mutation: str) -> None:
    """B3.6, over all 12 (export, anchor) combinations of one mutation.

    Where the chain reports a BREAK the bundle's window can see, the bundle must
    not reach for the strongest word it has.
    """
    with _baseline() as base:
        MUTATIONS[mutation](base.engine)
        for export in EXPORTS:
            for anchor in ANCHORS:
                cell = _run_cell(base, export, anchor)
                if cell.visible_chain_breaks:
                    assert not cell.says_verified, (
                        f"{mutation} / {export} / {anchor}: verify_chain reported "
                        f"{list(cell.visible_chain_breaks)} on entries this bundle carries, "
                        f"but the bundle says {cell.summary!r}"
                    )


@pytest.mark.parametrize("mutation", list(MUTATIONS))
def test_the_full_window_under_the_pre_mutation_anchor_agrees_exactly(mutation: str) -> None:
    """The one configuration where the two tools see the same thing must AGREE.

    A full window carries every entry, and the pre-mutation tip anchor is the
    trust root that existed before the damage. There is no window excuse left
    here, so `ok` is compared directly rather than only in one direction — that
    is what would have caught both historical holes on the spot.
    """
    with _baseline() as base:
        MUTATIONS[mutation](base.engine)
        cell = _run_cell(base, "full_window", "pre_mutation_tip")
        assert cell.bundle_ok == cell.chain_ok, (
            f"{mutation}: verify_chain ok={cell.chain_ok} but verify_bundle "
            f"ok={cell.bundle_ok} — {cell.summary}"
        )


@pytest.mark.parametrize("mutation", REAL_MUTATIONS)
def test_every_mutation_is_caught_in_the_strongest_configuration(mutation: str) -> None:
    """B3.4: prove the alarm RINGS, not only that it stays quiet.

    A differential that only forbids over-claiming is satisfied by a verifier
    that fails everything. Each of the five mutations must actually be detected
    by the full window under the pre-mutation anchor.
    """
    with _baseline() as base:
        MUTATIONS[mutation](base.engine)
        cell = _run_cell(base, "full_window", "pre_mutation_tip")
        assert not cell.bundle_ok, f"{mutation} went undetected: {cell.summary}"
        assert cell.bundle_breaks >= 1


def test_an_unmutated_chain_produces_no_break_anywhere() -> None:
    """Every one of the 12 clean combinations, zero BREAK-class findings.

    This is the standing guard against the false POSITIVES the two fixes were
    reacting to: a truthful out-of-window anchor and a legitimately sparse
    selection both used to produce BREAKs on an untouched chain.
    """
    with _baseline() as base:
        for export in EXPORTS:
            for anchor in ANCHORS:
                cell = _run_cell(base, export, anchor)
                assert cell.chain_ok, f"control / {export} / {anchor}: chain not ok"
                assert cell.bundle_ok, f"control / {export} / {anchor}: {cell.summary}"
                assert cell.bundle_breaks == 0, (
                    f"control / {export} / {anchor}: {cell.bundle_breaks} BREAK(s) on an "
                    f"untouched chain — {cell.summary}"
                )


def test_an_unmutated_chain_can_still_say_verified_and_the_old_anchor_stays_quiet() -> None:
    """The control's positive half: the strong word is reachable, the old anchor is silent."""
    with _baseline() as base:
        assert "VERIFIED" in _run_cell(base, "full_window", "pre_mutation_tip").summary
        for export in EXPORTS:
            cell = _run_cell(base, export, "old_seq3")
            assert cell.bundle_breaks == 0, (
                f"control / {export} / old_seq3: an anchor taken at seq 3 constrains nothing "
                f"here and must not be reported as evidence of a rewrite — {cell.summary}"
            )


#: (mutation, export, anchor) cells where the database FAILS and the export
#: still says VERIFIED — legitimately, because the damage is wholly outside the
#: exported window. Frozen rather than tolerated by a rule: the set is what
#: `docs/audit.md`'s "damage outside the window is invisible" costs in practice,
#: and anything joining it is a new hole, not a new excuse.
WINDOW_BLIND_SPOTS: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("delete_middle_entry", "tail_three", "post_mutation"),
        ("edit_without_rechaining", "tail_three", "post_mutation"),
        ("edit_without_rechaining", "tail_three", "pre_mutation_tip"),
        ("edit_prev_hash", "tail_three", "post_mutation"),
        ("edit_prev_hash", "tail_three", "pre_mutation_tip"),
    }
)


def test_the_window_blind_spots_are_exactly_the_five_we_know_about() -> None:
    """Enumerate what "weaker is allowed" actually costs, and pin it.

    Each cell here is a chain that FAILS with an export that says VERIFIED. That
    is only acceptable because the mutated position is not carried in the window
    — asserted, not assumed. A sixth entry appearing means some rule started
    reaching past what its evidence supports; a missing one means a rule got
    stricter and this list should shrink with it.
    """
    observed: set[tuple[str, str, str]] = set()
    for mutation in MUTATIONS:
        with _baseline() as base:
            MUTATIONS[mutation](base.engine)
            for export in EXPORTS:
                for anchor in ANCHORS:
                    cell = _run_cell(base, export, anchor)
                    if cell.chain_ok or not cell.says_verified:
                        continue
                    observed.add((mutation, export, anchor))
                    assert not cell.visible_chain_breaks, (
                        f"{mutation} / {export} / {anchor}: the bundle says VERIFIED while "
                        f"the chain reports {list(cell.visible_chain_breaks)} INSIDE this "
                        "window — that is B3.6, not a window limit"
                    )
    assert observed == WINDOW_BLIND_SPOTS, (
        f"blind-spot set changed.\n  new: {sorted(observed - WINDOW_BLIND_SPOTS)}\n"
        f"  gone: {sorted(WINDOW_BLIND_SPOTS - observed)}"
    )


def test_the_matrix_is_the_size_the_docstring_claims() -> None:
    """6 x 3 x 4. A guard whose coverage silently shrank is the failure mode of guards."""
    assert (len(MUTATIONS), len(EXPORTS), len(ANCHORS)) == (6, 3, 4)
    assert len(MATRIX) == 72

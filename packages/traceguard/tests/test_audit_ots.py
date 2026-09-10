"""OpenTimestamps anchor sink (SPEC v1.2, B4) — a second independent witness.

Every network call is injected; the proof parsing runs against real serialized
`.ots` bytes built with the library, so the pending/complete classification is
exercised for real rather than mocked.
"""
from __future__ import annotations

import pytest

from traceguard import audit
from traceguard.audit.anchors import AnchorSinkError, parse_sink_spec
from traceguard.audit.verify import ChainAnchor

ots_mod = pytest.importorskip(
    "opentimestamps", reason="needs the anchors extra (pip install 'traceguard[anchors]')"
)

from traceguard.audit.ots import (  # noqa: E402
    COMPLETE,
    PENDING,
    OtsAnchorSink,
    load_anchor_beside,
    ots_digest,
    parse_ots_proof,
    upgrade_proof,
    verify_ots_proof,
)

ANCHOR = ChainAnchor(
    seq=42,
    row_hash="a" * 64,
    algo_version=1,
    entry_count=42,
    exported_at="2026-09-01T00:00:00+00:00",
)
OTHER = ChainAnchor(
    seq=43,
    row_hash="b" * 64,
    algo_version=1,
    entry_count=43,
    exported_at="2026-09-02T00:00:00+00:00",
)


def _timestamp(digest, *, pending_uri=None, height=None):
    from opentimestamps.core.notary import (
        BitcoinBlockHeaderAttestation,
        PendingAttestation,
    )
    from opentimestamps.core.timestamp import Timestamp

    ts = Timestamp(digest)
    if pending_uri:
        ts.attestations.add(PendingAttestation(pending_uri))
    if height is not None:
        ts.attestations.add(BitcoinBlockHeaderAttestation(height))
    return ts


class _FakeCalendar:
    """Stands in for RemoteCalendar; records what it was asked."""

    def __init__(self, url, *, height=None, fail=False):
        self.url = url
        self.height = height
        self.fail = fail
        self.submitted = []

    def submit(self, digest):
        if self.fail:
            raise RuntimeError(f"{self.url} unreachable")
        self.submitted.append(digest)
        return _timestamp(digest, pending_uri=self.url, height=self.height)

    def get_timestamp(self, commitment):
        if self.fail:
            raise RuntimeError("nothing yet")
        return _timestamp(commitment, height=self.height)


def _factory(**kw):
    made = {}

    def make(url):
        made[url] = _FakeCalendar(url, **kw)
        return made[url]

    make.made = made
    return make


# ── the digest commits to the whole anchor, not just the head hash ─────────

def test_digest_covers_the_entire_anchor_statement():
    """Stamping row_hash alone would leave seq and entry_count unattested, so a
    proof would still match a head re-pointed inside a rewritten chain."""
    base = ots_digest(ANCHOR)
    moved = ChainAnchor(
        seq=99, row_hash=ANCHOR.row_hash, algo_version=1,
        entry_count=ANCHOR.entry_count, exported_at=ANCHOR.exported_at,
    )
    assert ots_digest(moved) != base
    assert len(base) == 32


def test_digest_is_deterministic():
    assert ots_digest(ANCHOR) == ots_digest(ANCHOR)


# ── stamping ────────────────────────────────────────────────────────────────

def test_store_writes_the_proof_and_its_anchor_sidecar(tmp_path):
    """Two files on purpose: a digest is meaningless without the exact anchor
    JSON it came from, and the pair must stay verifiable with no database."""
    factory = _factory()
    sink = OtsAnchorSink(tmp_path, calendars=("https://cal.example",), calendar_factory=factory)
    proof_path = sink.store(ANCHOR)

    assert proof_path.suffix == ".ots"
    sidecar = proof_path.with_suffix(".json")
    assert sidecar.exists()
    assert load_anchor_beside(proof_path) == ANCHOR
    assert factory.made["https://cal.example"].submitted == [ots_digest(ANCHOR)]


def test_a_fresh_stamp_is_pending_and_says_it_is_not_evidence(tmp_path):
    sink = OtsAnchorSink(
        tmp_path, calendars=("https://cal.example",), calendar_factory=_factory()
    )
    proof = parse_ots_proof(sink.store(ANCHOR))
    assert proof.status == PENDING
    assert not proof.is_complete
    assert proof.calendar_uris == ("https://cal.example",)
    assert "PENDING" in proof.describe()
    assert "is NOT evidence" in proof.describe()


def test_a_bitcoin_attested_proof_is_complete_and_hedges_the_time(tmp_path):
    sink = OtsAnchorSink(
        tmp_path, calendars=("https://cal.example",), calendar_factory=_factory(height=800000)
    )
    proof = parse_ots_proof(sink.store(ANCHOR))
    assert proof.status == COMPLETE
    assert proof.bitcoin_heights == (800000,)
    text = proof.describe()
    assert "COMPLETE" in text
    # Never claims a wall-clock instant.
    assert "uncertainty" in text
    assert "needs a Bitcoin node" in text


def test_every_calendar_failing_raises_rather_than_writing_nothing(tmp_path):
    """SPEC B3.4: an anchor that silently never landed is a false sense of
    coverage, so this is loud."""
    sink = OtsAnchorSink(
        tmp_path, calendars=("https://a.example", "https://b.example"),
        calendar_factory=_factory(fail=True),
    )
    with pytest.raises(AnchorSinkError) as excinfo:
        sink.store(ANCHOR)
    assert "no OpenTimestamps calendar accepted" in str(excinfo.value)
    assert list(tmp_path.glob("*.ots")) == []


def test_one_reachable_calendar_is_enough(tmp_path):
    def make(url):
        return _FakeCalendar(url, fail=url.startswith("https://dead"))

    sink = OtsAnchorSink(
        tmp_path, calendars=("https://dead.example", "https://live.example"),
        calendar_factory=make,
    )
    assert sink.store(ANCHOR).exists()


# ── verification ────────────────────────────────────────────────────────────

def test_a_proof_matches_the_anchor_it_stamped(tmp_path):
    sink = OtsAnchorSink(tmp_path, calendars=("https://c.example",), calendar_factory=_factory())
    proof = parse_ots_proof(sink.store(ANCHOR))
    matched, explanation = verify_ots_proof(proof, ANCHOR)
    assert matched
    assert "PENDING" in explanation


def test_a_proof_for_a_different_anchor_does_not_match(tmp_path):
    sink = OtsAnchorSink(tmp_path, calendars=("https://c.example",), calendar_factory=_factory())
    proof = parse_ots_proof(sink.store(ANCHOR))
    matched, explanation = verify_ots_proof(proof, OTHER)
    assert not matched
    assert "a different anchor" in explanation


def test_upgrade_turns_a_pending_proof_complete(tmp_path):
    sink = OtsAnchorSink(tmp_path, calendars=("https://c.example",), calendar_factory=_factory())
    path = sink.store(ANCHOR)
    assert parse_ots_proof(path).status == PENDING

    upgraded = upgrade_proof(
        path, calendars=("https://c.example",), calendar_factory=_factory(height=810000)
    )
    assert upgraded.status == COMPLETE
    assert upgraded.bitcoin_heights == (810000,)
    # rewritten in place, so a later read sees it too
    assert parse_ots_proof(path).status == COMPLETE


def test_upgrade_with_nothing_available_stays_pending_and_is_not_an_error(tmp_path):
    """Normal shortly after stamping — a calendar that has nothing yet is not
    a failure."""
    sink = OtsAnchorSink(tmp_path, calendars=("https://c.example",), calendar_factory=_factory())
    path = sink.store(ANCHOR)
    still = upgrade_proof(
        path, calendars=("https://c.example",), calendar_factory=_factory(fail=True)
    )
    assert still.status == PENDING


def test_proofs_and_latest_read_the_directory_back(tmp_path):
    sink = OtsAnchorSink(tmp_path, calendars=("https://c.example",), calendar_factory=_factory())
    sink.store(ANCHOR)
    sink.store(OTHER)
    assert len(sink.proofs()) == 2
    assert sink.latest() == OTHER  # seq 43 sorts after 42


def test_latest_on_an_empty_directory_is_none(tmp_path):
    assert OtsAnchorSink(tmp_path / "nope").latest() is None
    assert OtsAnchorSink(tmp_path / "nope").proofs() == []


# ── sink spec parsing, mirroring file: ──────────────────────────────────────

def test_ots_sink_spec_parses(tmp_path):
    sink = parse_sink_spec(f"ots:{tmp_path}")
    assert isinstance(sink, OtsAnchorSink)
    assert sink.name == f"ots:{tmp_path}"


def test_ots_sink_spec_without_a_directory_errors_like_file_does():
    with pytest.raises(ValueError, match="ots sink needs a directory"):
        parse_sink_spec("ots:")
    with pytest.raises(ValueError, match="file sink needs a path"):
        parse_sink_spec("file:")


def test_unknown_sink_message_lists_ots():
    with pytest.raises(ValueError, match="ots:DIR"):
        parse_sink_spec("carrier-pigeon:x")


# ── the public surface ──────────────────────────────────────────────────────

def test_ots_symbols_are_on_the_audit_surface():
    for name in ("OtsAnchorSink", "OtsProof", "ots_digest", "parse_ots_proof",
                 "verify_ots_proof", "upgrade_proof", "OTS_PENDING", "OTS_COMPLETE"):
        assert name in audit.__all__
        assert hasattr(audit, name)
    assert audit.OTS_PENDING == PENDING and audit.OTS_COMPLETE == COMPLETE


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_verify_reports_a_pending_proof(tmp_path, capsys):
    import traceguard
    from traceguard.audit.__main__ import main

    url = f"sqlite:///{tmp_path/'t.db'}"
    eng = traceguard.make_engine(url)
    audit.enable(eng)
    audit.detach(eng)

    head = audit.export_anchor(eng)
    sink = OtsAnchorSink(
        tmp_path / "ots", calendars=("https://c.example",), calendar_factory=_factory()
    )
    proof_path = sink.store(head)

    assert main(["--db", url, "verify", "--ots-proof", str(proof_path)]) == 0
    out = capsys.readouterr().out
    assert "ots:" in out and "PENDING" in out


def test_cli_verify_breaks_on_a_proof_for_another_anchor(tmp_path, capsys):
    import traceguard
    from traceguard.audit.__main__ import main

    url = f"sqlite:///{tmp_path/'t.db'}"
    eng = traceguard.make_engine(url)
    audit.enable(eng)
    audit.detach(eng)

    sink = OtsAnchorSink(
        tmp_path / "ots", calendars=("https://c.example",), calendar_factory=_factory()
    )
    proof_path = sink.store(ANCHOR)  # an anchor this chain never had
    proof_path.with_suffix(".json").unlink()  # ... and no sidecar to explain it

    code = main([
        "--db", url, "verify",
        "--anchor", audit.export_anchor(eng).to_json(),
        "--ots-proof", str(proof_path),
    ])
    assert code == 1
    assert "a different anchor" in capsys.readouterr().out

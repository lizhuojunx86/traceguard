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


class _RealisticCalendar:
    """A calendar that behaves the way the protocol actually does.

    Two properties the permissive fake above does not have, and both of them
    were hiding a permanent no-op in `upgrade_proof`:

    1. ``submit`` does not attest the digest you send. It appends a nonce and
       hashes, and the pending attestation hangs off THAT commitment — so the
       commitment to ask about later is not the file digest.
    2. ``get_timestamp`` raises for a commitment it does not know, exactly as
       the real one raises CommitmentNotFoundError on the calendar's 404.
    """

    def __init__(self, url, *, height=None, forwarded_uri=None):
        from opentimestamps.core.op import OpAppend, OpSHA256

        self.url = url
        self.height = height
        self.forwarded_uri = forwarded_uri or url
        self._OpAppend, self._OpSHA256 = OpAppend, OpSHA256
        self.known = {}
        self.asked = []

    def submit(self, digest, timeout=None):
        from opentimestamps.core.notary import PendingAttestation
        from opentimestamps.core.timestamp import Timestamp

        ts = Timestamp(digest)
        nonced = ts.ops.add(self._OpAppend(b"\x2a" * 16))
        leaf = nonced.ops.add(self._OpSHA256())
        leaf.attestations.add(PendingAttestation(self.forwarded_uri))
        self.known[leaf.msg] = leaf.msg
        return ts

    def get_timestamp(self, commitment, timeout=None):
        from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
        from opentimestamps.core.timestamp import Timestamp

        self.asked.append(commitment)
        if commitment not in self.known:
            raise KeyError("calendar has no such commitment")  # the real 404
        ts = Timestamp(commitment)
        if self.height is not None:
            ts.attestations.add(BitcoinBlockHeaderAttestation(self.height))
        return ts


def _realistic_pair(*, height, forwarded_uri=None):
    """One calendar object shared by the submit and the upgrade factories."""
    holder = {}

    def submit_factory(url):
        cal = holder.get("cal") or _RealisticCalendar(
            url, height=height, forwarded_uri=forwarded_uri
        )
        holder["cal"] = cal
        return cal

    submit_factory.holder = holder
    return submit_factory


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
    assert "Bitcoin node" in text
    # And never states the proof's claim as a fact of its own: nothing here
    # verified the merkle path or the block header, so a fabricated .ots
    # classifies COMPLETE too.
    assert "CLAIMS" in text
    assert "does NOT check that claim" in text


def test_a_fabricated_proof_classifies_complete_and_the_wording_admits_it(tmp_path):
    """The honesty pair for this capability: what COMPLETE proves is *nothing*
    on its own, and the text has to say so or it is an overclaim."""
    from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
    from opentimestamps.core.serialize import BytesSerializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp
    from opentimestamps.core.op import OpSHA256

    forged = Timestamp(ots_digest(ANCHOR))
    forged.attestations.add(BitcoinBlockHeaderAttestation(1))  # a height, invented
    ctx = BytesSerializationContext()
    DetachedTimestampFile(OpSHA256(), forged).serialize(ctx)
    path = tmp_path / "forged.ots"
    path.write_bytes(ctx.getbytes())

    proof = parse_ots_proof(path)
    assert proof.status == COMPLETE  # traceguard cannot tell, and must not pretend to
    matched, explanation = verify_ots_proof(proof, ANCHOR)
    assert matched  # it IS about this anchor; that is all matched means
    assert "CLAIMS" in explanation and "hand-written" in explanation


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


def test_upgrade_asks_the_uri_inside_the_attestation_not_the_submit_url(tmp_path):
    """The regression that made upgrade_proof a silent no-op in production.

    Submitting to a POOL address returns an attestation naming the concrete
    calendar the pool forwarded to — a different host from the one submitted
    to. Filtering attestations by the submit URL therefore skipped every real
    one and returned "still pending", which reads as the normal outcome.
    """
    pool = "https://a.pool.opentimestamps.org"
    forwarded = "https://alice.btc.calendar.opentimestamps.org"

    def submit_factory(url):
        cal = _FakeCalendar(url)
        # what a pool does: the promise names whoever actually holds it
        cal.submit = lambda digest: _timestamp(digest, pending_uri=forwarded)
        return cal

    sink = OtsAnchorSink(tmp_path, calendars=(pool,), calendar_factory=submit_factory)
    path = sink.store(ANCHOR)
    assert parse_ots_proof(path).status == PENDING

    asked = _factory(height=820000)
    upgraded = upgrade_proof(path, calendar_factory=asked)

    assert upgraded.status == COMPLETE, "the default must follow the attestation's own URI"
    assert list(asked.made) == [forwarded], "asked the wrong host"


def test_upgrade_calendars_is_an_allowlist_and_says_so_when_it_excludes_everything(
    tmp_path, caplog
):
    """Passing `calendars` restricts which hosts may be contacted. An allowlist
    that matches nothing must not be indistinguishable from "not ready yet"."""
    sink = OtsAnchorSink(
        tmp_path, calendars=("https://real.example",), calendar_factory=_factory()
    )
    path = sink.store(ANCHOR)

    asked = _factory(height=830000)
    with caplog.at_level("WARNING", logger="traceguard.audit.ots"):
        still = upgrade_proof(path, calendars=("https://other.example",), calendar_factory=asked)

    assert still.status == PENDING
    assert not asked.made, "an excluded attestation must not be contacted"
    assert "allowlist" in caplog.text and "https://real.example" in caplog.text


def test_upgrade_allowlist_still_admits_the_host_it_names(tmp_path):
    sink = OtsAnchorSink(
        tmp_path, calendars=("https://real.example",), calendar_factory=_factory()
    )
    path = sink.store(ANCHOR)
    upgraded = upgrade_proof(
        path, calendars=("https://real.example",), calendar_factory=_factory(height=840000)
    )
    assert upgraded.status == COMPLETE
    assert upgraded.bitcoin_heights == (840000,)


def test_upgrade_against_a_calendar_that_behaves_like_a_real_one(tmp_path):
    """The blocker the permissive fakes hid.

    A real calendar attests a NONCED commitment, not the file digest, and 404s
    on anything else. Asking it about the root digest therefore raised, the
    error was swallowed as "nothing yet", and every upgrade was a permanent
    silent no-op — indistinguishable from a proof that simply is not ready.
    """
    factory = _realistic_pair(height=850000)
    sink = OtsAnchorSink(tmp_path, calendars=("https://cal.example",), calendar_factory=factory)
    path = sink.store(ANCHOR)
    assert parse_ots_proof(path).status == PENDING

    upgraded = upgrade_proof(path, calendar_factory=factory)

    assert upgraded.status == COMPLETE
    assert upgraded.bitcoin_heights == (850000,)
    assert parse_ots_proof(path).status == COMPLETE

    cal = factory.holder["cal"]
    assert cal.asked, "no calendar call was made at all"
    root = ots_digest(ANCHOR)
    assert all(c != root for c in cal.asked), (
        "asked about the file digest; the calendar only knows the nonced commitment"
    )


def test_upgrade_through_a_pool_that_forwards_and_nonces(tmp_path):
    """Both failure causes at once: the pool answers under a different host AND
    the commitment is nonced."""
    factory = _realistic_pair(
        height=860000, forwarded_uri="https://bob.btc.calendar.opentimestamps.org"
    )
    sink = OtsAnchorSink(
        tmp_path,
        calendars=("https://a.pool.opentimestamps.org",),
        calendar_factory=factory,
    )
    path = sink.store(ANCHOR)
    assert upgrade_proof(path, calendar_factory=factory).status == COMPLETE


def test_allowlist_does_not_admit_a_lookalike_host(tmp_path):
    """`startswith` alone would admit cal.example.attacker.test."""
    factory = _realistic_pair(
        height=870000, forwarded_uri="https://cal.example.attacker.test/ots"
    )
    sink = OtsAnchorSink(tmp_path, calendars=("https://cal.example",), calendar_factory=factory)
    path = sink.store(ANCHOR)

    still = upgrade_proof(
        path, calendars=("https://cal.example",), calendar_factory=factory
    )
    assert still.status == PENDING
    assert not factory.holder["cal"].asked, "contacted a host the allowlist does not name"


def test_the_sink_passes_its_timeout_to_the_calendar(tmp_path):
    """An untimed socket in AnchorScheduler's daemon thread stalls the cadence."""
    seen = {}

    def factory(url):
        class _C:
            def submit(self, digest, timeout=None):
                seen["submit"] = timeout
                return _timestamp(digest, pending_uri=url)

            def get_timestamp(self, commitment, timeout=None):
                seen["get"] = timeout
                raise KeyError("nothing yet")

        return _C()

    sink = OtsAnchorSink(
        tmp_path, calendars=("https://c.example",), calendar_factory=factory, timeout=3.5
    )
    path = sink.store(ANCHOR)
    upgrade_proof(path, timeout=2.5, calendar_factory=factory)
    assert seen["submit"] == 3.5
    assert seen["get"] == 2.5


def test_a_failed_upgrade_leaves_the_existing_proof_intact(tmp_path):
    """upgrade_proof rewrites the only copy; a partial write destroys evidence."""
    sink = OtsAnchorSink(tmp_path, calendars=("https://c.example",), calendar_factory=_factory())
    path = sink.store(ANCHOR)
    before = path.read_bytes()

    still = upgrade_proof(path, calendar_factory=_factory(fail=True))
    assert still.status == PENDING
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


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

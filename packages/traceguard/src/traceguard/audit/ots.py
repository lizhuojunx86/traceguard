"""OpenTimestamps anchor sink — a second, independent witness (SPEC v1.2, B4).

Why a second anchor at all. Boundary statement 1 says the chain is not a MAC:
"the chain head existed at time T" is only as good as the anchor that says so,
and every v1 sink leans on ONE party — a file the DB writer can usually also
edit, a git remote, a webhook receiver. OpenTimestamps replaces that single
party with the Bitcoin block chain: the proof says *this digest existed before
block N*, and nobody involved has to be trusted individually.

What it does NOT do, stated up front because it is easy to oversell:

- **It does not shrink the exposure window.** Anchoring frequency is still the
  window (boundary statement 1, unchanged). A second witness makes the anchor
  harder to repudiate; it does not make un-anchored entries safe.
- **A `pending` proof is not evidence.** A fresh stamp carries only a calendar
  server's *promise* to include the digest. Until it is upgraded and carries a
  ``BitcoinBlockHeaderAttestation``, it proves nothing a plain HTTP receipt
  would not. :func:`parse_ots_proof` reports the two states separately and
  never conflates them.
- **Complete ≠ a precise time.** A Bitcoin attestation bounds the digest to
  "existed before block N was mined". Block timestamps are loose (miners have
  latitude, and the protocol only enforces a median-time rule), so the honest
  reading is minutes-to-hours of uncertainty, not a wall-clock instant.
- **Full verification needs a Bitcoin node.** Without one you are trusting a
  block explorer or the calendar server for the block hash. traceguard does
  neither: it parses and classifies the proof and stops there.

Requires the ``anchors`` extra (``pip install 'traceguard[anchors]'``); the
import is lazy so the core package keeps its zero-dependency posture.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from traceguard.audit.verify import ChainAnchor

#: Public OpenTimestamps calendars, the same default set the `ots` client uses.
DEFAULT_CALENDARS: tuple[str, ...] = (
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
    "https://a.pool.eternitywall.com",
)

_log = logging.getLogger("traceguard.audit.ots")

#: Every calendar call is network I/O, and OtsAnchorSink runs inside
#: AnchorScheduler's daemon thread — an untimed socket there stalls the
#: cadence indefinitely with nothing in the logs.
DEFAULT_TIMEOUT = 30.0

PENDING = "pending"
COMPLETE = "complete"

_EXTRA_HINT = (
    "OpenTimestamps support needs the 'anchors' extra: "
    "pip install 'traceguard[anchors]'"
)


def _ots():
    """Import the opentimestamps modules, or raise with the install hint."""
    try:
        from opentimestamps.calendar import RemoteCalendar
        from opentimestamps.core.op import OpSHA256
        from opentimestamps.core.notary import (
            BitcoinBlockHeaderAttestation,
            PendingAttestation,
        )
        from opentimestamps.core.serialize import (
            BytesDeserializationContext,
            BytesSerializationContext,
        )
        from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp
    except ImportError as exc:  # pragma: no cover - exercised by the skip path
        raise ImportError(f"{_EXTRA_HINT} ({exc})") from exc
    return {
        "RemoteCalendar": RemoteCalendar,
        "OpSHA256": OpSHA256,
        "PendingAttestation": PendingAttestation,
        "BitcoinBlockHeaderAttestation": BitcoinBlockHeaderAttestation,
        "BytesDeserializationContext": BytesDeserializationContext,
        "BytesSerializationContext": BytesSerializationContext,
        "DetachedTimestampFile": DetachedTimestampFile,
        "Timestamp": Timestamp,
    }


def ots_digest(anchor: ChainAnchor) -> bytes:
    """The 32 bytes an OTS proof commits to, for ``anchor``.

    ``sha256(anchor.to_json())`` — the WHOLE anchor statement, not just
    ``row_hash``. Stamping the head hash alone would leave ``seq`` and
    ``entry_count`` unattested, so a proof would still match after the head was
    re-pointed to a different position in a rewritten chain.
    ``ChainAnchor.to_json`` is deterministic (sorted keys, compact separators),
    so a verifier recomputes exactly these bytes.
    """
    return hashlib.sha256(anchor.to_json().encode("ascii")).digest()


@dataclass(frozen=True)
class OtsProof:
    """A parsed ``.ots`` file. ``status`` is the whole point of this type."""

    file_digest: bytes
    status: str  # PENDING | COMPLETE
    calendar_uris: tuple[str, ...] = ()
    bitcoin_heights: tuple[int, ...] = ()
    path: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.status == COMPLETE

    def describe(self) -> str:
        if self.is_complete:
            return (
                f"OTS proof COMPLETE: the proof CLAIMS the digest existed before Bitcoin "
                f"block(s) {list(self.bitcoin_heights)}. traceguard does NOT check that "
                "claim — it reads the attestation and verifies neither the merkle path to "
                "the block's merkle root nor the block header itself, so a hand-written "
                ".ots file reads COMPLETE here exactly like a real one. Run `ots verify` "
                "against a Bitcoin node before relying on it. Even once verified, block "
                "times carry minutes-to-hours of uncertainty."
            )
        return (
            f"OTS proof PENDING: only calendar-server promises so far "
            f"({list(self.calendar_uris)}). A pending proof is NOT evidence — upgrade it "
            "(python -m traceguard.audit ... --ots-upgrade) once the calendar has "
            "committed it to a block."
        )


def parse_ots_proof(data: bytes | str | os.PathLike[str]) -> OtsProof:
    """Parse a ``.ots`` detached proof and classify it pending vs complete.

    Accepts raw bytes or a path. Structure only: no network, no Bitcoin node,
    no block-hash confirmation.

    **What COMPLETE proves: nothing, on its own.** It reports what the file
    says, not whether the file is honest — the merkle path is not walked and no
    block header is fetched, so a fabricated attestation classifies COMPLETE.
    The classification is worth having because a PENDING proof is not evidence
    even when genuine, and telling the two apart is the part that can be done
    with no dependencies. Establishing that a COMPLETE proof is true is `ots
    verify`'s job and needs a Bitcoin node you choose to trust.
    """
    path: str | None = None
    if isinstance(data, (str, os.PathLike)):
        path = str(data)
        blob = Path(data).read_bytes()
    else:
        blob = data

    mod = _ots()
    ctx = mod["BytesDeserializationContext"](blob)
    detached = mod["DetachedTimestampFile"].deserialize(ctx)

    calendars: list[str] = []
    heights: list[int] = []
    for _msg, attestation in detached.timestamp.all_attestations():
        if isinstance(attestation, mod["BitcoinBlockHeaderAttestation"]):
            heights.append(int(attestation.height))
        elif isinstance(attestation, mod["PendingAttestation"]):
            uri = attestation.uri
            calendars.append(uri.decode() if isinstance(uri, bytes) else str(uri))
    return OtsProof(
        file_digest=bytes(detached.file_digest),
        # A proof with ANY bitcoin attestation is complete; pending ones may
        # still be listed alongside (a stamp submitted to several calendars
        # upgrades unevenly), and that is not a contradiction.
        status=COMPLETE if heights else PENDING,
        calendar_uris=tuple(calendars),
        bitcoin_heights=tuple(sorted(heights)),
        path=path,
    )


def verify_ots_proof(proof: OtsProof, anchor: ChainAnchor) -> tuple[bool, str]:
    """Does ``proof`` commit to ``anchor``? Returns ``(matches, explanation)``.

    This is a digest comparison, not a cryptographic verification of the
    Bitcoin attestation. A ``True`` here means "this proof is about this
    anchor" and nothing more; whether the proof's own claim holds is what a
    Bitcoin node would answer. In particular ``True`` on a COMPLETE proof is
    not "the anchor existed at that time" — it is "a file claiming so is about
    this anchor". See :func:`parse_ots_proof`.
    """
    expected = ots_digest(anchor)
    if proof.file_digest != expected:
        return (
            False,
            f"the OTS proof commits to {proof.file_digest.hex()} but this anchor "
            f"digests to {expected.hex()} — the proof is about a different anchor",
        )
    return True, proof.describe()


@dataclass
class OtsAnchorSink:
    """Stamp each anchor with OpenTimestamps; write ``.ots`` + ``.json`` to a directory.

    Two files per anchor, on purpose: the ``.ots`` proof commits to a digest,
    and the digest is meaningless without the exact anchor JSON it was computed
    from. Storing them together makes the pair self-verifying later, with no
    database.

    Freshly stamped proofs are **pending**: they carry a calendar's promise, not
    a Bitcoin attestation. Call :func:`upgrade_proof` later (hours, typically)
    to fetch the completed proof.

    Network failure raises :class:`~traceguard.audit.anchors.AnchorSinkError`,
    which ``anchor_to`` collects and reports — an anchor that silently never
    landed is a false sense of coverage (SPEC B3.4).
    """

    directory: str | os.PathLike[str]
    calendars: tuple[str, ...] = DEFAULT_CALENDARS
    timeout: float | None = DEFAULT_TIMEOUT
    #: Test seam: a callable(url) -> object with .submit(digest) / .get_timestamp(c).
    calendar_factory: Any = None
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)
        self.name = f"ots:{self.directory}"

    def _calendar(self, url: str):
        if self.calendar_factory is not None:
            return self.calendar_factory(url)
        return _ots()["RemoteCalendar"](url)

    def _stem(self, anchor: ChainAnchor, digest: bytes) -> str:
        return f"anchor-{anchor.seq:08d}-{digest.hex()[:16]}"

    def store(self, anchor: ChainAnchor) -> Path:
        """Stamp the anchor and write ``<dir>/<stem>.ots`` and ``<stem>.json``."""
        from traceguard.audit.anchors import AnchorSinkError

        mod = _ots()
        digest = ots_digest(anchor)
        timestamp = mod["Timestamp"](digest)

        failures: list[str] = []
        merged = False
        for url in self.calendars:
            try:
                remote = self._calendar(url)
                calendar_ts = _submit(remote, digest, self.timeout)
                timestamp.merge(calendar_ts)
                merged = True
            except Exception as exc:  # noqa: BLE001 - collect, decide after the loop
                failures.append(f"{url}: {exc}")
        if not merged:
            raise AnchorSinkError(
                f"no OpenTimestamps calendar accepted the stamp for anchor "
                f"seq={anchor.seq}: {'; '.join(failures)}"
            )

        detached = mod["DetachedTimestampFile"](mod["OpSHA256"](), timestamp)
        ctx = mod["BytesSerializationContext"]()
        detached.serialize(ctx)

        directory = Path(self.directory)
        directory.mkdir(parents=True, exist_ok=True)
        stem = self._stem(anchor, digest)
        proof_path = directory / f"{stem}.ots"
        # Sidecar first: a digest with no anchor beside it is unreadable, so if
        # only one of the pair survives a crash it must be the .json. The .ots
        # is what `proofs()` lists, so writing it last also keeps a half-stored
        # anchor from being listed as stored.
        _write_atomic(directory / f"{stem}.json", (anchor.to_json() + "\n").encode("ascii"))
        _write_atomic(proof_path, ctx.getbytes())
        return proof_path

    def proofs(self) -> list[Path]:
        """Every ``.ots`` file in the directory, oldest name first."""
        directory = Path(self.directory)
        return sorted(directory.glob("*.ots")) if directory.exists() else []

    def latest(self) -> ChainAnchor | None:
        """The anchor beside the newest ``.ots`` proof, if any."""
        proofs = self.proofs()
        if not proofs:
            return None
        sidecar = proofs[-1].with_suffix(".json")
        if not sidecar.exists():
            return None
        return ChainAnchor.from_json(sidecar.read_text(encoding="ascii").strip())


def _submit(remote: Any, digest: bytes, timeout: float | None) -> Any:
    """``submit`` with a timeout when the calendar accepts one (see _get_timestamp)."""
    if timeout is None:
        return remote.submit(digest)
    try:
        return remote.submit(digest, timeout=timeout)
    except TypeError:
        return remote.submit(digest)


def _write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` in one step.

    upgrade_proof rewrites the only copy of a proof; a partial write there
    destroys evidence rather than merely failing.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _uri_allowed(uri: str, allowlist: tuple[str, ...]) -> bool:
    """Match on a path boundary, not a bare prefix.

    ``startswith("https://cal.example")`` also admits
    ``https://cal.example.attacker.test`` — an allowlist that admits hosts it
    does not name is not an allowlist.
    """
    for allowed in allowlist:
        if uri == allowed:
            return True
        base = allowed if allowed.endswith("/") else allowed + "/"
        if uri.startswith(base):
            return True
    return False


def _node_for(timestamp: Any, msg: bytes) -> Any:
    """The sub-timestamp committing to ``msg``, or None.

    ``Timestamp.merge`` refuses timestamps for a different message, so the
    calendar's answer has to be merged at the node it answers about.
    """
    if timestamp.msg == msg:
        return timestamp
    for sub_stamp in timestamp.ops.values():
        found = _node_for(sub_stamp, msg)
        if found is not None:
            return found
    return None


def _get_timestamp(remote: Any, commitment: bytes, timeout: float | None) -> Any:
    """``get_timestamp`` with a timeout when the calendar accepts one.

    An injected fake (and older releases) may not take the keyword; a network
    call with no timeout inside the anchor scheduler's daemon thread is the
    thing worth avoiding, so try with it and fall back.
    """
    if timeout is None:
        return remote.get_timestamp(commitment)
    try:
        return remote.get_timestamp(commitment, timeout=timeout)
    except TypeError:
        return remote.get_timestamp(commitment)


def upgrade_proof(
    path: str | os.PathLike[str],
    *,
    calendars: Iterable[str] | None = None,
    calendar_factory: Any = None,
    timeout: float | None = DEFAULT_TIMEOUT,
) -> OtsProof:
    """Fetch the completed proof from a calendar and rewrite the ``.ots`` file.

    A stamp is pending until the calendar commits it to a Bitcoin block; this
    is the step that turns a promise into an attestation. Returns the proof as
    it stands AFTER the attempt — still pending if no calendar had it yet,
    which is a normal outcome shortly after stamping, not an error.

    ``calendars`` defaults to ``None``, meaning **ask the URI inside each
    pending attestation**, which is what that URI is for and what the reference
    ``ots upgrade`` client does. Pass a list only to restrict which hosts may be
    contacted; it is then an ALLOWLIST, not the set of hosts to ask.

    Why this is not merely a nicety: submitting to a *pool* address
    (``https://a.pool.opentimestamps.org``, the default) yields an attestation
    naming the concrete calendar the pool forwarded to, which is a different
    host. Treating ``calendars`` as the list to contact therefore skipped every
    real attestation and returned "still pending" forever — a silent no-op
    dressed as the normal outcome, which is the failure mode SPEC B3.4 is about.
    """
    mod = _ots()
    target = Path(path)
    blob = target.read_bytes()
    detached = mod["DetachedTimestampFile"].deserialize(
        mod["BytesDeserializationContext"](blob)
    )
    timestamp = detached.timestamp

    allowlist = tuple(calendars) if calendars is not None else None
    upgraded = False
    skipped: list[str] = []
    # (msg, attestation): msg is the commitment AT THAT NODE, which is what the
    # calendar knows the stamp by — not the file digest at the root.
    for msg, attestation in list(timestamp.all_attestations()):
        if not isinstance(attestation, mod["PendingAttestation"]):
            continue
        uri = attestation.uri
        uri = uri.decode() if isinstance(uri, bytes) else str(uri)
        if allowlist is not None and not _uri_allowed(uri, allowlist):
            skipped.append(uri)
            continue
        node = _node_for(timestamp, msg)
        if node is None:  # cannot happen: msg came from this tree
            continue
        try:
            remote = (
                calendar_factory(uri) if calendar_factory is not None
                else mod["RemoteCalendar"](uri)
            )
            node.merge(_get_timestamp(remote, msg, timeout))
            upgraded = True
        except Exception:  # noqa: BLE001 - a calendar that has nothing yet is normal
            continue

    if skipped and not upgraded:
        # Never silent: an allowlist that excludes every attestation looks
        # exactly like "the calendar has nothing yet" unless it says so.
        _log.warning(
            "upgrade_proof contacted no calendar for %s: the allowlist %r excluded "
            "every pending attestation URI %r. Pass calendars=None to ask the URI "
            "inside each attestation (the default).",
            target,
            allowlist,
            skipped,
        )

    if upgraded:
        detached = mod["DetachedTimestampFile"](detached.file_hash_op, timestamp)
        ctx = mod["BytesSerializationContext"]()
        detached.serialize(ctx)
        _write_atomic(target, ctx.getbytes())
    return parse_ots_proof(target)


def load_anchor_beside(proof_path: str | os.PathLike[str]) -> ChainAnchor | None:
    """The ``ChainAnchor`` stored next to a ``.ots`` proof, if the sidecar exists."""
    sidecar = Path(proof_path).with_suffix(".json")
    if not sidecar.exists():
        return None
    return ChainAnchor.from_json(sidecar.read_text(encoding="ascii").strip())


__all__ = [
    "DEFAULT_CALENDARS",
    "PENDING",
    "COMPLETE",
    "OtsProof",
    "OtsAnchorSink",
    "ots_digest",
    "parse_ots_proof",
    "verify_ots_proof",
    "upgrade_proof",
    "load_anchor_beside",
]

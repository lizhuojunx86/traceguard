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
                f"OTS proof COMPLETE: the digest existed before Bitcoin block(s) "
                f"{list(self.bitcoin_heights)}. Block times carry minutes-to-hours of "
                "uncertainty, and confirming the block hash needs a Bitcoin node or a "
                "block explorer you choose to trust — traceguard checks neither."
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
    anchor"; whether the proof's own claim holds is what a Bitcoin node would
    answer.
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
    timeout: float = 30.0
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
                calendar_ts = remote.submit(digest)
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
        proof_path.write_bytes(ctx.getbytes())
        (directory / f"{stem}.json").write_text(anchor.to_json() + "\n", encoding="ascii")
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


def upgrade_proof(
    path: str | os.PathLike[str],
    *,
    calendars: Iterable[str] | None = None,
    calendar_factory: Any = None,
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
    for _msg, attestation in list(timestamp.all_attestations()):
        if not isinstance(attestation, mod["PendingAttestation"]):
            continue
        uri = attestation.uri
        uri = uri.decode() if isinstance(uri, bytes) else str(uri)
        if allowlist is not None and not any(
            uri == allowed or uri.startswith(allowed) for allowed in allowlist
        ):
            skipped.append(uri)
            continue
        try:
            remote = (
                calendar_factory(uri) if calendar_factory is not None
                else mod["RemoteCalendar"](uri)
            )
            timestamp.merge(remote.get_timestamp(timestamp.msg))
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
        target.write_bytes(ctx.getbytes())
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

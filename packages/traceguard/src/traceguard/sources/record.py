"""Building a :class:`SourceSnapshot` — digests and metadata, never content.

Three constructors, in ascending order of how much the host has to spell out:

- :func:`content_digest` — the primitive: bytes (or a ``str``, encoded UTF-8)
  in, ``(sha256_hex, content_encoding)`` out.
- :func:`from_http_response` — duck-typed against the usual response objects
  (``httpx.Response``, ``requests.Response``, anything with ``url`` /
  ``headers`` / ``content``). This module imports NEITHER library: reading
  three attributes is not worth a dependency, and a host using a fourth client
  should still be able to hand its response over.
- :func:`from_mcp_result` — for MCP tool results, which arrive already parsed,
  so there are no wire bytes to hash (see that function's docstring).

Validation happens in :meth:`SourceSnapshot.__post_init__`, i.e. at
CONSTRUCTION time, on the host's own stack. That placement is deliberate and
load-bearing: the row write is deferred to the tracer's flush and is fail-open
there (SPEC §4.1), so anything raised at flush time would be swallowed. A
malformed snapshot must fail where the caller can see it.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from traceguard.sdk.normalizer import normalize_input
from traceguard.sources.models import SOURCE_KINDS

#: Encoding applied when a caller passes ``str`` instead of ``bytes``. Recorded
#: on the row so a later reader can reproduce the digest exactly (D6).
STR_ENCODING = "utf-8"


def _normalizer_id() -> str:
    """Identity of the §4.4 normalizer, as ``<name>@<version>``.

    ``normalize_input`` carries no algorithm version of its own — changing it
    is a SPEC-major event (§6.1), so the package version is the finest-grained
    identifier that actually tracks it. Using the package version means the id
    changes on releases that did NOT touch the algorithm; that is the safe
    direction of error (two hashes look incomparable when they are in fact
    comparable, rather than the reverse).
    """
    from traceguard import __version__

    return f"traceguard.normalize_input@{__version__}"


def content_digest(content: bytes | str) -> tuple[str, str | None]:
    """``(sha256 hex, content_encoding)`` for the bytes as received.

    No normalization whatsoever (D6): whitespace, BOM, line endings and key
    order are all part of what was served, and collapsing them here would
    silently make two different responses look identical. ``bytes`` yields
    ``encoding=None`` (there is nothing to record — they were already bytes);
    a ``str`` is encoded UTF-8 and that fact is recorded, so the digest can be
    reproduced from the same string later.
    """
    if isinstance(content, bytes):
        return hashlib.sha256(content).hexdigest(), None
    if isinstance(content, str):
        return hashlib.sha256(content.encode(STR_ENCODING)).hexdigest(), STR_ENCODING
    raise TypeError(
        f"content must be bytes or str, got {type(content).__name__!r}; serialize "
        "structured data first (or use from_mcp_result, which does it for you)"
    )


@dataclass(frozen=True)
class SourceSnapshot:
    """One retrieval, validated at construction. See docs/sources.md.

    Only ``source_uri``, ``source_kind``, ``content_hash`` and ``retrieved_at``
    are required; everything else is what the source happened to tell us, and
    ``None`` means "it did not say", never "zero" or "no".
    """

    source_uri: str
    source_kind: str
    content_hash: str
    retrieved_at: datetime
    content_encoding: str | None = None
    normalized_hash: str | None = None
    normalizer_id: str | None = None
    published_at: datetime | None = None
    effective_at: datetime | None = None
    source_version: str | None = None
    mcp_server_id: str | None = None
    tool_name: str | None = None
    cache_status: str | None = None

    def __post_init__(self) -> None:
        if not self.source_uri:
            raise ValueError("source_uri is required and must be non-empty")
        if self.source_kind not in SOURCE_KINDS:
            raise ValueError(
                f"source_kind must be one of {sorted(SOURCE_KINDS)}, got "
                f"{self.source_kind!r}"
            )
        if not self.content_hash:
            raise ValueError("content_hash is required (see content_digest)")

        # tz-awareness, checked HERE rather than at the DB bind: UTCDateTime
        # would reject a naive value at flush, where the fail-open tracer
        # swallows it and the snapshot vanishes with only a log line.
        for name in ("retrieved_at", "published_at", "effective_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(
                    f"{name} must be timezone-aware (e.g. datetime.now(timezone.utc)); "
                    "a naive timestamp cannot be compared against feature_as_of"
                )
        if self.retrieved_at is None:
            raise ValueError("retrieved_at is required (physical time of retrieval)")

        # normalized_hash and normalizer_id travel together, in BOTH
        # directions. A normalized hash without a named, versioned normalizer
        # is not comparable to any other hash — and worse, it LOOKS comparable.
        # A normalizer_id without a hash names a transformation of nothing.
        if (self.normalized_hash is None) != (self.normalizer_id is None):
            raise ValueError(
                "normalized_hash and normalizer_id must both be set or both be None; "
                f"got normalized_hash={self.normalized_hash!r}, "
                f"normalizer_id={self.normalizer_id!r}. A normalized digest without a "
                "named, versioned normalizer cannot be compared with any other digest."
            )
        if self.normalizer_id is not None and "@" not in self.normalizer_id:
            raise ValueError(
                f"normalizer_id must be '<name>@<version>', got {self.normalizer_id!r}"
            )


def _header(headers: Any, name: str) -> str | None:
    """Case-insensitive header read that works on dicts and SDK header objects."""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        # httpx/requests header mappings are already case-insensitive; a plain
        # dict is not, so try the common spellings before giving up.
        for key in (name, name.lower(), name.title(), name.upper()):
            value = getter(key)
            if value is not None:
                return str(value)
        return None
    return None


def _parse_http_date(raw: str | None) -> datetime | None:
    """RFC 7231 / ISO 8601 date → tz-aware datetime, or ``None``.

    Returns ``None`` on anything unparseable rather than raising: a malformed
    ``Last-Modified`` is the source's problem, and it must not cost the host a
    snapshot. The resulting row simply has no ``published_at`` and validates
    as ``unverifiable`` under loose mode (D1).
    """
    if not raw:
        return None
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed is not None and parsed.tzinfo is None:
        return None  # a date with no zone cannot be compared; treat as unstated
    return parsed


def from_http_response(response: Any, *, source_kind: str = "http") -> SourceSnapshot:
    """Build a snapshot from an HTTP response object (duck-typed).

    Reads ``url``, ``headers`` (``Last-Modified`` → ``published_at``, falling
    back to ``Date``; ``ETag`` → ``source_version``; ``X-Cache`` /
    ``CF-Cache-Status`` → ``cache_status``) and the body from ``content`` (or
    ``.text`` / ``.body``). ``retrieved_at`` is stamped now, in UTC.

    What this proves: these bytes, with this digest, were handed to traceguard
    at this moment, and the source's own headers said the above. What it does
    NOT prove: that they came from ``source_uri`` (traceguard never made the
    request), or that ``Last-Modified`` is true.
    """
    from datetime import timezone

    url = getattr(response, "url", None)
    if url is None:
        raise ValueError(
            "response has no 'url' attribute; build the SourceSnapshot directly "
            "and pass source_uri yourself"
        )
    headers = getattr(response, "headers", None)
    body = getattr(response, "content", None)
    if body is None:
        body = getattr(response, "text", None)
    if body is None:
        body = getattr(response, "body", None)
    if body is None:
        raise ValueError(
            "response exposes no body (content / text / body); pass the bytes to "
            "content_digest and build the SourceSnapshot directly"
        )

    digest, encoding = content_digest(body)
    published = _parse_http_date(_header(headers, "Last-Modified")) or _parse_http_date(
        _header(headers, "Date")
    )
    return SourceSnapshot(
        source_uri=str(url),
        source_kind=source_kind,
        content_hash=digest,
        content_encoding=encoding,
        retrieved_at=datetime.now(timezone.utc),
        published_at=published,
        source_version=_header(headers, "ETag"),
        cache_status=_header(headers, "X-Cache") or _header(headers, "CF-Cache-Status"),
    )


def from_mcp_result(
    server_id: str,
    tool_name: str,
    result: Any,
    *,
    source_uri: str | None = None,
) -> SourceSnapshot:
    """Build a snapshot from an MCP tool result.

    An MCP result reaches the host already parsed, so there are no wire bytes
    left to hash. The bytes that DO exist are the ones the §4.4 canonical
    normalizer produces — so those are what ``content_hash`` covers, and
    ``normalizer_id`` says so out loud. ``normalized_hash`` carries the same
    digest for exactly that reason: every digest in this table that came out of
    a normalizer is accompanied by that normalizer's name and version, with no
    exception a reader would have to know about.

    The normalizer is ``traceguard.sdk.normalizer.normalize_input`` — the one
    §4.4 already makes authoritative. Writing a second canonicalization here
    would create two ways to hash the same structure, which is the failure mode
    ``normalizer_id`` exists to prevent.

    ``source_uri`` defaults to ``mcp://<server_id>/<tool_name>``.
    """
    from datetime import timezone

    canonical = normalize_input(result)
    digest = hashlib.sha256(canonical).hexdigest()
    normalizer = _normalizer_id()
    return SourceSnapshot(
        source_uri=source_uri or f"mcp://{server_id}/{tool_name}",
        source_kind="mcp",
        content_hash=digest,
        content_encoding=STR_ENCODING,  # normalize_input returns UTF-8 bytes
        normalized_hash=digest,
        normalizer_id=normalizer,
        retrieved_at=datetime.now(timezone.utc),
        mcp_server_id=server_id,
        tool_name=tool_name,
    )

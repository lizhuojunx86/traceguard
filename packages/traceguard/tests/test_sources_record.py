"""Building and validating a SourceSnapshot (SPEC v1.2 §6.6, revision D6/D10).

Everything here runs on the caller's own stack: construction validates, and
record_source judges. The deferred part — writing the row — is covered by
test_sources_span.py.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from traceguard.sources import (
    SOURCE_KINDS,
    STR_ENCODING,
    SourceSnapshot,
    SourceVerdict,
    content_digest,
    from_http_response,
    from_mcp_result,
    validate_source_snapshot,
)
from traceguard.validators.lookahead import InvariantViolation

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _snapshot(**kwargs) -> SourceSnapshot:
    base = dict(
        source_uri="https://vendor.example/eps/AAPL",
        source_kind="vendor_api",
        content_hash="a" * 64,
        retrieved_at=NOW,
    )
    base.update(kwargs)
    return SourceSnapshot(**base)


# ── content_digest (D6: raw bytes, no normalization) ────────────────────────

def test_content_digest_bytes_is_sha256_of_exactly_those_bytes():
    payload = b'{"epsActual": 1.52}'
    digest, encoding = content_digest(payload)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert encoding is None  # already bytes; nothing to record


def test_content_digest_str_records_the_encoding_it_used():
    digest, encoding = content_digest('{"epsActual": 1.52}')
    assert encoding == STR_ENCODING
    assert digest == hashlib.sha256('{"epsActual": 1.52}'.encode("utf-8")).hexdigest()


def test_content_digest_does_not_normalize_whitespace_or_key_order():
    """The whole point: two payloads that differ only cosmetically are DIFFERENT.

    Collapsing them here would silently hide a real rewrite by the source.
    """
    a, _ = content_digest(b'{"a":1,"b":2}')
    b, _ = content_digest(b'{"b":2,"a":1}')
    c, _ = content_digest(b'{"a":1, "b":2}')
    assert a != b and a != c and b != c


def test_content_digest_rejects_structured_input():
    with pytest.raises(TypeError, match="bytes or str"):
        content_digest({"epsActual": 1.52})  # type: ignore[arg-type]


# ── SourceSnapshot construction-time validation ─────────────────────────────

def test_naive_retrieved_at_is_rejected_at_construction():
    """Not at flush: UTCDateTime would reject it there, where the fail-open
    tracer swallows the error and the snapshot silently vanishes."""
    with pytest.raises(ValueError, match="timezone-aware"):
        _snapshot(retrieved_at=datetime(2026, 9, 10, 12, 0))


def test_naive_published_at_is_rejected_too():
    with pytest.raises(ValueError, match="timezone-aware"):
        _snapshot(published_at=datetime(2026, 9, 1))


def test_normalized_hash_without_normalizer_id_is_rejected():
    with pytest.raises(ValueError, match="both be set or both be None"):
        _snapshot(normalized_hash="b" * 64)


def test_normalizer_id_without_normalized_hash_is_rejected():
    with pytest.raises(ValueError, match="both be set or both be None"):
        _snapshot(normalizer_id="stripper@1.0")


def test_normalizer_id_must_carry_a_version():
    with pytest.raises(ValueError, match="<name>@<version>"):
        _snapshot(normalized_hash="b" * 64, normalizer_id="stripper")


def test_paired_normalized_hash_and_normalizer_id_are_accepted():
    snap = _snapshot(normalized_hash="b" * 64, normalizer_id="cdn-stripper@2.1")
    assert snap.normalizer_id == "cdn-stripper@2.1"


def test_unknown_source_kind_is_rejected():
    with pytest.raises(ValueError, match="source_kind must be one of"):
        _snapshot(source_kind="ftp")


def test_other_is_an_accepted_kind():
    """`other` exists so an unforeseen shape is recorded honestly rather than
    mislabelled as one of the five known kinds."""
    assert "other" in SOURCE_KINDS
    assert _snapshot(source_kind="other").source_kind == "other"


def test_empty_source_uri_and_content_hash_are_rejected():
    with pytest.raises(ValueError, match="source_uri"):
        _snapshot(source_uri="")
    with pytest.raises(ValueError, match="content_hash"):
        _snapshot(content_hash="")


def test_snapshot_is_immutable():
    snap = _snapshot()
    with pytest.raises(Exception):
        snap.content_hash = "c" * 64  # type: ignore[misc]


# ── from_http_response (duck-typed, no httpx/requests import) ───────────────

def _response(**kwargs):
    base = dict(
        url="https://vendor.example/eps/AAPL",
        headers={
            "Last-Modified": "Tue, 01 Sep 2026 08:00:00 GMT",
            "ETag": 'W/"abc123"',
            "X-Cache": "HIT",
        },
        content=b'{"epsActual": 1.52}',
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_from_http_response_maps_every_header_it_claims_to():
    snap = from_http_response(_response())
    assert snap.source_uri == "https://vendor.example/eps/AAPL"
    assert snap.source_kind == "http"
    assert snap.content_hash == hashlib.sha256(b'{"epsActual": 1.52}').hexdigest()
    assert snap.published_at == datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
    assert snap.source_version == 'W/"abc123"'
    assert snap.cache_status == "HIT"
    assert snap.retrieved_at.tzinfo is not None


def test_from_http_response_falls_back_to_date_header():
    resp = _response(headers={"Date": "Wed, 02 Sep 2026 09:30:00 GMT"})
    assert from_http_response(resp).published_at == datetime(
        2026, 9, 2, 9, 30, tzinfo=timezone.utc
    )


def test_from_http_response_survives_an_unparseable_last_modified():
    """A malformed header is the source's problem; it must not cost a snapshot.

    The row simply carries no published_at and validates as `unverifiable`.
    """
    snap = from_http_response(_response(headers={"Last-Modified": "not a date"}))
    assert snap.published_at is None
    assert validate_source_snapshot(snap, NOW, strict=False) is SourceVerdict.UNVERIFIABLE


def test_from_http_response_accepts_a_text_body_and_records_the_encoding():
    resp = SimpleNamespace(url="https://x.example/a", headers={}, text="hello")
    snap = from_http_response(resp)
    assert snap.content_encoding == STR_ENCODING
    assert snap.content_hash == hashlib.sha256(b"hello").hexdigest()


def test_from_http_response_without_a_url_or_body_says_what_to_do_instead():
    with pytest.raises(ValueError, match="no 'url' attribute"):
        from_http_response(SimpleNamespace(headers={}, content=b"x"))
    with pytest.raises(ValueError, match="no body"):
        from_http_response(SimpleNamespace(url="https://x.example/a", headers={}))


def test_from_http_response_source_kind_is_overridable():
    assert from_http_response(_response(), source_kind="vendor_api").source_kind == "vendor_api"


# ── from_mcp_result ─────────────────────────────────────────────────────────

def test_from_mcp_result_uses_the_spec_normalizer_and_names_it():
    """There are no wire bytes left for an already-parsed MCP result, so the
    digest covers §4.4 canonical bytes — and says so via normalizer_id."""
    snap = from_mcp_result("srv-1", "get_filing", {"b": 2, "a": 1})
    assert snap.source_kind == "mcp"
    assert snap.mcp_server_id == "srv-1"
    assert snap.tool_name == "get_filing"
    assert snap.source_uri == "mcp://srv-1/get_filing"
    assert snap.normalizer_id is not None
    assert snap.normalizer_id.startswith("traceguard.normalize_input@")
    # Every digest in this table that came out of a normalizer carries that
    # normalizer's identity — no exception a reader has to know about.
    assert snap.normalized_hash == snap.content_hash


def test_from_mcp_result_is_key_order_independent():
    """Canonical JSON, so semantically identical results hash identically —
    the opposite of the raw-bytes rule, which is why normalizer_id is set."""
    a = from_mcp_result("s", "t", {"a": 1, "b": 2})
    b = from_mcp_result("s", "t", {"b": 2, "a": 1})
    assert a.content_hash == b.content_hash


def test_from_mcp_result_honours_an_explicit_source_uri():
    snap = from_mcp_result("s", "t", {"x": 1}, source_uri="https://real.example/doc")
    assert snap.source_uri == "https://real.example/doc"


# ── validate_source_snapshot: the four verdicts (D1) ────────────────────────

def test_verified_when_published_before_feature_as_of():
    snap = _snapshot(published_at=NOW - timedelta(days=1))
    assert validate_source_snapshot(snap, NOW, strict=True) is SourceVerdict.VERIFIED
    assert validate_source_snapshot(snap, NOW, strict=False) is SourceVerdict.VERIFIED


def test_verified_at_the_exact_boundary():
    """published_at == feature_as_of passes: invariant 3 is `<=`."""
    snap = _snapshot(published_at=NOW)
    assert validate_source_snapshot(snap, NOW, strict=True) is SourceVerdict.VERIFIED


def test_anachronistic_loose_returns_the_verdict():
    snap = _snapshot(published_at=NOW + timedelta(days=1))
    assert validate_source_snapshot(snap, NOW, strict=False) is SourceVerdict.ANACHRONISTIC


def test_anachronistic_strict_raises_invariant_3():
    snap = _snapshot(published_at=NOW + timedelta(days=1))
    with pytest.raises(InvariantViolation) as excinfo:
        validate_source_snapshot(snap, NOW, strict=True)
    assert excinfo.value.invariant == 3
    assert "[invariant 3]" in str(excinfo.value)


def test_unknown_published_at_strict_refuses_with_the_mandated_wording():
    """D1: strict refuses, and says WHY — it cannot establish existence."""
    with pytest.raises(InvariantViolation) as excinfo:
        validate_source_snapshot(_snapshot(), NOW, strict=True)
    assert excinfo.value.invariant == 3
    assert "cannot establish that the source existed at feature_as_of" in str(excinfo.value)


def test_unknown_published_at_loose_is_unverifiable_not_verified():
    """D1: being unable to prove existence is NOT proof of absence, and it is
    certainly not a pass."""
    verdict = validate_source_snapshot(_snapshot(), NOW, strict=False)
    assert verdict is SourceVerdict.UNVERIFIABLE
    assert verdict is not SourceVerdict.VERIFIED


def test_unchecked_when_no_feature_as_of_even_in_strict_mode():
    """`unchecked` and `unverifiable` are different states and never collapse:
    one is "the source would not say", the other "we never asked"."""
    for strict in (True, False):
        assert (
            validate_source_snapshot(_snapshot(published_at=NOW), None, strict=strict)
            is SourceVerdict.UNCHECKED
        )


def test_strict_is_keyword_only_with_no_default():
    """D10 / SPEC §4.2 discipline: every call site must state its mode."""
    import inspect

    param = inspect.signature(validate_source_snapshot).parameters["strict"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        validate_source_snapshot(_snapshot(), NOW)  # type: ignore[call-arg]


def test_actionable_covers_the_two_failure_verdicts_only():
    assert SourceVerdict.ANACHRONISTIC.actionable
    assert SourceVerdict.UNVERIFIABLE.actionable
    # Nothing was claimed, so there is nothing to distrust — counting these
    # would teach the reader to ignore the report (SPEC B3.4).
    assert not SourceVerdict.VERIFIED.actionable
    assert not SourceVerdict.UNCHECKED.actionable


# ── the bolded claim, made mechanical ───────────────────────────────────────

CONTENT_BEARING_HINTS = ("content", "body", "text", "payload", "raw", "response", "html")
CONTENT_HASH_COLUMNS = frozenset({"content_hash", "normalized_hash", "content_encoding"})


def test_no_column_on_source_snapshots_can_hold_retrieved_content():
    """models.py, docs/sources.md and the CLI all state, in bold, that this
    extension stores digests and metadata and never the retrieved bytes.

    That claim had nothing enforcing it: a future column called `content` or
    `response_body` would contradict three documents and pass every test. This
    is the guard. If a new column legitimately needs one of these words in its
    name, add it to CONTENT_HASH_COLUMNS deliberately — the point is that it
    cannot happen by accident.
    """
    from traceguard.sources.models import SourceSnapshotRow

    suspicious = []
    for column in SourceSnapshotRow.__table__.columns:
        if column.name in CONTENT_HASH_COLUMNS:
            continue
        if any(hint in column.name.lower() for hint in CONTENT_BEARING_HINTS):
            suspicious.append(column.name)
    assert not suspicious, (
        f"column(s) {suspicious} on source_snapshots may hold retrieved content, which "
        "contradicts the 'digests and metadata only' claim in models.py, docs/sources.md "
        "and the sources CLI"
    )

    # And no NEW column is unbounded. One is, for a real reason: a URI has no
    # useful maximum. Anything else arriving as unbounded text is the shape a
    # body would take, so it has to be added here on purpose.
    from sqlalchemy import String, Text

    UNBOUNDED_BY_DESIGN = {"source_uri"}
    for column in SourceSnapshotRow.__table__.columns:
        if isinstance(column.type, Text):
            assert column.name in UNBOUNDED_BY_DESIGN, (
                f"{column.name} is unbounded TEXT; source_snapshots stores digests and "
                "metadata, and an unbounded column is the shape retrieved content takes"
            )
        if isinstance(column.type, String) and column.type.length is not None:
            assert column.type.length <= 2048, (
                f"{column.name} is String({column.type.length}) — wide enough to hold a "
                "retrieved document, which this table promises never to store"
            )


def test_a_snapshot_does_not_retain_the_content_it_digested():
    """content_digest takes bytes and returns a digest; nothing keeps them."""
    from dataclasses import fields

    from traceguard.sources.record import SourceSnapshot

    names = {f.name for f in fields(SourceSnapshot)}
    for name in names:
        if name in CONTENT_HASH_COLUMNS:
            continue
        assert not any(hint in name for hint in CONTENT_BEARING_HINTS), (
            f"SourceSnapshot.{name} may carry retrieved content"
        )

    body = b"the vendor's actual response body"
    snapshot = SourceSnapshot(
        source_uri="https://v.example/eps",
        source_kind="http",
        content_hash=content_digest(body)[0],
        retrieved_at=NOW,
    )
    blob = repr(snapshot).encode()
    assert body not in blob
    assert b"vendor's actual response" not in blob

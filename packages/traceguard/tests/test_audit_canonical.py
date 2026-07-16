"""Golden tests freezing audit canonical serialization (algo v1).

Like test_normalizer_golden.py, these pin exact bytes and hashes: any change
to the canonicalization (field handling, dumps flags, datetime formatting)
must show up here and is an audit ``algo_version`` bump, never a silent drift
— historical chains would otherwise verify as tampered.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from traceguard.audit.canonical import (
    ALGO_VERSION,
    GENESIS_PREV_HASH,
    TRACE_CONTENT_FIELDS,
    CanonicalizationError,
    canon_error_content,
    canonical_json_bytes,
    compute_row_hash,
    entry_payload,
)


def test_algo_version_is_one():
    assert ALGO_VERSION == 1


def test_genesis_is_frozen():
    assert GENESIS_PREV_HASH == hashlib.sha256(b"traceguard-audit-genesis-v1").hexdigest()
    assert GENESIS_PREV_HASH == (
        "9947ce0eea432838ffba41db9b9086cb9f74de951e0a846ad02218795a4e99ed"
    )


def test_trace_content_fields_frozen_and_exclude_cost_usd():
    # cost_usd has a legal in-place UPDATE path (reprice) and MUST stay outside
    # the hash envelope; everything else from SPEC §3.1 is covered.
    assert "cost_usd" not in TRACE_CONTENT_FIELDS
    assert TRACE_CONTENT_FIELDS == (
        "trace_id", "project", "component", "operation", "parent_trace_id",
        "correlation_id", "input_hash", "input_summary", "model_id",
        "prompt_template_id", "prompt_template_hash", "output_parsed",
        "parse_status", "latency_ms", "tokens_in", "tokens_out",
        "feature_as_of", "invoked_at", "error_class", "error_message",
    )


GOLDEN_CONTENT = {
    "trace_id": 7,
    "project": "量化",
    "component": "α-signal",
    "output_parsed": {
        "answer": 42,
        "pi": 3.14159,
        "small": 1e-07,
        "big": 12345678901234567890,
        "list": [1, 2.5, "中文", None, True],
    },
    "feature_as_of": datetime(2026, 7, 1, 8, 30, 15, 123456, tzinfo=timezone.utc),
    # +08:00 wall time — must canonicalize to the same instant in UTC
    "invoked_at": datetime(2026, 7, 1, 16, 30, 15, tzinfo=timezone(timedelta(hours=8))),
    "cost": Decimal("0.001000"),
}

GOLDEN_BYTES = (
    b'{"component":"\\u03b1-signal","cost":"0.001000",'
    b'"feature_as_of":"2026-07-01T08:30:15.123456+00:00",'
    b'"invoked_at":"2026-07-01T08:30:15+00:00",'
    b'"output_parsed":{"answer":42,"big":12345678901234567890,'
    b'"list":[1,2.5,"\\u4e2d\\u6587",null,true],"pi":3.14159,"small":1e-07},'
    b'"project":"\\u91cf\\u5316","trace_id":7}'
)


def test_golden_canonical_bytes():
    assert canonical_json_bytes(GOLDEN_CONTENT) == GOLDEN_BYTES
    assert (
        hashlib.sha256(GOLDEN_BYTES).hexdigest()
        == "6ca0685c4dcac3bb9cdd76e91b90a7b9805d4b9f68e8976a4ae0a3208cb79d31"
    )


def test_golden_row_hash():
    payload = entry_payload(
        entry_type="write",
        trace_id=7,
        event_id=None,
        cost_at_event="0.001",
        note=None,
        canon_status="ok",
        canon_error=None,
        created_at=datetime(2026, 7, 13, 0, 0, 0, tzinfo=timezone.utc),
        content=GOLDEN_CONTENT,
    )
    assert compute_row_hash(GENESIS_PREV_HASH, payload) == (
        "ef7cd7e8203039f172c7b96207a1e87029a40f47aa31641e324135d755165413"
    )


def test_output_is_pure_ascii():
    # ensure_ascii=True is load-bearing: astral chars and lone surrogates must
    # not produce raw non-ASCII bytes (surrogate-pair round-trips change byte
    # length under ensure_ascii=False; lone surrogates can't be UTF-8 encoded).
    out = canonical_json_bytes({"astral": "𝕏", "text": "中文"})
    out.decode("ascii")  # raises if any non-ASCII byte slipped through


def test_lone_surrogate_is_hashable():
    out = canonical_json_bytes({"bad": "\ud800"})
    assert b"\\ud800" in out
    hashlib.sha256(out).hexdigest()  # must not raise


def test_datetime_offsets_collapse_to_same_bytes():
    utc = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    cn = utc.astimezone(timezone(timedelta(hours=8)))
    assert canonical_json_bytes({"t": utc}) == canonical_json_bytes({"t": cn})


def test_naive_datetime_raises():
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes({"t": datetime(2026, 1, 1)})


def test_nan_and_inf_raise():
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes({"x": float("nan")})
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes({"x": float("inf")})


def test_non_str_keys_coerce_like_a_json_roundtrip():
    """Write-time hashing sees {2: .., 10: ..} (numeric sort), verify-time
    sees {"2": .., "10": ..} (lexicographic) — key coercion BEFORE sorting is
    what keeps both sides byte-identical."""
    assert canonical_json_bytes({2: "a", 10: "b"}) == canonical_json_bytes(
        {"2": "a", "10": "b"}
    )
    assert canonical_json_bytes({2: "a", 10: "b"}) == b'{"10":"b","2":"a"}'
    assert canonical_json_bytes({True: 1, None: 2}) == b'{"null":2,"true":1}'
    assert canonical_json_bytes({1.5: "x"}) == b'{"1.5":"x"}'
    # mixed str/int keys are fine now — coercion happens before sorting
    assert canonical_json_bytes({1: "a", "b": 2}) == b'{"1":"a","b":2}'


def test_colliding_keys_after_coercion_raise():
    # json.dumps would emit duplicate keys and json.loads keeps the last —
    # no stable round-trip exists, so this must go the canon-failed path.
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes({1: "a", "1": "b"})


def test_non_json_representable_keys_raise():
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes({(1, 2): "x"})


def test_unknown_object_raises():
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes({"x": object()})


def test_canon_error_content_is_deterministic():
    assert canon_error_content("TypeError: boom") == {
        "__canonicalization_error__": "TypeError: boom"
    }
    assert canon_error_content(None) == {"__canonicalization_error__": "unknown"}


def test_entry_metadata_is_inside_the_preimage():
    """Forging entry_type / re-pointing trace_id / editing the cost snapshot
    must change the hash — hashing only the content would let all three pass."""
    base = dict(
        trace_id=1,
        event_id=None,
        cost_at_event="0.5",
        note=None,
        canon_status="ok",
        canon_error=None,
        created_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        content={"k": "v"},
    )
    reference = compute_row_hash(GENESIS_PREV_HASH, entry_payload(entry_type="write", **base))
    for mutation in (
        {"entry_type": "backfill"},
        {"entry_type": "write", "trace_id": 2},
        {"entry_type": "write", "cost_at_event": "999"},
        {"entry_type": "write", "canon_status": "failed"},
    ):
        forged = entry_payload(**{**base, **mutation})
        assert compute_row_hash(GENESIS_PREV_HASH, forged) != reference


def test_prev_hash_is_inside_the_preimage():
    payload = entry_payload(
        entry_type="write",
        trace_id=1,
        event_id=None,
        cost_at_event=None,
        note=None,
        canon_status="ok",
        canon_error=None,
        created_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        content=None,
    )
    assert compute_row_hash(GENESIS_PREV_HASH, payload) != compute_row_hash("0" * 64, payload)

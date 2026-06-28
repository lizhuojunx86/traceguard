"""Frozen golden-hash vectors for normalize_input / input_hash (SPEC §4.4).

input_hash is the highest-blast-radius function in the SDK: a one-byte change
to the canonicalization invalidates every historical input_hash and every
replay-dedup key, which is precisely why SPEC §4.4/§6.1 make it a
major-bump-protected, cross-version-reproducible MUST.

This table pins the exact digests of representative payloads (floats, Decimal,
bytes, datetime, unicode, whitespace, nesting, bool/int). If a change to the
normalizer alters any digest, this test fails — forcing a deliberate decision
(and a SemVer-major bump) rather than a silent break. Per the project's carve-
out rule these are a NEW invariant guard freezing current behaviour, not an
edit of an existing snapshot.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from traceguard.sdk.normalizer import input_hash

# Frozen 2026-06-28 against the shipped normalizer. Do NOT edit a value to make
# a failing test pass — a changed digest means the canonicalization changed,
# which is a SPEC-major event.
GOLDEN = {
    "empty_dict": ("44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a", {}),
    "simple": (
        "ecf9e98ec0641e23113ff3ce8bdc78d0ddd249886517fd4a7f68cc83d4e65667",
        {"a": 1, "b": "x"},
    ),
    "nested": (
        "f0c83bfdd268898d9eb0bdd66b0200c74a17ab6682bac945900a7d12716084e4",
        {"outer": {"k": [1, 2, {"z": None}]}, "n": 3},
    ),
    "float": (
        "f06def17b72ff8adf5f0c3ecea2f2856e74237cc5ef35cf216892d8fca7e3bb8",
        {"pi": 3.14159, "neg": -0.0, "big": 1e20},
    ),
    "decimal": (
        "c2fbf022779f34280a4fd4a878bed38819e376a24bfa3899ffcf63a0029f0963",
        {"d": Decimal("1.2300")},
    ),
    "bytes": (
        "904817bc9899d0b677af6f9cf629f6c12e532ecfa44150b76dc299a08f4b5cd9",
        {"blob": b"\x00\x01hello"},
    ),
    "datetime_utc": (
        "6d0eb75bab1e8e2c0ffb1aa8c96d8639d6ad5e7ee8d4582ecde7f0dd4de349f5",
        {"t": datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)},
    ),
    "unicode": (
        "e16fdd1f1cd413c062642ad746e21fc55139ec470ab3b98af275fe84bdfecebd",
        {"s": "héllo 世界"},
    ),
    "str_whitespace": (
        "539d05783bcaa18932974451c64e9489fa08792d632859fd746379bbec1d8db7",
        {"s": "  a\r\nb  "},
    ),
    "list_vs_tuple": (
        "a615eeaee21de5179de080de8c3052c8da901138406ba71c38c032845f7d54f4",
        [1, 2, 3],
    ),
    "bool_int": (
        "3158cb745c61205314e9f8a03b7533e3cf1b02203ff2c78db951c14f35e4e49f",
        {"t": True, "f": False, "n": 0},
    ),
}


def test_golden_hashes_are_stable():
    for name, (expected, payload) in GOLDEN.items():
        assert input_hash(payload) == expected, f"normalizer digest drift for {name!r}"


def test_dict_key_order_is_irrelevant():
    assert input_hash({"a": 1, "b": 2}) == input_hash({"b": 2, "a": 1})


def test_list_and_tuple_collapse_to_same_hash():
    assert input_hash([1, 2, 3]) == input_hash((1, 2, 3))


def test_whitespace_and_line_endings_normalize():
    assert input_hash("  a\r\nb  ") == input_hash("a\nb")

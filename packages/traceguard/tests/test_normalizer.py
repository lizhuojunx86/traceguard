"""Tests for input normalization & hashing (SPEC §4.4)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from traceguard.sdk.normalizer import input_hash, normalize_input


def test_dict_key_order_is_canonical():
    a = {"x": 1, "y": 2}
    b = {"y": 2, "x": 1}
    assert input_hash(a) == input_hash(b)


def test_nested_dict_key_order_is_canonical():
    a = {"outer": {"a": 1, "b": 2}}
    b = {"outer": {"b": 2, "a": 1}}
    assert input_hash(a) == input_hash(b)


def test_string_whitespace_and_line_endings_normalized():
    assert input_hash("  hello\r\nworld  ") == input_hash("hello\nworld")
    assert input_hash("hello\r\n") == input_hash("hello")


def test_list_order_is_preserved():
    assert input_hash([1, 2, 3]) != input_hash([3, 2, 1])


def test_bytes_supported():
    h = input_hash(b"binary data")
    assert isinstance(h, str) and len(h) == 64


def test_datetime_requires_timezone():
    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    naive = datetime(2026, 1, 1)
    input_hash(aware)  # ok
    with pytest.raises(ValueError, match="naive datetime"):
        input_hash(naive)


def test_decimal_serialized_as_string():
    a = Decimal("3.14")
    b = Decimal("3.140")
    # Decimal preserves precision in str()
    assert input_hash(a) != input_hash(b)


def test_nan_and_inf_rejected():
    with pytest.raises(ValueError):
        input_hash(float("nan"))
    with pytest.raises(ValueError):
        input_hash(float("inf"))


def test_pydantic_model_normalized():
    from pydantic import BaseModel

    class M(BaseModel):
        x: int
        y: str

    a = M(x=1, y="hello")
    h_model = input_hash(a)
    h_dict = input_hash({"x": 1, "y": "hello"})
    assert h_model == h_dict


def test_hash_is_64_hex_chars():
    h = input_hash({"any": "thing"})
    assert len(h) == 64
    int(h, 16)  # parses as hex


def test_normalize_input_returns_bytes():
    out = normalize_input({"a": 1})
    assert isinstance(out, bytes)
    assert out == b'{"a":1}'

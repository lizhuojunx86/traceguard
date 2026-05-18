"""Canonical input normalization for SHA-256 hashing (SPEC §4.4).

The normalization function is the single authoritative source for input_hash.
Business code MUST NOT compute hashes by other means; doing so breaks dedup
and replay consistency.

Rules (SPEC §4.4):
- dict: keys sorted, JSON serialized with separators=(",", ":"),
  ensure_ascii=False
- str: leading/trailing whitespace stripped, all line endings → "\n"
- float: serialized via Python's json (shortest repr, stable across versions);
  NaN / Inf raise (allow_nan=False) — business code must convert first
- bytes: base64-encoded into a marker dict {"__bytes_b64__": "..."}
- datetime: ISO 8601 with timezone (naive datetime → raises)
- Decimal: serialized via str() to preserve precision
- pydantic BaseModel: dumped via model_dump() then normalized
- list / tuple: order preserved, elements normalized recursively
- unknown types: str() fallback with a UserWarning

Changing this algorithm is a SPEC-major bump (SPEC §6.1) because it
invalidates historical input_hash comparisons.
"""
from __future__ import annotations

import base64
import hashlib
import json
import warnings
from datetime import datetime
from decimal import Decimal
from typing import Any

try:
    from pydantic import BaseModel as _PydanticBaseModel
except ImportError:  # pragma: no cover - pydantic is a required dep, but keep guard
    _PydanticBaseModel = None  # type: ignore[assignment, misc]


_BYTES_MARKER = "__bytes_b64__"


def _normalize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # json.dumps with allow_nan=False will raise on NaN/Inf below.
        return value
    if isinstance(value, str):
        # Unify line endings then strip outer whitespace.
        cleaned = value.replace("\r\n", "\n").replace("\r", "\n")
        return cleaned.strip()
    if isinstance(value, bytes):
        return {_BYTES_MARKER: base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(
                "normalize_input: naive datetime not supported "
                "(attach a timezone, e.g. datetime.now(timezone.utc))"
            )
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if _PydanticBaseModel is not None and isinstance(value, _PydanticBaseModel):
        return _normalize(value.model_dump())
    warnings.warn(
        f"normalize_input: unsupported type {type(value).__name__!r}, "
        "falling back to str(); hash stability is not guaranteed across versions",
        UserWarning,
        stacklevel=3,
    )
    return str(value)


def normalize_input(data: Any) -> bytes:
    """Return canonical UTF-8 bytes for ``data``.

    Identical inputs always yield identical bytes; semantically equivalent
    inputs (e.g. dicts with different key orders) also collapse to the same
    bytes.
    """
    normalized = _normalize(data)
    return json.dumps(
        normalized,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def input_hash(data: Any) -> str:
    """Return SHA-256 hex digest of ``normalize_input(data)``."""
    return hashlib.sha256(normalize_input(data)).hexdigest()

"""Canonical serialisation and hashing.

Signatures and hash chains only mean something if the same data always
produces the same bytes. Everything that gets signed or hashed goes through
``canonical_json`` first: keys sorted, no insignificant whitespace, UTF-8.

Money is always an integer number of minor units (cents), never a float,
so there is no rounding ambiguity in what was signed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> bytes:
    """Serialise ``obj`` to deterministic UTF-8 JSON bytes."""
    _reject_floats(obj)
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(obj: Any) -> str:
    """SHA-256 of the canonical form of ``obj``, as lowercase hex."""
    return sha256_hex(canonical_json(obj))


def _reject_floats(obj: Any) -> None:
    """Floats serialise differently across platforms; refuse them outright."""
    if isinstance(obj, float):
        raise TypeError("floats are not allowed in signed or hashed data; use integers")
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError("only string keys are allowed in signed or hashed data")
            _reject_floats(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _reject_floats(value)

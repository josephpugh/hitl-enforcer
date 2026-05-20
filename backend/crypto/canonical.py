"""RFC 8785 JSON Canonicalization Scheme wrapper."""
from __future__ import annotations

import hashlib
from typing import Any

import rfc8785


def canonicalize(payload: Any) -> bytes:
    """Return the JCS (RFC 8785) canonical byte serialization of `payload`."""
    return rfc8785.dumps(payload)


def digest_hex(payload: Any) -> str:
    """sha256 hex of JCS(payload). Used in `*_digest` fields."""
    return hashlib.sha256(canonicalize(payload)).hexdigest()


def sha256_prefixed(payload: Any) -> str:
    """Same as digest_hex but prefixed with `sha256:`."""
    return f"sha256:{digest_hex(payload)}"

"""Sign / verify envelope over JCS-canonicalized payloads."""
from __future__ import annotations

import base64
from typing import Any

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from .canonical import canonicalize
from .keys import KeyPair, load_trust_bundle


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def sign_envelope(payload: dict[str, Any], keypair: KeyPair) -> dict[str, Any]:
    """Sign `payload` and return `{"payload": ..., "signature": {...}}`."""
    canonical = canonicalize(payload)
    sig = keypair.signing_key.sign(canonical).signature
    return {
        "payload": payload,
        "signature": {
            "alg": "Ed25519",
            "kid": keypair.kid,
            "sig": _b64u(sig),
        },
    }


def verify_envelope(envelope: dict[str, Any], trust_bundle: dict[str, VerifyKey] | None = None) -> bool:
    """Re-canonicalize envelope.payload and verify the signature against the trust bundle.

    Returns True iff the signature is valid AND the kid is present in the trust bundle.
    """
    try:
        signature = envelope["signature"]
        payload = envelope["payload"]
        kid = signature["kid"]
        sig_bytes = _b64u_decode(signature["sig"])
    except (KeyError, TypeError):
        return False

    bundle = trust_bundle if trust_bundle is not None else load_trust_bundle()
    verify_key = bundle.get(kid)
    if verify_key is None:
        return False

    canonical = canonicalize(payload)
    try:
        verify_key.verify(canonical, sig_bytes)
        return True
    except BadSignatureError:
        return False

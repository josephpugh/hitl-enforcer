"""Ed25519 key generation, loading, and trust bundle management.

Two distinct keypairs:
- server: signs intents and execution records (MCP server)
- confirmer: signs approval receipts (confirmation surface)

Keys are generated on first boot to `keys/{name}_ed25519.{priv,pub}` and
indexed in `keys/trust_bundle.json` for the verifier CLI.
"""
from __future__ import annotations

import base64
import datetime
import json
from dataclasses import dataclass
from pathlib import Path

from nacl.signing import SigningKey, VerifyKey

from ..config import CONFIG


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


@dataclass(frozen=True)
class KeyPair:
    """An Ed25519 signing keypair with a stable `kid`."""

    name: str
    kid: str
    signing_key: SigningKey
    verify_key: VerifyKey

    @property
    def verify_key_b64u(self) -> str:
        return _b64u(bytes(self.verify_key))


def _kid(name: str) -> str:
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
    return f"{name}-{today}"


def _ensure_keypair(name: str, keys_dir: Path) -> KeyPair:
    keys_dir.mkdir(parents=True, exist_ok=True)
    priv_path = keys_dir / f"{name}_ed25519.priv"
    pub_path = keys_dir / f"{name}_ed25519.pub"
    kid_path = keys_dir / f"{name}_ed25519.kid"

    if priv_path.exists() and pub_path.exists() and kid_path.exists():
        signing_key = SigningKey(priv_path.read_bytes())
        verify_key = VerifyKey(pub_path.read_bytes())
        kid = kid_path.read_text().strip()
        return KeyPair(name=name, kid=kid, signing_key=signing_key, verify_key=verify_key)

    signing_key = SigningKey.generate()
    verify_key = signing_key.verify_key
    priv_path.write_bytes(bytes(signing_key))
    priv_path.chmod(0o600)
    pub_path.write_bytes(bytes(verify_key))
    kid = _kid(name)
    kid_path.write_text(kid)
    return KeyPair(name=name, kid=kid, signing_key=signing_key, verify_key=verify_key)


def _write_trust_bundle(keys_dir: Path, keypairs: list[KeyPair]) -> None:
    bundle = {
        "version": "1.0.0",
        "keys": {
            kp.kid: {
                "name": kp.name,
                "alg": "Ed25519",
                "public_key_b64u": kp.verify_key_b64u,
            }
            for kp in keypairs
        },
    }
    bundle_path = keys_dir / "trust_bundle.json"
    bundle_path.write_text(json.dumps(bundle, indent=2, sort_keys=True))


_KEYS_CACHE: dict[str, KeyPair] = {}


def get_server_key() -> KeyPair:
    return _get_key("server")


def get_confirmer_key() -> KeyPair:
    return _get_key("confirmer")


def _get_key(name: str) -> KeyPair:
    if name in _KEYS_CACHE:
        return _KEYS_CACHE[name]
    kp = _ensure_keypair(name, CONFIG.keys_dir)
    _KEYS_CACHE[name] = kp
    # Re-write the trust bundle whenever we touch either key, so it always reflects current state.
    _write_trust_bundle(
        CONFIG.keys_dir,
        [v for v in _KEYS_CACHE.values()],
    )
    return kp


def ensure_all_keys() -> None:
    """Touch both keypairs on startup so they get generated and trust_bundle.json is complete."""
    get_server_key()
    get_confirmer_key()


def load_trust_bundle(keys_dir: Path | None = None) -> dict[str, VerifyKey]:
    """Load the trust bundle as `kid -> VerifyKey`."""
    keys_dir = keys_dir or CONFIG.keys_dir
    bundle_path = keys_dir / "trust_bundle.json"
    if not bundle_path.exists():
        return {}
    data = json.loads(bundle_path.read_text())
    return {
        kid: VerifyKey(_b64u_decode(meta["public_key_b64u"]))
        for kid, meta in data.get("keys", {}).items()
    }

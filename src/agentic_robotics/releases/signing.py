"""Signing keys for release metadata.

Two key types:

- ``hmac-sha256`` (stdlib): a shared-secret key for tests and closed single-owner setups.
  It is **not** a TUF key type and gives no non-repudiation: anyone who can verify can
  also sign. The key's public value is only a fingerprint of the secret; verifying needs
  the secret, supplied through :class:`KeyStore`.
- ``ed25519`` (TUF keytype and scheme ``ed25519``, public key as 64 hex characters):
  behind the optional ``[signing]`` extra, importing ``cryptography`` lazily.

No home-made cryptography: HMAC is ``hmac``/``hashlib`` from the standard library and
Ed25519 is ``cryptography``'s implementation.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..core.errors import FrameworkError

__requirements__ = ("REQ10", "G15", "D15")

HMAC = "hmac-sha256"
ED25519 = "ed25519"


class SigningError(FrameworkError):
    code = "signing_error"


def canonical_bytes(obj: Any) -> bytes:
    """Canonical JSON for signing: sorted keys, no whitespace, UTF-8.

    Like OLPC canonical JSON (which TUF references) it is deterministic; floats are
    refused because their text form is not canonical."""
    _no_floats(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _no_floats(obj: Any) -> None:
    if isinstance(obj, float):
        raise SigningError("floats are not allowed in signed metadata")
    if isinstance(obj, dict):
        for v in obj.values():
            _no_floats(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _no_floats(v)


def key_id(key: dict[str, Any]) -> str:
    """TUF keyid: hex SHA-256 of the canonical form of the key object."""
    return hashlib.sha256(canonical_bytes(key)).hexdigest()


class Signer(Protocol):
    @property
    def key(self) -> dict[str, Any]: ...
    @property
    def keyid(self) -> str: ...
    def sign(self, data: bytes) -> str: ...


@dataclass(frozen=True)
class HmacSigner:
    secret: bytes

    @property
    def key(self) -> dict[str, Any]:
        return {"keytype": HMAC, "scheme": HMAC,
                "keyval": {"public": hashlib.sha256(b"air-hmac-key:" + self.secret)
                           .hexdigest()}}

    @property
    def keyid(self) -> str:
        return key_id(self.key)

    def sign(self, data: bytes) -> str:
        return hmac.new(self.secret, data, hashlib.sha256).hexdigest()


class Ed25519Signer:
    """Ed25519 signer (requires the ``[signing]`` extra)."""

    def __init__(self, private_key: Any) -> None:
        self._sk = private_key

    @classmethod
    def generate(cls) -> Ed25519Signer:
        ed = _ed25519()
        return cls(ed.Ed25519PrivateKey.generate())

    @classmethod
    def from_seed(cls, seed: bytes) -> Ed25519Signer:
        ed = _ed25519()
        return cls(ed.Ed25519PrivateKey.from_private_bytes(seed))

    @property
    def key(self) -> dict[str, Any]:
        serialization = _crypto("cryptography.hazmat.primitives.serialization")
        raw = self._sk.public_key().public_bytes(serialization.Encoding.Raw,
                                                 serialization.PublicFormat.Raw)
        return {"keytype": ED25519, "scheme": ED25519, "keyval": {"public": raw.hex()}}

    @property
    def keyid(self) -> str:
        return key_id(self.key)

    def sign(self, data: bytes) -> str:
        return str(self._sk.sign(data).hex())


def _crypto(module: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SigningError("Ed25519 needs the optional extra: pip install "
                           "'agentic-robotics[signing]'") from exc


def _ed25519() -> Any:
    return _crypto("cryptography.hazmat.primitives.asymmetric.ed25519")


def ed25519_available() -> bool:
    try:
        _ed25519()
    except SigningError:
        return False
    return True


@dataclass
class KeyStore:
    """Verification material: HMAC secrets by keyid (Ed25519 needs none)."""

    hmac_secrets: dict[str, bytes] = field(default_factory=dict)

    def add_hmac(self, signer: HmacSigner) -> None:
        self.hmac_secrets[signer.keyid] = signer.secret

    def verify(self, key: dict[str, Any], sig: str, data: bytes) -> bool:
        keytype = key.get("keytype")
        if keytype == HMAC:
            secret = self.hmac_secrets.get(key_id(key))
            if secret is None:
                return False
            expected = hmac.new(secret, data, hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, sig)
        if keytype == ED25519 and key.get("scheme") == ED25519:
            ed = _ed25519()
            invalid = _crypto("cryptography.exceptions").InvalidSignature
            try:
                pk = ed.Ed25519PublicKey.from_public_bytes(
                    bytes.fromhex(key["keyval"]["public"]))
                pk.verify(bytes.fromhex(sig), data)
            except (invalid, ValueError, KeyError):
                return False
            return True
        return False

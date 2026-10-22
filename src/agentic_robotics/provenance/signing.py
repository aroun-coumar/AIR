"""Signers and verifiers for checkpoints (C2SP signed notes) and DSSE envelopes.

Three kinds, all behind one small interface:

* **unsigned**: no signer. A checkpoint is still useful when it is committed to git
  (git history is then the anchor), but nothing proves who wrote it. Tools say so.
* **HMAC-SHA256** (stdlib): a shared-secret MAC. Anyone holding the key can verify and
  also forge, so it proves "someone with the project key", not a particular signer. As
  a C2SP note signature it uses the private-use type byte ``0xff`` followed by
  ``air-hmac-sha256``; other C2SP verifiers will (correctly) ignore it as an unknown key.
* **Ed25519** (C2SP signature type ``0x01``, RFC 8032): signing needs the optional
  ``cryptography`` package (``pip install agentic-robotics[signing]``). *Verifying*
  works without it: a small RFC 8032 verifier is included, since verification touches no
  secret and so needs no constant-time arithmetic.

Keys are never written into the project. Keep them outside the repository.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..core.errors import FrameworkError, SchemaError

__requirements__ = ("REQ10",)

ED25519_TYPE = 0x01
PRIVATE_TYPE = 0xFF
HMAC_ALG_ID = b"air-hmac-sha256"


class SigningUnavailable(FrameworkError):
    code = "signing_unavailable"


class Signer(Protocol):
    """Signs bytes. ``name`` is the C2SP key name, ``key_id`` the 4-byte key hash."""

    name: str
    algorithm: str

    @property
    def key_id(self) -> bytes: ...

    def sign(self, message: bytes) -> bytes: ...


class Verifier(Protocol):
    name: str
    algorithm: str

    @property
    def key_id(self) -> bytes: ...

    def verify(self, message: bytes, signature: bytes) -> bool: ...


def _check_name(name: str) -> str:
    if not name or any(c.isspace() or c == "+" for c in name):
        raise SchemaError("key names must be non-empty, without spaces or '+'", name=name)
    return name


def c2sp_key_id(name: str, sig_type: bytes, public: bytes) -> bytes:
    """``SHA-256(key name || 0x0A || signature type || public key)[:4]`` (signed-note)."""
    return hashlib.sha256(name.encode() + b"\n" + sig_type + public).digest()[:4]


# -- HMAC (stdlib) ------------------------------------------------------------------------
@dataclass(frozen=True)
class HmacKey:
    """Shared-secret signer and verifier (both, by nature of a MAC)."""

    name: str
    secret: bytes
    algorithm: str = "hmac-sha256"

    def __post_init__(self) -> None:
        _check_name(self.name)
        if len(self.secret) < 16:
            raise SchemaError("HMAC keys must be at least 16 bytes")

    @property
    def key_id(self) -> bytes:
        return c2sp_key_id(self.name, bytes([PRIVATE_TYPE]) + HMAC_ALG_ID,
                           hashlib.sha256(self.secret).digest())

    def sign(self, message: bytes) -> bytes:
        return hmac.new(self.secret, message, hashlib.sha256).digest()

    def verify(self, message: bytes, signature: bytes) -> bool:
        return hmac.compare_digest(self.sign(message), signature)

    @classmethod
    def from_file(cls, name: str, path: str | Path) -> HmacKey:
        raw = Path(path).read_bytes().strip()
        try:
            secret = bytes.fromhex(raw.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            secret = raw
        return cls(name, secret)


# -- Ed25519 ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Ed25519PublicKey:
    name: str
    public: bytes
    algorithm: str = "ed25519"

    def __post_init__(self) -> None:
        _check_name(self.name)
        if len(self.public) != 32:
            raise SchemaError("an Ed25519 public key is 32 bytes")

    @property
    def key_id(self) -> bytes:
        return c2sp_key_id(self.name, bytes([ED25519_TYPE]), self.public)

    def vkey(self) -> str:
        """C2SP verifier key: ``name+hex(key id)+base64(0x01 || public key)``."""
        return (f"{self.name}+{self.key_id.hex()}+"
                f"{base64.b64encode(bytes([ED25519_TYPE]) + self.public).decode()}")

    @classmethod
    def from_vkey(cls, vkey: str) -> Ed25519PublicKey:
        parts = vkey.strip().split("+", 2)  # base64 may contain "+"
        if len(parts) != 3:
            raise SchemaError("malformed verifier key", vkey=vkey)
        name, hex_id, b64 = parts
        material = b64decode_canonical(b64)
        if not material or material[0] != ED25519_TYPE:
            raise SchemaError("verifier key is not an Ed25519 key", vkey=vkey)
        key = cls(name, material[1:])
        if key.key_id.hex() != hex_id.lower():
            raise SchemaError("verifier key id does not match its key", vkey=vkey)
        return key

    def verify(self, message: bytes, signature: bytes) -> bool:
        try:
            from cryptography.exceptions import InvalidSignature  # type: ignore[import-not-found, unused-ignore]
            from cryptography.hazmat.primitives.asymmetric import ed25519  # type: ignore[import-not-found, unused-ignore]
        except ImportError:
            return ed25519_verify(self.public, message, signature)
        try:
            ed25519.Ed25519PublicKey.from_public_bytes(self.public).verify(signature, message)
        except InvalidSignature:
            return False
        return True


class Ed25519Signer:
    """Ed25519 signing through the optional ``cryptography`` package."""

    algorithm = "ed25519"

    def __init__(self, name: str, seed: bytes) -> None:
        try:
            from cryptography.hazmat.primitives import serialization  # type: ignore[import-not-found, unused-ignore]
            from cryptography.hazmat.primitives.asymmetric import ed25519  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:
            raise SigningUnavailable(
                "Ed25519 signing needs the optional 'cryptography' package: "
                "pip install agentic-robotics[signing] (or sign with an HMAC key, or "
                "leave the checkpoint unsigned and rely on git as the anchor)") from exc
        self.name = _check_name(name)
        self._key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        self.public = Ed25519PublicKey(name, self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    @property
    def key_id(self) -> bytes:
        return self.public.key_id

    def sign(self, message: bytes) -> bytes:
        return bytes(self._key.sign(message))

    @classmethod
    def generate_seed(cls) -> bytes:
        import secrets
        return secrets.token_bytes(32)

    @classmethod
    def from_file(cls, name: str, path: str | Path) -> Ed25519Signer:
        """A file holding the 32-byte seed as 64 hex digits."""
        try:
            seed = bytes.fromhex(Path(path).read_text("ascii").strip())
        except ValueError as exc:
            raise SchemaError("Ed25519 key file must hold a 32-byte seed in hex",
                              path=str(path)) from exc
        if len(seed) != 32:
            raise SchemaError("Ed25519 seed must be 32 bytes", path=str(path))
        return cls(name, seed)


def b64decode_canonical(text: str) -> bytes:
    """Standard base64 with padding; reject non-canonical encodings (C2SP rule)."""
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SchemaError("invalid base64", value=text[:40]) from exc
    if base64.b64encode(raw).decode() != text:
        raise SchemaError("non-canonical base64", value=text[:40])
    return raw


# -- RFC 8032 Ed25519 verification (no secrets involved) ----------------------------------
_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)
_Point = tuple[int, int, int, int]


def _add(a: _Point, b: _Point) -> _Point:
    p = _P
    aa = (a[1] - a[0]) * (b[1] - b[0]) % p
    bb = (a[1] + a[0]) * (b[1] + b[0]) % p
    cc = 2 * a[3] * b[3] * _D % p
    dd = 2 * a[2] * b[2] % p
    e, f, g, h = bb - aa, dd - cc, dd + cc, bb + aa
    return (e * f % p, g * h % p, f * g % p, e * h % p)


def _mul(s: int, pt: _Point) -> _Point:
    acc: _Point = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            acc = _add(acc, pt)
        pt = _add(pt, pt)
        s >>= 1
    return acc


def _equal(a: _Point, b: _Point) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _decompress(s: bytes) -> _Point | None:
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
assert _GX is not None
_G: _Point = (_GX, _GY, 1, _GX * _GY % _P)


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 section 5.1.7 verification (cofactorless), pure Python."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a = _decompress(public)
    r = _decompress(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _Q:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(),
                       "little") % _Q
    return _equal(_mul(s, _G), _add(r, _mul(h, a)))

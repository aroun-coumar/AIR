"""RFC 8032 Ed25519 *signing* for tests only (not constant time; never use for real keys).

Lets the signing paths be tested where the optional ``cryptography`` package is absent.
"""

from __future__ import annotations

import hashlib

from agentic_robotics.provenance.signing import _G, _P, _Q, Ed25519PublicKey, _mul


def _compress(pt: tuple[int, int, int, int]) -> bytes:
    zinv = pow(pt[2], _P - 2, _P)
    x, y = pt[0] * zinv % _P, pt[1] * zinv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _secret(seed: bytes) -> tuple[int, bytes]:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    return _compress(_mul(_secret(seed)[0], _G))


def sign(seed: bytes, message: bytes) -> bytes:
    a, prefix = _secret(seed)
    pub = _compress(_mul(a, _G))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _Q
    big_r = _compress(_mul(r, _G))
    h = int.from_bytes(hashlib.sha512(big_r + pub + message).digest(), "little") % _Q
    return big_r + ((r + h * a) % _Q).to_bytes(32, "little")


class RefSigner:
    algorithm = "ed25519"

    def __init__(self, name: str, seed: bytes) -> None:
        self.name = name
        self.seed = seed
        self.public = Ed25519PublicKey(name, public_key(seed))

    @property
    def key_id(self) -> bytes:
        return self.public.key_id

    def sign(self, message: bytes) -> bytes:
        return sign(self.seed, message)

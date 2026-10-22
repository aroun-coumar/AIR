"""A small standard-library MessagePack codec with openpi's ndarray convention.

openpi (``packages/openpi-client/src/openpi_client/msgpack_numpy.py``, Apache-2.0; read,
not copied) packs a NumPy array as a map ``{b"__ndarray__": True, b"data": <bytes>,
b"dtype": <dtype.str>, b"shape": <tuple>}`` and a scalar as
``{b"__npgeneric__": True, b"data": <item>, b"dtype": <dtype.str>}``. With msgpack's
default ``use_bin_type=True`` the ``bytes`` keys travel as *bin*. Object, void and
complex dtypes are refused there, and never decoded here (no pickle, ever).

Supported: nil, bool, int (to 64 bit), float32/64, str, bin, array, map. No ext types.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from ...core.errors import FrameworkError

__requirements__ = ("REQ01",)

_DTYPES = {"<f4": "f", "<f8": "d", "<i1": "b", "<i2": "h", "<i4": "i", "<i8": "q",
           "|u1": "B", "<u2": "H", "<u4": "I", "<u8": "Q", "|b1": "?", "|i1": "b"}


class CodecError(FrameworkError):
    code = "msgpack_codec"


@dataclass(frozen=True)
class NdArray:
    """An array on the wire, without NumPy. ``data`` is C-order bytes."""

    dtype: str
    shape: tuple[int, ...]
    data: bytes

    def __post_init__(self) -> None:
        if self.dtype not in _DTYPES:
            raise CodecError("unsupported dtype", dtype=self.dtype)
        n = 1
        for d in self.shape:
            n *= d
        if n * struct.calcsize(_DTYPES[self.dtype]) != len(self.data):
            raise CodecError("array data does not match dtype and shape", dtype=self.dtype,
                             shape=self.shape)

    @classmethod
    def from_list(cls, values: Any, dtype: str = "<f4") -> NdArray:
        shape: list[int] = []
        probe = values
        while isinstance(probe, list | tuple):
            shape.append(len(probe))
            probe = probe[0] if probe else 0
        flat: list[Any] = []

        def walk(v: Any) -> None:
            if isinstance(v, list | tuple):
                for x in v:
                    walk(x)
            else:
                flat.append(v)

        walk(values)
        return cls(dtype, tuple(shape), struct.pack("<" + _DTYPES[dtype] * len(flat), *flat))

    def tolist(self) -> Any:
        flat = list(struct.unpack("<" + _DTYPES[self.dtype] * (len(self.data) // struct.calcsize(
            _DTYPES[self.dtype])), self.data))
        if not self.shape:
            return flat[0]
        out: Any = flat
        for dim in reversed(self.shape[1:]):
            out = [out[i:i + dim] for i in range(0, len(out), dim)]
        return out


def packb(obj: Any) -> bytes:
    out = bytearray()
    _pack(obj, out)
    return bytes(out)


def _pack(o: Any, out: bytearray) -> None:
    if isinstance(o, NdArray):
        _pack({b"__ndarray__": True, b"data": o.data, b"dtype": o.dtype,
               b"shape": list(o.shape)}, out)
    elif o is None:
        out.append(0xC0)
    elif o is True:
        out.append(0xC3)
    elif o is False:
        out.append(0xC2)
    elif isinstance(o, int):
        _pack_int(o, out)
    elif isinstance(o, float):
        out.append(0xCB)
        out += struct.pack(">d", o)
    elif isinstance(o, str):
        b = o.encode("utf-8")
        _len(len(b), out, 0xA0, 31, 0xD9, 0xDA, 0xDB)
        out += b
    elif isinstance(o, bytes | bytearray):
        _len(len(o), out, None, 0, 0xC4, 0xC5, 0xC6)
        out += o
    elif isinstance(o, list | tuple):
        _len(len(o), out, 0x90, 15, None, 0xDC, 0xDD)
        for x in o:
            _pack(x, out)
    elif isinstance(o, dict):
        _len(len(o), out, 0x80, 15, None, 0xDE, 0xDF)
        for k, v in o.items():
            _pack(k, out)
            _pack(v, out)
    else:
        raise CodecError("cannot pack type", type=type(o).__name__)


def _pack_int(o: int, out: bytearray) -> None:
    if 0 <= o < 128:
        out.append(o)
    elif -32 <= o < 0:
        out.append(o & 0xFF)
    elif 0 <= o < 2**64:
        for code, fmt, lim in ((0xCC, ">B", 2**8), (0xCD, ">H", 2**16), (0xCE, ">I", 2**32),
                               (0xCF, ">Q", 2**64)):
            if o < lim:
                out.append(code)
                out += struct.pack(fmt, o)
                return
    elif -(2**63) <= o < 0:
        for code, fmt, lim in ((0xD0, ">b", 2**7), (0xD1, ">h", 2**15), (0xD2, ">i", 2**31),
                               (0xD3, ">q", 2**63)):
            if o >= -lim:
                out.append(code)
                out += struct.pack(fmt, o)
                return
    else:
        raise CodecError("integer out of range")


def _len(n: int, out: bytearray, fix: int | None, fixmax: int, c8: int | None, c16: int,
         c32: int) -> None:
    if fix is not None and n <= fixmax:
        out.append(fix | n)
    elif c8 is not None and n < 2**8:
        out += bytes([c8, n])
    elif n < 2**16:
        out.append(c16)
        out += struct.pack(">H", n)
    else:
        out.append(c32)
        out += struct.pack(">I", n)


def unpackb(data: bytes) -> Any:
    obj, end = _unpack(memoryview(data), 0, 0)
    if end != len(data):
        raise CodecError("trailing bytes after msgpack object")
    return obj


_MAX_DEPTH = 64


def _unpack(b: memoryview, i: int, depth: int) -> tuple[Any, int]:
    if depth > _MAX_DEPTH:
        raise CodecError("msgpack nesting too deep")
    try:
        t = b[i]
    except IndexError as exc:
        raise CodecError("truncated msgpack data") from exc
    i += 1
    if t <= 0x7F:
        return t, i
    if t >= 0xE0:
        return t - 0x100, i
    if 0xA0 <= t <= 0xBF:
        return _str(b, i, t & 0x1F)
    if 0x90 <= t <= 0x9F:
        return _array(b, i, t & 0x0F, depth)
    if 0x80 <= t <= 0x8F:
        return _map(b, i, t & 0x0F, depth)
    simple = {0xC0: None, 0xC2: False, 0xC3: True}
    if t in simple:
        return simple[t], i
    fixed = {0xCA: ">f", 0xCB: ">d", 0xCC: ">B", 0xCD: ">H", 0xCE: ">I", 0xCF: ">Q",
             0xD0: ">b", 0xD1: ">h", 0xD2: ">i", 0xD3: ">q"}
    if t in fixed:
        size = struct.calcsize(fixed[t])
        _need(b, i, size)
        return struct.unpack_from(fixed[t], b, i)[0], i + size
    lens = {0xD9: (">B", "str"), 0xDA: (">H", "str"), 0xDB: (">I", "str"),
            0xC4: (">B", "bin"), 0xC5: (">H", "bin"), 0xC6: (">I", "bin"),
            0xDC: (">H", "arr"), 0xDD: (">I", "arr"), 0xDE: (">H", "map"), 0xDF: (">I", "map")}
    if t in lens:
        fmt, kind = lens[t]
        size = struct.calcsize(fmt)
        _need(b, i, size)
        n = struct.unpack_from(fmt, b, i)[0]
        i += size
        if kind == "str":
            return _str(b, i, n)
        if kind == "bin":
            _need(b, i, n)
            return bytes(b[i:i + n]), i + n
        if kind == "arr":
            return _array(b, i, n, depth)
        return _map(b, i, n, depth)
    raise CodecError("unsupported msgpack type byte", byte=hex(t))


def _need(b: memoryview, i: int, n: int) -> None:
    if i + n > len(b):
        raise CodecError("truncated msgpack data")


def _str(b: memoryview, i: int, n: int) -> tuple[str, int]:
    _need(b, i, n)
    return bytes(b[i:i + n]).decode("utf-8"), i + n


def _array(b: memoryview, i: int, n: int, depth: int) -> tuple[list[Any], int]:
    out = []
    for _ in range(n):
        v, i = _unpack(b, i, depth + 1)
        out.append(v)
    return out, i


def _map(b: memoryview, i: int, n: int, depth: int) -> tuple[Any, int]:
    out: dict[Any, Any] = {}
    for _ in range(n):
        k, i = _unpack(b, i, depth + 1)
        v, i = _unpack(b, i, depth + 1)
        if isinstance(k, list | dict):
            raise CodecError("unhashable map key")
        out[k] = v
    if out.get(b"__ndarray__") is True:
        try:
            dtype = out[b"dtype"]
            return NdArray(dtype.decode() if isinstance(dtype, bytes) else str(dtype),
                           tuple(int(x) for x in out[b"shape"]), bytes(out[b"data"])), i
        except (KeyError, TypeError, ValueError) as exc:
            raise CodecError("malformed ndarray") from exc
    if out.get(b"__npgeneric__") is True:
        return out.get(b"data"), i
    return out, i


__all__ = ["CodecError", "NdArray", "packb", "unpackb"]

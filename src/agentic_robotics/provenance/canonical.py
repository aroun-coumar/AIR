"""Canonical JSON (RFC 8785, JSON Canonicalization Scheme).

Hashes and signatures over evidence must be reproducible by any implementation, in any
language. This serializer follows JCS: object members sorted by UTF-16 code units, no
insignificant whitespace, minimal string escaping, and numbers in the ECMAScript
``Number.prototype.toString`` shortest round-trip form. Only plain JSON values are
accepted; :func:`to_json_value` normalizes richer Python values first.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Mapping
from enum import Enum
from typing import Any

from ..core.errors import SchemaError

__requirements__ = ("REQ10",)


def to_json_value(value: Any) -> Any:
    """Normalize to plain JSON types: dataclasses/Mappings -> dict, sequences -> list,
    Enums -> value, other objects -> ``str``. Applied *before* storing and hashing, so
    the stored bytes and the hashed bytes always agree."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return value
    if isinstance(value, Enum):
        return to_json_value(value.value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_json_value(getattr(value, f.name))
                for f in dataclasses.fields(value) if getattr(value, f.name) is not None}
    if isinstance(value, Mapping):
        return {str(k): to_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [to_json_value(v) for v in value]
        return sorted(items, key=canonical) if isinstance(value, (set, frozenset)) else items
    return str(value)


def canonical(value: Any) -> str:
    """Serialize a plain JSON value canonically. Raises on anything else."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return es6_number(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        for k in value:
            if not isinstance(k, str):
                raise SchemaError("canonical JSON object keys must be strings", key=repr(k))
        keys = sorted(value, key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(_string(k) + ":" + canonical(value[k]) for k in keys) + "}"
    raise SchemaError("value is not plain JSON", type=type(value).__name__)


def canonical_bytes(value: Any) -> bytes:
    return canonical(value).encode("utf-8")


def _string(s: str) -> str:
    # json.dumps(ensure_ascii=False) escapes exactly '"', '\\' and U+0000..U+001F, using
    # \b \f \n \r \t or lowercase \u00xx, which is the JCS rule.
    return json.dumps(s, ensure_ascii=False)


def es6_number(x: float) -> str:
    """ECMAScript Number::toString for a finite double (shortest round-trip digits)."""
    if math.isnan(x) or math.isinf(x):
        raise SchemaError("NaN and Infinity are not valid JSON numbers")
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    mantissa, _, exp_text = repr(abs(x)).partition("e")
    exponent = int(exp_text) if exp_text else 0
    int_part, _, frac_part = mantissa.partition(".")
    all_digits = int_part + frac_part
    stripped = all_digits.lstrip("0")
    leading_zeros = len(all_digits) - len(stripped)
    digits = stripped.rstrip("0") or "0"
    k = len(digits)
    n = len(int_part) + exponent - leading_zeros  # value = 0.digits * 10^n
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        out = digits[0] + ("." + digits[1:] if k > 1 else "") + \
            "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + out

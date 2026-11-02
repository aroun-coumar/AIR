"""Version precedence shared by the registry, deprecations and compatibility checks."""

from __future__ import annotations

import re
from typing import Any

__requirements__ = ("REQ18",)

_VERSION = re.compile(r"^(\d+(?:\.\d+)*)(.*)$")
_PRE_PART = re.compile(r"\d+|[A-Za-z]+")


def is_prerelease(version: str) -> bool:
    """True for ``1.0.0-rc.1``, ``2.0.0rc1``, ``0.1.0.dev0`` and non-numeric versions."""
    m = _VERSION.match(version)
    return m is None or bool(m.group(2).strip("-."))


def version_key(version: str) -> tuple[Any, ...]:
    """SemVer / PEP 440 style precedence: ``1.0.0-rc.1 < 1.0.0 < 1.0.1 < 1.10.0``.

    Release numbers compare numerically (padded, so ``1.0 == 1.0.0``); a prerelease sorts
    before its release; prerelease identifiers compare numerically when numeric.
    Versions that do not start with a number sort first, lexicographically.
    """
    m = _VERSION.match(version)
    if m is None:
        return (0, (), 0, (version,))
    release = tuple(int(p) for p in m.group(1).split("."))
    release = release + (0,) * (4 - len(release)) if len(release) < 4 else release
    pre = m.group(2).strip("-.")
    if not pre:
        return (1, release, 1, ())
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower())
                  for p in _PRE_PART.findall(pre))
    return (1, release, 0, parts)


def _release(version: str) -> tuple[int, ...]:
    m = _VERSION.match(version)
    if m is None:
        raise ValueError(f"not a version: {version!r}")
    parts = tuple(int(p) for p in m.group(1).split("."))
    return parts + (0,) * (3 - len(parts)) if len(parts) < 3 else parts


def _bump(parts: tuple[int, ...], index: int) -> str:
    out = list(parts[:index]) + [parts[index] + 1] + [0] * (len(parts) - index - 1)
    return ".".join(str(p) for p in out)


def _clauses(spec: str) -> list[tuple[str, str]]:
    """Expand a range into ``(operator, version)`` clauses, all of which must hold."""
    out: list[tuple[str, str]] = []
    for raw in spec.split(","):
        part = raw.strip()
        if not part or part == "*":
            continue
        if part.startswith("^"):          # same major (same minor while major is 0)
            base = _release(part[1:])
            index = 0 if base[0] > 0 else (1 if base[1] > 0 else 2)
            out += [(">=", part[1:]), ("<", _bump(base, index))]
        elif part.startswith("~"):        # same minor
            base = _release(part[1:])
            out += [(">=", part[1:]), ("<", _bump(base, 1))]
        else:
            m = re.match(r"^(>=|<=|==|!=|>|<|=)?\s*(.+)$", part)
            assert m is not None
            out.append((m.group(1) or "==", m.group(2).strip()))
    return out


def satisfies(version: str, spec: str) -> bool:
    """Whether ``version`` is in ``spec``: ``^1.2``, ``~1.2.3``, ``>=1.0,<2``, ``1.4.0``, ``*``.

    A prerelease only satisfies a range that names a prerelease explicitly, so
    ``^1.2`` does not pick ``1.3.0-rc.1`` (npm/Cargo convention).
    """
    clauses = _clauses(spec)
    if is_prerelease(version) and not any(is_prerelease(v) for _, v in clauses):
        return False
    key = version_key(version)
    for op, bound in clauses:
        other = version_key(bound)
        ok = {">=": key >= other, "<=": key <= other, ">": key > other, "<": key < other,
              "==": key == other, "=": key == other, "!=": key != other}[op]
        if not ok:
            return False
    return True


def valid_range(spec: str) -> bool:
    try:
        for _, v in _clauses(spec):
            _release(v)
    except (ValueError, AssertionError):
        return False
    return True

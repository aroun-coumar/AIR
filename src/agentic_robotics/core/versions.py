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

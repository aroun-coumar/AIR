"""Deprecation and stability markers (see ``docs/policy/versioning.md``).

    @deprecated(since="0.2.0", removal="0.4.0", alternative="Runtime.submit")
    def old_submit(...): ...

    @experimental
    class NewThing: ...

Deprecations warn with :class:`AIRDeprecationWarning`, a ``FutureWarning`` subclass,
because ``DeprecationWarning`` is hidden by default and users would never see it.
Every deprecation is registered, so a test fails once a symbol outlives its
announced removal version.
"""

from __future__ import annotations

import functools
import typing
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from .errors import FrameworkError
from .versions import version_key

__requirements__ = ("REQ18",)

T = TypeVar("T")


class AIRDeprecationWarning(FutureWarning):
    """Shown to end users by default (unlike DeprecationWarning)."""


@dataclass(frozen=True)
class Deprecation:
    qualname: str
    since: str
    removal: str
    alternative: str | None
    reason: str

    def message(self) -> str:
        msg = (f"{self.qualname} is deprecated since {self.since} and will be removed in "
               f"{self.removal}.")
        if self.alternative:
            msg += f" Use {self.alternative} instead."
        if self.reason:
            msg += f" {self.reason}"
        return msg


_DEPRECATIONS: list[Deprecation] = []


def deprecations() -> list[Deprecation]:
    return list(_DEPRECATIONS)


def deprecated(*, since: str, removal: str, alternative: str | None = None,
               reason: str = "") -> Callable[[T], T]:
    if version_key(removal) <= version_key(since):
        raise FrameworkError("removal version must be after the deprecation version",
                             since=since, removal=removal)

    def wrap(obj: T) -> T:
        info = Deprecation(f"{getattr(obj, '__module__', '?')}."
                           f"{getattr(obj, '__qualname__', repr(obj))}",
                           since, removal, alternative, reason)
        _DEPRECATIONS.append(info)
        if isinstance(obj, type):
            original_init = typing.cast(Any, obj).__init__

            @functools.wraps(original_init)
            def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
                warnings.warn(info.message(), AIRDeprecationWarning, stacklevel=2)
                original_init(self, *args, **kwargs)

            obj.__init__ = __init__  # type: ignore[misc]
            obj.__deprecated__ = info.message()  # type: ignore[attr-defined]
            return obj
        if callable(obj):
            @functools.wraps(obj)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                warnings.warn(info.message(), AIRDeprecationWarning, stacklevel=2)
                return obj(*args, **kwargs)

            wrapper.__deprecated__ = info.message()  # type: ignore[attr-defined]
            return wrapper  # type: ignore[return-value]
        raise FrameworkError("@deprecated applies to functions and classes")

    return wrap


def experimental(obj: T) -> T:
    """Mark an API as experimental: it may change or disappear in any minor release
    without a deprecation period. Recorded on the object, no runtime warning."""
    obj.__air_stability__ = "experimental"  # type: ignore[attr-defined]
    return obj


def stability(obj: Any) -> str:
    if getattr(obj, "__deprecated__", None):
        return "deprecated"
    return str(getattr(obj, "__air_stability__", "stable"))


def overdue(current_version: str) -> list[Deprecation]:
    """Deprecations whose removal version has been reached but still exist."""
    return [d for d in _DEPRECATIONS if version_key(current_version) >= version_key(d.removal)]

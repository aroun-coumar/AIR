"""Deprecation and stability markers, and the overdue-removal guard."""

from __future__ import annotations

import warnings

import pytest

import agentic_robotics
from agentic_robotics.core import FrameworkError
from agentic_robotics.core.deprecation import (
    AIRDeprecationWarning,
    deprecated,
    deprecations,
    experimental,
    overdue,
    stability,
)

pytestmark = pytest.mark.req("REQ18")


def test_deprecated_function_and_class_warn_visibly() -> None:
    @deprecated(since="0.1.0", removal="0.3.0", alternative="new_fn")
    def old_fn(x: int) -> int:
        return x + 1

    @deprecated(since="0.1.0", removal="0.3.0")
    class OldThing:
        def __init__(self, v: int) -> None:
            self.v = v

    with pytest.warns(AIRDeprecationWarning, match="removed in 0.3.0. Use new_fn"):
        assert old_fn(1) == 2
    with pytest.warns(AIRDeprecationWarning):
        assert OldThing(3).v == 3
    assert issubclass(AIRDeprecationWarning, FutureWarning)  # shown by default
    assert stability(old_fn) == "deprecated"


def test_default_filters_show_the_warning() -> None:
    @deprecated(since="0.1.0", removal="0.2.0")
    def f() -> None:
        pass

    with warnings.catch_warnings(record=True) as caught:
        warnings.resetwarnings()  # Python's defaults, not pytest's
        f()
    assert any(issubclass(w.category, AIRDeprecationWarning) for w in caught)


def test_removal_must_follow_deprecation() -> None:
    with pytest.raises(FrameworkError):
        deprecated(since="0.3.0", removal="0.3.0")


def test_experimental_marker() -> None:
    @experimental
    def new_api() -> None:
        pass

    assert stability(new_api) == "experimental"


def test_overdue_detection() -> None:
    @deprecated(since="0.1.0", removal="0.2.0")
    def soon_gone() -> None:
        pass

    assert any(d.qualname.endswith("soon_gone") for d in overdue("0.2.0"))
    assert not any(d.qualname.endswith("soon_gone") for d in overdue("0.1.5"))


def test_no_framework_symbol_outlives_its_removal_version() -> None:
    from agentic_robotics.core.schema import load_contract_modules

    load_contract_modules()
    own = [d for d in deprecations() if d.qualname.startswith("agentic_robotics.")]
    late = [d for d in own if d in overdue(agentic_robotics.__version__)]
    assert not late, [d.message() for d in late]

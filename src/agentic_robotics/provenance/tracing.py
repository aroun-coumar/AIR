"""Causal trace context.

Every trace event and ledger entry carries a ``trace_id`` (one end-to-end story, e.g. an
agent decision and everything it caused) and a ``span_id`` (one step within it), with a
``parent_id`` link. Context propagates through ``contextvars``, so asyncio tasks created
inside a span inherit it automatically; across process or network boundaries it is
carried explicitly (``Command.trace_id``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ..core.ids import new_id

__requirements__ = ("REQ02", "REQ15")


@dataclass(frozen=True)
class SpanContext:
    trace_id: str
    span_id: str
    parent_id: str | None
    name: str


_current: ContextVar[SpanContext | None] = ContextVar("agentic_robotics_span", default=None)


def current() -> SpanContext | None:
    return _current.get()


@contextmanager
def span(name: str, trace_id: str | None = None) -> Iterator[SpanContext]:
    """Open a child span of the current one (or a new trace if there is none).

    Passing ``trace_id`` joins an existing trace received from elsewhere.
    """
    parent = _current.get()
    if trace_id is None:
        trace_id = parent.trace_id if parent else new_id("trc")
    parent_id = parent.span_id if parent and parent.trace_id == trace_id else None
    ctx = SpanContext(trace_id, new_id("spn"), parent_id, name)
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)

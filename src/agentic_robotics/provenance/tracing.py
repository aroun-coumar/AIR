"""Causal trace context, compatible with W3C Trace Context and OpenTelemetry.

Every trace event and ledger entry carries a ``trace_id`` (one end-to-end story, e.g. an
agent decision and everything it caused) and a ``span_id`` (one step within it), with a
``parent_id`` link. Ids use the W3C/OTel wire format: a 128-bit trace id as 32 lowercase
hex digits and a 64-bit span id as 16, never all zeros. They can be exported to any
OpenTelemetry backend unchanged and carried in a ``traceparent`` header.

Context propagates through ``contextvars`` (asyncio tasks created inside a span inherit
it) and, across process or network boundaries, explicitly: ``Command.trace_id``,
``TaskPlan.trace_id``, ``CoordinationMessage.trace_id``, or a ``traceparent`` header via
:func:`to_traceparent` / :func:`from_traceparent`.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ..core.errors import SchemaError

__requirements__ = ("REQ02", "REQ15")

_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
_TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_LEGACY = re.compile(r"^(?:trc|spn)_([0-9a-f]{16})$")


def new_trace_id() -> str:
    while True:
        value = secrets.token_hex(16)
        if value != "0" * 32:
            return value


def new_span_id() -> str:
    while True:
        value = secrets.token_hex(8)
        if value != "0" * 16:
            return value


def normalize_trace_id(value: str) -> str:
    """Accept a W3C trace id, or a legacy AIR ``trc_<16hex>`` id (left-padded, as
    OpenTelemetry does for 64-bit ids). Raise on anything else."""
    if _TRACE_ID.match(value) and value != "0" * 32:
        return value
    legacy = _LEGACY.match(value)
    if legacy and value.startswith("trc_"):
        return legacy.group(1).rjust(32, "0")
    raise SchemaError("invalid trace id (expected 32 lowercase hex digits)", trace_id=value)


def is_valid_span_id(value: str) -> bool:
    return bool(_SPAN_ID.match(value)) and value != "0" * 16


@dataclass(frozen=True)
class SpanContext:
    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    sampled: bool = True
    remote: bool = False


_current: ContextVar[SpanContext | None] = ContextVar("agentic_robotics_span", default=None)


def current() -> SpanContext | None:
    return _current.get()


@contextmanager
def span(name: str, trace_id: str | None = None,
         parent: SpanContext | None = None) -> Iterator[SpanContext]:
    """Open a child span of ``parent`` (default: the current span), or a new trace.

    ``trace_id`` joins an existing trace received from elsewhere; ``parent`` (e.g. from
    :func:`from_traceparent`) also links to the remote parent span.
    """
    parent = parent if parent is not None else _current.get()
    if trace_id is not None:
        trace_id = normalize_trace_id(trace_id)
    elif parent is not None:
        trace_id = parent.trace_id
    else:
        trace_id = new_trace_id()
    parent_id = parent.span_id if parent and parent.trace_id == trace_id else None
    ctx = SpanContext(trace_id, new_span_id(), parent_id, name,
                      sampled=parent.sampled if parent else True)
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def to_traceparent(ctx: SpanContext | None = None) -> str:
    """Encode a span as a W3C ``traceparent`` header value (version 00)."""
    ctx = ctx or _current.get()
    if ctx is None:
        raise SchemaError("no active span to encode")
    return f"00-{ctx.trace_id}-{ctx.span_id}-{'01' if ctx.sampled else '00'}"


def from_traceparent(header: str) -> SpanContext:
    """Decode a ``traceparent`` header into a remote parent span context."""
    m = _TRACEPARENT.match(header.strip())
    if not m:
        raise SchemaError("invalid traceparent header", header=header)
    version, trace_id, span_id, flags = m.groups()
    if version == "ff" or trace_id == "0" * 32 or span_id == "0" * 16:
        raise SchemaError("invalid traceparent header", header=header)
    return SpanContext(trace_id, span_id, None, "remote",
                       sampled=bool(int(flags, 16) & 1), remote=True)

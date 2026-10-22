"""W3C Trace Context / OpenTelemetry-compatible trace and span ids."""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from agentic_robotics.core import SchemaError
from agentic_robotics.provenance import (
    current,
    from_traceparent,
    normalize_trace_id,
    span,
    to_traceparent,
)

from .conftest import Env

pytestmark = pytest.mark.req("REQ15", "REQ02")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX16 = re.compile(r"^[0-9a-f]{16}$")


def test_ids_use_w3c_widths_and_nest() -> None:
    with span("outer") as outer:
        assert HEX32.match(outer.trace_id) and HEX16.match(outer.span_id)
        assert outer.parent_id is None
        with span("inner") as inner:
            assert inner.trace_id == outer.trace_id
            assert inner.parent_id == outer.span_id
            assert current() == inner
    assert current() is None


def test_traceparent_round_trip_continues_a_remote_trace() -> None:
    with span("producer") as producer:
        header = to_traceparent()
    assert header == f"00-{producer.trace_id}-{producer.span_id}-01"
    remote = from_traceparent(header)
    assert remote.remote and remote.sampled
    with span("consumer", parent=remote) as consumer:
        assert consumer.trace_id == producer.trace_id
        assert consumer.parent_id == producer.span_id


@pytest.mark.parametrize("bad", [
    "00-" + "0" * 32 + "-" + "a" * 16 + "-01",   # all-zero trace id
    "00-" + "a" * 32 + "-" + "0" * 16 + "-01",   # all-zero span id
    "ff-" + "a" * 32 + "-" + "b" * 16 + "-01",   # forbidden version
    "00-" + "A" * 32 + "-" + "b" * 16 + "-01",   # uppercase not allowed
    "garbage",
])
def test_invalid_traceparent_is_rejected(bad: str) -> None:
    with pytest.raises(SchemaError):
        from_traceparent(bad)


def test_legacy_ids_are_left_padded_like_otel() -> None:
    assert normalize_trace_id("trc_0123456789abcdef") == "0" * 16 + "0123456789abcdef"
    with pytest.raises(SchemaError):
        normalize_trace_id("not-a-trace")


async def test_runtime_events_carry_w3c_ids(make_env: Callable[..., Env]) -> None:
    env = make_env()
    h = env.runtime.submit(env.command("navigate_to", {"x": 0.5, "y": 0.0}))
    await h.wait(5)
    events = env.runtime.trace.for_subject(h.command_id)
    assert events and all(HEX32.match(e.trace_id or "") for e in events)
    assert all(HEX16.match(e.span_id or "") for e in events)

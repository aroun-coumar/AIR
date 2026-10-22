"""Small helpers shared by the ecosystem bridges (ADR 0019). Standard library only.

Bridges talk to something outside AIR (a daemon, a DDS graph, a CAN bus, an MQTT broker,
a policy server). Each one gets its peer through an *injected* transport so tests can use
a fake peer that speaks the verified wire protocol; the helpers here keep the adapter side
consistent: observations stamped with the runtime clock at the moment AIR learned a fact,
and capability revisions that change only when a profile actually changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Callable, Iterable
from typing import Any

from ..contracts.capability import CapabilityProfile, CapabilityReport
from ..contracts.observation import Observation, Quality
from ..core.clock import Clock

__requirements__ = ("REQ03", "REQ12", "REQ13")


class CapabilityTracker:
    """Builds capability reports whose revision bumps only when a profile changes."""

    def __init__(self) -> None:
        self.revision = 0
        self._key: tuple[Any, ...] | None = None

    def report(self, robot_id: str, clock: Clock,
               profiles: Iterable[CapabilityProfile]) -> CapabilityReport:
        profs = tuple(profiles)
        key = tuple((p.name, p.status, tuple(sorted(p.limiting_factors))) for p in profs)
        if key != self._key:
            self._key = key
            self.revision += 1
        return CapabilityReport(robot_id=robot_id, capability_revision=self.revision,
                                observed_at=clock.now(), clock_domain=clock.domain,
                                profiles=profs)


def observation(clock: Clock, source: str, signal: str, value: Any, *,
                acquired_at: float | None = None, **kw: Any) -> Observation:
    """An observation in the runtime clock domain. ``acquired_at`` is when AIR received the
    fact (peer clocks are never mixed into the runtime domain)."""
    now = clock.now()
    at = now if acquired_at is None else min(acquired_at, now)
    return Observation(source=source, signal=signal, value=value, clock_domain=clock.domain,
                       acquired_at=at, received_at=now, **kw)


def degraded(obs: Observation) -> Observation:
    return Observation.from_dict({**obs.to_dict(), "quality": Quality.DEGRADED.value})


def finite(*values: float) -> bool:
    return all(isinstance(v, int | float) and math.isfinite(v) for v in values)


async def wait_until(predicate: Callable[[], bool], timeout_s: float,
                     poll_s: float = 0.002) -> bool:
    """Poll ``predicate`` until it is true or ``timeout_s`` passes. Never blocks forever."""
    loop = asyncio.get_running_loop()
    end = loop.time() + max(0.0, timeout_s)
    while True:
        if predicate():
            return True
        if loop.time() >= end:
            return False
        await asyncio.sleep(poll_s)


class InterruptGate:
    """Lets ``execute`` wait for an in-flight ``interrupt`` to finish before it returns, so
    the runtime settles the command with the interrupt's own stop confirmation."""

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}

    def begin(self, command_id: str) -> asyncio.Event:
        return self._events.setdefault(command_id, asyncio.Event())

    def requested(self, command_id: str) -> bool:
        return command_id in self._events

    async def settle(self, command_id: str, timeout_s: float) -> None:
        event = self._events.get(command_id)
        if event is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout_s)

    def clear(self, command_id: str) -> None:
        self._events.pop(command_id, None)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


__all__ = ["CapabilityTracker", "InterruptGate", "clamp", "degraded", "finite", "observation",
           "wait_until"]
